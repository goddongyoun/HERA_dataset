"""Run one manifest trial in an isolated process.

Process isolation prevents a timed-out HTTP request or MuJoCo object from
leaking into the next randomized condition.  The parent campaign runner owns
timeouts and resume behavior.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
from pathlib import Path
import subprocess
import sys
import time
import traceback
from typing import Any

import httpx

from hera_v2.events import EventIdentity, JsonlEventLogger
from hera_v2.manifest import (
    TrialSpec,
    load_manifest,
    manifest_file_sha256,
    trial_spec_sha256,
)
from hera_v2.ollama_backend import OllamaBackend
from hera_v2.physics import run_physics_trial
from hera_v2.integrated import run_integrated_trial
from hera_v2.provenance import source_digest
from hera_v2.scheduler import (
    EmergencyContext,
    TrialSpec as SchedulerTrialSpec,
    run_scheduler_trial,
)


LEG_CHANNELS = {
    "FL": (0, 1, 2),
    "FR": (3, 4, 5),
    "BR": (6, 7, 8),
    "BL": (9, 10, 11),
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False),
        encoding="utf-8",
    )
    temporary.replace(path)


def _atomic_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    payload = "\n".join(
        json.dumps(row, sort_keys=True, allow_nan=False) for row in rows
    )
    temporary.write_text(payload + ("\n" if rows else ""), encoding="utf-8")
    temporary.replace(path)


def _artifact_snapshot(path: Path) -> dict[str, Any]:
    payload = path.read_bytes()
    return {
        "file": path.name,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "bytes": len(payload),
        "rows": sum(1 for line in payload.splitlines() if line.strip()),
    }


def _audit_events(path: Path, spec: TrialSpec) -> dict[str, Any]:
    """Fail closed unless one attempt has a coherent, terminal event stream."""

    if not path.is_file() or path.stat().st_size == 0:
        raise ValueError("events.jsonl is missing or empty")
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid event JSON at line {line_number}: {exc}") from exc
        if record.get("sequence") != len(records) + 1:
            raise ValueError(
                f"event sequence is not contiguous at line {line_number}: "
                f"{record.get('sequence')!r}"
            )
        if record.get("trial_id") != spec.trial_id or record.get("batch_id") != spec.batch_id:
            raise ValueError(f"event identity mismatch at line {line_number}")
        records.append(record)
    if not records:
        raise ValueError("events.jsonl contains no records")
    event_ids = [record.get("event_id") for record in records]
    if None in event_ids or len(event_ids) != len(set(event_ids)):
        raise ValueError("event_id values must be present and unique")
    if spec.study == "scheduler":
        first_stage, terminal_stage = "trial_started", "trial_completed"
    elif spec.study == "physics":
        first_stage, terminal_stage = "physics_trial_started", "physics_trial_finished"
    elif spec.study == "integrated":
        first_stage, terminal_stage = "integrated_trial_started", "integrated_trial_finished"
    else:
        raise ValueError(f"unsupported study for event audit: {spec.study!r}")
    stages = [str(record.get("stage")) for record in records]
    if stages[0] != first_stage:
        raise ValueError(f"first event must be {first_stage!r}, got {stages[0]!r}")
    if stages[-1] != terminal_stage:
        raise ValueError(f"last event must be {terminal_stage!r}, got {stages[-1]!r}")
    if stages.count(first_stage) != 1 or stages.count(terminal_stage) != 1:
        raise ValueError("trial start and terminal events must each occur exactly once")
    return _artifact_snapshot(path)


def _best_effort_artifacts(output_dir: Path) -> dict[str, Any]:
    artifacts: dict[str, Any] = {}
    for name, filename in (("events", "events.jsonl"), ("trace", "trace.jsonl")):
        path = output_dir / filename
        try:
            if path.is_file():
                artifacts[name] = _artifact_snapshot(path)
        except OSError:
            pass
    return artifacts


def _source_digest() -> str:
    return source_digest()


def _git_state() -> dict[str, Any]:
    root = Path(__file__).resolve().parent.parent
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True, capture_output=True,
            timeout=5, check=True,
        ).stdout.strip()
        dirty = bool(subprocess.run(
            ["git", "status", "--porcelain", "--", "revision_v3"], cwd=root,
            text=True, capture_output=True, timeout=5, check=True,
        ).stdout.strip())
        return {"root_commit": commit, "revision_v3_uncommitted": dirty}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def _package_versions() -> dict[str, str | None]:
    result: dict[str, str | None] = {}
    for distribution in ("dm-control", "mujoco", "httpx", "pydantic", "numpy"):
        try:
            result[distribution] = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            result[distribution] = None
    return result


def _ollama_snapshot(base_url: str, model: str) -> dict[str, Any]:
    snapshot: dict[str, Any] = {"base_url": base_url, "requested_model": model}
    try:
        with httpx.Client(timeout=5.0) as client:
            version = client.get(f"{base_url.rstrip('/')}/api/version")
            version.raise_for_status()
            snapshot["version"] = version.json().get("version")
            tags = client.get(f"{base_url.rstrip('/')}/api/tags")
            tags.raise_for_status()
            models = tags.json().get("models", [])
            match = next(
                (entry for entry in models if entry.get("name") == model or entry.get("model") == model),
                None,
            )
            if match:
                snapshot["model_digest"] = match.get("digest")
                snapshot["model_size"] = match.get("size")
    except Exception as exc:
        snapshot["snapshot_error"] = f"{type(exc).__name__}: {exc}"
    return snapshot


def _safe_action(fault_leg: str) -> tuple[float, ...]:
    support = (0.0, -0.08, -0.48)
    failed = (0.0, 0.0, -0.4)
    values: list[float] = []
    for leg in ("FL", "FR", "BR", "BL"):
        values.extend(failed if leg == fault_leg else support)
    return tuple(values)


def _mission_prompt(spec: TrialSpec) -> str:
    # The text depends only on paired scenario fields, never on scheduling
    # method, so method comparisons receive exactly the same workload.
    return (
        f"Scenario seed {spec.seed}; quadruped route sector {spec.fault_leg}. "
        "Produce a long-horizon inspection and mapping plan with exactly 160 numbered "
        "items. Each item must contain a navigation action, a sensing action, a risk "
        "check, and a fallback, written as complete sentences. Continue through item "
        "160 without summarizing or stopping early."
    )


async def _run_scheduler(spec: TrialSpec, logger: JsonlEventLogger) -> dict[str, Any]:
    async def delayed_fault() -> None:
        await asyncio.sleep(spec.fault_offset_s)

    scheduler_spec = SchedulerTrialSpec(
        batch_id=spec.batch_id,
        run_id=spec.trial_id,
        trial_id=spec.trial_id,
        method=spec.method,  # type: ignore[arg-type]
        model=spec.model,
        mission_prompt=_mission_prompt(spec),
        emergency=EmergencyContext(
            fault_label=f"plant-side {spec.fault_leg} actuator degradation",
            affected_channels=LEG_CHANNELS[spec.fault_leg],
            observed_values=(0.0, 0.0, 0.0),
            safe_action=_safe_action(spec.fault_leg),
        ),
        emergency_timeout_s=spec.deadline_s,
        emergency_max_attempts=int((spec.metadata or {}).get("emergency_max_attempts", 1)),
        mission_barrier_timeout_s=min(60.0, spec.deadline_s),
        cancel_grace_s=5.0,
        mission_temperature=0.0,
        emergency_temperature=0.0,
        seed=spec.seed,
        mission_num_predict=spec.mission_num_predict,
        emergency_num_predict=int((spec.metadata or {}).get("emergency_num_predict", 512)),
        on_fault=delayed_fault,
    )
    backend = OllamaBackend(spec.ollama_host)
    try:
        return await run_scheduler_trial(scheduler_spec, backend, logger)
    finally:
        await backend.aclose()


class _PhysicsEventAdapter:
    def __init__(self, logger: JsonlEventLogger, spec: TrialSpec) -> None:
        self.logger = logger
        self.identity = EventIdentity(
            batch_id=spec.batch_id,
            run_id=spec.trial_id,
            trial_id=spec.trial_id,
            request_id=None,
            request_type="physics",
            generation=0,
        )

    def __call__(self, event: dict[str, Any]) -> None:
        details = dict(event)
        stage = str(details.pop("event_type"))
        self.logger.emit(stage, self.identity, details)


def _failure_reason_physics(raw: dict[str, Any]) -> str | None:
    if raw.get("success"):
        return None
    if not raw.get("pre_fault_eligible"):
        return "invalid_pre_fault_state"
    if not raw.get("fault_injected"):
        return "fault_not_injected"
    if raw.get("fall_detected"):
        return "post_fault_fall"
    return "physical_safety_criterion_not_met"


def run_one(
    spec: TrialSpec,
    output_dir: Path,
    *,
    manifest_sha256: str = "unbound",
    attempt_id: str = "standalone",
) -> dict[str, Any]:
    started = time.perf_counter()
    started_utc = _utc_now()
    events_path = output_dir / "events.jsonl"
    if events_path.exists():
        raise FileExistsError(
            f"attempt event log already exists; use a fresh attempt directory: {events_path}"
        )
    logger = JsonlEventLogger(events_path)
    environment = {
        "captured_utc": _utc_now(),
        "python": sys.version,
        "packages": _package_versions(),
        "source_sha256": _source_digest(),
        "latency_clock": vars(time.get_clock_info("perf_counter")),
        "git": _git_state(),
    }

    if spec.study == "scheduler":
        environment["ollama"] = _ollama_snapshot(spec.ollama_host, spec.model)
        raw = asyncio.run(_run_scheduler(spec, logger))
        success = raw.get("status") == "success"
        event_time = (
            float(raw["fault_to_accept_ms"]) / 1000.0
            if raw.get("fault_to_accept_ms") is not None else None
        )
        failure_reason = None if success else str(raw.get("status") or "scheduler_failure")
    elif spec.study in {"physics", "integrated"}:
        if spec.study == "integrated":
            environment["ollama"] = _ollama_snapshot(spec.ollama_host, spec.model)
            raw = asyncio.run(run_integrated_trial(spec, logger, _PhysicsEventAdapter(logger, spec)))
        else:
            raw = run_physics_trial(spec, _PhysicsEventAdapter(logger, spec))
        trace = raw.pop("trace", [])
        if trace:
            trace_path = output_dir / "trace.jsonl"
            _atomic_jsonl(trace_path, trace)
        raw["trace_rows"] = len(trace)
        success = bool(raw.get("success"))
        # Physical safety is assessed over the complete fixed horizon.  It is
        # not a time-to-recovery event and must not enter latency comparisons.
        event_time = None
        failure_reason = _failure_reason_physics(raw)
    else:
        raise ValueError(f"Unsupported study: {spec.study!r}")

    artifacts = {"events": _audit_events(events_path, spec)}
    trace_path = output_dir / "trace.jsonl"
    if trace_path.is_file():
        artifacts["trace"] = _artifact_snapshot(trace_path)
    elapsed = time.perf_counter() - started
    return {
        **spec.to_dict(),
        "schema_version": 1,
        "trial_spec_sha256": trial_spec_sha256(spec),
        "manifest_sha256": manifest_sha256,
        "attempt_id": attempt_id,
        "status": "complete",
        "success": success,
        "event_time_s": event_time,
        "failure_reason": failure_reason,
        "started_utc": started_utc,
        "elapsed_s": elapsed,
        "completed_utc": _utc_now(),
        "environment": environment,
        "artifacts": artifacts,
        "result": raw,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--trial-id", required=True)
    destination = parser.add_mutually_exclusive_group(required=True)
    destination.add_argument("--output-dir", type=Path)
    destination.add_argument("--results-dir", type=Path)
    parser.add_argument("--attempt-id")
    args = parser.parse_args()

    manifest_digest = manifest_file_sha256(args.manifest)
    specs = load_manifest(args.manifest)
    matches = [spec for spec in specs if spec.trial_id == args.trial_id]
    if len(matches) != 1:
        raise ValueError(f"Expected one trial_id={args.trial_id!r}, found {len(matches)}")
    spec = matches[0]
    output_dir = (
        args.output_dir
        if args.output_dir is not None
        else args.results_dir / spec.trial_id
    )
    attempt_id = args.attempt_id or "standalone"
    output_dir.mkdir(parents=True, exist_ok=True)
    summary_path = output_dir / "summary.json"
    started_utc = _utc_now()
    started = time.perf_counter()
    try:
        summary = run_one(
            spec,
            output_dir,
            manifest_sha256=manifest_digest,
            attempt_id=attempt_id,
        )
    except Exception as exc:
        summary = {
            **spec.to_dict(),
            "schema_version": 1,
            "trial_spec_sha256": trial_spec_sha256(spec),
            "manifest_sha256": manifest_digest,
            "attempt_id": attempt_id,
            "status": "error",
            "success": False,
            "event_time_s": None,
            "failure_reason": f"{type(exc).__name__}: {exc}",
            "started_utc": started_utc,
            "elapsed_s": time.perf_counter() - started,
            "completed_utc": _utc_now(),
            "artifacts": _best_effort_artifacts(output_dir),
        }
        traceback.print_exc()
        _atomic_json(summary_path, summary)
        return 1
    _atomic_json(summary_path, summary)
    print(json.dumps({
        "trial_id": spec.trial_id,
        "status": summary["status"],
        "success": summary["success"],
        "event_time_s": summary["event_time_s"],
        "elapsed_s": summary["elapsed_s"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
