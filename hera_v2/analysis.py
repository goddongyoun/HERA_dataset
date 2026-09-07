"""Manifest-bound descriptive analysis for revision-v2 trials.

No timestamp ranges or directory globs are used.  Failed trials remain in the
deadline-restricted outcome, while successful-only latency is explicitly named
as a descriptive quantity.
"""

from __future__ import annotations

from collections import Counter, defaultdict
import csv
import hashlib
import json
import math
from pathlib import Path
import random
from statistics import mean, median, stdev
from typing import Any, Iterable, Sequence

from .manifest import TrialSpec, trial_spec_sha256, validate_manifest


def _quantile(values: Sequence[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def load_trial_summaries(
    specs: Sequence[TrialSpec], results_dir: str | Path, *, require_all: bool = False
) -> tuple[list[dict[str, Any]], list[str]]:
    validate_manifest(specs)
    base = Path(results_dir)
    rows: list[dict[str, Any]] = []
    missing: list[str] = []
    for spec in specs:
        path = base / spec.trial_id / "summary.json"
        if not path.exists():
            missing.append(spec.trial_id)
            continue
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw.get("trial_id") != spec.trial_id:
            raise ValueError(f"summary trial_id mismatch: {path}")
        if raw.get("method") != spec.method or raw.get("study") != spec.study:
            raise ValueError(f"summary condition mismatch: {path}")
        if raw.get("batch_id") != spec.batch_id or raw.get("block_id") != spec.block_id:
            raise ValueError(f"summary block mismatch: {path}")
        if raw.get("trial_spec_sha256") != trial_spec_sha256(spec):
            raise ValueError(f"summary trial_spec_sha256 mismatch: {path}")
        for field, expected in spec.to_dict().items():
            if raw.get(field) != expected:
                raise ValueError(
                    f"summary manifest field mismatch for {field!r}: {path}"
                )
        if raw.get("status") == "complete":
            attempt_relative = raw.get("attempt_relative_dir")
            if not isinstance(attempt_relative, str):
                raise ValueError(f"summary adopted attempt is missing: {path}")
            trial_dir = path.parent.resolve()
            attempt_dir = (trial_dir / attempt_relative).resolve()
            if not attempt_dir.is_relative_to(trial_dir / "attempts"):
                raise ValueError(f"summary attempt path escapes trial: {path}")
            worker_summary_path = attempt_dir / "summary.json"
            if (
                not worker_summary_path.is_file()
                or hashlib.sha256(worker_summary_path.read_bytes()).hexdigest()
                != raw.get("worker_summary_sha256")
            ):
                raise ValueError(f"adopted worker summary hash mismatch: {path}")
            artifacts = raw.get("artifacts") or {}
            if "events" not in artifacts:
                raise ValueError(f"summary events artifact missing: {path}")
            for name, artifact in artifacts.items():
                filename = artifact.get("file")
                if not isinstance(filename, str) or Path(filename).name != filename:
                    raise ValueError(f"unsafe {name} artifact filename: {path}")
                artifact_path = attempt_dir / filename
                if (
                    not artifact_path.is_file()
                    or hashlib.sha256(artifact_path.read_bytes()).hexdigest()
                    != artifact.get("sha256")
                ):
                    raise ValueError(f"{name} artifact hash mismatch: {path}")
        row = dict(raw)
        row.setdefault("deadline_s", spec.deadline_s)
        row.setdefault("fault_leg", spec.fault_leg)
        row.setdefault("seed", spec.seed)
        row.setdefault("order_index", spec.order_index)
        rows.append(row)
    ids = [row["trial_id"] for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate trial summaries")
    errors = [
        str(row["trial_id"]) for row in rows if row.get("status") != "complete"
    ]
    if require_all and (missing or errors):
        details = []
        if missing:
            details.append(f"missing={len(missing)} first={missing[0]}")
        if errors:
            details.append(f"error={len(errors)} first={errors[0]}")
        raise RuntimeError("Manifest is not analysis-complete: " + "; ".join(details))
    return rows, missing


def _event_time(row: dict[str, Any]) -> float | None:
    for field in (
        "event_time_s",
        "response_time_s",
        "stabilization_latency_s",
        "detection_latency_s",
    ):
        value = row.get(field)
        if value is not None:
            return float(value)
    return None


def _deadline_event(row: dict[str, Any]) -> tuple[bool, float]:
    deadline = float(row["deadline_s"])
    value = _event_time(row)
    observed = (
        row.get("status") == "complete"
        and bool(row.get("success"))
        and value is not None
        and math.isfinite(value)
        and 0.0 <= value <= deadline
    )
    return observed, float(value) if observed and value is not None else deadline


def _physics_value(row: dict[str, Any], field: str) -> float | None:
    result = row.get("result")
    if not isinstance(result, dict):
        return None
    value = result.get(field)
    if value is None or isinstance(value, (dict, list)):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


def summarize_group(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    is_physics = bool(rows and rows[0].get("study") in {"physics", "integrated"})
    complete = [row for row in rows if row.get("status") == "complete"]
    infrastructure_errors = [row for row in rows if row.get("status") not in {"complete", "missing"}]
    missing = [row for row in rows if row.get("status") == "missing"]
    observed_and_time = [
        (row.get("status") == "complete" and bool(row.get("success")), 0.0)
        if is_physics else _deadline_event(row)
        for row in rows
    ]
    successes = [row for row, (observed, _) in zip(rows, observed_and_time) if observed]
    success_times = [value for observed, value in observed_and_time if observed]
    restricted = [value for _, value in observed_and_time]
    failures = Counter(
        (
            "missing_summary"
            if row.get("status") == "missing"
            else str(row.get("failure_reason") or row.get("status") or "unspecified")
        )
        for row, (observed, _) in zip(rows, observed_and_time)
        if not observed
    )
    elapsed = [float(row["elapsed_s"]) for row in complete if row.get("elapsed_s") is not None]
    result = {
        "n_expected": len(rows),
        "n_manifested": len(rows),
        "n_complete": len(complete),
        "n_infrastructure_error": len(infrastructure_errors),
        "n_missing": len(missing),
        "n_success": len(successes),
        "deadline_success_rate": (len(successes) / len(rows)) if rows else None,
        "deadline_penalized_mean_s": mean(restricted) if restricted else None,
        "restricted_mean_to_deadline_s": mean(restricted) if restricted else None,
        "restricted_mean_label": (
            "deadline-penalized descriptive mean; not a survival-model RMST estimate"
        ),
        "success_only_latency_is_descriptive": True,
        "success_only_mean_s": mean(success_times) if success_times else None,
        "success_only_sd_s": stdev(success_times) if len(success_times) > 1 else None,
        "success_only_median_s": median(success_times) if success_times else None,
        "success_only_q1_s": _quantile(success_times, 0.25),
        "success_only_q3_s": _quantile(success_times, 0.75),
        "failure_reasons": dict(sorted(failures.items())),
        "mean_process_elapsed_s": mean(elapsed) if elapsed else None,
    }

    if is_physics:
        for key in list(result):
            if key.startswith(("deadline_", "restricted_mean", "success_only_")):
                result.pop(key)
        result["physical_safety_success_rate"] = (
            len(successes) / len(rows) if rows else None
        )
        result["endpoint_definition"] = "fixed-horizon physical safety; no time-to-recovery estimand"
        physical_fields = (
            "post_fault_min_upright",
            "post_fault_min_torso_height",
            "post_fault_upright_fraction",
            "terminal_safe_fraction",
            "upright_deficit_integral",
        )
        result["physics_endpoints"] = {
            field: {
                "n": len(values),
                "mean": mean(values) if values else None,
                "median": median(values) if values else None,
            }
            for field in physical_fields
            if (values := [
                value for row in complete
                if (value := _physics_value(row, field)) is not None
            ])
        }
        result["physics_endpoints"].update(
            {
                name: (
                    sum(bool(value) for value in values) / len(values)
                    if (values := [
                        (row.get("result") or {}).get(field) for row in complete
                        if (row.get("result") or {}).get(field) is not None
                    ]) else None
                )
                for name, field in (
                    ("pre_fault_eligibility_rate", "pre_fault_eligible"),
                    ("correct_detection_rate", "detector_correct"),
                    ("correct_reflex_application_rate", "reflex_applied_correctly"),
                    ("fall_rate", "fall_detected"),
                )
            }
        )
    return result


def _bootstrap_clustered_mean_ci(
    values: Sequence[tuple[str, float]], *, samples: int = 5000, seed: int = 20260907
) -> tuple[float | None, float | None]:
    clusters = sorted({cluster for cluster, _ in values})
    if len(clusters) < 2 or not values:
        return None, None
    by_cluster: dict[str, list[float]] = defaultdict(list)
    for cluster, value in values:
        by_cluster[cluster].append(value)
    rng = random.Random(seed)
    draws: list[float] = []
    for _ in range(samples):
        sampled = [rng.choice(clusters) for _ in clusters]
        flattened = [value for cluster in sampled for value in by_cluster[cluster]]
        draws.append(mean(flattened))
    return _quantile(draws, 0.025), _quantile(draws, 0.975)


def paired_contrasts(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {}
    indexed: dict[tuple[str, str, str, int], dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        key = (
            str(row.get("study")),
            str(row.get("block_id")),
            str(row.get("fault_leg")),
            int(row.get("seed", -1)),
        )
        indexed[key][str(row.get("method"))] = row

    studies = {str(row.get("study")) for row in rows}
    definitions: list[tuple[str, str, str, tuple[str, ...]]] = []
    if "scheduler" in studies:
        metrics = ("deadline_penalized_latency_s", "deadline_success")
        definitions.extend(
            [
                ("hera_preempt_minus_fifo_single", "hera_preempt", "fifo_single", metrics),
                ("hera_preempt_minus_reserved_slot", "hera_preempt", "reserved_slot", metrics),
                ("reserved_slot_minus_fifo_single", "reserved_slot", "fifo_single", metrics),
            ]
        )
    if "physics" in studies:
        definitions.append(
            (
                "local_reflex_minus_no_reflex",
                "local_reflex",
                "no_reflex",
                (
                    "physical_success",
                    "fall",
                    "post_fault_min_upright",
                    "post_fault_min_torso_height",
                    "post_fault_upright_fraction",
                    "terminal_safe_fraction",
                    "upright_deficit_integral",
                ),
            )
        )
    if "integrated" in studies:
        physical_metrics = ("physical_success", "fall", "upright_deficit_integral",
                            "post_fault_min_upright", "post_fault_min_torso_height")
        for method in ("hera_preempt", "fifo_single", "reserved_slot", "backend_failure"):
            definitions.append((f"{method}_minus_local_only", method, "local_only", physical_metrics))

    def metric(row: dict[str, Any], name: str) -> float | None:
        if name == "deadline_penalized_latency_s":
            return _deadline_event(row)[1]
        if name == "deadline_success":
            return float(_deadline_event(row)[0])
        if name == "physical_success":
            return float(row.get("status") == "complete" and bool(row.get("success")))
        if name == "fall":
            value = (row.get("result") or {}).get("fall_detected")
            return float(bool(value)) if row.get("status") == "complete" else None
        return _physics_value(row, name)

    report: dict[str, Any] = {}
    for label, left, right, metrics in definitions:
        metric_values: dict[str, list[tuple[str, float]]] = defaultdict(list)
        pair_count = 0
        for group in indexed.values():
            if left not in group or right not in group:
                continue
            pair_count += 1
            batch = str(group[left].get("batch_id"))
            for metric_name in metrics:
                left_value = metric(group[left], metric_name)
                right_value = metric(group[right], metric_name)
                if left_value is not None and right_value is not None:
                    metric_values[metric_name].append((batch, left_value - right_value))
        report[label] = {
            "left": left,
            "right": right,
            "difference_definition": "left minus right within block, leg, and paired seed",
            "n_manifest_pairs": pair_count,
            "metrics": {},
        }
        for metric_name in metrics:
            values = metric_values.get(metric_name, [])
            numeric = [value for _, value in values]
            low, high = _bootstrap_clustered_mean_ci(values)
            report[label]["metrics"][metric_name] = {
                "n_pairs": len(numeric),
                "mean_difference": mean(numeric) if numeric else None,
                "median_difference": median(numeric) if numeric else None,
                "batch_cluster_bootstrap_95_ci": [low, high],
                "bootstrap_samples": 5000 if low is not None else 0,
            }
    return report


def analyze(
    rows: Sequence[dict[str, Any]],
    missing: Sequence[str] = (),
    *,
    specs: Sequence[TrialSpec] | None = None,
) -> dict[str, Any]:
    if specs is not None:
        loaded = {str(row["trial_id"]): row for row in rows}
        analysis_rows = [
            loaded.get(spec.trial_id, {
                **spec.to_dict(),
                "status": "missing",
                "success": False,
                "event_time_s": None,
                "failure_reason": "missing_summary",
                "elapsed_s": None,
            })
            for spec in specs
        ]
    else:
        analysis_rows = list(rows)
    by_method: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_method_leg: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    by_method_batch: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in analysis_rows:
        method = str(row["method"])
        leg = str(row["fault_leg"])
        batch = str(row["batch_id"])
        by_method[method].append(row)
        by_method_leg[(method, leg)].append(row)
        by_method_batch[(method, batch)].append(row)
    return {
        "n_expected": len(analysis_rows),
        "n_loaded": len(rows),
        "n_missing": len(missing),
        "missing_trial_ids": list(missing),
        "methods": {method: summarize_group(group) for method, group in sorted(by_method.items())},
        "method_by_leg": {
            f"{method}|{leg}": summarize_group(group)
            for (method, leg), group in sorted(by_method_leg.items())
        },
        "method_by_batch": {
            f"{method}|{batch}": summarize_group(group)
            for (method, batch), group in sorted(by_method_batch.items())
        },
        "paired_contrasts": paired_contrasts(analysis_rows),
        "inference_policy": (
            "Descriptive only. Every manifested trial enters its study-specific "
            "success denominator. Scheduler failures receive deadline penalties; "
            "physical/integrated safety is fixed-horizon, not recovery time. Paired differences "
            "use deterministic batch-cluster bootstrap intervals, not confirmatory "
            "p-values. Successful-only latency is conditional and descriptive."
        ),
    }


def write_trial_csv(rows: Sequence[dict[str, Any]], path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    core = [
        "trial_id", "study", "method", "batch_id", "block_id", "order_index",
        "fault_leg", "seed", "status", "success", "event_time_s", "deadline_s",
        "failure_reason", "elapsed_s",
    ]
    extras = sorted({key for row in rows for key in row} - set(core))
    fields = core + extras
    with target.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            normalized = dict(row)
            normalized.setdefault("event_time_s", _event_time(row))
            for key, value in list(normalized.items()):
                if isinstance(value, (dict, list, tuple)):
                    normalized[key] = json.dumps(value, sort_keys=True)
            writer.writerow(normalized)
    return target


def estimate_remaining_seconds(
    specs: Sequence[TrialSpec], rows: Sequence[dict[str, Any]], *,
    overhead_fraction: float = 0.10,
    pilot_method_seconds: dict[str, float] | None = None,
) -> dict[str, Any]:
    # Workload and realtime mode change process duration by orders of magnitude.
    # Never use fast serialization/offline trials to estimate live plant runs.
    def eta_key(value: Any) -> str:
        if isinstance(value, TrialSpec):
            value = value.to_dict()
        return (f"{value.get('study')}|{value.get('method')}|{value.get('model')}|"
                f"{value.get('mission_num_predict')}|rt={value.get('realtime')}")
    completed_ids = {row["trial_id"] for row in rows if row.get("status") == "complete"}
    elapsed_by_method: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        elapsed = row.get("parent_wall_elapsed_s", row.get("elapsed_s"))
        if row.get("status") == "complete" and elapsed is not None:
            elapsed_by_method[eta_key(row)].append(float(elapsed))
    remaining = [spec for spec in specs if spec.trial_id not in completed_ids]
    estimate = 0.0
    unestimated = 0
    by_method: dict[str, dict[str, Any]] = {}
    for spec in remaining:
        key = eta_key(spec)
        samples = elapsed_by_method.get(key, [])
        if samples:
            expected = mean(samples)
            source = "completed-trial method mean"
        elif pilot_method_seconds and (key in pilot_method_seconds or spec.method in pilot_method_seconds):
            expected = float(pilot_method_seconds.get(key, pilot_method_seconds.get(spec.method)))
            source = "pre-campaign pilot method mean"
        else:
            expected = (
                spec.duration_s + 1.0 if spec.study in {"physics", "integrated"}
                else spec.deadline_s + spec.fault_offset_s + 5.0
            )
            source = "configured method-specific budget"
        entry = by_method.setdefault(key, {
            "remaining": 0, "seconds": 0.0,
            "observed_n": len(samples), "source": source,
        })
        entry["remaining"] += 1
        if expected is None:
            unestimated += 1
        else:
            estimate += expected
            entry["seconds"] += expected
    estimate *= 1.0 + overhead_fraction
    return {
        "completed": len(completed_ids),
        "remaining": len(remaining),
        "estimated_remaining_s": estimate if unestimated == 0 else None,
        "unestimated_trials": unestimated,
        "overhead_fraction": overhead_fraction,
        "by_method": by_method,
    }
