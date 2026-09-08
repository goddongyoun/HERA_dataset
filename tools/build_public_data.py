"""Build an allowlisted, privacy-reviewed HERA data deposit (standard library).

Never modifies research inputs, recaptures a live server, or publishes anything.
Historical metadata hashes stay historical; PUBLIC_DATA_MANIFEST.json records
both original and public derivative hashes. Privacy masks preserve byte lengths.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import zipfile

SOURCE_SHA256 = "73b25c2272adadd37b4cfad44bc5cde3b8e2821d13f886f2b1996f8ca463f1c5"
CAMPAIGN = "runs/main_001"
MANIFEST_NAME = "PUBLIC_DATA_MANIFEST.json"
TEXT_SUFFIXES = {".py", ".ps1", ".json", ".jsonl", ".md", ".txt", ".svg", ".tex", ".cff", ".log"}
REPO_ROOT_FILES = {
    ".gitattributes", ".gitignore", ".zenodo.json", "README.md", "CITATION.cff",
    "LICENSE", "LICENSE-DATA", "LICENSING.md", "PROTOCOL.md", "release_manifest.json",
    "requirements-analysis.txt", "requirements-core.txt", "requirements-environment.txt",
    "analyze_results.py", "make_manifests.py", "make_paper_manifests.py", "run_campaign.py",
    "start_dedicated_servers.ps1", "trial_worker.py", "verify_servers.py", "warmup_servers.py",
}
REPO_DIR_SUFFIXES = {
    "docs": {".md", ".json"}, "figures": {".png", ".svg"}, "hera_v2": {".py"},
    "paper": {".py"}, "results": {".json", ".md", ".tex"},
    "runs": {".json", ".jsonl"}, "tests": {".py"}, "tools": {".py"},
}
_ROOT_PATH = re.compile(rb"\b[A-Za-z]:[\\/]+([A-Za-z0-9_. -]+)(?=[\\/])")
_USER_PATH = re.compile(rb"(?i)(\b[A-Z]:[\\/]+Users[\\/]+)([^\\/\s\"<>]+)")
_GPU = re.compile(rb"GPU-[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_SECRET = re.compile(rb"(?i)(?:-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9]{40,})")
_SECRET_FIELD = re.compile(rb'(?i)"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|client[_-]?secret)"\s*:\s*"([^"\s]+)"')


def digest_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def digest_file(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def redact_bytes(payload: bytes) -> tuple[bytes, list[str]]:
    """Only mask private filesystem components and GPU UUIDs, length-preserving."""
    labels = set()
    def root_mask(match):
        name = match.group(1)
        if name.lower() in {b"users", b"windows", b"program files", b"program files (x86)", b"programdata"}:
            return match.group(0)
        masked = b"x" * len(name)
        if name != masked:
            labels.add("workspace_folder_mask")
        return match.group(0)[:-len(name)] + masked
    def user_mask(match):
        name = match.group(2)
        masked = b"X" * len(name)
        if name != masked:
            labels.add("user_profile_mask")
        return match.group(1) + masked
    def gpu_mask(match):
        masked = b"GPU-00000000-0000-0000-0000-000000000000"
        if match.group(0) != masked:
            labels.add("gpu_uuid_mask")
        return masked
    result = _GPU.sub(gpu_mask, _USER_PATH.sub(user_mask, _ROOT_PATH.sub(root_mask, payload)))
    if len(result) != len(payload):
        raise ValueError("Privacy transformation changed byte length")
    return result, sorted(labels)


def privacy_check(payload: bytes, label: str) -> None:
    if _SECRET.search(payload):
        raise ValueError("Credential-like content requires manual review: " + label)
    if _SECRET_FIELD.search(payload):
        raise ValueError("Nonempty credential field requires manual review: " + label)
    if redact_bytes(payload)[0] != payload:
        raise ValueError("Unmasked private filesystem/GPU identifier: " + label)
    # Other private path styles are not silently guessed or redacted.
    if re.search(rb"/(?:home|Users)/(?!X+(?:/|$))[^\s/\"']+", payload):
        raise ValueError("Unexpected user-home path requires manual review: " + label)


def safe_relative(value: str) -> str:
    path = PurePosixPath(value)
    if not value or "\\" in value or ":" in value or path.is_absolute() or any(p in {".", ".."} for p in path.parts) or path.as_posix() != value:
        raise ValueError("Unsafe relative path")
    return value


def json_bytes(value) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")


def scientific_equal(original, public) -> bool:
    """All keys, types, numbers, booleans, list ordering and nonprivate text equal."""
    if type(original) is not type(public):
        return False
    if isinstance(original, dict):
        return original.keys() == public.keys() and all(scientific_equal(original[k], public[k]) for k in original)
    if isinstance(original, list):
        return len(original) == len(public) and all(scientific_equal(a, b) for a, b in zip(original, public))
    if isinstance(original, str):
        return redact_bytes(original.encode("utf-8"))[0].decode("utf-8") == public
    return original == public


def source_digest(root: Path) -> tuple[str, int]:
    paths = sorted([*root.glob("*.py"), *root.glob("*.ps1"), *(root / "hera_v2").glob("*.py"), *(root / "tests").glob("*.py")], key=lambda p: p.relative_to(root).as_posix())
    result = hashlib.sha256()
    for path in paths:
        result.update(path.relative_to(root).as_posix().encode("utf-8") + b"\0" + path.read_bytes() + b"\0")
    return result.hexdigest(), len(paths)


def collect_candidates(source: Path, repo: Path) -> dict[str, tuple[Path, str, str]]:
    entries = {}
    def add(path: Path, origin: str, base: Path, target: str | None = None):
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(base):
            raise ValueError("Nonregular or outside-root candidate")
        relative = path.relative_to(base).as_posix()
        key = safe_relative(target or relative)
        if key in entries:
            raise ValueError("Duplicate archive member: " + key)
        entries[key] = (path, origin, relative)
    # Public code repository only: no Git objects, environments or internal docs.
    for path in sorted(repo.rglob("*")):
        relative = path.relative_to(repo)
        if relative.parts[0] == ".git" or "__pycache__" in relative.parts:
            continue
        if path.is_symlink():
            raise ValueError("Repository symlink is not allowed")
        if not path.is_file():
            continue
        if len(relative.parts) == 1:
            if path.name not in REPO_ROOT_FILES:
                raise ValueError("Unreviewed repository root file: " + path.name)
        elif relative.parts[0] not in REPO_DIR_SUFFIXES or path.suffix not in REPO_DIR_SUFFIXES[relative.parts[0]]:
            raise ValueError("Unreviewed public repository candidate: " + relative.as_posix())
        if relative.parts[0] == "runs" and relative.parts[:3] != ("runs", "manifests", "main_001"):
            raise ValueError("Only fixed-main repository manifests are allowed")
        if relative.as_posix() == "README.md":
            add(path, "repository", repo, "docs/SOFTWARE_README.md")
        else:
            add(path, "repository", repo)
    add(repo / "docs/DATASET_README.md", "repository", repo, "README.md")
    campaign = source / CAMPAIGN
    provenance = json.loads((campaign / "provenance.json").read_bytes())
    frozen = {row["file"] for row in provenance["source_files"]}
    expected_attempts = set()
    for trial in (campaign / "trials").iterdir():
        if not trial.is_dir() or trial.is_symlink():
            raise ValueError("Unexpected trial candidate")
        adopted = json.loads((trial / "summary.json").read_bytes())
        attempt = adopted["attempt_relative_dir"]
        if attempt != "attempts/attempt-0001":
            raise ValueError("Unexpected attempt history; manual review required")
        expected_attempts.add(trial.name)
    if len(expected_attempts) != 1472:
        raise ValueError("Expected exactly 1472 trial directories")
    for path in sorted(campaign.rglob("*")):
        if path.is_symlink():
            raise ValueError("Campaign symlink is not allowed")
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        rel = path.relative_to(campaign)
        parts = rel.parts
        include = False
        if len(parts) == 1:
            if path.name not in {"campaign.json", "progress.json", "provenance.json", "runner.stderr.log", "runner.stdout.log"}:
                raise ValueError("Unexpected campaign root file: " + path.name)
            include = path.suffix == ".json"
        elif parts[0] == "source_snapshot":
            sub = Path(*parts[1:]).as_posix()
            if sub not in frozen | {"README.md", "PROTOCOL.md", "requirements-core.txt", "requirements-environment.txt"}:
                raise ValueError("Unexpected source snapshot member")
            include = sub != "README.md"  # obsolete private execution instructions, not hashed execution code
        elif parts[0] == "manifests":
            if len(parts) != 2 or not re.fullmatch(r"\d{3}_[a-z0-9_]+\.jsonl", path.name):
                raise ValueError("Unexpected manifest candidate")
            include = True
        elif parts[0] == "analysis":
            if len(parts) != 3 or path.name not in {"analysis.json", "trial_summary.csv", "stdout.log", "stderr.log"}:
                raise ValueError("Unexpected analysis candidate")
            include = path.name == "analysis.json"  # CSV is a redundant derived table
        elif parts[0] == "batches":
            if len(parts) != 4 or path.name not in {"lifecycle.json", "verification.json", "warmup.json", "stdout.log", "stderr.log"}:
                raise ValueError("Unexpected batch candidate")
            include = path.suffix == ".json"
        elif parts[0] == "trials":
            valid = (len(parts) == 3 and path.name == "summary.json") or (len(parts) == 5 and parts[2:4] == ("attempts", "attempt-0001") and path.name in {"summary.json", "runner.json", "events.jsonl", "trace.jsonl", "stdout.log", "stderr.log"})
            if not valid:
                raise ValueError("Unexpected trial candidate: " + rel.as_posix())
            include = path.suffix in {".json", ".jsonl"}
        else:
            raise ValueError("Unexpected campaign directory")
        if include:
            add(path, "source", source)
    server = source / "paper/generated/server_main"
    for path in sorted(server.rglob("*")):
        if path.is_symlink():
            raise ValueError("Server archive symlink is not allowed")
        if not path.is_file():
            continue
        rel = path.relative_to(server)
        valid = rel.as_posix() in {"server_evidence.json", "server_evidence.md"}
        valid |= len(rel.parts) == 3 and rel.parts[0] == "batches" and re.fullmatch(r"\d{4}", rel.parts[1]) is not None and path.name in {"server_01.stderr.prefix.log", "server_02.stderr.prefix.log", "server_state.snapshot.json", "verification.json"}
        valid |= len(rel.parts) == 2 and rel.parts[0] == "manifests" and re.fullmatch(r"\d{3}\.jsonl", path.name) is not None
        valid |= len(rel.parts) == 2 and rel.parts[0] == "progress_snapshots" and re.fullmatch(r"[0-9a-f]{64}\.json", path.name) is not None
        if not valid:
            raise ValueError("Unreviewed server evidence candidate: " + rel.as_posix())
        add(path, "source", source)
    return entries


def capture_server_lines(source: Path) -> list[dict]:
    base = source / "paper/generated/server_main"
    evidence = json.loads((base / "server_evidence.json").read_bytes())
    if not evidence["valid"] or evidence["errors"] or evidence["n_archived_batches"] != 32 or evidence["n_log_prefixes"] != 64:
        raise ValueError("Incomplete original server evidence")
    records = []
    for index, batch in enumerate(evidence["batches"], 1):
        for server_index, server in enumerate(batch["servers"], 1):
            path = base / "batches" / f"{index:04d}" / f"server_{server_index:02d}.stderr.prefix.log"
            original = path.read_bytes()
            prefix = server["stderr_prefix"]
            if len(original) != prefix["archived_bytes"] or digest_bytes(original) != prefix["archived_sha256"] or prefix["archived_sha256"] != prefix["expected_sha256"]:
                raise ValueError("Original archived server prefix failed historical hash")
            parsed = server["parsed_runner"]
            offset = parsed["byte_offset"]
            line = original.splitlines(keepends=True)[parsed["line_number"] - 1]
            if original[offset:offset + len(line)] != line or digest_bytes(line) != parsed["line_sha256"] or line.decode("utf-8").rstrip("\r\n") != parsed["raw_line"]:
                raise ValueError("Original archived server runner line failed historical binding")
            public, transformations = redact_bytes(line)
            records.append({"path": path.relative_to(source).as_posix(), "line_number": parsed["line_number"], "byte_offset": offset, "original_sha256": digest_bytes(line), "sha256": digest_bytes(public), "bytes": len(public), "transformations": transformations})
    if len(records) != 64:
        raise ValueError("Expected 64 server runner lines")
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    source, repo, output = args.source_root.resolve(), args.repo_root.resolve(), args.output_dir.resolve()
    if output.exists() or output.is_relative_to(source) or output.is_relative_to(repo) or source.is_relative_to(output) or repo.is_relative_to(output):
        parser.error("Choose a NEW output directory outside source/repository trees")
    for root in (source, repo, source / CAMPAIGN / "source_snapshot"):
        if source_digest(root) != (SOURCE_SHA256, 30):
            raise ValueError("Frozen execution-source fingerprint/count changed")
    # The input software snapshot must already be committed, but no upload occurs.
    status = subprocess.run(["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=all"], check=True, capture_output=True, text=True).stdout
    if status.strip():
        raise ValueError("Commit the public software snapshot before building (dirty repository)")
    commit = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    progress = json.loads((source / CAMPAIGN / "progress.json").read_bytes())
    if progress["runner_status"] != "complete" or progress["failures"] or progress["total_trials"] != 1472 or progress["completed_trials"] != 1472:
        raise ValueError("Campaign must be complete with 1472 trials and no runner failures")
    entries = collect_candidates(source, repo)
    baseline = {key: (path.stat().st_size, digest_file(path)) for key, (path, _, _) in entries.items()}
    server_lines = capture_server_lines(source)
    output.mkdir(parents=True, exist_ok=False)
    root = output / "HERA"
    root.mkdir()
    records = []
    for key, (path, origin, relative) in sorted(entries.items()):
        payload = path.read_bytes()
        original_sha = digest_bytes(payload)
        if (len(payload), original_sha) != baseline[key]:
            raise ValueError("Source changed during packaging: " + key)
        text_file = path.suffix in TEXT_SUFFIXES or path.name in {"LICENSE", "LICENSE-DATA", ".gitattributes", ".gitignore"}
        public, transformations = redact_bytes(payload) if text_file else (payload, [])
        protected = key.startswith(CAMPAIGN + "/trials/") or path.suffix in {".jsonl", ".py", ".ps1"}
        if protected and public != payload:
            raise ValueError("Protected scientific/source bytes require redaction; manual review: " + key)
        if text_file:
            privacy_check(public, key)
        equal = None
        if path.suffix == ".json":
            equal = scientific_equal(json.loads(payload), json.loads(public))
            if not equal:
                raise ValueError("Scientific JSON content changed: " + key)
        destination = root / key
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("xb") as stream:
            stream.write(public)
        records.append({"path": key, "origin": origin, "source_path": relative, "original_sha256": original_sha, "original_bytes": len(payload), "sha256": digest_bytes(public), "bytes": len(public), "transformations": transformations, "scientific_json_unchanged": equal})
    # Recheck input bytes after the complete copy, not only before each file.
    for key, (path, _, _) in entries.items():
        if (path.stat().st_size, digest_file(path)) != baseline[key]:
            raise ValueError("Input changed during packaging: " + key)
    if set(collect_candidates(source, repo)) != set(entries):
        raise ValueError("Input file inventory changed during packaging")
    final_commit = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    final_status = subprocess.run(["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=all"], check=True, capture_output=True, text=True).stdout
    if final_commit != commit or final_status.strip():
        raise ValueError("Public software repository changed during packaging")
    manifest = {
        "schema_version": 1, "title": "HERA", "campaign": "main_001",
        "created_utc": datetime.now(timezone.utc).isoformat(), "software_commit": commit,
        "execution_source_sha256": SOURCE_SHA256, "execution_source_file_count": 30,
        "historical_metadata_notice": "Hash and absolute-path fields inside historical metadata describe original captured evidence. Private path components and GPU UUIDs are length-preserving masks. They are NOT current public file hashes or usable filesystem paths. The original/public hash mapping below, plus server_line_provenance, binds the public derivatives without rewriting or claiming new historical audits.",
        "scope": {"trials": 1472, "summaries": 2944, "events": 1472, "traces": 1184, "trace_rows": 608000, "analysis_json": 11, "server_batches": 32, "server_prefixes": 64},
        "excluded": ["Git history", "environments", "manuscript and reviewer documents", "development/pilot experiments", "redundant trial_summary.csv tables", "trial/batch/analysis stdout and stderr", "obsolete source_snapshot README", "unarchived live server logs"],
        "files": records, "server_line_provenance": server_lines,
    }
    (root / MANIFEST_NAME).write_bytes(json_bytes(manifest))
    from verify_public_data import verify_public_data
    report = verify_public_data(root, source_root=source, repo_root=repo)
    if not report["valid"]:
        raise ValueError("Public data verification failed: " + json.dumps(report["errors"]))
    archive = output / "HERA_dataset.zip"
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as bundle:
        for path in sorted(root.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                bundle.write(path, "HERA/" + path.relative_to(root).as_posix())
    with zipfile.ZipFile(archive) as bundle:
        expected = {"HERA/" + row["path"]: row["sha256"] for row in records}
        expected["HERA/" + MANIFEST_NAME] = digest_file(root / MANIFEST_NAME)
        if len(bundle.namelist()) != len(expected) or set(bundle.namelist()) != set(expected):
            raise ValueError("Archive membership mismatch")
        for name, sha in expected.items():
            if digest_bytes(bundle.read(name)) != sha:
                raise ValueError("Archive member hash mismatch: " + name)
    (output / "HERA_dataset.manifest.json").write_bytes(json_bytes(manifest))
    sha = digest_file(archive)
    (output / "HERA_dataset.sha256").write_text(sha + "  HERA_dataset.zip\n", encoding="ascii")
    report.update({"archive": "HERA_dataset.zip", "archive_sha256": sha, "archive_bytes": archive.stat().st_size, "archive_members_verified": len(records) + 1, "software_commit": commit})
    (output / "BUILD_REPORT.json").write_bytes(json_bytes(report))
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
