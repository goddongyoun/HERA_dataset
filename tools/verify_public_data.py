"""Verify an extracted HERA data deposit offline, without simulation or writes."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

from build_public_data import CAMPAIGN, MANIFEST_NAME, SOURCE_SHA256, TEXT_SUFFIXES, digest_bytes, digest_file, privacy_check, redact_bytes, safe_relative, scientific_equal, source_digest


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def verify_public_data(root: Path, *, source_root: Path | None = None, repo_root: Path | None = None) -> dict:
    root = root.resolve()
    errors = []
    counts = Counter()
    manifest = json.loads((root / MANIFEST_NAME).read_bytes())
    rows = manifest["files"]
    records = {}
    for row in rows:
        try:
            key = safe_relative(row["path"])
            safe_relative(row["source_path"])
            path = root / key
            if key in records or path.is_symlink() or not path.resolve().is_relative_to(root):
                raise ValueError("duplicate, symlink or outside-root member")
            records[key] = row
            public = path.read_bytes()
            if len(public) != row["bytes"] or digest_bytes(public) != row["sha256"]:
                raise ValueError("public file hash/byte count mismatch")
            if row["original_bytes"] != row["bytes"]:
                raise ValueError("non-length-preserving derivative")
            if not row["transformations"] and row["original_sha256"] != row["sha256"]:
                raise ValueError("undocumented transformation")
            text_file = path.suffix in TEXT_SUFFIXES or path.name in {"LICENSE", "LICENSE-DATA", ".gitattributes", ".gitignore"}
            if text_file:
                privacy_check(public, key)
            if row["origin"] not in {"source", "repository"}:
                raise ValueError("unknown origin")
            base = source_root if row["origin"] == "source" else repo_root
            if base is not None:
                original_path = (base / row["source_path"]).resolve()
                if not original_path.is_relative_to(base.resolve()) or original_path.is_symlink():
                    raise ValueError("invalid original source path")
                original = original_path.read_bytes()
                if len(original) != row["original_bytes"] or digest_bytes(original) != row["original_sha256"]:
                    raise ValueError("original input hash mismatch")
                expected, transformations = redact_bytes(original) if text_file else (original, [])
                if expected != public or transformations != row["transformations"]:
                    raise ValueError("public derivative differs from documented transformation")
                if path.suffix == ".json" and not scientific_equal(json.loads(original), json.loads(public)):
                    raise ValueError("scientific JSON content mismatch")
                counts["original_files_checked"] += 1
            counts["payload_files_checked"] += 1
            counts["transformed_files"] += bool(row["transformations"])
        except (OSError, ValueError, KeyError) as exc:
            errors.append(row.get("path", "<missing-path>") + ": " + str(exc))
    actual = set()
    for path in root.rglob("*"):
        if "__pycache__" in path.parts:
            continue
        if path.is_symlink():
            errors.append("Unexpected symlink in extracted archive")
        elif path.is_file():
            actual.add(path.relative_to(root).as_posix())
    if actual != set(records) | {MANIFEST_NAME}:
        errors.append("Extracted file membership differs from complete manifest")
    try:
        if manifest["execution_source_sha256"] != SOURCE_SHA256 or manifest["execution_source_file_count"] != 30:
            raise ValueError("Manifest source identity mismatch")
        for location in (root, root / CAMPAIGN / "source_snapshot"):
            if source_digest(location) != (SOURCE_SHA256, 30):
                raise ValueError("Frozen source fingerprint/count mismatch")
        campaign = root / CAMPAIGN
        definition = json.loads((campaign / "campaign.json").read_bytes())
        progress = json.loads((campaign / "progress.json").read_bytes())
        provenance = json.loads((campaign / "provenance.json").read_bytes())
        if progress["runner_status"] != "complete" or progress["failures"] or progress["completed_trials"] != 1472 or progress["total_trials"] != 1472:
            raise ValueError("Incomplete campaign")
        if provenance["source_sha256"] != SOURCE_SHA256 or len(provenance["source_files"]) != 30:
            raise ValueError("Original provenance source identity mismatch")
        for item in provenance["source_files"]:
            for location in (root, campaign / "source_snapshot"):
                if digest_file(location / safe_relative(item["file"])) != item["sha256"]:
                    raise ValueError("Individual frozen source hash mismatch")
        hashes = [item["snapshot_sha256"] for item in definition["manifests"]]
        if len(hashes) != 11 or digest_bytes(canonical({"ordered_manifest_sha256": hashes})) != definition["campaign_definition_sha256"]:
            raise ValueError("Campaign manifest identity mismatch")
        trials = set()
        for item in definition["manifests"]:
            manifest_path = campaign / safe_relative(item["snapshot_path"])
            payload = manifest_path.read_bytes()
            sha = digest_bytes(payload)
            if sha != item["snapshot_sha256"] or sha != item["source_sha256"]:
                raise ValueError("Original manifest hash mismatch")
            specs = [json.loads(line) for line in payload.splitlines() if line.strip()]
            if len(specs) != item["trial_count"]:
                raise ValueError("Manifest trial count mismatch")
            for spec in specs:
                trial_id = safe_relative(spec["trial_id"])
                if "/" in trial_id or trial_id in trials:
                    raise ValueError("Duplicate/unsafe trial identifier")
                trials.add(trial_id)
                trial_dir = campaign / "trials" / trial_id
                adopted = json.loads((trial_dir / "summary.json").read_bytes())
                attempt_rel = safe_relative(adopted["attempt_relative_dir"])
                if attempt_rel != "attempts/" + adopted["adopted_attempt_id"] or adopted["adopted_attempt_id"] != "attempt-0001":
                    raise ValueError("Unexpected trial attempt path")
                attempt = trial_dir / attempt_rel
                worker_bytes = (attempt / "summary.json").read_bytes()
                if digest_bytes(worker_bytes) != adopted["worker_summary_sha256"]:
                    raise ValueError("Worker/adopted summary hash mismatch")
                worker = json.loads(worker_bytes)
                runner = json.loads((attempt / "runner.json").read_bytes())
                if runner["adopted"] is not True or runner["worker_returncode"] != 0 or runner["attempt_id"] != adopted["adopted_attempt_id"]:
                    raise ValueError("Runner adoption mismatch")
                for summary in (adopted, worker):
                    if summary["status"] != "complete" or summary["manifest_sha256"] != sha or summary["trial_spec_sha256"] != digest_bytes(canonical(spec)) or summary["environment"]["source_sha256"] != SOURCE_SHA256:
                        raise ValueError("Summary identity/status mismatch")
                    if any(summary.get(k) != v for k, v in spec.items()):
                        raise ValueError("Summary manifested trial parameters differ")
                if adopted["artifacts"] != worker["artifacts"]:
                    raise ValueError("Adopted/worker artifact records differ")
                for kind, artifact in worker["artifacts"].items():
                    if kind not in {"events", "trace"} or artifact["file"] != kind + ".jsonl":
                        raise ValueError("Unknown artifact kind/path")
                    artifact_path = attempt / artifact["file"]
                    artifact_payload = artifact_path.read_bytes()
                    if len(artifact_payload) != artifact["bytes"] or digest_bytes(artifact_payload) != artifact["sha256"]:
                        raise ValueError("Raw artifact hash/byte count mismatch")
                    count = sum(bool(line.strip()) for line in artifact_payload.splitlines())
                    if count != artifact["rows"]:
                        raise ValueError("Raw artifact row count mismatch")
                    counts[kind + "_files"] += 1
                    counts[kind + "_rows"] += count
                counts["trials"] += 1
                counts["summaries"] += 2
                counts["runner_records"] += 1
            counts["manifest_snapshots"] += 1
        if set(p.name for p in (campaign / "trials").iterdir()) != trials:
            raise ValueError("Unmanifested/missing trial directories")
        counts["analysis_json"] = len(list((campaign / "analysis").glob("*/analysis.json")))
        for name, expected in {"trials": 1472, "summaries": 2944, "runner_records": 1472, "events_files": 1472, "trace_files": 1184, "trace_rows": 608000, "manifest_snapshots": 11, "analysis_json": 11}.items():
            if counts[name] != expected:
                raise ValueError("Fixed-main coverage mismatch: " + name)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        errors.append("Scientific closure: " + str(exc))
    from verify_server_archive import verify_server_archive
    server_report = verify_server_archive(root, manifest)
    if not server_report["valid"]:
        errors.append("Archived server evidence failed: " + json.dumps(server_report.get("errors", [])))
    return {"valid": not errors, "counts": dict(counts), "execution_source_sha256": SOURCE_SHA256, "server_verification": server_report, "errors": errors, "scope_note": "Integrity/privacy and original scientific artifact closure; not a claim of new experiments or DOI publication."}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--repo-root", type=Path)
    args = parser.parse_args()
    report = verify_public_data(args.root, source_root=args.source_root, repo_root=args.repo_root)
    print(json.dumps(report, indent=2))
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
