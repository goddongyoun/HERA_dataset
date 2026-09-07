"""Independently audit manifested physics/integrated JSON and render figures.

No experiment modules are imported: physical outcomes are recomputed from raw
trace samples. This reporting script is outside the frozen experiment-source
set; its own hash and every inspected artifact are recorded in report.json.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import platform
import sys
from typing import Any

import numpy as np


LEGS = ("FL", "FR", "BR", "BL")
METHODS = ("local_only", "local_reflex", "no_reflex", "hera_preempt", "fifo_single", "reserved_slot", "backend_failure")
COLORS = {"local_only": "#0072B2", "local_reflex": "#0072B2", "no_reflex": "#D55E00",
          "hera_preempt": "#009E73", "fifo_single": "#E69F00", "reserved_slot": "#CC79A7", "backend_failure": "#666666"}
SHORT = {"local_only": "Local only", "local_reflex": "Local reflex", "no_reflex": "No reflex",
         "hera_preempt": "Preempt", "fifo_single": "FIFO", "reserved_slot": "Parallel-2", "backend_failure": "Backend failure"}
LIMITATION = (
    "The enumerated phase/leg grid is descriptive, not independent stochastic replication; "
    "no bootstrap interval or p-value is inferred here. Physical safety is a fixed-horizon "
    "criterion, not time to locomotion recovery. Integrated accepted commands reaffirm the "
    "same canonical support action as the local reflex; these runs establish bounded command "
    "application and failure containment, not extra physical benefit or LLM-discovered control. "
    "Velocity norm is not forward progress. Detector noise is synthetic and partial."
    " Detector-state replay starts from stored scalar scores; traces do not contain all "
    "raw per-actuator/per-joint sensor channels needed to independently reconstruct the "
    "weighted sensor-residual formula. That force-model equality is covered by separate "
    "dynamic simulator regression tests, not claimed as a full raw-trace sensor replay."
)


def digest_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]


def within(root: Path, relative: str) -> Path:
    if not isinstance(relative, str):
        raise ValueError("relative artifact path must be a string")
    target = (root / relative).resolve()
    if not target.is_relative_to(root.resolve()):
        raise ValueError(f"path escapes its artifact root: {relative!r}")
    return target


def stats(values: list[float]) -> dict[str, Any]:
    values = [float(x) for x in values if x is not None and math.isfinite(float(x))]
    if not values:
        return {"n": 0, "mean": None, "median": None, "minimum": None, "maximum": None, "q1": None, "q3": None}
    array = np.asarray(values)
    return {"n": len(values), "mean": float(np.mean(array)), "median": float(np.median(array)),
            "minimum": float(np.min(array)), "maximum": float(np.max(array)),
            "q1": float(np.quantile(array, .25)), "q3": float(np.quantile(array, .75))}


def compare(errors: list[str], label: str, actual: Any, expected: Any, tolerance: float = 1e-9) -> None:
    if expected is None or isinstance(expected, (bool, str, list, dict)):
        equal = actual == expected
    else:
        try:
            equal = math.isfinite(float(actual)) and math.isclose(float(actual), float(expected), abs_tol=tolerance, rel_tol=1e-9)
        except (TypeError, ValueError):
            equal = False
    if not equal:
        errors.append(f"{label}: stored={actual!r}, recomputed={expected!r}")


def recompute_physics(trace: list[dict[str, Any]], spec: dict[str, Any], reference: int, dt: float = .02) -> dict[str, Any]:
    """Independent finite-sample definitions, using noiseless trace fields only."""
    pre = trace[:reference]
    post = trace[reference:]
    eligible_window = pre[-int(spec["eligibility_window_steps"]):]
    terminal = post[-int(spec["terminal_window_steps"]):]
    if not post or not eligible_window:
        raise ValueError("trace has no pre-reference or post-reference observations")
    eligible = (
        len(pre) >= spec["minimum_pre_fault_samples"]
        and np.mean([t["torso_upright"] >= spec["eligibility_upright_threshold"] for t in eligible_window]) >= spec["eligibility_upright_fraction"]
        and max(t["imu_gyro_norm"] for t in eligible_window) <= spec["eligibility_gyro_threshold"]
        and min(t["torso_height"] for t in eligible_window) >= spec["eligibility_height_threshold"]
    )
    consecutive = 0
    first_fall = recognized_fall = None
    for t in post:
        bad = t["torso_upright"] < spec["fall_upright_threshold"] or t["torso_height"] < spec["fall_height_threshold"]
        consecutive = consecutive + 1 if bad else 0
        if consecutive >= spec["fall_debounce_steps"]:
            recognized_fall = t["step"]
            first_fall = recognized_fall - spec["fall_debounce_steps"] + 1
            break
    upright_fraction = float(np.mean([t["torso_upright"] >= spec["safe_upright_threshold"] for t in post]))
    terminal_safe = float(np.mean([t["torso_upright"] >= spec["safe_upright_threshold"] and t["torso_height"] >= spec["safe_height_threshold"] for t in terminal]))
    evaluable = bool(eligible and len(post) >= spec["terminal_window_steps"])
    safe = bool(evaluable and first_fall is None and upright_fraction >= spec["safe_upright_fraction"] and terminal_safe >= spec["safe_upright_fraction"])
    result = {
        "pre_fault_eligible": bool(eligible), "physical_safety_evaluable": evaluable,
        "physical_safe": safe, "success": safe, "fall_detected": first_fall is not None,
        "first_fall_step": first_fall, "fall_recognized_step": recognized_fall,
        "post_fault_min_upright": min(t["torso_upright"] for t in post),
        "post_fault_min_torso_height": min(t["torso_height"] for t in post),
        "post_fault_upright_fraction": upright_fraction, "terminal_safe_fraction": terminal_safe,
        "upright_deficit_integral": sum(max(0., 1.-t["torso_upright"]) * dt for t in post),
        "mean_speed_m_s": float(np.mean([t["torso_speed"] for t in post])),
        "terminal_mean_speed_m_s": float(np.mean([t["torso_speed"] for t in terminal])),
        "mean_gyro_rad_s": float(np.mean([t["imu_gyro_norm"] for t in post])),
        "terminal_mean_gyro_rad_s": float(np.mean([t["imu_gyro_norm"] for t in terminal])),
        "pre_mean_speed_m_s": float(np.mean([t["torso_speed"] for t in pre])),
        "pre_mean_gyro_rad_s": float(np.mean([t["imu_gyro_norm"] for t in pre])),
        "post_samples": len(post), "pre_samples": len(pre),
    }
    return result


def replay_detector(trace: list[dict[str, Any]], config: dict[str, Any], *, reference: int, fault_leg: str, enabled: bool) -> dict[str, Any]:
    """Independent threshold/debounce/hysteresis replay from saved scalar scores."""
    states = {leg: False for leg in LEGS}
    runs = {leg: 0 for leg in LEGS}
    active_by_step, triggers, clears = [], [], []
    for sample in trace:
        for leg in LEGS:
            score = float(sample["detector_scores"][leg])
            if not math.isfinite(score) or not 0 <= score <= 1:
                raise ValueError(f"invalid detector score at step {sample['step']} for {leg}")
            crossing = score <= config["exit_threshold"] if states[leg] else score >= config["enter_threshold"]
            runs[leg] = runs[leg]+1 if crossing else 0
            required = config["exit_steps"] if states[leg] else config["enter_steps"]
            if runs[leg] >= required:
                event = {"leg": leg, "step": sample["step"], "monotonic_ns": sample["monotonic_ns"]}
                (clears if states[leg] else triggers).append(event)
                states[leg] = not states[leg]
                runs[leg] = 0
        active_by_step.append([leg for leg in LEGS if states[leg]])
    correct = [event for event in triggers if enabled and event["leg"] == fault_leg and event["step"] >= reference]
    false = [event for event in triggers if not enabled or event["leg"] != fault_leg or event["step"] < reference]
    selected = triggers[0]["leg"] if triggers else None
    return {
        "active_by_step": active_by_step,
        "trigger_transitions": triggers,
        "clear_transitions": clears,
        "first_correct_detection": correct[0] if correct else None,
        "false_trigger_transitions": false,
        "detector_selected_leg": selected,
        "detected": bool(correct) if enabled else None,
        "detector_correct": bool(correct and selected == fault_leg and not false) if enabled else None,
        "detection_miss": not bool(correct) if enabled else None,
        "clear_validation": "Replayed exits checked against every saved active-leg state; separate clear events are not emitted by this source.",
        "scope": "Independent entry/debounce/exit/hysteresis replay from saved scores, not reconstruction of the full sensor-residual score formula.",
    }


def healthy_exposure(trace: list[dict[str, Any]], reference: int, false_steps: list[int], dt: float = .02) -> dict[str, Any]:
    """Simulated observation and time-to-first-trigger exposure, not a hazard model."""
    first = min(false_steps) if false_steps else None
    # Detection reads an end-of-integration sample, not the start of its step.
    at_risk_steps = first + 1 if first is not None else len(trace)
    local = [row for row in trace if row["action_source"] == "local_reflex"]
    return {
        "measured_duration_s": len(trace)*dt,
        "pre_reference_duration_s": reference*dt,
        "post_reference_duration_s": (len(trace)-reference)*dt,
        "first_false_trigger_step": first,
        "first_false_trigger_sim_s": (first+1)*dt if first is not None else None,
        "first_false_trigger_censored": first is None,
        "at_risk_duration_s": at_risk_steps*dt,
        "pre_reference_at_risk_duration_s": min(reference, at_risk_steps)*dt,
        "post_reference_at_risk_duration_s": max(0, at_risk_steps-reference)*dt,
        "pre_reference_false_trigger_events": sum(step < reference for step in false_steps),
        "post_reference_false_trigger_events": sum(step >= reference for step in false_steps),
        "false_local_response_applied": bool(local),
        "false_local_response_onset_step": local[0]["step"] if local else None,
        "false_local_response_onset_sim_s": local[0]["step"]*dt if local else None,
        "false_local_response_steps": len(local),
        "false_local_response_duration_s": len(local)*dt,
        "false_local_response_duty_fraction": len(local)/len(trace),
        "definition": "Healthy trials only. First-trigger at-risk time includes its end-of-step sample; censored at measured horizon. Full observation includes any post-response closed-loop trajectory. Local-response onset is start-of-control-step. No stationarity or independent hazard assumption.",
    }


def audit_trace(summary: dict[str, Any], trace: list[dict[str, Any]], events: list[dict[str, Any]]) -> tuple[dict[str, Any], list[str]]:
    raw, errors = summary["result"], []
    spec = raw["spec"]
    dt = .02
    expected_steps = int(math.ceil(float(spec["duration_s"]) / dt))
    reference = min(expected_steps-1, int(math.ceil(float(spec["fault_offset_s"]) / dt)))
    enabled = bool(spec.get("fault_enabled", True))
    for name in ("duration_s", "fault_offset_s", "seed", "fault_leg", "realtime"):
        compare(errors, "physical_spec."+name, spec.get(name), summary[name])
    compare(errors, "physical_spec.method", spec.get("method"), "local_reflex" if summary["study"] == "integrated" else summary["method"])
    def compare_overrides(actual, expected, prefix):
        for key, value in expected.items():
            if isinstance(value, dict):
                compare_overrides(actual.get(key, {}), value, prefix+key+".")
            else:
                compare(errors, prefix+key, actual.get(key), value)
    compare_overrides(spec, (summary.get("metadata") or {}).get("physics", {}), "physics_override.")
    scenario_digest = hashlib.sha256(f"hera-v2-physics|{summary['seed']}".encode("ascii")).digest()
    expected_yaw = spec.get("initial_yaw_rad")
    if expected_yaw is None:
        expected_yaw = (2*int.from_bytes(scenario_digest[:8], "big")/float(1 << 64)-1)*math.pi
    cycle = 2*int(spec["gait_phase_steps"])
    expected_phase = spec.get("gait_phase_offset_steps")
    if expected_phase is None:
        expected_phase = int.from_bytes(scenario_digest[8:16], "big") % cycle
    compare(errors, "paired_scenario.initial_yaw_rad", raw["initial_yaw_rad"], expected_yaw)
    compare(errors, "paired_scenario.gait_phase_offset_steps", raw["gait_phase_offset_steps"], expected_phase % cycle)
    reference_phase = (reference + expected_phase) % cycle
    if "injection_phase_steps" in raw:
        compare(errors, "paired_scenario.injection_phase_steps", raw["injection_phase_steps"], reference_phase)
    compare(errors, "trace.length", len(trace), expected_steps)
    compare(errors, "trace.step sequence", [t["step"] for t in trace], list(range(expected_steps)))
    compare(errors, "reference_step", raw.get("reference_step"), reference)
    compare(errors, "reset_count", raw.get("reset_count"), 1)
    compare(errors, "measured_steps", raw.get("measured_steps"), expected_steps)
    compare(errors, "trace_rows", raw.get("trace_rows"), expected_steps)
    for t in trace:
        step = t["step"]
        compare(errors, f"trace[{step}].sim_time_s", t["sim_time_s"], (step+1)*dt)
        compare(errors, f"trace[{step}].reference_reached", t.get("reference_reached"), step >= reference)
        compare(errors, f"trace[{step}].fault_active", t["fault_active"], enabled and step >= reference)
        if t["control_started_ns"] > t["monotonic_ns"]:
            errors.append(f"step {step}: control starts after its measurement")
        action = np.asarray(t["commanded_action"], dtype=float)
        lower, upper = np.tile([-1., -1., -.8], 4), np.tile([1., 1.1, .8], 4)
        if action.shape != (12,) or not np.all(np.isfinite(action)) or np.any(action < lower) or np.any(action > upper):
            errors.append(f"step {step}: invalid/nonfinite/out-of-range action")
    if any(a["monotonic_ns"] > b["monotonic_ns"] for a, b in zip(trace, trace[1:])):
        errors.append("trace measurement clock is not monotonic")
    computed = recompute_physics(trace, spec, reference, dt)
    for key, value in computed.items():
        if key in {"first_fall_step", "fall_recognized_step"}:
            compare(errors, f"physical_safety.{key}", raw["physical_safety"].get(key), value)
        elif key not in {"pre_mean_speed_m_s", "pre_mean_gyro_rad_s", "post_samples", "pre_samples"}:
            compare(errors, key, raw.get(key), value)
    compare(errors, "summary.success", summary["success"], computed["success"])
    compare(errors, "summary.event_time_s", summary.get("event_time_s"), None)
    compare(errors, "stability_latency_ms", raw.get("stability_latency_ms"), None)
    compare(errors, "physical_safety.post_fault_samples", raw["physical_safety"].get("post_fault_samples"), len(trace)-reference)
    compare(errors, "pre_fault_eligibility.sample_count", raw["pre_fault_eligibility"].get("sample_count"), reference)
    compare(errors, "fault_injected", raw.get("fault_injected"), enabled)
    compare(errors, "fault_active_until_trial_end", raw.get("fault_active_until_trial_end"), enabled)
    snapshot = raw.get("fault_parameter_snapshot")
    if enabled and raw.get("measurement_sync") == "mj_forward_post_integration" and snapshot is None:
        errors.append("synchronized plant fault is missing actual parameter snapshot")
    if snapshot is not None:
        compare(errors, "fault_parameter_snapshot.leg", snapshot["leg"], spec["fault_leg"])
        compare(errors, "fault_parameter_snapshot.strength_scale", snapshot["strength_scale"], spec["fault_strength"])
        compare(errors, "fault_parameter_snapshot.actuator_indices", snapshot["actuator_indices"], raw["actuator_layout"]["indices_by_leg"][spec["fault_leg"]])
        for kind in ("gainprm", "biasprm"):
            nominal = np.asarray(snapshot["nominal_"+kind], dtype=float)
            faulted = np.asarray(snapshot["faulted_"+kind], dtype=float)
            if nominal.shape != (3, 10) or nominal.shape != faulted.shape or not np.allclose(faulted, nominal*spec["fault_strength"], atol=1e-12, rtol=1e-12):
                errors.append("fault_parameter_snapshot."+kind+": actual plant values do not match strength scaling")
    if not enabled and snapshot is not None:
        errors.append("healthy control has a fault parameter snapshot")
    by_stage: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        by_stage[event["stage"]].append(event)
    reference_stage = "plant_fault_injected" if enabled else "healthy_reference_reached"
    compare(errors, reference_stage+".count", len(by_stage[reference_stage]), 1)
    if by_stage[reference_stage]:
        reference_details = by_stage[reference_stage][0]["details"]
        reference_ns = reference_details["monotonic_ns"]
        compare(errors, "reference_monotonic_ns", raw["reference_monotonic_ns"], reference_ns)
        if enabled:
            compare(errors, "fault_monotonic_ns", raw["fault_monotonic_ns"], reference_ns)
        if snapshot is not None:
            compare(errors, "event.fault_parameter_snapshot", reference_details.get("fault_parameter_snapshot"), snapshot)
        if "injection_phase_steps" in raw:
            compare(errors, "event.injection_phase_steps", reference_details.get("injection_phase_steps"), reference_phase)
    false_events = by_stage["detector_false_trigger"]
    replay = replay_detector(trace, spec["detector"], reference=reference, fault_leg=spec["fault_leg"], enabled=enabled)
    for sample, active in zip(trace, replay.pop("active_by_step")):
        compare(errors, f"detector_replay.active_legs[{sample['step']}]", sample["detector_active_legs"], active)
    replay_false = replay["false_trigger_transitions"]
    compare(errors, "false_trigger_count", raw.get("false_trigger_count"), len(replay_false))
    compare(errors, "false_triggers", raw.get("false_triggers", []), replay_false)
    compare(errors, "false_trigger_events", [
        {key: event["details"][key] for key in ("leg", "step", "monotonic_ns")} for event in false_events
    ], replay_false)
    for name in ("detector_selected_leg", "detected", "detector_correct"):
        compare(errors, "detector_replay."+name, raw.get(name), replay[name])
    first_correct = replay["first_correct_detection"]
    compare(errors, "detector_replay.detect_step", raw.get("detect_step"), first_correct["step"] if first_correct else None)
    compare(errors, "detector_replay.detect_monotonic_ns", raw.get("detect_monotonic_ns"), first_correct["monotonic_ns"] if first_correct else None)
    compare(errors, "fault_detected_event.count", len(by_stage["fault_detected"]), int(first_correct is not None))
    if first_correct and by_stage["fault_detected"]:
        correct_details = by_stage["fault_detected"][0]["details"]
        for key in ("step", "monotonic_ns"):
            compare(errors, "fault_detected_event."+key, correct_details[key], first_correct[key])
        compare(errors, "fault_detected_event.leg", correct_details["fault_leg"], first_correct["leg"])
    computed["detector_replay"] = replay
    compare(errors, "action_source_steps", raw.get("action_source_steps"), dict(Counter(t["action_source"] for t in trace)))
    for source, count in Counter(t["action_source"] for t in trace).items():
        compare(errors, "action_source_duty_fraction."+source, raw["action_source_duty_fraction"].get(source), count/len(trace))
    first_local = next((t for t in trace if t["action_source"] == "local_reflex"), None)
    compare(errors, "reflex_step", raw.get("reflex_step"), first_local["step"] if first_local else None)
    compare(errors, "reflex_monotonic_ns", raw.get("reflex_monotonic_ns"), first_local["control_started_ns"] if first_local else None)
    reflex_correct = bool(first_local and replay["detector_selected_leg"] == spec["fault_leg"] and first_local["step"] >= reference) if enabled and spec["method"] == "local_reflex" else None
    false_reflex = bool(first_local and (not enabled or replay["detector_selected_leg"] != spec["fault_leg"] or first_local["step"] < reference))
    compare(errors, "detector_replay.reflex_applied_correctly", raw.get("reflex_applied_correctly"), reflex_correct)
    compare(errors, "detector_replay.false_reflex_applied", raw.get("false_reflex_applied"), false_reflex)
    if raw.get("detect_step") is not None:
        detection = trace[int(raw["detect_step"])]
        compare(errors, "detect_latency_sim_s", raw["detect_latency_sim_s"], detection["sim_time_s"]-reference*dt)
        compare(errors, "detect_monotonic_ns", raw["detect_monotonic_ns"], detection["monotonic_ns"])
        compare(errors, "detect_latency_ms", raw["detect_latency_ms"], (detection["monotonic_ns"]-raw["fault_monotonic_ns"])/1e6)
        if raw.get("reflex_applied_correctly") and first_local:
            compare(errors, "reflex_dispatch_latency_ms", raw["reflex_dispatch_latency_ms"], (first_local["control_started_ns"]-detection["monotonic_ns"])/1e6)
            compare(errors, "reflex_dispatch_latency_sim_s", raw["reflex_dispatch_latency_sim_s"], first_local["step"]*dt-detection["sim_time_s"])
    if not enabled:
        for name in ("detected", "detector_correct", "detect_monotonic_ns", "fault_monotonic_ns", "detect_latency_ms", "detect_latency_sim_s", "reflex_applied_correctly"):
            compare(errors, "healthy."+name, raw.get(name), None)
    for label, section in (("pre_reference", trace[:reference]), ("post_reference", trace[reference:])):
        threshold = float(spec["detector"]["enter_threshold"])
        for leg in LEGS:
            above = [t["detector_scores"][leg] >= threshold for t in section]
            current = maximum = 0
            for flag in above:
                current = current+1 if flag else 0
                maximum = max(maximum, current)
            expected = {"above_threshold_samples": sum(above), "above_threshold_fraction": sum(above)/len(above),
                        "maximum_consecutive_samples": maximum,
                        "detector_active_fraction": sum(leg in t["detector_active_legs"] for t in section)/len(section)}
            for key, value in expected.items():
                compare(errors, f"excursions.{label}.{leg}.{key}", raw["residual_threshold_excursions"][label]["by_leg"][leg][key], value)
    post = trace[reference:]
    yaw = float(raw["initial_yaw_rad"])
    heading, lateral = np.array([math.cos(yaw), math.sin(yaw), 0.]), np.array([-math.sin(yaw), math.cos(yaw), 0.])
    displacement = np.array(post[-1]["torso_position_world_m"])-np.array(trace[reference-1]["torso_position_world_m"])
    computed["forward_displacement_m"] = float(displacement @ heading)
    computed["lateral_displacement_m"] = float(displacement @ lateral)
    for key in ("forward_displacement_m", "lateral_displacement_m"):
        compare(errors, key, raw.get(key), computed[key])
    if enabled and spec["fault_strength"] == 0:
        if any(any(abs(x) > 1e-10 for x in t["fault_leg_actuator_force"]) for t in post):
            errors.append("full actuator loss has nonzero post-reference actuator force")
    push_force = float(spec.get("lateral_push_force_n", 0))
    push_start = math.ceil(float(spec.get("lateral_push_start_s", 3)) / dt)
    push_end = math.ceil((float(spec.get("lateral_push_start_s", 3))+float(spec.get("lateral_push_duration_s", .2))) / dt)
    push_count = sum(bool(push_force) and push_start <= t["step"] < push_end for t in trace)
    compare(errors, "lateral_push.applied_steps", raw["lateral_push"]["applied_steps"], push_count)
    compare(errors, "lateral_push.impulse_n_s", raw["lateral_push"]["impulse_n_s"], push_force*push_count*dt)
    computed.update({"false_trigger_count": len(replay_false), "false_trigger_trial": bool(replay_false),
                     "pre_threshold_excursion_samples": sum(sum(t["detector_scores"][leg] >= spec["detector"]["enter_threshold"] for leg in LEGS) for t in trace[:reference]),
                     "phase": int(raw["gait_phase_offset_steps"]), "initial_yaw_rad": yaw,
                     "reference_phase": reference_phase, "reference_step": reference, "gait_cycle_steps": cycle,
                     "fault_enabled": enabled, "detector_correct": replay["detector_correct"],
                     "detected": replay["detected"], "detection_miss": replay["detection_miss"],
                     "reflex_applied_correctly": reflex_correct,
                     "detect_latency_ms": raw.get("detect_latency_ms"), "reflex_dispatch_latency_ms": raw.get("reflex_dispatch_latency_ms"),
                     "max_pacing_lateness_ms": raw.get("max_pacing_lateness_ms")})
    computed["measurement_sync"] = raw.get("measurement_sync", "not recorded; inspect frozen source")
    computed["fault_parameter_evidence"] = "recorded" if snapshot is not None else ("not applicable" if not enabled else "not recorded")
    computed["measured_duration_s"] = len(trace)*dt
    computed["pre_reference_duration_s"] = reference*dt
    computed["post_reference_duration_s"] = (len(trace)-reference)*dt
    computed["healthy_exposure"] = healthy_exposure(
        trace, reference, [event["step"] for event in replay_false], dt,
    ) if not enabled else None
    if not enabled:
        compare(errors, "healthy.false_reflex_applied", raw.get("false_reflex_applied"), computed["healthy_exposure"]["false_local_response_applied"])
    if summary["study"] == "integrated":
        application = raw["supervisory_application"]
        applied = [t for t in trace if t["action_source"] == "validated_supervisor"]
        compare(errors, "supervisor.applied", application["applied"], bool(applied))
        compare(errors, "supervisor.applied_steps", application["applied_steps"], len(applied))
        accepted = application.get("accepted_monotonic_ns")
        compare(errors, "supervisor.accepted", application["accepted"], accepted is not None)
        if accepted is not None:
            command = application["command"]
            scheduler = raw["scheduler"]
            compare(errors, "supervisor.canonical", command, scheduler["runtime_canonical_command"])
            compare(errors, "supervisor.accepted_payload", command, scheduler["emergency_preset"])
            compare(errors, "supervisor.acceptance_events", len(by_stage["response_accepted"]), 1)
            if by_stage["response_accepted"]:
                compare(errors, "supervisor.acceptance_clock", accepted, by_stage["response_accepted"][0]["monotonic_ns"])
            compare(errors, "scheduler.detector_origin", scheduler["fault_monotonic_ns"], raw["detect_monotonic_ns"])
            phase = command["actions"][0]
            support = [x for leg in LEGS for x in ((0., 0., -.4) if leg == raw["detector_selected_leg"] else (0., -.08, -.48))]
            compare(errors, "supervisor.same_local_action", phase["actuator_targets"], support)
            if applied:
                first = applied[0]
                compare(errors, "supervisor.first_applied_step", application["first_applied_step"], first["step"])
                compare(errors, "supervisor.applied_clock", application["applied_monotonic_ns"], first["control_started_ns"])
                compare(errors, "supervisor.applied_contiguous", [t["step"] for t in applied], list(range(first["step"], first["step"]+len(applied))))
                compare(errors, "supervisor.bounded_duration", len(applied), min(phase["duration_steps"], len(trace)-first["step"]))
                for t in applied:
                    compare(errors, f"supervisor.actual_action[{t['step']}]", t["commanded_action"], phase["actuator_targets"])
                    if t["control_started_ns"] < accepted:
                        errors.append(f"supervisor step {t['step']} applied before acceptance")
                expiry = first["step"]+phase["duration_steps"]
                if any(t["action_source"] != "local_reflex" for t in trace[expiry:]):
                    errors.append("supervisor did not fall back to local reflex after expiry")
                compare(errors, "supervisor.accept_to_apply_ms", application["accept_to_apply_ms"], (first["control_started_ns"]-accepted)/1e6)
                compare(errors, "supervisor.detect_to_apply_ms", application["detect_to_apply_ms"], (first["control_started_ns"]-raw["detect_monotonic_ns"])/1e6)
        elif applied:
            errors.append("supervisory action exists without accepted command")
        computed.update({"supervisor_accepted": application["accepted"], "supervisor_applied": bool(applied),
                         "supervisor_applied_steps": len(applied), "accept_to_apply_ms": application.get("accept_to_apply_ms"),
                         "detect_to_supervisor_apply_ms": application.get("detect_to_apply_ms"),
                         "fault_to_supervisor_apply_ms": (application["applied_monotonic_ns"]-raw["fault_monotonic_ns"])/1e6 if applied else None,
                         "fault_to_local_apply_ms": (raw["reflex_monotonic_ns"]-raw["fault_monotonic_ns"])/1e6 if raw.get("reflex_monotonic_ns") is not None else None,
                         "scheduler_status": (raw.get("scheduler") or {}).get("status"),
                         "same_canonical_support": application.get("same_as_local_support")})
    return computed, errors


def load_campaign(campaign: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    definition = read_json(campaign / "campaign.json")
    provenance = read_json(campaign / "provenance.json")
    input_hashes = {"campaign.json": digest_bytes((campaign / "campaign.json").read_bytes()),
                    "provenance.json": digest_bytes((campaign / "provenance.json").read_bytes())}
    source_hasher = hashlib.sha256()
    for entry in provenance.get("source_files", []):
        source_path = within(campaign / "source_snapshot", entry["file"])
        payload = source_path.read_bytes()
        if digest_bytes(payload) != entry["sha256"]:
            raise ValueError(f"frozen source artifact mismatch: {source_path}")
        source_hasher.update(entry["file"].encode("utf-8"))
        source_hasher.update(b"\0")
        source_hasher.update(payload)
        source_hasher.update(b"\0")
        input_hashes[str(source_path.relative_to(campaign))] = entry["sha256"]
    if provenance.get("source_files") and source_hasher.hexdigest() != provenance["source_sha256"]:
        raise ValueError("frozen aggregate source hash differs from provenance")
    package_versions = {name.lower().replace("_", "-"): version for item in provenance.get("packages", []) for name, _, version in [item.partition("==")]}
    records, seen = [], set()
    manifests = sorted(definition["manifests"], key=lambda entry: entry["order"])
    for entry in manifests:
        path = within(campaign, entry["snapshot_path"])
        actual = digest_bytes(path.read_bytes())
        if actual != entry["snapshot_sha256"]:
            raise ValueError(f"manifest hash mismatch: {path}")
        input_hashes[entry["snapshot_path"]] = actual
        specs = read_jsonl(path)
        if len(specs) != entry["trial_count"]:
            raise ValueError(f"manifest row count mismatch: {path}")
        for spec in specs:
            trial_id = spec["trial_id"]
            if trial_id in seen:
                raise ValueError(f"duplicate manifested trial: {trial_id}")
            seen.add(trial_id)
            if spec["study"] not in {"physics", "integrated"}:
                continue
            record = {"spec": spec, "manifest": entry["snapshot_path"], "verified": False, "errors": []}
            records.append(record)
            trial_dir = within(campaign / "trials", trial_id)
            summary_path = trial_dir / "summary.json"
            if not summary_path.is_file():
                record.update(status="missing", errors=["missing adopted summary"])
                continue
            try:
                summary = read_json(summary_path)
                record["status"] = summary.get("status", "unknown")
                record["summary_path"] = str(summary_path.relative_to(campaign))
                input_hashes[str(summary_path.relative_to(campaign))] = digest_bytes(summary_path.read_bytes())
                if summary.get("status") != "complete":
                    record["errors"].append("worker/infrastructure failure: "+str(summary.get("failure_reason")))
                    continue
                for field, expected in spec.items():
                    compare(record["errors"], "manifest."+field, summary.get(field), expected)
                compare(record["errors"], "trial_spec_sha256", summary.get("trial_spec_sha256"), digest_bytes(canonical(spec)))
                compare(record["errors"], "manifest_sha256", summary.get("manifest_sha256"), actual)
                compare(record["errors"], "source_sha256", summary["environment"].get("source_sha256"), provenance["source_sha256"])
                compare(record["errors"], "python.environment", summary["environment"].get("python"), provenance["python"])
                for name, version in summary["environment"].get("packages", {}).items():
                    if name.lower().replace("_", "-") in package_versions:
                        compare(record["errors"], "package."+name, version, package_versions[name.lower().replace("_", "-")])
                attempt_dir = within(trial_dir, summary["attempt_relative_dir"])
                if not attempt_dir.is_relative_to((trial_dir / "attempts").resolve()):
                    raise ValueError("adopted attempt is not beneath attempts/")
                worker_path = attempt_dir / "summary.json"
                worker_hash = digest_bytes(worker_path.read_bytes())
                compare(record["errors"], "worker_summary_sha256", summary.get("worker_summary_sha256"), worker_hash)
                worker = read_json(worker_path)
                for field, value in worker.items():
                    compare(record["errors"], "adopted."+field, summary.get(field), value)
                input_hashes[str(worker_path.relative_to(campaign))] = worker_hash
                paths = {}
                for name in ("events", "trace"):
                    artifact = summary["artifacts"][name]
                    filename = artifact["file"]
                    if Path(filename).name != filename:
                        raise ValueError("artifact filename is not a basename")
                    artifact_path = within(attempt_dir, filename)
                    payload = artifact_path.read_bytes()
                    artifact_hash = digest_bytes(payload)
                    compare(record["errors"], name+".sha256", artifact["sha256"], artifact_hash)
                    compare(record["errors"], name+".bytes", artifact["bytes"], len(payload))
                    compare(record["errors"], name+".rows", artifact["rows"], sum(bool(line.strip()) for line in payload.splitlines()))
                    input_hashes[str(artifact_path.relative_to(campaign))] = artifact_hash
                    paths[name] = artifact_path
                trace, events = read_jsonl(paths["trace"]), read_jsonl(paths["events"])
                expected_first, expected_last = (("physics_trial_started", "physics_trial_finished") if spec["study"] == "physics" else ("integrated_trial_started", "integrated_trial_finished"))
                compare(record["errors"], "event.sequence", [event["sequence"] for event in events], list(range(1, len(events)+1)))
                compare(record["errors"], "event.first", events[0]["stage"], expected_first)
                compare(record["errors"], "event.last", events[-1]["stage"], expected_last)
                if len({event["event_id"] for event in events}) != len(events):
                    record["errors"].append("duplicate event IDs")
                if any(event["trial_id"] != trial_id or event["batch_id"] != spec["batch_id"] for event in events):
                    record["errors"].append("event trial/batch identity mismatch")
                if any(a["monotonic_ns"] > b["monotonic_ns"] for a, b in zip(events, events[1:])):
                    record["errors"].append("event clock is not monotonic")
                computed, errors = audit_trace(summary, trace, events)
                record["errors"].extend(errors)
                record.update(verified=not record["errors"], recomputed=computed,
                              trace_rows=len(trace), event_rows=len(events),
                              attempt_id=summary["adopted_attempt_id"], environment=summary["environment"])
            except (ValueError, KeyError, TypeError, IndexError, OSError) as exc:
                record["errors"].append(f"audit error: {type(exc).__name__}: {exc}")
    return records, {"campaign_definition": definition, "experiment_provenance": provenance, "input_sha256": input_hashes}


def build_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        spec = record["spec"]
        profile = (spec.get("metadata") or {}).get("profile", "integrated" if spec["study"] == "integrated" else "unspecified")
        groups[f"{spec['study']}|{profile}|{spec['method']}"].append(record)
    output = {}
    numeric = ("upright_deficit_integral", "post_fault_min_upright", "post_fault_min_torso_height", "mean_speed_m_s",
               "terminal_mean_speed_m_s", "forward_displacement_m", "mean_gyro_rad_s", "detect_latency_ms", "reflex_dispatch_latency_ms",
               "max_pacing_lateness_ms", "accept_to_apply_ms", "detect_to_supervisor_apply_ms", "fault_to_local_apply_ms", "fault_to_supervisor_apply_ms")
    for key, group in sorted(groups.items()):
        valid = [row["recomputed"] for row in group if row["verified"]]
        healthy = [row for row in valid if not row["fault_enabled"]]
        exposure = [row["healthy_exposure"] for row in healthy]
        realtime = all(row["spec"]["realtime"] for row in group)
        output[key] = {
            "n_expected": len(group), "n_verified": len(valid), "n_unverified_or_missing": len(group)-len(valid),
            "safety_success_count": sum(row["success"] for row in valid),
            "safety_success_rate_all_expected": sum(row["success"] for row in valid)/len(group),
            "fall_count": sum(row["fall_detected"] for row in valid),
            "false_trigger_trial_count": sum(row["false_trigger_trial"] for row in valid),
            "false_trigger_event_count": sum(row["false_trigger_count"] for row in valid),
            "healthy_false_alarm_trial_rate": sum(row["false_trigger_trial"] for row in healthy)/len(healthy) if healthy else None,
            "healthy_exposure": {
                "n_trials": len(healthy),
                **{name: sum(row[name] for row in exposure) for name in (
                    "measured_duration_s", "pre_reference_duration_s", "post_reference_duration_s",
                    "at_risk_duration_s", "pre_reference_at_risk_duration_s", "post_reference_at_risk_duration_s",
                    "pre_reference_false_trigger_events", "post_reference_false_trigger_events",
                    "false_local_response_steps", "false_local_response_duration_s",
                )},
                "false_local_response_trial_count": sum(row["false_local_response_applied"] for row in exposure),
                "false_local_response_duty_fraction": sum(row["false_local_response_duration_s"] for row in exposure)/sum(row["measured_duration_s"] for row in exposure) if exposure else None,
                "false_local_response_onset_sim_s": stats([row["false_local_response_onset_sim_s"] for row in exposure if row["false_local_response_applied"]]),
                "first_false_trigger_censored_trials": sum(row["first_false_trigger_censored"] for row in exposure),
                "interpretation": "Descriptive simulated exposure; full duration includes post-response closed-loop time. At-risk duration ends at first false-trigger end-of-step sample or fixed horizon. No independent/stationary hazard model.",
            } if healthy else None,
            "detector_correct_count": sum(row["detector_correct"] is True for row in valid),
            "detected_count": sum(row["detected"] is True for row in valid),
            "detection_miss_count": sum(row["detection_miss"] is True for row in valid),
            "detector_applicable_count": sum(row["detector_correct"] is not None for row in valid),
            "supervisor_applied_count": sum(row.get("supervisor_applied", False) for row in valid),
            "supervisory_expectation": (
                "not applicable: local-only control requests no supervisory command" if group[0]["spec"]["method"] == "local_only"
                else "expected absence: preregistered injected emergency transport failure" if group[0]["spec"]["method"] == "backend_failure"
                else "supervisory application expected" if group[0]["spec"]["study"] == "integrated"
                else "not applicable: standalone physics"
            ),
            "supervisor_applicable_count": len(group) if group[0]["spec"]["study"] == "integrated" and group[0]["spec"]["method"] not in {"local_only", "backend_failure"} else None,
            "phases": sorted({row["phase"] for row in valid}),
            "reference_phases": sorted({row["reference_phase"] for row in valid}),
            "full_20_phase_coverage_verified": {row["reference_phase"] for row in valid} == set(range(20)),
            "reference_phase_definition": "(reference_step + gait_phase_offset_steps) modulo gait cycle; pseudo-reference for healthy controls",
            "measurement_sync": dict(Counter(row["measurement_sync"] for row in valid)),
            "latency_scope": "real-time measured clocks" if realtime else "offline: wall-clock latency is intentionally omitted",
            "metrics": {field: stats([row[field] for row in valid if row.get(field) is not None and (realtime or not field.endswith("_ms"))]) for field in numeric},
        }
    pairs = []
    index: dict[tuple, dict[str, dict]] = defaultdict(dict)
    for record in records:
        if not record["verified"] or record["spec"]["study"] != "physics":
            continue
        s = record["spec"]
        key = (s["study"], s["block_id"], s["fault_leg"], s["seed"])
        if s["method"] in index[key]:
            raise ValueError("duplicate method within a paired scenario")
        index[key][s["method"]] = record
    for group in index.values():
        if not {"local_reflex", "no_reflex"} <= set(group):
            continue
        left, right = group["local_reflex"], group["no_reflex"]
        a, b = left["recomputed"], right["recomputed"]
        if a["phase"] != b["phase"] or a["initial_yaw_rad"] != b["initial_yaw_rad"]:
            raise ValueError("paired scenario has mismatched phase/yaw")
        pairs.append({"profile": (left["spec"].get("metadata") or {}).get("profile", "unspecified"),
                      "phase": a["phase"], "leg": left["spec"]["fault_leg"], "seed": left["spec"]["seed"],
                      "reference_phase": a["reference_phase"],
                      "left_trial": left["spec"]["trial_id"], "right_trial": right["spec"]["trial_id"],
                      "deficit_difference_s": a["upright_deficit_integral"]-b["upright_deficit_integral"],
                      "safety_difference": int(a["success"])-int(b["success"]),
                      "terminal_speed_difference_m_s": a["terminal_mean_speed_m_s"]-b["terminal_mean_speed_m_s"]})
    return {"groups": output, "paired_phase_grid": pairs, "inference": "Descriptive finite-grid summaries only; no bootstrap or p-values.", "limitations": LIMITATION}


def render_figures(records: list[dict[str, Any]], summary: dict[str, Any], output: Path) -> list[str]:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 8, "axes.spines.top": False,
                         "axes.spines.right": False, "axes.labelsize": 8, "axes.titlesize": 9,
                         "xtick.labelsize": 7, "ytick.labelsize": 7, "legend.fontsize": 7,
                         "svg.fonttype": "none", "svg.hashsalt": digest_bytes(Path(__file__).read_bytes()), "savefig.dpi": 300})
    files = []
    def jitter(count, span):
        return np.linspace(-span, span, count) if count > 1 else np.zeros(count)
    def save(fig, name):
        if any(not r["verified"] for r in records):
            fig.text(.99, .003, "PARTIAL AUDIT — NOT FINAL RESULTS", ha="right", va="bottom", fontsize=6, color="#777777")
        for suffix in ("png", "svg"):
            path = output / f"{name}.{suffix}"
            fig.savefig(path, bbox_inches="tight", facecolor="white", **({"metadata": {"Date": None}} if suffix == "svg" else {}))
            files.append(path.name)
        plt.close(fig)
    pairs = summary["paired_phase_grid"]
    profiles = sorted({p["profile"] for p in pairs if p["profile"] != "realtime_audit"})
    if profiles:
        columns = 3
        rows = math.ceil(len(profiles)/columns)
        fig, axes = plt.subplots(rows, columns, figsize=(7.25, 2.1*rows), squeeze=False)
        leg_colors = ("#0072B2", "#D55E00", "#009E73", "#CC79A7")
        for ax, profile in zip(axes.flat, profiles):
            for leg, color in zip(LEGS, leg_colors):
                group = sorted((p for p in pairs if p["profile"] == profile and p["leg"] == leg), key=lambda p: p["phase"])
                ax.plot([p["phase"] for p in group], [p["deficit_difference_s"] for p in group], ".-", ms=4, lw=.7, color=color, label=leg)
            ax.axhline(0, color="black", lw=.6, ls="--")
            count = sum(p["profile"] == profile for p in pairs)
            ax.set(title=f"{profile.replace('_', ' ')} (n={count} pairs)", xlabel="Gait phase offset (steps)", ylabel="Δ upright deficit (s)")
            ax.set_xlim(-.5, 19.5)
            ax.set_xticks([0, 5, 10, 15, 19])
            ax.grid(axis="y", alpha=.15)
        for ax in list(axes.flat)[len(profiles):]:
            ax.set_visible(False)
        handles, labels = axes.flat[0].get_legend_handles_labels()
        fig.legend(handles, labels, ncol=4, frameon=False, loc="upper center", bbox_to_anchor=(.5, .94))
        fig.suptitle("Paired phase grid: local reflex − no reflex (negative favors local)", fontsize=10)
        fig.text(.5, .03, "Injection / healthy reference phase = (reference step + displayed offset) mod 20", ha="center", fontsize=6.5)
        fig.tight_layout(rect=(0, .055, 1, .91))
        save(fig, "physics_paired_phase_grid")
        fig, axes = plt.subplots(1, 3, figsize=(7.25, 3.1))
        y = np.arange(len(profiles))
        for method, offset in (("local_reflex", -.18), ("no_reflex", .18)):
            values = [summary["groups"].get(f"physics|{profile}|{method}") for profile in profiles]
            safe = [v["safety_success_rate_all_expected"] if v else np.nan for v in values]
            false = [v["false_trigger_trial_count"]/v["n_expected"] if v else np.nan for v in values]
            deficit = [v["metrics"]["upright_deficit_integral"]["mean"] if v else np.nan for v in values]
            for ax, numbers in zip(axes, (safe, false, deficit)):
                ax.barh(y+offset, numbers, height=.32, color=COLORS[method], label=SHORT[method])
                ax.set_yticks(y, [p.replace("_", " ") for p in profiles])
                ax.grid(axis="x", alpha=.15)
        for ax in axes:
            ax.invert_yaxis()
        axes[0].set(xlabel="Fixed-horizon safety fraction", xlim=(0, 1.05))
        axes[1].set(xlabel="False-trigger trial fraction", xlim=(0, 1.05))
        axes[2].set(xlabel="Mean upright deficit (s)")
        handles, labels = axes[0].get_legend_handles_labels()
        fig.legend(handles, labels, loc="upper center", bbox_to_anchor=(.5, .94), ncol=2, frameon=False)
        for ax in axes[1:]:
            ax.set_yticklabels([])
        fig.suptitle("Enumerated scenarios; fractions are descriptive", fontsize=10)
        fig.tight_layout(rect=(0, 0, 1, .91))
        save(fig, "physics_profile_outcomes")
    realtime = [record for record in records if record["verified"] and record["spec"]["realtime"]]
    if realtime:
        grouped = defaultdict(list)
        for record in realtime:
            grouped[(record["spec"]["study"], record["spec"]["method"])].append(record["recomputed"])
        keys = sorted(grouped, key=lambda key: (key[0], METHODS.index(key[1]) if key[1] in METHODS else 99))
        fig, axes = plt.subplots(1, 2, figsize=(7.25, max(2.8, .34*len(keys))))
        for ax, metric, label in zip(axes, ("detect_latency_ms", "reflex_dispatch_latency_ms"), ("Fault → detector (ms)", "Detector → local action (ms)")):
            for position, key in enumerate(keys):
                values = [r[metric] for r in grouped[key] if r.get(metric) is not None]
                if values:
                    ax.boxplot([values], positions=[position], vert=False, widths=.5, showfliers=False,
                               patch_artist=True, boxprops={"facecolor": COLORS.get(key[1], "grey"), "alpha": .5},
                               medianprops={"color": "black"})
                    ax.scatter(values, position+jitter(len(values), .13), s=7, color=COLORS.get(key[1], "grey"), alpha=.55)
            ax.set_yticks(range(len(keys)), [f"{a}: {SHORT.get(b,b)}" for a, b in keys])
            ax.set_xlabel(label)
            ax.set_ylim(-.5, len(keys)-.5)
            ax.grid(axis="x", alpha=.15)
        axes[1].set_yticklabels([])
        fig.suptitle("Real-time measured-clock latencies only (offline grid excluded)", fontsize=10)
        fig.tight_layout()
        save(fig, "physics_realtime_latency")
    integrated = [r for r in records if r["verified"] and r["spec"]["study"] == "integrated"]
    if integrated:
        expected_integrated = [r for r in records if r["spec"]["study"] == "integrated"]
        methods = [m for m in METHODS if any(r["spec"]["method"] == m for r in expected_integrated)]
        fig, axes = plt.subplots(1, 2, figsize=(7.25, 3.1))
        for index, method in enumerate(methods):
            values = [r["recomputed"] for r in integrated if r["spec"]["method"] == method]
            for offset, row in zip(jitter(len(values), .17), values):
                if row.get("fault_to_local_apply_ms") is not None:
                    axes[0].scatter(row["fault_to_local_apply_ms"]/1000, index+offset, marker="o", s=15, color=COLORS[method])
                if row.get("fault_to_supervisor_apply_ms") is not None:
                    axes[0].scatter(row["fault_to_supervisor_apply_ms"]/1000, index+offset, marker="s", s=15, color=COLORS[method])
            accepted = [r["accept_to_apply_ms"] for r in values if r.get("accept_to_apply_ms") is not None]
            if accepted:
                axes[1].boxplot([accepted], positions=[index], vert=False, widths=.45, patch_artist=True,
                               boxprops={"facecolor": COLORS[method], "alpha": .5}, medianprops={"color": "black"})
                axes[1].scatter(accepted, index+jitter(len(accepted), .12), color=COLORS[method], s=10)
            else:
                absence = ("N/A: no supervisor requested" if method == "local_only"
                           else "Expected absence: injected failure" if method == "backend_failure"
                           else "No verified supervisory application")
                axes[1].text(.03, index, absence, va="center", fontsize=6.5, transform=axes[1].get_yaxis_transform())
        labels = []
        for method in methods:
            group = [r["recomputed"] for r in integrated if r["spec"]["method"] == method]
            expected = sum(r["spec"]["method"] == method for r in expected_integrated)
            labels.append(
                "Local only (supervisor N/A)" if method == "local_only"
                else "Backend failure (injected)" if method == "backend_failure"
                else f"{SHORT[method]} ({sum(r['supervisor_applied'] for r in group)}/{expected})"
            )
        for ax in axes:
            ax.set_yticks(range(len(methods)), labels)
            ax.set_ylim(-.5, len(methods)-.5)
            ax.grid(axis="x", alpha=.15)
        axes[0].set_xlabel("Time from physical fault (s)")
        axes[0].set_xscale("log")
        axes[0].legend(handles=[Line2D([], [], marker="o", color="black", ls="", label="Local reflex"), Line2D([], [], marker="s", color="black", ls="", label="Supervisor")], frameon=False, fontsize=6.5)
        axes[1].set_xlabel("Validation → first applied control (ms)")
        axes[1].set_yticklabels([])
        fig.suptitle("Integrated execution: same support command; application rates exclude local-only / injected failure", fontsize=8.5)
        fig.tight_layout()
        save(fig, "integrated_application_timeline")
    return files


def markdown_report(report: dict[str, Any]) -> str:
    audit = report["audit"]
    lines = ["# Physics and integrated execution audit", "",
             f"Verified **{audit['verified']}/{audit['expected']}** manifested physics/integrated trials; "
             f"{audit['trace_rows']} raw trace samples inspected. No trial selection by timestamp or directory glob.", "",
             "| Study / profile / method | Verified / expected | Safety successes | False-trigger trials | Mean deficit (s) | Median deficit (s) | Terminal speed (m/s) |",
             "|---|---:|---:|---:|---:|---:|---:|"]
    def fmt(value):
        return "—" if value is None else f"{value:.4g}"
    for key, group in report["summary"]["groups"].items():
        metrics = group["metrics"]
        lines.append(f"| {key.replace('|', ' / ')} | {group['n_verified']}/{group['n_expected']} | {group['safety_success_count']} | {group['false_trigger_trial_count']} | {fmt(metrics['upright_deficit_integral']['mean'])} | {fmt(metrics['upright_deficit_integral']['median'])} | {fmt(metrics['terminal_mean_speed_m_s']['mean'])} |")
    lines += ["", "Safety/figure rates retain the manifested denominator; observed healthy false-alarm rates and continuous metrics use verified observations only. "
              "False-trigger counts are reported for every profile; only the healthy profiles are fault-free false-alarm tests.", "",
              "## Interpretation", "", LIMITATION, "",
              "The report checks fixed-horizon safety, continuous physical endpoints, healthy pseudo-reference handling, "
              "end-of-integration simulation latency, exact validated control arrays, acceptance/application chronology, "
              "finite command duration and local fallback directly against JSONL samples and events.", ""]
    lines += ["Detector state is independently replayed from every saved score using the stored entry/exit thresholds and consecutive-sample counts. Every active-leg set, trigger classification, first correct detection, false-trigger event and correct/missed endpoint is checked. Clear transitions are checked through the saved active-state sequence; this experiment source emits no separate clear events. This is not a full raw-sensor score-formula reconstruction.", ""]
    lines += ["## Detector endpoints (independent score-state replay)", "",
              "| Study / profile / method | Correct / applicable verified | Correct-leg detection event | Missed detection | False-trigger trials |",
              "|---|---:|---:|---:|---:|"]
    for key, group in report["summary"]["groups"].items():
        applicable = group["detector_applicable_count"]
        absent = "N/A" if group["n_verified"] else "pending"
        correct = f"{group['detector_correct_count']}/{applicable}" if applicable else absent
        detected = str(group["detected_count"]) if applicable else absent
        missed = str(group["detection_miss_count"]) if applicable else absent
        lines.append(f"| {key.replace('|', ' / ')} | {correct} | {detected} | {missed} | {group['false_trigger_trial_count']} |")
    lines += ["", "Correct detection additionally requires correct first selection and no false trigger. A later correct-leg event can therefore occur in a detector-incorrect trial. Healthy controls have no true-detection target (N/A); every trigger is false. A missed partial-strength fault remains a detector miss even when fixed-horizon physical safety succeeds. Denominators here include verified applicable trials only; missing observations remain in the audit above.", ""]
    lines += ["Phase plots display the configured gait offset. The actual injection (or healthy pseudo-reference) "
              "phase is `(reference_step + offset) % 20`; report.json records both axes and full-20-phase coverage. "
              "Local-only supervisory application is N/A. The backend-failure condition deliberately expects no "
              "supervisory application; neither is included in the successful-supervisor denominator.", ""]
    healthy_groups = [(key, group["healthy_exposure"]) for key, group in report["summary"]["groups"].items() if group["healthy_exposure"] is not None]
    if healthy_groups:
        lines += ["## Healthy-control exposure", "",
                  "| Profile / method | Trials | Observation (sim s) | Pre / post-reference (sim s) | First-trigger at risk (sim s) | False local-response trials | False local-response duty |",
                  "|---|---:|---:|---:|---:|---:|---:|"]
        for key, exposure in healthy_groups:
            lines.append(f"| {key.replace('|', ' / ')} | {exposure['n_trials']} | {fmt(exposure['measured_duration_s'])} | {fmt(exposure['pre_reference_duration_s'])} / {fmt(exposure['post_reference_duration_s'])} | {fmt(exposure['at_risk_duration_s'])} | {exposure['false_local_response_trial_count']} | {fmt(exposure['false_local_response_duty_fraction'])} |")
        lines += ["", "Observation includes post-response closed-loop time; first-trigger at-risk time ends at the end-of-step detection sample or is censored at the fixed horizon. False local-response onset is the start of the first affected control step. These are descriptive simulated-time denominators, not independent or stationary hazard estimates. Per-trial onset and pre/post at-risk exposure are retained in report.json.", ""]
    sync = Counter(r["recomputed"]["measurement_sync"] for r in report["trials"] if r["verified"])
    lines += ["Measurement synchronization metadata: "+", ".join(f"`{name}`: {count} trials" for name, count in sorted(sync.items()))+". "
              "An unrecorded sampling-sync mode must be resolved against the frozen source before citing detector residuals as calibrated measurements.", ""]
    if report["audit"]["errors"]:
        lines += ["## Audit failures", "", "These results are not ready for a final manuscript claim.", ""]
        for item in report["audit"]["errors"]:
            lines.append(f"- `{item['trial_id']}`: {'; '.join(item['errors'][:5])}")
    if report["figures"]:
        lines += ["## Figures", ""]
        for filename in report["figures"]:
            if filename.endswith(".png"):
                lines += [f"![{filename}]({filename})", ""]
    lines += ["## Provenance", "", f"Report script SHA-256: `{report['report_provenance']['script_sha256']}`. "
              "The accompanying report.json records the script, source campaign, exact input hashes, per-trial audit results and recomputed outcomes.", ""]
    return "\n".join(lines)


def self_test() -> None:
    spec = {"eligibility_window_steps": 5, "minimum_pre_fault_samples": 5,
            "eligibility_upright_threshold": .95, "eligibility_upright_fraction": .95,
            "eligibility_gyro_threshold": .5, "eligibility_height_threshold": .35,
            "terminal_window_steps": 5, "fall_upright_threshold": .3, "fall_height_threshold": .18,
            "fall_debounce_steps": 3, "safe_upright_threshold": .8, "safe_height_threshold": .3, "safe_upright_fraction": .95}
    trace = [dict(step=i, torso_upright=1., torso_height=.5, imu_gyro_norm=.1, torso_speed=.2) for i in range(30)]
    good = recompute_physics(trace, spec, reference=10)
    assert good["success"] and good["upright_deficit_integral"] == 0
    assert good["post_samples"] == 20
    for i in range(15, 18):
        trace[i] = {**trace[i], "torso_upright": .1}
    bad = recompute_physics(trace, spec, reference=10)
    assert not bad["success"] and bad["first_fall_step"] == 15 and bad["fall_recognized_step"] == 17
    assert math.isclose(bad["upright_deficit_integral"], .054)
    errors = []
    compare(errors, "sim latency", .08, (154+1)*.02-150*.02)
    assert errors, "The v2 one-step latency error must be rejected"
    assert len(digest_bytes(canonical({"x": 1}))) == 64
    detector_trace = [{"step": i, "monotonic_ns": i+1, "detector_scores": {leg: 0. for leg in LEGS}} for i in range(36)]
    for i in range(5):
        detector_trace[i]["detector_scores"]["BR"] = .58
    # BR triggers at4 and clears at19 after15 samples at/below exit.
    for i in range(25, 30):
        detector_trace[i]["detector_scores"]["FL"] = .58
    replay = replay_detector(detector_trace, {"enter_threshold": .58, "exit_threshold": .24, "enter_steps": 5, "exit_steps": 15}, reference=20, fault_leg="FL", enabled=True)
    assert replay["trigger_transitions"][0]["step"] == 4
    assert replay["clear_transitions"][0]["step"] == 19
    assert replay["first_correct_detection"]["step"] == 29
    assert replay["detected"] and not replay["detector_correct"] and not replay["detection_miss"]
    assert replay["detector_selected_leg"] == "BR" and len(replay["false_trigger_transitions"]) == 1
    assert replay["active_by_step"][4] == ["BR"] and replay["active_by_step"][19] == []
    detector_trace[12]["detector_scores"]["BR"] = .3  # Hysteresis band resets the low run.
    for i in range(13, 28):
        detector_trace[i]["detector_scores"]["BR"] = .24
    replay_reset = replay_detector(detector_trace, {"enter_threshold": .58, "exit_threshold": .24, "enter_steps": 5, "exit_steps": 15}, reference=20, fault_leg="FL", enabled=False)
    assert replay_reset["clear_transitions"][0]["step"] == 27
    assert replay_reset["detected"] is None and replay_reset["detector_correct"] is None
    assert len(replay_reset["false_trigger_transitions"]) == 2
    exposure_trace = [{"step": i, "action_source": "nominal_trot" if i < 6 else "local_reflex"} for i in range(20)]
    exposure = healthy_exposure(exposure_trace, reference=10, false_steps=[5, 12])
    assert math.isclose(exposure["at_risk_duration_s"], .12)
    assert exposure["post_reference_at_risk_duration_s"] == 0
    assert math.isclose(exposure["false_local_response_onset_sim_s"], .12)
    assert math.isclose(exposure["false_local_response_duty_fraction"], .7)
    censored = healthy_exposure(exposure_trace, reference=10, false_steps=[])
    assert censored["first_false_trigger_censored"] and censored["at_risk_duration_s"] == .4
    print("Self-test passed: endpoint recomputation, sustained-fall logic, v2 latency-error detection, detector-state replay, healthy exposure boundaries.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-dir", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path("paper/generated/physics"))
    parser.add_argument("--allow-incomplete", action="store_true", help="Write a partial descriptive report; failed audits remain explicit")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return 0
    if args.campaign_dir is None:
        parser.error("--campaign-dir is required unless --self-test is used")
    campaign, output = args.campaign_dir.resolve(), args.output_dir.resolve()
    if output.is_relative_to(campaign):
        parser.error("report output must be outside the immutable campaign directory")
    records, provenance = load_campaign(campaign)
    if not records:
        raise ValueError("campaign has no manifested physics or integrated trials")
    summary = build_summary(records)
    output.mkdir(parents=True, exist_ok=True)
    figures = render_figures(records, summary, output)
    script_payload = Path(__file__).read_bytes()
    script_snapshot = "build_physics_report.snapshot.py"
    (output / script_snapshot).write_bytes(script_payload)
    report = {
        "report_schema_version": 1,
        "report_provenance": {"created_utc": datetime.now(timezone.utc).isoformat(),
                              "script": str(Path(__file__).resolve()), "script_sha256": digest_bytes(script_payload),
                              "script_snapshot": script_snapshot,
                              "campaign_dir": str(campaign), "python": sys.version, "platform": platform.platform(), "numpy": np.__version__,
                              "figure_sha256": {filename: digest_bytes((output / filename).read_bytes()) for filename in figures}},
        "campaign_provenance": provenance,
        "audit": {"expected": len(records), "verified": sum(r["verified"] for r in records),
                  "trace_rows": sum(r.get("trace_rows", 0) for r in records),
                  "errors": [{"trial_id": r["spec"]["trial_id"], "errors": r["errors"]} for r in records if not r["verified"]]},
        "summary": summary, "trials": records, "figures": figures,
    }
    (output / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False)+"\n", encoding="utf-8")
    (output / "report.md").write_text(markdown_report(report), encoding="utf-8")
    artifact_names = ["report.json", "report.md", script_snapshot, *figures]
    (output / "artifact_hashes.json").write_text(json.dumps(
        {name: digest_bytes((output / name).read_bytes()) for name in artifact_names},
        indent=2, sort_keys=True,
    )+"\n", encoding="utf-8")
    print(json.dumps({"report": str(output / "report.json"), "verified": report["audit"]["verified"],
                      "expected": len(records), "figures": len(figures), "audit_failures": len(report["audit"]["errors"])}))
    return 0 if not report["audit"]["errors"] or args.allow_incomplete else 2


if __name__ == "__main__":
    raise SystemExit(main())
