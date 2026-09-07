"""Sequential, resumable, process-isolated manifest runner.

Campaigns are bound to immutable manifest snapshots. Every retry receives a
fresh attempt directory, and only a fully validated worker summary is adopted
as the trial-level summary used for resume and analysis.
"""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any, Sequence
import uuid

from hera_v2.analysis import estimate_remaining_seconds
from hera_v2.batch_lifecycle import prepare_scheduler_batch
from hera_v2.provenance import bind_campaign_source, source_digest
from hera_v2.manifest import (
    TrialSpec,
    canonical_json_bytes,
    load_manifest,
    manifest_file_sha256,
    trial_spec_sha256,
)


ATTEMPT_PATTERN = re.compile(r"^attempt-(\d{4,})$")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False),
        encoding="utf-8",
    )
    temporary.replace(path)


def _atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)


def _read_summary(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _sha256_path(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        # os.kill(pid, 0) is not a harmless existence probe on Windows.
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        open_process.restype = wintypes.HANDLE
        get_exit_code = kernel32.GetExitCodeProcess
        get_exit_code.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        get_exit_code.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL
        handle = open_process(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            # ERROR_INVALID_PARAMETER means no such PID. Access-denied and
            # unexpected failures must not authorize reclaiming another lock.
            return ctypes.get_last_error() != 87
        try:
            exit_code = wintypes.DWORD()
            if not get_exit_code(handle, ctypes.byref(exit_code)):
                return True
            return exit_code.value == 259  # STILL_ACTIVE
        finally:
            close_handle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _read_lock(path: Path) -> dict[str, Any]:
    try:
        payload = path.read_text(encoding="utf-8").strip()
        value = json.loads(payload)
        if isinstance(value, dict):
            return value
        return {"pid": int(value)}
    except Exception:
        try:
            return {"pid": int(path.read_text(encoding="ascii").strip())}
        except Exception:
            return {}


def _acquire_lock(path: Path) -> str:
    """Acquire a PID lock, replacing it only when its recorded PID is dead."""

    token = str(uuid.uuid4())
    record = {
        "schema_version": 1,
        "pid": os.getpid(),
        "token": token,
        "created_utc": _utc_now(),
    }
    payload = canonical_json_bytes(record)
    for _ in range(8):
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                before = path.stat()
            except FileNotFoundError:
                continue
            existing = _read_lock(path)
            try:
                existing_pid = int(existing.get("pid", -1))
            except (TypeError, ValueError):
                existing_pid = -1
            if _pid_is_alive(existing_pid):
                raise RuntimeError(
                    f"Campaign is already locked by live PID {existing_pid}: {path}"
                )
            try:
                after = path.stat()
            except FileNotFoundError:
                continue
            identity_before = (before.st_ino, before.st_size, before.st_mtime_ns)
            identity_after = (after.st_ino, after.st_size, after.st_mtime_ns)
            if identity_before != identity_after:
                continue
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            continue
        try:
            os.write(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return token
    raise RuntimeError(f"Could not acquire campaign lock safely: {path}")


def _release_lock(path: Path, token: str) -> None:
    """Remove only the lock created by this process."""

    current = _read_lock(path)
    if current.get("token") != token:
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _load_source_manifests(paths: Sequence[Path]) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    for path in paths:
        resolved = path.resolve()
        digest_before = manifest_file_sha256(resolved)
        specs = load_manifest(resolved)
        digest_after = manifest_file_sha256(resolved)
        if digest_before != digest_after:
            raise RuntimeError(f"Manifest changed while it was being read: {resolved}")
        sources.append({"path": resolved, "sha256": digest_before, "specs": specs})
    return sources


def _campaign_definition_sha256(hashes: Sequence[str]) -> str:
    return hashlib.sha256(
        canonical_json_bytes({"ordered_manifest_sha256": list(hashes)})
    ).hexdigest()


def _prepare_manifest_snapshots(
    campaign_dir: Path, sources: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Create or verify exact, immutable manifest copies for this campaign."""

    snapshots_dir = campaign_dir / "manifests"
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    campaign_path = campaign_dir / "campaign.json"
    source_hashes = [str(source["sha256"]) for source in sources]
    definition_hash = _campaign_definition_sha256(source_hashes)
    existing = _read_summary(campaign_path)

    if existing is None and campaign_path.exists():
        raise RuntimeError(f"Campaign metadata is unreadable: {campaign_path}")
    if existing is None:
        entries: list[dict[str, Any]] = []
        for index, source in enumerate(sources, 1):
            source_path = Path(source["path"])
            payload = source_path.read_bytes()
            actual_hash = hashlib.sha256(payload).hexdigest()
            if actual_hash != source["sha256"]:
                raise RuntimeError(f"Manifest changed before snapshot: {source_path}")
            snapshot_name = f"{index:03d}_{source_path.name}"
            snapshot_path = snapshots_dir / snapshot_name
            if snapshot_path.exists():
                if _sha256_path(snapshot_path) != actual_hash:
                    raise RuntimeError(f"Conflicting manifest snapshot: {snapshot_path}")
            else:
                _atomic_bytes(snapshot_path, payload)
            entries.append(
                {
                    "order": index - 1,
                    "source_path": str(source_path),
                    "source_sha256": actual_hash,
                    "snapshot_path": f"manifests/{snapshot_name}",
                    "snapshot_sha256": actual_hash,
                    "trial_count": len(source["specs"]),
                }
            )
        existing = {
            "schema_version": 1,
            "created_utc": _utc_now(),
            "campaign_definition_sha256": definition_hash,
            "manifests": entries,
        }
        _atomic_json(campaign_path, existing)
    else:
        if existing.get("schema_version") != 1:
            raise RuntimeError("Unsupported campaign metadata schema")
        if existing.get("campaign_definition_sha256") != definition_hash:
            raise RuntimeError(
                "Input manifests do not match the manifests bound to this campaign"
            )
        entries = existing.get("manifests")
        if not isinstance(entries, list) or len(entries) != len(sources):
            raise RuntimeError("Campaign manifest list is malformed or has changed")
        recorded_hashes = [entry.get("source_sha256") for entry in entries]
        if recorded_hashes != source_hashes:
            raise RuntimeError("Input manifest order/content differs from this campaign")

    prepared: list[dict[str, Any]] = []
    entries = existing.get("manifests")
    if not isinstance(entries, list):
        raise RuntimeError("Campaign manifest metadata is malformed")
    snapshots_root = snapshots_dir.resolve()
    for index, (entry, source) in enumerate(zip(entries, sources, strict=True)):
        if entry.get("order") != index:
            raise RuntimeError("Campaign snapshot order is malformed")
        relative = entry.get("snapshot_path")
        if not isinstance(relative, str):
            raise RuntimeError("Campaign snapshot path is malformed")
        snapshot_path = (campaign_dir / relative).resolve()
        if not snapshot_path.is_relative_to(snapshots_root):
            raise RuntimeError(f"Campaign snapshot escapes manifest directory: {relative}")
        expected_hash = str(entry.get("snapshot_sha256"))
        if not snapshot_path.is_file() or manifest_file_sha256(snapshot_path) != expected_hash:
            raise RuntimeError(f"Campaign manifest snapshot is missing or modified: {snapshot_path}")
        specs = load_manifest(snapshot_path)
        if specs != source["specs"]:
            raise RuntimeError(f"Snapshot content differs from source manifest: {snapshot_path}")
        prepared.append(
            {"path": snapshot_path, "sha256": expected_hash, "specs": specs}
        )
    return prepared


def _artifact_error(metadata: Any, attempt_dir: Path, label: str) -> str | None:
    if not isinstance(metadata, dict):
        return f"missing {label} artifact metadata"
    filename = metadata.get("file")
    digest = metadata.get("sha256")
    if not isinstance(filename, str) or Path(filename).name != filename:
        return f"invalid {label} artifact filename"
    if not isinstance(digest, str) or len(digest) != 64:
        return f"invalid {label} artifact digest"
    path = attempt_dir / filename
    if not path.is_file():
        return f"missing {label} artifact file"
    if _sha256_path(path) != digest:
        return f"{label} artifact digest mismatch"
    return None


def _summary_validation_error(
    summary: dict[str, Any] | None,
    spec: TrialSpec,
    manifest_sha256: str,
    *,
    attempt_id: str,
    attempt_dir: Path,
    verify_artifact_hashes: bool,
) -> str | None:
    if summary is None:
        return "summary is missing or unreadable"
    if summary.get("schema_version") != 1:
        return "summary schema_version is not 1"
    if summary.get("trial_id") != spec.trial_id:
        return "summary trial_id mismatch"
    if summary.get("trial_spec_sha256") != trial_spec_sha256(spec):
        return "summary TrialSpec hash mismatch"
    if summary.get("manifest_sha256") != manifest_sha256:
        return "summary manifest hash mismatch"
    if summary.get("attempt_id") != attempt_id:
        return "summary attempt_id mismatch"
    for field, expected in spec.to_dict().items():
        if summary.get(field) != expected:
            return f"summary TrialSpec field mismatch: {field}"
    if summary.get("status") != "complete":
        return f"summary status is not complete: {summary.get('status')!r}"
    if not isinstance(summary.get("success"), bool):
        return "summary success is not boolean"
    event_time = summary.get("event_time_s")
    if event_time is not None:
        try:
            numeric_event_time = float(event_time)
        except (TypeError, ValueError):
            return "summary event_time_s is not numeric"
        if not math.isfinite(numeric_event_time) or numeric_event_time < 0:
            return "summary event_time_s is invalid"
    elif summary.get("success") and spec.study == "scheduler":
        return "successful summary has no event_time_s"
    if (
        spec.study == "scheduler"
        and summary.get("success")
        and float(event_time) > spec.deadline_s
    ):
        return "successful scheduler summary exceeds deadline"
    try:
        elapsed = float(summary.get("elapsed_s"))
    except (TypeError, ValueError):
        return "summary elapsed_s is not numeric"
    if not math.isfinite(elapsed) or elapsed < 0:
        return "summary elapsed_s is invalid"
    if not isinstance(summary.get("environment"), dict):
        return "summary environment snapshot is missing"
    if not isinstance(summary.get("result"), dict):
        return "summary result payload is missing"
    artifacts = summary.get("artifacts")
    if not isinstance(artifacts, dict) or "events" not in artifacts:
        return "summary events artifact metadata is missing"
    if verify_artifact_hashes:
        error = _artifact_error(artifacts.get("events"), attempt_dir, "events")
        if error:
            return error
        if "trace" in artifacts:
            error = _artifact_error(artifacts.get("trace"), attempt_dir, "trace")
            if error:
                return error
    return None


def _complete(
    spec: TrialSpec,
    results_dir: Path,
    manifest_sha256: str,
) -> bool:
    trial_dir = results_dir / spec.trial_id
    summary = _read_summary(trial_dir / "summary.json")
    if summary is None:
        return False
    attempt_id = summary.get("adopted_attempt_id")
    if not isinstance(attempt_id, str) or ATTEMPT_PATTERN.fullmatch(attempt_id) is None:
        return False
    attempt_dir = trial_dir / "attempts" / attempt_id
    error = _summary_validation_error(
        summary,
        spec,
        manifest_sha256,
        attempt_id=attempt_id,
        attempt_dir=attempt_dir,
        verify_artifact_hashes=False,
    )
    if error:
        return False
    worker_summary_path = attempt_dir / "summary.json"
    expected_worker_hash = summary.get("worker_summary_sha256")
    if not isinstance(expected_worker_hash, str) or not worker_summary_path.is_file():
        return False
    if _sha256_path(worker_summary_path) != expected_worker_hash:
        return False
    worker_summary = _read_summary(worker_summary_path)
    return _summary_validation_error(
        worker_summary,
        spec,
        manifest_sha256,
        attempt_id=attempt_id,
        attempt_dir=attempt_dir,
        verify_artifact_hashes=True,
    ) is None


def _next_attempt_dir(trial_dir: Path) -> tuple[str, Path]:
    attempts_dir = trial_dir / "attempts"
    attempts_dir.mkdir(parents=True, exist_ok=True)
    existing_numbers = [
        int(match.group(1))
        for path in attempts_dir.iterdir()
        if path.is_dir() and (match := ATTEMPT_PATTERN.fullmatch(path.name))
    ]
    number = max(existing_numbers, default=0) + 1
    while True:
        attempt_id = f"attempt-{number:04d}"
        attempt_dir = attempts_dir / attempt_id
        try:
            attempt_dir.mkdir()
        except FileExistsError:
            number += 1
            continue
        return attempt_id, attempt_dir


def _fallback_seconds(spec: TrialSpec) -> float:
    if spec.study in {"physics", "integrated"}:
        return spec.duration_s + 5.0
    return spec.deadline_s + spec.fault_offset_s + 10.0


def _load_pilot_estimates(value: str | None) -> dict[str, float]:
    if value is None:
        return {}
    payload = value if value.lstrip().startswith("{") else Path(value).read_text(encoding="utf-8-sig")
    estimates = json.loads(payload)
    if not isinstance(estimates, dict):
        raise ValueError("Pilot estimates must be a JSON object mapping method to seconds")
    parsed: dict[str, float] = {}
    for method, seconds in estimates.items():
        if not isinstance(method, str) or not method or isinstance(seconds, bool):
            raise ValueError("Invalid pilot method/seconds entry")
        numeric = float(seconds)
        if not math.isfinite(numeric) or numeric <= 0:
            raise ValueError(f"Pilot estimate must be finite and positive: {method}")
        parsed[method] = numeric
    return parsed


def _batch_key(spec: TrialSpec) -> tuple[str, str]:
    return spec.batch_id, spec.model


def _progress(
    *,
    campaign_dir: Path,
    all_specs: list[TrialSpec],
    manifest_hash_for_trial: dict[str, str],
    results_dir: Path,
    started_monotonic: float,
    current: TrialSpec | None,
    failures: list[dict[str, Any]],
    pilot_method_seconds: dict[str, float] | None = None,
    lifecycle_records: list[dict[str, Any]] | None = None,
    prepared_batches: set[tuple[str, str]] | None = None,
    scheduler_batch_warmup: bool = False,
    phase: str = "trials",
    analysis: dict[str, Any] | None = None,
    validated_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    validated_index = ({row["trial_id"]: row for row in validated_rows}
                       if validated_rows is not None else None)
    completed = [
        spec
        for spec in all_specs
        if (spec.trial_id in validated_index
            if validated_index is not None
            else _complete(spec, results_dir, manifest_hash_for_trial[spec.trial_id]))
    ]
    rows: list[dict[str, Any]] = []
    for spec in completed:
        summary = (validated_index[spec.trial_id]
                   if validated_index is not None
                   else _read_summary(results_dir / spec.trial_id / "summary.json"))
        if summary:
            # ETA must use the actual parent-observed process cycle, not merely
            # time spent inside run_one().
            eta_row = dict(summary)
            parent_wall = summary.get("parent_wall_elapsed_s", summary.get("parent_wall_cycle_s"))
            if parent_wall is not None:
                eta_row["parent_wall_elapsed_s"] = parent_wall
            rows.append(eta_row)
    completed_ids = {row["trial_id"] for row in rows}
    remaining = [spec for spec in all_specs if spec.trial_id not in completed_ids]
    pilot_eta = estimate_remaining_seconds(
        all_specs, rows, pilot_method_seconds=pilot_method_seconds
    )
    if pilot_eta["estimated_remaining_s"] is None:
        estimated_remaining = sum(_fallback_seconds(spec) for spec in remaining)
        eta_source = "configured upper-budget fallback"
    else:
        estimated_remaining = float(pilot_eta["estimated_remaining_s"])
        eta_source = "method parent wall means, then pilot estimates, then configured budgets; 10% trial overhead"
    lifecycle_records = lifecycle_records or []
    lifecycle_samples = [
        float(record["elapsed_s"]) for record in lifecycle_records
        if record.get("status", "complete") == "complete" and record.get("elapsed_s") is not None
    ]
    pending_batches = {
        _batch_key(spec) for spec in remaining if spec.study in {"scheduler", "integrated"}
    } - (prepared_batches or set())
    lifecycle_per_batch = (
        sum(lifecycle_samples) / len(lifecycle_samples) if lifecycle_samples else 60.0
    )
    lifecycle_remaining = (
        len(pending_batches) * lifecycle_per_batch if scheduler_batch_warmup else 0.0
    )
    estimated_remaining += lifecycle_remaining
    state = {
        "pid": os.getpid(),
        "updated_utc": _utc_now(),
        "campaign_elapsed_s": time.perf_counter() - started_monotonic,
        "total_trials": len(all_specs),
        "completed_trials": len(completed),
        "remaining_trials": len(remaining),
        "current_trial_id": current.trial_id if current else None,
        "current_study": current.study if current else None,
        "phase": phase,
        "estimated_remaining_s": estimated_remaining,
        "eta_source": eta_source,
        "eta_by_method": pilot_eta["by_method"],
        "scheduler_lifecycle": {
            "enabled": scheduler_batch_warmup,
            "completed_this_invocation": len(lifecycle_samples),
            "elapsed_s": sum(float(record.get("elapsed_s", 0.0)) for record in lifecycle_records),
            "remaining_batches": len(pending_batches) if scheduler_batch_warmup else 0,
            "estimated_remaining_s": lifecycle_remaining,
            "estimate_per_batch_s": lifecycle_per_batch,
            "estimate_source": "observed batch lifecycle mean" if lifecycle_samples else "60 second initial batch budget",
            "records": lifecycle_records,
        },
        "analysis": analysis or {"status": "pending", "manifests": []},
        "failures": failures,
    }
    _atomic_json(campaign_dir / "progress.json", state)
    return state


def _runner_error_summary(
    spec: TrialSpec,
    manifest_sha256: str,
    attempt_id: str,
    reason: str,
    parent_wall_elapsed_s: float,
    started_utc: str,
    worker_summary: dict[str, Any] | None,
) -> dict[str, Any]:
    worker_reason = worker_summary.get("failure_reason") if worker_summary else None
    return {
        **spec.to_dict(),
        "schema_version": 1,
        "trial_spec_sha256": trial_spec_sha256(spec),
        "manifest_sha256": manifest_sha256,
        "attempt_id": attempt_id,
        "latest_attempt_id": attempt_id,
        "status": "error",
        "success": False,
        "event_time_s": None,
        "failure_reason": worker_reason or reason,
        "runner_failure_reason": reason,
        "started_utc": started_utc,
        "elapsed_s": worker_summary.get("elapsed_s") if worker_summary else None,
        "parent_wall_elapsed_s": parent_wall_elapsed_s,
        "completed_utc": _utc_now(),
        "environment": worker_summary.get("environment") if worker_summary else None,
        "artifacts": worker_summary.get("artifacts", {}) if worker_summary else {},
    }


def _run_final_analyses(
    prepared: Sequence[dict[str, Any]], results_dir: Path, campaign_dir: Path
) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    analyzer = Path(__file__).resolve().parent / "analyze_results.py"
    for manifest in prepared:
        manifest_path = Path(manifest["path"])
        output_dir = campaign_dir / "analysis" / manifest_path.stem
        output_dir.mkdir(parents=True, exist_ok=True)
        command = [
            sys.executable, str(analyzer), str(manifest_path),
            "--results-dir", str(results_dir), "--output-dir", str(output_dir),
            "--require-all",
        ]
        started = time.perf_counter()
        record: dict[str, Any] = {
            "manifest": str(manifest_path), "manifest_sha256": manifest["sha256"],
            "output_dir": str(output_dir),
        }
        with (output_dir / "stdout.log").open("wb") as stdout, (output_dir / "stderr.log").open("wb") as stderr:
            try:
                completed = subprocess.run(
                    command, stdout=stdout, stderr=stderr, timeout=300, check=False
                )
                record["returncode"] = completed.returncode
                record["status"] = (
                    "complete" if completed.returncode == 0
                    and (output_dir / "analysis.json").is_file()
                    and (output_dir / "trial_summary.csv").is_file() else "failed"
                )
            except (subprocess.TimeoutExpired, OSError) as exc:
                record.update(status="failed", error=str(exc))
        record["elapsed_s"] = time.perf_counter() - started
        records.append(record)
    return {
        "status": "complete" if all(row["status"] == "complete" for row in records) else "failed",
        "manifests": records,
        "elapsed_s": sum(row["elapsed_s"] for row in records),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one or more HERA v2 manifests sequentially")
    parser.add_argument("manifests", nargs="+", type=Path)
    parser.add_argument("--campaign-dir", type=Path, required=True)
    parser.add_argument("--trial-timeout-s", type=float, default=90.0)
    parser.add_argument("--stop-after", type=int)
    parser.add_argument(
        "--scheduler-batch-warmup", action=argparse.BooleanOptionalAction, default=True,
        help="Unload, warm, and verify dedicated servers before each pending scheduler batch",
    )
    parser.add_argument(
        "--server-state", type=Path,
        default=Path(__file__).resolve().parent / "runs" / "servers" / "servers.current.json",
    )
    parser.add_argument(
        "--pilot-estimates",
        help='JSON object or JSON file path mapping method to parent wall seconds',
    )
    args = parser.parse_args()
    if args.trial_timeout_s <= 0:
        parser.error("--trial-timeout-s must be positive")
    if args.stop_after is not None and args.stop_after < 0:
        parser.error("--stop-after cannot be negative")
    try:
        pilot_method_seconds = _load_pilot_estimates(args.pilot_estimates)
    except (OSError, ValueError, TypeError) as exc:
        parser.error(f"Invalid --pilot-estimates: {exc}")

    # Parse and validate all user-supplied manifests before creating a lock.
    sources = _load_source_manifests(args.manifests)
    campaign_dir = args.campaign_dir.resolve()
    campaign_dir.mkdir(parents=True, exist_ok=True)
    lock_path = campaign_dir / "runner.lock"
    lock_token = _acquire_lock(lock_path)
    started = time.perf_counter()
    failures: list[dict[str, Any]] = []
    try:
        prepared = _prepare_manifest_snapshots(campaign_dir, sources)
        provenance = bind_campaign_source(campaign_dir)
        campaign_source_hash = provenance["source_sha256"]
        results_dir = campaign_dir / "trials"
        results_dir.mkdir(parents=True, exist_ok=True)
        worker = Path(__file__).resolve().parent / "trial_worker.py"
        all_specs: list[TrialSpec] = []
        manifest_for_trial: dict[str, Path] = {}
        manifest_hash_for_trial: dict[str, str] = {}
        for manifest in prepared:
            for spec in manifest["specs"]:
                if spec.trial_id in manifest_for_trial:
                    raise ValueError(f"Duplicate trial across manifests: {spec.trial_id}")
                all_specs.append(spec)
                manifest_for_trial[spec.trial_id] = manifest["path"]
                manifest_hash_for_trial[spec.trial_id] = manifest["sha256"]

        prepared_batches: set[tuple[str, str]] = set()
        lifecycle_records: list[dict[str, Any]] = []
        analysis_status: dict[str, Any] = {"status": "pending", "manifests": []}
        # Fully validate once on resume and once at finalization. During the
        # run, immutable adopted rows avoid quadratic rehashing of all traces.
        validated_by_id = {
            spec.trial_id: _read_summary(results_dir / spec.trial_id / "summary.json")
            for spec in all_specs
            if _complete(spec, results_dir, manifest_hash_for_trial[spec.trial_id])
        }

        def report_progress(current: TrialSpec | None = None, phase: str = "trials") -> dict[str, Any]:
            return _progress(
                campaign_dir=campaign_dir, all_specs=all_specs,
                manifest_hash_for_trial=manifest_hash_for_trial, results_dir=results_dir,
                started_monotonic=started, current=current, failures=failures,
                pilot_method_seconds=pilot_method_seconds,
                lifecycle_records=lifecycle_records, prepared_batches=prepared_batches,
                scheduler_batch_warmup=args.scheduler_batch_warmup,
                phase=phase, analysis=analysis_status,
                validated_rows=list(validated_by_id.values()),
            )

        attempted = 0
        report_progress()
        for spec in all_specs:
            expected_manifest_hash = manifest_hash_for_trial[spec.trial_id]
            if spec.trial_id in validated_by_id:
                continue
            if args.stop_after is not None and attempted >= args.stop_after:
                break
            if source_digest() != campaign_source_hash:
                failures.append({"trial_id": spec.trial_id, "reason": "campaign_source_changed"})
                break
            batch_key = _batch_key(spec)
            if (
                spec.study in {"scheduler", "integrated"} and args.scheduler_batch_warmup
                and batch_key not in prepared_batches
            ):
                report_progress(spec, "scheduler_batch_lifecycle")
                batch_specs = [
                    item for item in all_specs
                    if item.study in {"scheduler", "integrated"} and _batch_key(item) == batch_key
                ]
                lifecycle_started = time.perf_counter()
                try:
                    lifecycle = prepare_scheduler_batch(
                        batch_specs, campaign_dir, args.server_state.resolve()
                    )
                except Exception as exc:
                    record = {
                        "batch_id": spec.batch_id, "model": spec.model,
                        "status": "failed", "error": str(exc),
                        "elapsed_s": time.perf_counter() - lifecycle_started,
                    }
                    lifecycle_records.append(record)
                    failures.append({"trial_id": spec.trial_id, "reason": "scheduler_batch_lifecycle_failed", **record})
                    break
                lifecycle_records.append({
                    **lifecycle, "status": "complete",
                    "elapsed_s": time.perf_counter() - lifecycle_started,
                })
                prepared_batches.add(batch_key)
                if source_digest() != campaign_source_hash:
                    failures.append({"trial_id": spec.trial_id, "reason": "campaign_source_changed"})
                    break
            attempted += 1
            report_progress(spec)
            trial_dir = results_dir / spec.trial_id
            trial_dir.mkdir(parents=True, exist_ok=True)
            attempt_id, attempt_dir = _next_attempt_dir(trial_dir)
            command = [
                sys.executable,
                str(worker),
                "--manifest",
                str(manifest_for_trial[spec.trial_id]),
                "--trial-id",
                spec.trial_id,
                "--output-dir",
                str(attempt_dir),
                "--attempt-id",
                attempt_id,
            ]
            attempt_started_utc = _utc_now()
            attempt_started = time.perf_counter()
            timed_out = False
            with (attempt_dir / "stdout.log").open("ab") as stdout, (
                attempt_dir / "stderr.log"
            ).open("ab") as stderr:
                try:
                    completed = subprocess.run(
                        command,
                        stdout=stdout,
                        stderr=stderr,
                        timeout=args.trial_timeout_s,
                        check=False,
                    )
                except subprocess.TimeoutExpired:
                    completed = None
                    timed_out = True
            parent_wall_elapsed_s = time.perf_counter() - attempt_started
            worker_summary_path = attempt_dir / "summary.json"
            worker_summary = _read_summary(worker_summary_path)
            validation_error = _summary_validation_error(
                worker_summary,
                spec,
                expected_manifest_hash,
                attempt_id=attempt_id,
                attempt_dir=attempt_dir,
                verify_artifact_hashes=True,
            )
            if validation_error is None and worker_summary["environment"].get("source_sha256") != campaign_source_hash:
                validation_error = "worker source hash differs from campaign source"
            returncode = completed.returncode if completed is not None else None
            if not timed_out and returncode == 0 and validation_error is None:
                worker_summary_hash = _sha256_path(worker_summary_path)
                adopted = {
                    **worker_summary,
                    "adopted_attempt_id": attempt_id,
                    "attempt_relative_dir": f"attempts/{attempt_id}",
                    "worker_summary_sha256": worker_summary_hash,
                    "parent_wall_elapsed_s": parent_wall_elapsed_s,
                }
                _atomic_json(
                    attempt_dir / "runner.json",
                    {
                        "schema_version": 1,
                        "attempt_id": attempt_id,
                        "worker_returncode": returncode,
                        "parent_wall_elapsed_s": parent_wall_elapsed_s,
                        "adopted": True,
                    },
                )
                _atomic_json(trial_dir / "summary.json", adopted)
                if not _complete(spec, results_dir, expected_manifest_hash):
                    reason = "adopted summary failed post-write validation"
                    failures.append(
                        {"trial_id": spec.trial_id, "attempt_id": attempt_id, "reason": reason}
                    )
                    error_summary = _runner_error_summary(
                        spec,
                        expected_manifest_hash,
                        attempt_id,
                        reason,
                        parent_wall_elapsed_s,
                        attempt_started_utc,
                        worker_summary,
                    )
                    _atomic_json(trial_dir / "summary.json", error_summary)
                else:
                    validated_by_id[spec.trial_id] = adopted
            else:
                if timed_out:
                    reason = "parent_timeout"
                elif returncode != 0:
                    reason = f"worker_exit_{returncode}"
                else:
                    reason = f"invalid_worker_summary: {validation_error}"
                failures.append(
                    {"trial_id": spec.trial_id, "attempt_id": attempt_id, "reason": reason}
                )
                error_summary = _runner_error_summary(
                    spec,
                    expected_manifest_hash,
                    attempt_id,
                    reason,
                    parent_wall_elapsed_s,
                    attempt_started_utc,
                    worker_summary,
                )
                _atomic_json(
                    attempt_dir / "runner.json",
                    {
                        "schema_version": 1,
                        "attempt_id": attempt_id,
                        "worker_returncode": returncode,
                        "parent_wall_elapsed_s": parent_wall_elapsed_s,
                        "adopted": False,
                        "failure_reason": reason,
                        "worker_summary_validation_error": validation_error,
                    },
                )
                _atomic_json(trial_dir / "summary.json", error_summary)

            report_progress()

        final = report_progress()
        if final["remaining_trials"] == 0:
            invalid_final = [spec.trial_id for spec in all_specs
                             if not _complete(spec, results_dir, manifest_hash_for_trial[spec.trial_id])]
            if invalid_final:
                raise RuntimeError(f"Final artifact integrity failed: {invalid_final}")
            if source_digest() != campaign_source_hash:
                analysis_status = {"status": "failed", "reason": "campaign_source_changed", "manifests": []}
                failures.append({"reason": "campaign_source_changed_before_analysis"})
            else:
                analysis_status = {"status": "running", "manifests": []}
                report_progress(phase="analysis")
                analysis_status = _run_final_analyses(prepared, results_dir, campaign_dir)
        else:
            analysis_status = {"status": "not_run", "reason": "incomplete_trials", "manifests": []}
        final = report_progress(phase="finished")
        final["runner_finished_utc"] = _utc_now()
        if final["remaining_trials"]:
            final["runner_status"] = "incomplete"
        else:
            final["runner_status"] = "complete" if analysis_status["status"] == "complete" else "analysis_failed"
        _atomic_json(campaign_dir / "progress.json", final)
        print(json.dumps(final, indent=2, sort_keys=True))
        return 0 if not failures and final["runner_status"] == "complete" else 2
    finally:
        _release_lock(lock_path, lock_token)


if __name__ == "__main__":
    raise SystemExit(main())
