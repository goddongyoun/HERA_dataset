"""Independent manifest/event/trace audit of finite supervisory command delivery.

Only paper outputs are written. No experiment package is imported: saved runs
remain auditable after the live experiment implementation changes. A fixed
horizon truncating a valid command is reported, never removed or called an
infrastructure failure.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from statistics import mean, median


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()


def summarize(values):
    values = [float(value) for value in values if value is not None]
    return {
        "n": len(values), "mean": mean(values) if values else None,
        "median": median(values) if values else None,
        "minimum": min(values) if values else None,
        "maximum": max(values) if values else None,
    }


def audit_trial(spec, row, trial_dir, source_sha256, manifest_sha256):
    errors = []

    def check(condition, reason):
        if not condition:
            errors.append(reason)

    trial_id = spec["trial_id"]
    check(row.get("status") == "complete", "worker summary is not complete")
    check(all(row.get(key) == value for key, value in spec.items()), "summary differs from manifest fields")
    check(row.get("trial_spec_sha256") == canonical_digest(spec), "TrialSpec hash mismatch")
    check(row.get("manifest_sha256") == manifest_sha256, "manifest hash mismatch")
    check(row.get("environment", {}).get("source_sha256") == source_sha256, "source provenance mismatch")
    attempt = (trial_dir / row["attempt_relative_dir"]).resolve()
    if not attempt.is_relative_to(trial_dir.resolve()):
        raise ValueError(f"Attempt escapes trial directory: {trial_id}")
    check(row.get("worker_summary_sha256") == digest(attempt / "summary.json"), "worker summary digest mismatch")
    check(row.get("adopted_attempt_id") == row.get("attempt_id") == attempt.name, "adopted attempt mismatch")
    for name, filename in (("events", "events.jsonl"), ("trace", "trace.jsonl")):
        artifact = row.get("artifacts", {}).get(name, {})
        check(artifact.get("file") == filename, f"unexpected {name} artifact filename")
        check(artifact.get("sha256") == digest(attempt / filename), f"{name} digest mismatch")
    events = read_jsonl(attempt / "events.jsonl")
    trace = read_jsonl(attempt / "trace.jsonl")
    check(bool(trace), "empty physical trace")
    check(all(event["trial_id"] == trial_id for event in events), "event identity mismatch")
    check([event["sequence"] for event in events] == list(range(1, len(events) + 1)), "event sequence is not contiguous")
    check([event["monotonic_ns"] for event in events] == sorted(event["monotonic_ns"] for event in events), "event clock order violation")
    check([sample["step"] for sample in trace] == list(range(len(trace))), "trace step sequence is not contiguous")
    check(events[0]["stage"] == "integrated_trial_started" and events[-1]["stage"] == "integrated_trial_finished", "integrated lifecycle boundary mismatch")
    raw = row["result"]
    scheduler = raw.get("scheduler") or {}
    application = raw["supervisory_application"]
    accepted_ns = application["accepted_monotonic_ns"]
    accepted = accepted_ns is not None
    check(accepted == bool(application["accepted"]), "acceptance flag/timestamp mismatch")
    accepted_events = [event for event in events if event["stage"] == "response_accepted"]
    check(len(accepted_events) == int(accepted), "acceptance event count mismatch")
    if accepted and accepted_events:
        check(accepted_events[0]["monotonic_ns"] == accepted_ns, "acceptance event timestamp mismatch")
    applied = [sample for sample in trace if sample["action_source"] == "validated_supervisor"]
    check(bool(applied) == bool(application["applied"]), "application flag/trace mismatch")
    check(len(applied) == application["applied_steps"], "application step count mismatch")
    check(not applied or accepted, "unaccepted command applied")
    check(not applied or all(sample["control_started_ns"] >= accepted_ns for sample in applied), "command applied before acceptance")
    requested_steps = None
    expected_targets = None
    if accepted:
        command = application["command"]
        check(command == scheduler.get("runtime_canonical_command"), "accepted command differs from runtime canonical contract")
        check(command == scheduler.get("emergency_preset"), "mailbox command differs from scheduler accepted payload")
        check(set(command) == {"type", "skill_name", "actions", "stop_condition", "max_cycles"}, "unexpected command fields")
        check(command.get("type") == "one-shot" and command.get("max_cycles") is None, "invalid one-shot/cycle semantics")
        check(command.get("skill_name") == "runtime_safe_stabilization", "unexpected runtime skill name")
        check(command.get("stop_condition") == "duration_steps_elapsed", "invalid finite stopping rule")
        check(len(command["actions"]) == 1, "command must have one phase")
        phase = command["actions"][0]
        check(set(phase) == {"actuator_targets", "duration_steps"}, "unexpected action fields")
        requested_steps = phase["duration_steps"]
        check(type(requested_steps) is int and requested_steps > 0, "invalid command duration")
        selected_leg = raw["detector_selected_leg"]
        expected_targets = [value for leg in ("FL", "FR", "BR", "BL")
                            for value in ((0., 0., -.4) if leg == selected_leg else (0., -.08, -.48))]
        check(phase["actuator_targets"] == expected_targets, "canonical command differs from trusted local support")
        check(all(sample["commanded_action"] == expected_targets for sample in applied), "actual command differs from accepted targets")
        check(len(applied) <= requested_steps, "command exceeded prescribed duration")
        check(not applied or [sample["step"] for sample in applied] == list(range(applied[0]["step"], applied[0]["step"] + len(applied))), "supervisor action is not a contiguous finite phase")
    first_applied = applied[0] if applied else None
    last_applied = applied[-1] if applied else None
    full_completion = len(applied) == requested_steps if accepted else None
    following = next((sample for sample in trace if last_applied and sample["step"] > last_applied["step"]), None)
    expiry_observed = bool(full_completion and following and following["action_source"] == "local_reflex") if accepted else None
    reaches_horizon = bool(last_applied and last_applied["step"] == trace[-1]["step"])
    horizon_censored = bool(accepted and not full_completion and (reaches_horizon or not applied))
    if applied and not full_completion:
        check(reaches_horizon, "command ended early before fixed observation horizon")
    if full_completion and following:
        check(following["action_source"] == "local_reflex", "finite command expiry did not return to local controller")
    applied_ns = first_applied["control_started_ns"] if first_applied else None
    check(applied_ns == application["applied_monotonic_ns"], "application timestamp does not match trace control-start")
    detect_ns = raw.get("detect_monotonic_ns")
    scheduler_detect_ns = scheduler.get("fault_monotonic_ns")
    if scheduler_detect_ns is not None:
        check(scheduler_detect_ns == detect_ns, "scheduler deadline origin differs from measured detector timestamp")
    detect_to_accept = (accepted_ns - scheduler_detect_ns) / 1e6 if accepted and scheduler_detect_ns is not None else None
    if detect_to_accept is not None:
        check(math.isclose(detect_to_accept, scheduler["fault_to_accept_ms"], abs_tol=1e-6), "fault-to-accept latency mismatch")
        check(accepted_ns <= scheduler["fault_deadline_monotonic_ns"], "accepted command exceeded absolute deadline")
    accept_to_apply = (applied_ns - accepted_ns) / 1e6 if applied_ns is not None else None
    detect_to_apply = (applied_ns - detect_ns) / 1e6 if applied_ns is not None and detect_ns is not None else None
    for name, value in (("accept_to_apply_ms", accept_to_apply), ("detect_to_apply_ms", detect_to_apply)):
        recorded = application.get(name)
        check(value == recorded or (value is not None and recorded is not None and math.isclose(value, recorded, abs_tol=1e-6)), f"{name} mismatch")
    local_rows = [sample for sample in trace if sample["action_source"] == "local_reflex"]
    local_first = local_rows[0] if local_rows else None
    local_delay = (local_first["control_started_ns"] - detect_ns) / 1e6 if local_first and detect_ns is not None else None
    if local_delay is not None and raw.get("reflex_dispatch_latency_ms") is not None:
        check(math.isclose(local_delay, raw["reflex_dispatch_latency_ms"], abs_tol=1e-6), "local reflex timestamp/trace mismatch")
    requests = scheduler.get("requests", [])
    missions = [request for request in requests if request["request_type"] == "mission"]
    emergencies = [request for request in requests if request["request_type"] == "emergency"]
    if accepted:
        request_id = scheduler.get("accepted_emergency_request_id")
        accepted_requests = [request for request in emergencies if request["request_id"] == request_id]
        check(len(accepted_requests) == 1, "accepted request identity is not unique")
        if accepted_requests:
            check(accepted_requests[0]["generation"] == scheduler["generation"], "accepted generation mismatch")
            check(accepted_requests[0]["completed_ns"] == accepted_ns, "request acceptance timestamp mismatch")
    failure_injected = spec["method"] == "backend_failure"
    if failure_injected:
        check(not accepted and not applied, "injected failing transport accepted/applied a command")
        check(scheduler.get("status") == "emergency_failed", "injected failure did not reach emergency failure endpoint")
        check(any("preregistered injected emergency transport failure" in error for error in scheduler.get("errors", [])), "missing actual injected transport failure evidence")
    if spec["method"] == "local_only":
        check(not scheduler and not accepted and not applied, "local-only condition used supervisor")
    return {
        "trial_id": trial_id, "method": spec["method"], "model": spec["model"],
        "batch_id": spec["batch_id"], "fault_leg": spec["fault_leg"],
        "mission_num_predict": spec["mission_num_predict"], "duration_s": spec["duration_s"],
        "summary": str(trial_dir / "summary.json"), "events": str(attempt / "events.jsonl"), "trace": str(attempt / "trace.jsonl"),
        "accepted": accepted, "applied": bool(applied), "requested_steps": requested_steps,
        "observed_steps": len(applied), "full_command_completion": full_completion,
        "horizon_censored": horizon_censored, "expiry_observed": expiry_observed,
        "expiry_horizon_censored": bool(accepted and not expiry_observed and (reaches_horizon or not applied)),
        "first_applied_step": first_applied["step"] if first_applied else None,
        "last_applied_step": last_applied["step"] if last_applied else None,
        "final_trace_step": trace[-1]["step"], "trace_rows": len(trace),
        "accepted_monotonic_ns": accepted_ns, "applied_monotonic_ns": applied_ns,
        "detect_to_accept_ms": detect_to_accept, "accept_to_apply_ms": accept_to_apply,
        "detect_to_apply_ms": detect_to_apply,
        "local_detect_to_apply_ms": local_delay, "local_action_steps": len(local_rows),
        "local_steps_before_acceptance": sum(accepted_ns is None or sample["control_started_ns"] < accepted_ns for sample in local_rows),
        "physical_safe": raw.get("physical_safe"), "fall_detected": raw.get("fall_detected"),
        "mission_active_at_detection": scheduler.get("mission_active_at_fault"),
        "mission_status": missions[0]["status"] if missions else None,
        "mission_eval_count": missions[0].get("backend_metadata", {}).get("eval_count") if missions else None,
        "scheduler_status": scheduler.get("status"), "scheduler_error": raw.get("scheduler_error"),
        "injected_backend_failure": failure_injected,
        "max_pacing_lateness_ms": raw.get("max_pacing_lateness_ms"),
        "maximum_hook_duration_ms": raw.get("maximum_hook_duration_ms"),
        "audit_errors": errors,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--campaign-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    campaign = args.campaign_dir.resolve()
    output = args.output_dir.resolve()
    if output.is_relative_to(campaign):
        parser.error("Audit output must be outside the immutable campaign directory")
    definition = read_json(campaign / "campaign.json")
    provenance = read_json(campaign / "provenance.json")
    manifests = []
    audits = []
    for entry in definition["manifests"]:
        path = campaign / entry["snapshot_path"]
        specs = read_jsonl(path)
        if not specs or specs[0]["study"] != "integrated":
            continue
        if digest(path) != entry["snapshot_sha256"]:
            raise ValueError(f"Frozen manifest digest mismatch: {path}")
        manifests.append({"path": str(path), "sha256": entry["snapshot_sha256"], "n_expected": len(specs)})
        for spec in specs:
            trial_dir = campaign / "trials" / spec["trial_id"]
            row = read_json(trial_dir / "summary.json")
            audits.append(audit_trial(spec, row, trial_dir, provenance["source_sha256"], entry["snapshot_sha256"]))
    if not audits:
        raise ValueError("Campaign contains no integrated trials")
    if len({row["trial_id"] for row in audits}) != len(audits):
        raise ValueError("Duplicate integrated trial IDs")
    grouped = defaultdict(list)
    for row in audits:
        grouped[(row["model"], row["method"], row["mission_num_predict"], row["duration_s"])].append(row)
    summaries = []
    for (model, method, tokens, horizon), rows in sorted(grouped.items()):
        summaries.append({
            "model": model, "method": method, "mission_num_predict": tokens, "duration_s": horizon,
            "n_trials": len(rows), "n_accepted": sum(row["accepted"] for row in rows),
            "n_applied": sum(row["applied"] for row in rows),
            "n_full_command_completion": sum(row["full_command_completion"] is True for row in rows),
            "n_horizon_censored": sum(row["horizon_censored"] for row in rows),
            "n_expiry_observed": sum(row["expiry_observed"] is True for row in rows),
            "n_expiry_horizon_censored": sum(row["expiry_horizon_censored"] for row in rows),
            "n_physical_safe": sum(row["physical_safe"] is True for row in rows),
            "mission_activity_counts": dict(Counter(str(row["mission_active_at_detection"]) for row in rows)),
            "mission_status_counts": dict(Counter(str(row["mission_status"]) for row in rows)),
            **{field: summarize(row[field] for row in rows) for field in (
                "detect_to_accept_ms", "accept_to_apply_ms", "detect_to_apply_ms",
                "local_detect_to_apply_ms", "maximum_hook_duration_ms", "max_pacing_lateness_ms",
            )},
        })
    errors = [{"trial_id": row["trial_id"], "error": error} for row in audits for error in row["audit_errors"]]
    report = {
        "campaign": str(campaign), "script_sha256": digest(Path(__file__)),
        "audited_utc": datetime.now(timezone.utc).isoformat(),
        "source_sha256": provenance["source_sha256"], "manifest_provenance": manifests,
        "n_expected": sum(entry["n_expected"] for entry in manifests), "n_audited": len(audits),
        "audit_errors": errors, "group_summaries": summaries, "trials": audits,
        "definitions": {
            "accepted": "validated canonical command recorded by scheduler response_accepted",
            "applied": "at least one trace control step uses the exact accepted command after acceptance",
            "full_command_completion": "observed supervisor steps equal requested duration; null without acceptance",
            "horizon_censored": "accepted command duration not fully observed before fixed horizon; not an infrastructure failure",
            "expiry_observed": "full duration followed by a recorded local_reflex control step; null without acceptance",
            "expiry_horizon_censored": "observation ends before the expected subsequent local fallback can be observed",
            "local_detect_to_apply_ms": "first trace local_reflex control_started_ns minus measured detector timestamp",
        },
        "interpretation": "Runtime-given command reaffirms the same local support. Acceptance, application, complete duration, and observed expiry are distinct endpoints. No additional physical benefit or LLM-discovered control is inferred. Retain the original fixed horizon.",
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "integrated_audit.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    lines = ["# Integrated command lifecycle audit", "", f"Audited {len(audits)} manifested trials; integrity/timing errors: {len(errors)}.", "",
             "Acceptance is not application, and application is not completion of the prescribed duration. Fixed-horizon truncation is retained and is not an infrastructure failure.", "",
             "| Model | Method | N | Accepted | Applied | Full duration | Horizon truncated | Expiry observed | Physically safe |",
             "|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    for group in summaries:
        lines.append(f"| {group['model']} | {group['method']} | {group['n_trials']} | {group['n_accepted']} | {group['n_applied']} | {group['n_full_command_completion']} | {group['n_horizon_censored']} | {group['n_expiry_observed']} | {group['n_physical_safe']} |")
    lines += ["", "Without an accepted command, duration/expiry counts are not applicable rather than supervisor failures.", "",
              "| Method | Detect to accept mean (ms) | Accept to apply mean (ms) | Detect to apply mean (ms) | Local detect to apply mean (ms) |",
              "|---|---:|---:|---:|---:|"]
    def fmt(value):
        return "N/A" if value is None else f"{value:.3f}"
    for group in summaries:
        lines.append("| " + group["method"] + " | " + " | ".join(fmt(group[field]["mean"]) for field in ("detect_to_accept_ms", "accept_to_apply_ms", "detect_to_apply_ms", "local_detect_to_apply_ms")) + " |")
    lines += ["", "## Fixed-horizon cases", "", "| Trial | Requested / observed steps | Full duration | Expiry observed |", "|---|---:|---|---|"]
    for row in audits:
        if row["horizon_censored"] or row["expiry_horizon_censored"]:
            lines.append(f"| {row['trial_id']} | {row['requested_steps']} / {row['observed_steps']} | {row['full_command_completion']} | {row['expiry_observed']} |")
    lines += ["", report["interpretation"], "", "The JSON includes every manifested trial, exact artifact paths and hash checks, mission activity, timing ranges, and endpoint definitions. Inference precision is not estimated from this supplemental audit."]
    (output / "integrated_audit.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"n_trials": len(audits), "audit_errors": len(errors), "output": str(output)}))
    return 2 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
