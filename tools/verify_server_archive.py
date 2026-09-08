"""Verify relocated HERA server evidence without contacting or starting servers.

Historical hashes identify the original private-provenance bytes. The public
manifest binds those hashes to the length-preserving redacted derivatives; this
verifier never claims that a redacted file has its original digest.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re


FIELD = re.compile(r'(?:^|\s)(runner\.[\w.]+)=("(?:\\.|[^"\\])*"|\S+)')


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def read_json(path: Path):
    return json.loads(path.read_bytes().decode("utf-8-sig"))


def parse_runner(payload: bytes, model: str) -> dict:
    found = None
    offset = 0
    for number, raw in enumerate(payload.splitlines(keepends=True), 1):
        line = raw.decode("utf-8", errors="replace")
        if "finished setting up" in line:
            fields = {key: json.loads(value) if value.startswith('"') else value
                      for key, value in FIELD.findall(line)}
            name = fields.get("runner.name", "")
            if name == model or name.endswith("/" + model):
                found = {"model": name, "parallel": int(fields["runner.parallel"]),
                         "runner_pid": int(fields["runner.pid"]),
                         "num_ctx": int(fields["runner.num_ctx"]),
                         "line_number": number, "byte_offset": offset,
                         "line_sha256": digest(raw), "bytes": len(raw),
                         "raw_line": line.rstrip("\r\n")}
        offset += len(raw)
    if found is None:
        raise ValueError("No matching finished-runner record")
    return found


def verify_server_archive(root: Path, manifest: dict | None = None) -> dict:
    root = Path(root).resolve()
    manifest = manifest or read_json(root / "PUBLIC_DATA_MANIFEST.json")
    records = {item["path"]: item for item in manifest["files"]}
    line_records = {(item["path"], item["line_number"]): item
                    for item in manifest.get("server_line_provenance", [])}
    errors = []
    checked = {}
    counts = {"batches": 0, "prefixes": 0, "runner_lines": 0, "trials": 0}

    def require(condition, message):
        if not condition:
            raise ValueError(message)

    def locate(value):
        value = str(value).replace("\\", "/")
        for anchor in ("paper/generated/server_main/", "runs/main_001/"):
            if anchor in value:
                value = value[value.index(anchor):]
                break
        path = (root / value).resolve()
        require(path.is_relative_to(root), "Evidence path escapes public root")
        return path

    def verify_file(path, historical_sha=None):
        path = locate(path)
        key = path.relative_to(root).as_posix()
        require(key in records, "Evidence absent from public manifest: " + key)
        record = records[key]
        if key not in checked:
            payload = path.read_bytes()
            require(digest(payload) == record["sha256"], "Public SHA mismatch: " + key)
            require(len(payload) == record["bytes"], "Public size mismatch: " + key)
            checked[key] = payload
        if historical_sha is not None:
            require(historical_sha == record["original_sha256"],
                    "Historical SHA binding mismatch: " + key)
        return checked[key]

    def json_file(path, historical_sha=None):
        return json.loads(verify_file(path, historical_sha).decode("utf-8-sig"))

    try:
        report = json_file("paper/generated/server_main/server_evidence.json")
        progress = json_file("runs/main_001/progress.json")
        definition = json_file("runs/main_001/campaign.json")
        require(report["valid"] and not report["errors"] and report["coverage"] == "complete",
                "Historical server report is not complete and valid")
        require(report["n_recorded_batches"] == report["n_archived_batches"] == 32,
                "Expected 32 historical batches")
        require(report["n_log_prefixes"] == 64, "Expected 64 historical prefixes")
        lifecycle = progress["scheduler_lifecycle"]["records"]
        require(len(lifecycle) == len(report["batches"]) == 32, "Batch coverage differs")
        snapshot = "paper/generated/server_main/progress_snapshots/" + report["progress_snapshot_sha256"] + ".json"
        require(verify_file(snapshot, report["progress_snapshot_sha256"]) ==
                verify_file("runs/main_001/progress.json"), "Progress snapshot differs")
        batch_by_id = {}
        for index, (batch, record) in enumerate(zip(report["batches"], lifecycle), 1):
            require(batch["valid"] and not batch["errors"], "Invalid archived batch")
            require(record.get("status", "complete") == "complete", "Incomplete lifecycle")
            require((batch["batch_id"], batch["model"]) == (record["batch_id"], record["model"]),
                    "Lifecycle and archived batch identity differ")
            folder = f"paper/generated/server_main/batches/{index:04d}/"
            require(locate(batch["verification_archive"]) == locate(folder + "verification.json"),
                    "Unexpected archived verification location")
            verification = json_file(folder + "verification.json", batch["verification_sha256"])
            require(locate(batch["verification_source"]).parent == locate(record["directory"] + "/verification.json").parent,
                    "Lifecycle verification directory differs")
            require(verify_file(batch["verification_source"], batch["verification_sha256"]) ==
                    verify_file(folder + "verification.json"), "Verification copies differ")
            require(verification["valid"] and not verification["failures"], "Invalid original verification")
            require(verification["requested_model"] == batch["model"], "Verified model differs")
            state_info = batch["state"]
            require(locate(state_info["archive"]) == locate(folder + "server_state.snapshot.json"),
                    "Unexpected archived state location")
            state_payload = verify_file(folder + "server_state.snapshot.json", verification["state_sha256"])
            state = json.loads(state_payload.decode("utf-8-sig"))
            require(state_info["valid"] and state_info["expected_sha256"] ==
                    state_info["archived_sha256"] == verification["state_sha256"], "State digest binding differs")
            require(len(batch["servers"]) == len(verification["servers"]) == len(state["servers"]) == 2,
                    "Expected two server profiles per batch")
            servers_by_host = {}
            for server_index, (server, verified) in enumerate(zip(batch["servers"], verification["servers"]), 1):
                prefix_key = folder + f"server_{server_index:02d}.stderr.prefix.log"
                prefix_info = server["stderr_prefix"]
                prefix = verify_file(prefix_key, verified["stderr_sha256_at_verification"])
                require(locate(prefix_info["archive"]) == locate(prefix_key), "Unexpected prefix location")
                require(prefix_info["valid"] and not prefix_info["errors"] and server["valid"] and
                        not server["errors"], "Invalid historical prefix or profile")
                require(prefix_info["expected_sha256"] == prefix_info["archived_sha256"] ==
                        verified["stderr_sha256_at_verification"], "Prefix historical SHA differs")
                require(len(prefix) == records[prefix_key]["original_bytes"] ==
                        prefix_info["requested_bytes"] == prefix_info["archived_bytes"] ==
                        verified["stderr_size_at_verification"], "Prefix byte count changed")
                parsed = parse_runner(prefix, batch["model"])
                historical = server["parsed_runner"]
                for key in ("model", "parallel", "runner_pid", "num_ctx"):
                    require(parsed[key] == historical[key] == verified["runner"][key] ==
                            server["recorded_runner"][key], "Runner field differs: " + key)
                for key in ("line_number", "byte_offset", "raw_line"):
                    require(parsed[key] == historical[key], "Runner line location/text differs: " + key)
                line_record = line_records.get((prefix_key, parsed["line_number"]))
                require(line_record is not None, "Missing original/public runner line binding")
                require(line_record["original_sha256"] == historical["line_sha256"] and
                        line_record["sha256"] == parsed["line_sha256"] and
                        line_record["byte_offset"] == parsed["byte_offset"] and
                        line_record["bytes"] == parsed["bytes"], "Runner line digest binding differs")
                matching = [item for item in state["servers"] if item["name"] == verified["name"]]
                require(len(matching) == 1, "Server state profile not unique")
                for key in ("name", "pid", "host", "port", "parallel", "context_length", "stderr", "stdout"):
                    require(matching[0][key] == verified[key], "State/verification differs: " + key)
                require(parsed["parallel"] == verified["parallel"] == server["parallel"] and
                        parsed["num_ctx"] == verified["context_length"] == server["context_length"],
                        "Declared profile differs from actual runner")
                require(server["host"] == verified["host"].rstrip("/") and
                        server["api_version"] == verified["api_version"] and
                        server["server_pid"] == verified["pid"], "Server endpoint/API/PID differs")
                servers_by_host[server["host"]] = server
                counts["prefixes"] += 1
                counts["runner_lines"] += 1
            identity = (batch["batch_id"], batch["model"])
            require(identity not in batch_by_id, "Repeated batch identity")
            batch_by_id[identity] = servers_by_host
            counts["batches"] += 1
        controls = report["trial_controls"]
        require(not controls["errors"] and controls["n_expected"] == controls["n_audited"] == 448 and
                controls["n_pending"] == 0 and not controls["pending_trial_ids"], "Incomplete inference controls")
        trial_rows = {row["trial_id"]: row for row in controls["trials"]}
        require(len(trial_rows) == len(controls["trials"]) == 448, "Control rows duplicate or missing")
        manifest_rows = {locate(item["source"]): item for item in controls["manifests"]}
        require(len(manifest_rows) == 4, "Expected four inference manifest groups")
        observed_digests = {}
        for entry in definition["manifests"]:
            manifest_path = locate("runs/main_001/" + entry["snapshot_path"])
            if manifest_path not in manifest_rows:
                continue
            manifest_row = manifest_rows[manifest_path]
            payload = verify_file(manifest_path, entry["snapshot_sha256"])
            require(verify_file(manifest_row["archive"], manifest_row["sha256"]) == payload and
                    manifest_row["sha256"] == entry["snapshot_sha256"] and manifest_row["valid"],
                    "Archived manifest binding differs")
            specs = [json.loads(line) for line in payload.decode("utf-8-sig").splitlines() if line.strip()]
            require(len(specs) == manifest_row["n_expected"], "Manifest row count differs")
            for spec in specs:
                row = trial_rows.pop(spec["trial_id"])
                summary_path = "runs/main_001/trials/" + spec["trial_id"] + "/summary.json"
                require(locate(row["summary_source"]) == locate(summary_path), "Control summary location differs")
                summary = json_file(summary_path, row["summary_sha256"])
                require(row["valid"] and not row["errors"] and summary["status"] == "complete",
                        "Invalid inference-control trial")
                require(all(summary.get(key) == value for key, value in spec.items()), "Summary differs from manifest")
                environment = summary["environment"]["ollama"]
                require(environment == row["worker_environment_ollama"], "Worker environment differs")
                host = spec["ollama_host"].rstrip("/")
                server = batch_by_id[(spec["batch_id"], spec["model"])][host]
                require(environment["base_url"].rstrip("/") == host == row["host"] and
                        environment["requested_model"] == spec["model"] == row["model"] and
                        environment["version"] == server["api_version"], "Trial/server binding differs")
                observed_digests.setdefault(spec["model"], set()).add(environment["model_digest"])
                inference = spec["method"] != "local_only"
                parallel = (2 if spec["method"] == "reserved_slot" else 1) if inference else None
                require(row["inference_expected"] == inference and row["expected_parallel"] == parallel and
                        row["observed_parallel"] == server["parsed_runner"]["parallel"] and
                        row["observed_context"] == server["parsed_runner"]["num_ctx"], "Trial profile differs")
                require(parallel is None or parallel == row["observed_parallel"], "Wrong inference parallelism")
                scheduler = summary["result"] if spec["study"] == "scheduler" else (summary["result"].get("scheduler") or {})
                requests = scheduler.get("requests", [])
                expected_requests = [{"request_id": item["request_id"], "request_type": item["request_type"],
                                      "status": item["status"], "generation": item["generation"],
                                      "backend_model": item.get("backend_metadata", {}).get("model"),
                                      "eval_count": item.get("backend_metadata", {}).get("eval_count")}
                                     for item in requests]
                models = [item["backend_model"] for item in expected_requests if item["backend_model"] is not None]
                require(row["requests"] == expected_requests and row["observed_backend_models"] == models,
                        "Persisted request/model control differs")
                require(all(model == spec["model"] or model.endswith("/" + spec["model"]) for model in models),
                        "Backend returned another model")
                require(inference or not requests, "Local-only condition contains inference requests")
                require(not (inference and scheduler.get("status") == "success") or bool(models),
                        "Successful inference lacks backend-reported model")
                counts["trials"] += 1
        require(not trial_rows and counts == {"batches": 32, "prefixes": 64, "runner_lines": 64, "trials": 448},
                "Final server evidence coverage differs")
        require({key: sorted(value) for key, value in observed_digests.items()} == controls["model_digests"] and
                all(len(value) == 1 for value in observed_digests.values()), "Model digest consistency differs")
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        errors.append(str(exc))
    return {"valid": not errors, **counts, "verified_files": len(checked), "errors": errors,
            "scope": "Offline public-byte verification and historical-to-public SHA bindings; no server contact or new experiment."}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path)
    args = parser.parse_args()
    manifest = read_json(args.manifest) if args.manifest else None
    result = verify_server_archive(args.root, manifest)
    print(json.dumps(result, indent=2))
    return 0 if result["valid"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
