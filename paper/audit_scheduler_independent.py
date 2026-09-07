"""Independent standard-library audit of saved scheduler trials; no experiment imports."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
import math
from pathlib import Path
import random
from statistics import mean, median


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def distribution(values):
    return {"n": len(values), "mean": mean(values), "min": min(values), "max": max(values)} if values else {"n": 0}


def canonical(leg):
    targets = [number for name in ("FL", "FR", "BR", "BL")
               for number in ((0.0, 0.0, -0.4) if name == leg else (0.0, -0.08, -0.48))]
    return {"type": "one-shot", "skill_name": "runtime_safe_stabilization",
            "actions": [{"actuator_targets": targets, "duration_steps": 1}],
            "stop_condition": "duration_steps_elapsed", "max_cycles": None}


def quantile(values, p):
    ordered = sorted(values)
    index = p * (len(ordered) - 1)
    low, high = math.floor(index), math.ceil(index)
    return ordered[low] * (high - index) + ordered[high] * (index - low) if high != low else ordered[low]


def independent_bootstrap(batch_differences):
    """Balanced four-leg clusters: resample eight batch means, not 32 trial rows."""
    sizes = {len(values) for values in batch_differences.values()}
    if len(sizes) != 1:
        raise ValueError("Independent equal-cluster implementation requires balanced batches")
    ordered = sorted(batch_differences)
    means = [mean(batch_differences[key]) for key in ordered]
    if len(means) < 2:
        return [None, None]
    rng = random.Random(20260907)
    draws = [mean(means[rng.randrange(len(means))] for _ in means) for _ in range(5000)]
    return [quantile(draws, 0.025), quantile(draws, 0.975)]


def audit_trial(campaign, spec, manifest_sha):
    path = campaign / "trials" / spec["trial_id"] / "summary.json"
    row = read(path)
    raw = row["result"]
    errors = []

    def check(condition, message):
        if not condition:
            errors.append(message)

    check(all(row.get(key) == value for key, value in spec.items()), "Manifest fields differ")
    check(row["manifest_sha256"] == manifest_sha, "Manifest binding differs")
    attempt = (path.parent / row["attempt_relative_dir"]).resolve()
    check(attempt.is_relative_to(path.parent.resolve() / "attempts"), "Attempt escapes trial")
    check(digest(attempt / "summary.json") == row["worker_summary_sha256"], "Worker summary hash differs")
    for name, artifact in row["artifacts"].items():
        check(digest(attempt / artifact["file"]) == artifact["sha256"], name + " artifact hash differs")
    event_path = attempt / row["artifacts"]["events"]["file"]
    events = [json.loads(line) for line in event_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    check([e["sequence"] for e in events] == list(range(1, len(events) + 1)), "Noncontiguous event sequence")
    check(len({e["event_id"] for e in events}) == len(events), "Duplicate event ID")
    check(all(a["monotonic_ns"] <= b["monotonic_ns"] for a, b in zip(events, events[1:])), "Nonmonotonic events")
    check(all(e["trial_id"] == spec["trial_id"] and e["batch_id"] == spec["batch_id"] for e in events), "Event trial/batch identity differs")
    check(row["status"] == "complete" and row["success"] is True and raw["status"] == "success", "Trial not successful")
    expected = canonical(spec["fault_leg"])
    check(raw["emergency_preset"] == expected, "Accepted command differs from independently constructed full canonical command")
    check(raw["runtime_canonical_command"] == expected, "Runtime command differs from independent command")
    action = raw["emergency_preset"]["actions"][0]
    check(type(action["duration_steps"]) is int and action["duration_steps"] == 1, "Noninteger or noncanonical duration")
    check(all(type(x) in (int, float) and math.isfinite(x) for x in action["actuator_targets"]), "Nonnumeric/nonfinite targets")
    requests = raw["requests"]
    missions = [r for r in requests if r["request_type"] == "mission"]
    emergencies = [r for r in requests if r["request_type"] == "emergency"]
    check(len(missions) == 1 and len(emergencies) == 1 and len(requests) == 2, "Request/retry count differs from primary one-attempt protocol")
    mission, emergency = missions[0], emergencies[0]
    check(mission["request_id"] == raw["mission_request_id"], "Mission ID differs")
    check(emergency["request_id"] == raw["accepted_emergency_request_id"], "Accepted request ID differs")
    check(mission["generation"] == 0 and emergency["generation"] == raw["generation"] == 1, "Generation differs")
    check(all(r["attempt"] == 1 for r in requests), "Unexpected backend retry")
    check(emergency["status"] == "accepted", "Emergency status is not accepted")
    request_map = {r["request_id"]: r for r in requests}
    check(len(request_map) == 2, "Duplicate request IDs")
    for event in events:
        if event["request_id"] is not None:
            request = request_map.get(event["request_id"])
            check(request is not None and event["request_type"] == request["request_type"] and event["generation"] == request["generation"], "Event request identity/generation differs")

    def one(stage, request_type=None):
        matching = [e for e in events if e["stage"] == stage and (request_type is None or e["request_type"] == request_type)]
        check(len(matching) == 1, "Expected one " + str(request_type) + ":" + stage)
        return matching[0]

    first = one("first_chunk", "mission")
    barrier = one("mission_barrier_passed")
    ready = one("mission_ready")
    fault = one("fault_triggered")
    accepted = one("response_accepted")
    queued = one("request_queued", "emergency")
    dispatch = one("request_dispatched", "emergency")
    close = one("connection_closed", "mission")
    complete = one("response_complete", "emergency")
    check(first["sequence"] < barrier["sequence"] < ready["sequence"] < fault["sequence"], "Valid frame/barrier/ready/fault order violated")
    check(raw["mission_active_at_fault"] is True and fault["details"]["mission_active_at_fault"] is True and fault["details"]["require_active_mission_at_fault"] is True, "Active mission condition absent")
    check(mission["first_chunk_ns"] <= raw["fault_monotonic_ns"] < mission["completed_ns"], "Measured mission activity does not straddle fault")
    check(fault["monotonic_ns"] == raw["fault_monotonic_ns"], "Fault raw/event timestamp differs")
    check(ready["monotonic_ns"] == raw["mission_ready_monotonic_ns"], "Ready raw/event timestamp differs")
    check(complete["sequence"] < accepted["sequence"] and accepted["request_id"] == emergency["request_id"], "Accept before complete or mismatched identity")
    check(accepted["monotonic_ns"] == emergency["completed_ns"], "Accepted timestamp differs")
    check(events[-1]["stage"] == "trial_completed", "Terminal event not last")
    latency = (accepted["monotonic_ns"] - raw["fault_monotonic_ns"]) / 1e9
    check(abs(latency - row["event_time_s"]) < 1e-12 and abs(latency * 1000 - raw["fault_to_accept_ms"]) < 1e-8, "Raw latency differs")
    check(raw["fault_deadline_monotonic_ns"] == raw["fault_monotonic_ns"] + round(spec["deadline_s"] * 1e9) and 0 <= latency <= spec["deadline_s"], "Deadline origin/acceptance invalid")
    cancels = [e for e in events if e["stage"] == "cancel_requested"]
    method = spec["method"]
    if method == "hera_preempt":
        check(len(cancels) == 1 and cancels[0]["details"]["reason"] == "hera_fault_preemption", "HERA preemption reason/count differs")
        check(fault["sequence"] < cancels[0]["sequence"] < close["sequence"] < queued["sequence"] < dispatch["sequence"], "HERA fault/cancel/close/queue/dispatch order violated")
        check(mission["status"] == "cancelled" and close["details"]["cancelled"] is True, "HERA mission was not cancelled")
    elif method == "fifo_single":
        check(not cancels and mission["status"] == "complete", "FIFO cancelled mission")
        check(fault["sequence"] < queued["sequence"] < close["sequence"] < one("response_complete", "mission")["sequence"] < dispatch["sequence"], "FIFO queue/mission completion/dispatch order violated")
        check(mission["backend_metadata"].get("eval_count") == spec["mission_num_predict"], "FIFO did not reach stated mission token cap")
    elif method == "reserved_slot":
        check(len(cancels) == 1 and cancels[0]["details"]["reason"] == "trial_cleanup", "Parallel-slot cleanup reason/count differs")
        check(fault["sequence"] < dispatch["sequence"] < accepted["sequence"] < cancels[0]["sequence"] < close["sequence"], "Parallel overlap/acceptance/cleanup order violated")
        check(mission["status"] == "cancelled" and close["details"]["cancelled"] is True, "Parallel-slot cleanup did not cancel")
    check(complete["details"]["backend_metadata"] == emergency["backend_metadata"], "Backend metadata event/summary differs")
    check(emergency["backend_metadata"].get("eval_count") == 88, "Emergency output tokens differ from 88")
    check(emergency["backend_metadata"].get("prompt_eval_count") == 283, "Emergency prompt tokens differ from 283")
    check(not raw["errors"], "Result contains errors")
    return {
        "trial_id": spec["trial_id"], "batch_id": spec["batch_id"], "block_id": spec["block_id"],
        "fault_leg": spec["fault_leg"], "seed": spec["seed"], "method": method,
        "mission_num_predict": spec["mission_num_predict"], "order_index": spec["order_index"],
        "latency_s": latency, "mission_status": mission["status"], "attempt_id": row["adopted_attempt_id"],
        "cancel_reasons": [e["details"]["reason"] for e in cancels],
        "emergency_requests": len(emergencies), "emergency_tokens": emergency["backend_metadata"]["eval_count"],
        "mission_eval_count": mission["backend_metadata"].get("eval_count"), "mission_response_chars": mission["response_chars"],
        "ready_to_fault_ms": (raw["fault_monotonic_ns"] - ready["monotonic_ns"]) / 1e6,
        "nominal_fault_offset_ms": spec["fault_offset_s"] * 1000,
        "fault_to_dispatch_ms": raw["fault_to_dispatch_ms"], "queue_ms": raw["emergency_queue_ms"],
        "fault_to_first_chunk_ms": raw["fault_to_first_chunk_ms"],
        "first_chunk_to_accept_ms": raw["fault_to_accept_ms"] - raw["fault_to_first_chunk_ms"],
        "cancel_to_connection_close_ms": (close["monotonic_ns"] - cancels[0]["monotonic_ns"]) / 1e6 if cancels else None,
        "events_path": str(event_path), "events_sha256": digest(event_path),
        "stage_lines": {"fault": fault["sequence"], "mission_close": close["sequence"], "emergency_dispatch": dispatch["sequence"], "accept": accepted["sequence"]},
        "valid": not errors, "errors": errors,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-dir", type=Path, required=True)
    parser.add_argument("--scheduler-report", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    campaign, output = args.campaign_dir.resolve(), args.output_dir.resolve()
    if output.is_relative_to(campaign):
        parser.error("Output must be outside campaign")
    definition = read(campaign / "campaign.json")
    saved = read(args.scheduler_report)
    rows, errors = [], []
    for entry in definition["manifests"]:
        path = campaign / entry["snapshot_path"]
        specs = [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
        if specs[0]["study"] != "scheduler":
            continue
        if digest(path) != entry["snapshot_sha256"]:
            errors.append({"manifest": str(path), "error": "Manifest SHA differs"})
        for spec in specs:
            audited = audit_trial(campaign, spec, entry["snapshot_sha256"])
            rows.append(audited)
            errors.extend({"trial_id": spec["trial_id"], "error": error} for error in audited["errors"])
    contrasts, workloads = {}, {}
    for tokens in sorted({row["mission_num_predict"] for row in rows}):
        selected = [row for row in rows if row["mission_num_predict"] == tokens]
        pairs = defaultdict(dict)
        for row in selected:
            key = row["block_id"], row["fault_leg"], row["seed"]
            if row["method"] in pairs[key]:
                errors.append({"trial_id": row["trial_id"], "error": "Duplicate pairing key/method"})
            pairs[key][row["method"]] = row
        workloads[str(tokens)] = {}
        for method in ("hera_preempt", "fifo_single", "reserved_slot"):
            chosen = [row for row in selected if row["method"] == method]
            summary = {field: distribution([row[field] for row in chosen if row[field] is not None])
                       for field in ("latency_s", "ready_to_fault_ms", "fault_to_dispatch_ms", "queue_ms", "fault_to_first_chunk_ms", "first_chunk_to_accept_ms", "cancel_to_connection_close_ms", "mission_response_chars")}
            summary.update({"n": len(chosen), "mission_status_counts": dict(Counter(row["mission_status"] for row in chosen)),
                            "cancel_reasons": dict(Counter(reason for row in chosen for reason in row["cancel_reasons"])),
                            "emergency_tokens": dict(Counter(row["emergency_tokens"] for row in chosen)),
                            "mission_eval_count": dict(Counter(str(row["mission_eval_count"]) for row in chosen)),
                            "attempt_ids": dict(Counter(row["attempt_id"] for row in chosen)),
                            "leg_mean_latency_s": {leg: mean(row["latency_s"] for row in chosen if row["fault_leg"] == leg) for leg in ("FL", "FR", "BR", "BL")},
                            "batch_mean_latency_s": {batch: mean(row["latency_s"] for row in chosen if row["batch_id"] == batch) for batch in sorted({row["batch_id"] for row in chosen})}})
            workloads[str(tokens)][method] = summary
            expected = saved["workloads"][str(tokens)][method]["deadline_penalized_mean_s"]
            if abs(summary["latency_s"]["mean"] - expected) > 1e-12:
                errors.append({"tokens": tokens, "method": method, "error": "Method mean differs from paper report"})
        contrasts[str(tokens)] = {}
        for left, right in (("hera_preempt", "fifo_single"), ("hera_preempt", "reserved_slot"), ("reserved_slot", "fifo_single")):
            label = left + "_minus_" + right
            by_batch = defaultdict(list)
            for key, paired in pairs.items():
                if set(paired) != {"hera_preempt", "fifo_single", "reserved_slot"}:
                    errors.append({"pair": str(key), "error": "Incomplete paired methods"})
                by_batch[paired[left]["batch_id"]].append(paired[left]["latency_s"] - paired[right]["latency_s"])
            differences = [value for values in by_batch.values() for value in values]
            result = {"n_pairs": len(differences), "n_batches": len(by_batch), "pairs_per_batch": {k: len(v) for k, v in by_batch.items()},
                      "mean_difference_s": mean(differences), "median_difference_s": median(differences), "bootstrap_95_ci_s": independent_bootstrap(by_batch)}
            expected = saved["paired_contrasts"][str(tokens)][label]["metrics"]["deadline_penalized_latency_s"]
            result["matches_saved_report"] = abs(result["mean_difference_s"] - expected["mean_difference"]) < 1e-12 and all(abs(a-b) < 1e-12 for a,b in zip(result["bootstrap_95_ci_s"], expected["batch_cluster_bootstrap_95_ci"])) and result["n_pairs"] == expected["n_pairs"]
            if not result["matches_saved_report"]:
                errors.append({"tokens": tokens, "contrast": label, "error": "Independent paired/bootstrap recomputation differs"})
            contrasts[str(tokens)][label] = result
    result = {"campaign": str(campaign), "script_sha256": digest(Path(__file__)), "n_trials": len(rows),
              "valid": not errors, "errors": errors, "workloads": workloads, "contrasts": contrasts, "trials": rows,
              "bootstrap_audit": "Each workload has eight batches and four paired legs per batch. Independently resampled balanced batch means using 5000 draws, seed 20260907, percentile interpolation. Same cluster draw sequence, independently aggregated values; negative latency difference favors left. No multiplicity correction or population-general confidence interpretation.",
              "scope": "Independent canonical construction and event verification use no experiment source imports. Scheduler acceptance is not plant application. Cancelled streams lack final backend eval_count; response chars are not tokens. Client connection close is not server/GPU idle attestation."}
    output.mkdir(parents=True, exist_ok=True)
    (output / "scheduler_independent_audit.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    offsets = [row["ready_to_fault_ms"] for row in rows]
    lines = ["# Independent scheduler audit", "",
             f"Manifested trials: {len(rows)}; audit errors: {len(errors)}. No experiment implementation was imported.", "",
             "The audit checks full independently constructed canonical commands, worker/events hashes, event sequence and request generation, active-mission barrier, absolute deadline origin, raw acceptance timing, policy-specific cancellation/dispatch order, and reported output-token controls.", "",
             "| Token cap | Method | Mean acceptance (s) | Queue (ms) | First frame (ms from fault) | First frame to acceptance (ms) |",
             "|---:|---|---:|---:|---:|---:|"]
    for tokens, methods in workloads.items():
        for method, values in methods.items():
            lines.append(f"| {tokens} | {method} | {values['latency_s']['mean']:.6f} | {values['queue_ms']['mean']:.3f} | {values['fault_to_first_chunk_ms']['mean']:.3f} | {values['first_chunk_to_accept_ms']['mean']:.3f} |")
    lines += ["", "## Timing qualification", "",
              f"Nominal ready-to-fault delay is 250 ms. The measured range is {min(offsets):.4f}–{max(offsets):.4f} ms, mean {mean(offsets):.4f} ms; {sum(value < 250 for value in offsets)}/{len(offsets)} fall below 250 ms. Report the nominal setting and actual range rather than claiming an exact offset. Acceptance latency uses the actual logged fault timestamp, and all audited missions remain active at that timestamp.", "",
              "## Statistical and scope qualifications", "", result["bootstrap_audit"], "", result["scope"], "",
              "The token-cap workloads were collected in separate sequential workload blocks, so between-workload differences can also include time-dependent host/backend drift. Within each workload the method contrasts are paired by batch, leg, and seed. A batch bootstrap accounts for within-batch clustering, not arbitrary cross-batch temporal dependence.", "",
              "The canonical command is supplied by the runtime and the accepted payload is audited from persisted artifacts. This is a formatting/scheduling benchmark, not evidence that the model discovered a safe controller. All observed acceptances are before 30 s; that does not prove a population success probability of one. Mission throughput, wasted generation after cancellation, GPU resource isolation, and server-side quiescence are not established by these client events."]
    if errors:
        lines += ["", "## Errors", "", "```json", json.dumps(errors, indent=2), "```"]
    (output / "scheduler_independent_audit.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"n_trials": len(rows), "errors": len(errors), "output": str(output)}))
    return 2 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
