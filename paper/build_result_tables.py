"""Lightweight audited JSON-to-LaTeX conversion; no plotting or reanalysis.

Main mode requires a complete 1472-trial campaign. Pilot-test mode produces
conspicuously labeled tables in a pilot-named output directory only. Neither
mode edits the manuscript or any campaign/source/test artifact.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from statistics import mean
from typing import Any


PAPER = Path(__file__).resolve().parent
METHODS = ("hera_preempt", "fifo_single", "reserved_slot")
PHYSICS_METHODS = ("local_reflex", "no_reflex")
INTEGRATED_METHODS = ("local_only", *METHODS, "backend_failure")
PROFILES = ("full_clean", "partial_clean", "healthy_clean", "full_noisy", "healthy_noisy", "healthy_push")
LOADS = (256, 1024, 2048)
LABELS = {
    "hera_preempt": "Preempt", "fifo_single": "FIFO", "reserved_slot": "Two-slot",
    "local_reflex": "Local reflex", "no_reflex": "No reflex", "local_only": "Local only",
    "backend_failure": "Transport failure",
    "full_clean": "Full, clean", "partial_clean": "Partial, clean", "healthy_clean": "Healthy, clean",
    "full_noisy": "Full, noisy", "healthy_noisy": "Healthy, noisy", "healthy_push": "Healthy, push",
}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def pointer(*parts: Any) -> str:
    return "/" + "/".join(str(part).replace("~", "~0").replace("/", "~1") for part in parts)


def ref(source: str, *parts: Any) -> dict[str, str]:
    return {"input": source, "json_pointer": pointer(*parts)}


def number(value: float | None, *, digits: int = 3) -> str:
    if value is None:
        return "N/A"
    value = float(value)
    require(math.isfinite(value), "Non-finite table value")
    if 0 < abs(value) < 10 ** (-digits):
        mantissa, exponent = f"{value:.2e}".split("e")
        return rf"${mantissa}\times10^{{{int(exponent)}}}$"
    return f"{value:.{digits}f}"


def integer(value: Any) -> str:
    require(isinstance(value, int) and not isinstance(value, bool) and value >= 0,
            f"Expected nonnegative integer, got {value!r}")
    return str(value)


def cell(value: Any, display: str, refs: list[dict], *, unit: str = "count",
         applicable: bool = True, operation: str | None = None) -> dict:
    result = {"value": value, "display_latex": display, "unit": unit,
              "applicable": applicable, "source_refs": refs}
    if operation:
        result["operation"] = operation
    return result


class Inputs:
    def __init__(self) -> None:
        self.sources: dict[str, dict] = {}

    def read(self, name: str, path: Path) -> dict:
        path = path.resolve(strict=True)
        data = path.read_bytes()
        self.sources[name] = {"path": str(path), "sha256": digest(data), "bytes": len(data)}
        return json.loads(data.decode("utf-8-sig"))


def validate(inputs: dict, campaign: Path, mode: str) -> dict:
    scheduler, physics, integrated = (inputs[name] for name in ("scheduler", "physics", "integrated"))
    progress, provenance = inputs["progress"], inputs["provenance"]
    require(progress.get("runner_status") == "complete" and progress.get("phase") == "finished",
            "Campaign is not finished; no tables are emitted")
    require(progress.get("analysis", {}).get("status") == "complete", "Campaign analyses incomplete")
    require(progress["completed_trials"] == progress["total_trials"], "Incomplete trial accounting")
    require(not scheduler["canonical_and_raw_timing_audit_errors"], "Scheduler audit errors")
    require(not physics["audit"]["errors"], "Physics audit errors")
    require(physics["audit"]["expected"] == physics["audit"]["verified"] == len(physics["trials"]),
            "Physics report does not verify every expected record")
    require(not integrated["audit_errors"], "Integrated audit errors")
    require(integrated["n_expected"] == integrated["n_audited"] == len(integrated["trials"]),
            "Integrated audit does not account for every expected record")
    for recorded in (scheduler["campaign"], physics["report_provenance"]["campaign_dir"], integrated["campaign"]):
        require(Path(recorded).resolve() == campaign, "Mixed report/campaign paths")
    source_hash = provenance["source_sha256"]
    require(physics["campaign_provenance"]["experiment_provenance"]["source_sha256"] == source_hash,
            "Physics source provenance mismatch")
    require(integrated["source_sha256"] == source_hash, "Integrated source provenance mismatch")
    require(set(scheduler["workloads"]) == {str(value) for value in LOADS}, "Unexpected workload set")
    scheduler_n = 0
    for load in LOADS:
        require(set(scheduler["workloads"][str(load)]) == set(METHODS), "Unexpected scheduler methods")
        for method in METHODS:
            group = scheduler["workloads"][str(load)][method]
            n = group["n_expected"]
            require(n == group["n_manifested"] == group["n_complete"], "Incomplete scheduler group")
            require(group["n_missing"] == group["n_infrastructure_error"] == 0, "Scheduler infrastructure/missing rows")
            require(0 <= group["n_success"] <= n, "Impossible scheduler success count")
            scheduler_n += n
    require(scheduler_n == scheduler["n_trials"], "Scheduler report total mismatch")
    require(scheduler_n + physics["audit"]["expected"] == progress["total_trials"], "Study counts do not cover campaign")
    trial_ids = [row["spec"]["trial_id"] for row in physics["trials"]]
    require(len(trial_ids) == len(set(trial_ids)), "Duplicate physics/integrated record")
    for row in physics["trials"]:
        require(row["verified"] and not row["errors"], "Unverified physics record")
        require(row["recomputed"]["measurement_sync"] == "mj_forward_post_integration", "Pre-synchronization record")
    integrated_methods = [group["method"] for group in integrated["group_summaries"]]
    require(len(integrated_methods) == 5 and set(integrated_methods) == set(INTEGRATED_METHODS),
            "Integrated groups are duplicated or missing")
    if mode == "main":
        require(not campaign.name.lower().startswith("pilot"), "Pilot cannot be presented as main results")
        require(progress["total_trials"] == 1472 and scheduler_n == 288,
                "Main matrix does not match the fixed 1472-trial design")
        require(physics["audit"]["expected"] == 1184 and integrated["n_expected"] == 160,
                "Main physics/integration totals mismatch")
        for load in LOADS:
            for method in METHODS:
                require(scheduler["workloads"][str(load)][method]["n_expected"] == 32, "Main scheduler cell is not 32")
        for profile in PROFILES:
            for method in PHYSICS_METHODS:
                group = physics["summary"]["groups"][f"physics|{profile}|{method}"]
                require(group["n_expected"] == group["n_verified"] == 80, "Main offline cell is not 80")
                require(group["full_20_phase_coverage_verified"], "Main phase grid is incomplete")
        for method in PHYSICS_METHODS:
            require(physics["summary"]["groups"][f"physics|realtime_audit|{method}"]["n_expected"] == 32,
                    "Main realtime cell is not 32")
        require(all(group["n_trials"] == 32 for group in integrated["group_summaries"]), "Main integrated cell is not 32")
    else:
        require(campaign.name.lower().startswith("pilot"), "Pilot-test mode requires an explicitly pilot-named campaign")
    return {"campaign_finished": True, "analysis_complete": True, "all_report_audits_passed": True,
            "same_campaign_and_source": True, "total_trials": progress["total_trials"],
            "main_matrix_verified": mode == "main", "source_sha256": source_hash}


def scheduler_rows(report: dict) -> list[dict]:
    rows = []
    for load in LOADS:
        for method in METHODS:
            g = report["workloads"][str(load)][method]
            base = ("workloads", str(load), method)
            r = lambda field: ref("scheduler", *base, field)
            mu, sd = g["success_only_mean_s"], g["success_only_sd_s"]
            mean_sd_display = ("N/A" if mu is None else
                               f"{number(mu)} (SD N/A)" if sd is None else
                               f"{number(mu)} $\\pm$ {number(sd)}")
            fraction = g["retrospective_deadline_fraction"]["1"]
            require(0 <= fraction <= 1, "Invalid retrospective fraction")
            cells = {
                "load": cell(load, str(load), [ref("scheduler", "workloads", str(load))], unit="token cap"),
                "method": cell(method, LABELS[method], [ref("scheduler", *base)], unit="label"),
                "n": cell(g["n_expected"], integer(g["n_expected"]), [r("n_expected")]),
                "accepted": cell(g["n_success"], integer(g["n_success"]), [r("n_success")]),
                "penalized_mean": cell(g["deadline_penalized_mean_s"], number(g["deadline_penalized_mean_s"]), [r("deadline_penalized_mean_s")], unit="s"),
                "accepted_mean_sd": cell({"mean": mu, "sd": sd, "n": g["n_success"]},
                    mean_sd_display,
                    [r("success_only_mean_s"), r("success_only_sd_s"), r("n_success")], unit="s"),
                "accepted_median": cell(g["success_only_median_s"], number(g["success_only_median_s"]), [r("success_only_median_s")], unit="s"),
                "within_1s": cell(fraction, f"{100*fraction:.1f}\\%", [ref("scheduler", *base, "retrospective_deadline_fraction", "1")],
                                  unit="fraction", operation="display = 100 × fraction; denominator n_expected"),
            }
            rows.append({"id": f"t{load}_{method}", "cells": cells})
    return rows


def physics_rows(report: dict) -> list[dict]:
    rows = []
    for profile in PROFILES:
        for method in PHYSICS_METHODS:
            key = f"physics|{profile}|{method}"
            g = report["summary"]["groups"][key]
            base = ("summary", "groups", key)
            r = lambda *fields: ref("physics", *base, *fields)
            healthy = profile.startswith("healthy")
            n, det_n = g["n_expected"], g["detector_applicable_count"]
            require(g["n_verified"] == n, "Unverified physics denominator")
            require((det_n == 0) if healthy else (det_n == n), "Detector applicability denominator mismatch")
            deficit = g["metrics"]["upright_deficit_integral"]
            cells = {
                "profile": cell(profile, LABELS[profile], [r()], unit="label"),
                "method": cell(method, LABELS[method], [r()], unit="label"),
                "n": cell(n, integer(n), [r("n_expected")]),
                "safe": cell({"count": g["safety_success_count"], "n": n}, f"{g['safety_success_count']}/{n}", [r("safety_success_count"), r("n_expected")]),
                "detected": cell(None if healthy else {"count": g["detected_count"], "n": det_n},
                    "N/A" if healthy else f"{g['detected_count']}/{det_n}", [r("detected_count"), r("detector_applicable_count")], applicable=not healthy),
                "false_events_trials": cell({"events": g["false_trigger_event_count"], "trials": g["false_trigger_trial_count"]},
                    f"{g['false_trigger_event_count']} ({g['false_trigger_trial_count']})", [r("false_trigger_event_count"), r("false_trigger_trial_count")]),
                "deficit_mean": cell({"mean": deficit["mean"], "n": deficit["n"]}, number(deficit["mean"]),
                    [r("metrics", "upright_deficit_integral", "mean"), r("metrics", "upright_deficit_integral", "n")], unit="s"),
            }
            rows.append({"id": f"{profile}_{method}", "cells": cells,
                         "supplementary": {"detector_correct_count": g["detector_correct_count"],
                                           "healthy_exposure": g.get("healthy_exposure"),
                                           "reference_phases": g["reference_phases"]},
                         "supplementary_source": r()})
    return rows


def integrated_rows(report: dict) -> list[dict]:
    indexed = {g["method"]: (i, g) for i, g in enumerate(report["group_summaries"])}
    rows = []
    for method in INTEGRATED_METHODS:
        index, g = indexed[method]
        r = lambda *fields: ref("integrated", "group_summaries", index, *fields)
        local_only, failure = method == "local_only", method == "backend_failure"
        cells = {"method": cell(method, LABELS[method], [r("method")], unit="label"),
                 "n": cell(g["n_trials"], integer(g["n_trials"]), [r("n_trials")])}
        for name, field in (("accepted", "n_accepted"), ("applied", "n_applied"),
                            ("full_hold", "n_full_command_completion"), ("truncated", "n_horizon_censored"),
                            ("expiry", "n_expiry_observed")):
            applicable = not local_only and (not failure or name in {"accepted", "applied"})
            display = integer(g[field]) if applicable else "N/A"
            if failure and name in {"accepted", "applied"}:
                require(g[field] == 0, "Injected transport-failure condition unexpectedly delivered a command")
                display += r"$^{*}$"
            cells[name] = cell(g[field], display, [r(field)], applicable=applicable,
                              operation="expected zero under injected emergency transport failure" if failure else None)
        for name, field in (("local_delay", "local_detect_to_apply_ms"), ("supervisor_delay", "detect_to_apply_ms")):
            metric = g[field]
            applicable = name == "local_delay" or not (local_only or failure)
            cells[name] = cell(metric, f"{number(metric['mean'])} ({metric['n']})" if applicable else "N/A",
                               [r(field)], unit="ms", applicable=applicable)
        rows.append({"id": method, "cells": cells,
                     "supplementary": {"expiry_horizon_censored": g["n_expiry_horizon_censored"],
                                       "detect_to_accept_ms": g["detect_to_accept_ms"],
                                       "accept_to_apply_ms": g["accept_to_apply_ms"]},
                     "supplementary_source": r()})
    return rows


def realtime_rows(report: dict, campaign: Path, loaded: Inputs) -> list[dict]:
    rows = []
    input_hashes = report["campaign_provenance"]["input_sha256"]
    source_hash = report["campaign_provenance"]["experiment_provenance"]["source_sha256"]
    for method in PHYSICS_METHODS:
        selected = [(i, record) for i, record in enumerate(report["trials"])
                    if record["spec"]["study"] == "physics" and record["spec"]["method"] == method
                    and record["spec"].get("metadata", {}).get("profile") == "realtime_audit"]
        require(bool(selected), "No realtime audit rows")
        require(len(selected) == report["summary"]["groups"][f"physics|realtime_audit|{method}"]["n_expected"],
                "Realtime record count differs from audited group")
        observations = []
        for index, record in selected:
            relative = record["summary_path"]
            path = (campaign / relative).resolve(strict=True)
            require(path.is_relative_to(campaign), "Realtime summary escaped campaign directory")
            name = "realtime:" + record["spec"]["trial_id"]
            summary = loaded.read(name, path)
            require(loaded.sources[name]["sha256"] == input_hashes[relative], "Realtime summary changed since report audit")
            require(summary["status"] == "complete" and summary["trial_id"] == record["spec"]["trial_id"], "Realtime summary identity/status mismatch")
            require(summary["environment"]["source_sha256"] == source_hash, "Realtime summary source mismatch")
            raw = summary["result"]
            require(raw["measurement_sync"] == "mj_forward_post_integration", "Realtime measurement synchronization mismatch")
            observations.append((name, raw))
        cells = {"method": cell(method, LABELS[method], [ref("physics", "trials", i, "spec", "method") for i, _ in selected], unit="label"),
                 "n": cell(len(selected), str(len(selected)), [ref("physics", "trials", i) for i, _ in selected], operation="count selected verified realtime trial records")}
        for name, field, factor, is_max in (
            ("detect_sim", "detect_latency_sim_s", 1000, False),
            ("detect_wall", "detect_latency_ms", 1, False),
            ("local_sim", "reflex_dispatch_latency_sim_s", 1000, False),
            ("local_wall", "reflex_dispatch_latency_ms", 1, False),
            ("pacing_max", "max_pacing_lateness_ms", 1, True),
        ):
            applicable = not (method == "no_reflex" and name.startswith("local"))
            values = [raw[field]*factor for _, raw in observations if raw.get(field) is not None]
            statistic = (max(values) if is_max else mean(values)) if values else None
            val = {"maximum" if is_max else "mean": statistic, "n": len(values)}
            cells[name] = cell(val, f"{number(statistic)} ({len(values)})" if applicable else "N/A",
                [ref(source, "result", field) for source, _ in observations], unit="ms", applicable=applicable,
                operation=("maximum" if is_max else "arithmetic mean") + f" of non-null values × {factor}; n counts non-null values")
        rows.append({"id": method, "cells": cells})
    return rows


TABLES = {
    "scheduler": {
        "columns": ("load", "method", "n", "accepted", "penalized_mean", "accepted_mean_sd", "accepted_median", "within_1s"),
        "headers": ("Cap", "Method", "$N$", "Accepted", r"\shortstack{Pen. mean\\(s)}", r"\shortstack{Accepted mean\\$\pm$ SD (s)}", r"\shortstack{Accepted\\median (s)}", r"$\leq1$ s"),
        "caption": "Canonical-command scheduling by incumbent token cap. Pen. mean includes each nonaccepted manifested trial at its 30-s deadline; accepted-only summaries are conditional. The 1-s fraction retrospectively reuses these trials, not a separate timeout experiment.",
    },
    "physics": {
        "columns": ("profile", "method", "n", "safe", "detected", "false_events_trials", "deficit_mean"),
        "headers": ("Profile", "Method", "$N$", "Safe/$N$", "Detected/$N$", r"\shortstack{False events\\(trials)}", r"\shortstack{Mean deficit\\(s)}"),
        "caption": "Offline finite-grid outcomes. Detected denotes a recorded selected-leg postfault detection; healthy detection is not applicable. False-event parentheses count trials with at least one false trigger. Safe uses all manifested trials; deficit is the upright-deficit integral. These are finite-grid descriptions, not population-inference estimates.",
    },
    "integrated": {
        "columns": ("method", "n", "accepted", "applied", "full_hold", "truncated", "expiry", "local_delay", "supervisor_delay"),
        "headers": ("Method", "$N$", "Accept", "Apply", "Full", "Trunc.", "Expiry", r"\shortstack{Local mean\\ms ($n$)}", r"\shortstack{Supervisor mean\\ms ($n$)}"),
        "caption": "Integrated command delivery and observation. Full means all 50 requested ticks were observed; truncation retains the fixed horizon; expiry requires an observed subsequent local-fallback tick. Both delays start at the detector event and end at the relevant actual control tick. Parentheses give applicable timing counts. Local-only supervision is N/A; $^{*}$zero delivery is expected under the injected emergency transport failure, with duration/expiry endpoints N/A. Canonical reaffirmation does not establish extra physical benefit.",
    },
    "realtime": {
        "columns": ("method", "n", "detect_sim", "detect_wall", "local_sim", "local_wall", "pacing_max"),
        "headers": ("Method", "$N$", r"\shortstack{Detect sim.\\mean ms ($n$)}", r"\shortstack{Detect wall\\mean ms ($n$)}", r"\shortstack{Local sim.\\mean ms ($n$)}", r"\shortstack{Local wall\\mean ms ($n$)}", r"\shortstack{Max lateness\\ms ($n$)}"),
        "caption": "Separate realtime physics audit. Detection starts at fault injection; local dispatch starts at detection. Parentheses count non-null observations, with no-reflex application N/A. Simulation and wall clocks are distinct: zero simulation delay at an adjacent tick boundary is not zero wall latency. Maximum lateness is the largest observed trial maximum, not a hard-real-time bound.",
    },
}


def render_table(name: str, rows: list[dict], mode: str) -> str:
    template = TABLES[name]
    prefix = r"\textbf{PILOT TEST ONLY --- NOT MAIN RESULTS.} " if mode == "pilot-test" else ""
    align = "l" * (2 if name == "physics" else 1) + "r" * (len(template["columns"]) - (2 if name == "physics" else 1))
    if name == "scheduler":
        align = "rl" + "r" * (len(template["columns"]) - 2)
    lines = ["% Generated by build_result_tables.py; values/provenance: numeric_claims.json",
             r"\begin{table}[htbp]", r"\centering\small", r"\setlength{\tabcolsep}{3pt}",
             "\\caption{" + prefix + template["caption"] + "}",
             "\\label{tab:v3_" + name + ("_pilot_test" if mode == "pilot-test" else "") + "}",
             "\\begin{tabular}{" + align + "}", r"\toprule",
             " & ".join(template["headers"]) + r" \\", r"\midrule"]
    for row in rows:
        lines.append(" & ".join(row["cells"][column]["display_latex"] for column in template["columns"]) + r" \\")
    lines += [r"\bottomrule", r"\end{tabular}", r"\end{table}", ""]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("scheduler-report", "physics-report", "integrated-audit", "campaign-dir", "output-dir"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--mode", choices=("main", "pilot-test"), default="main")
    args = parser.parse_args()
    campaign = args.campaign_dir.resolve(strict=True)
    output = args.output_dir.resolve()
    generated = PAPER / "generated"
    require(output.is_relative_to(generated) and output != generated, "Output must be a named subdirectory of paper/generated")
    require(not output.is_relative_to(campaign), "Do not write into an immutable campaign")
    if args.mode == "pilot-test":
        require("pilot" in output.name.lower(), "Pilot-test output directory must be visibly pilot-named")
    else:
        require("pilot" not in output.name.lower(), "Main output must not reuse a pilot-named directory")
    loaded = Inputs()
    inputs = {
        "scheduler": loaded.read("scheduler", args.scheduler_report),
        "physics": loaded.read("physics", args.physics_report),
        "integrated": loaded.read("integrated", args.integrated_audit),
        "progress": loaded.read("progress", campaign / "progress.json"),
        "provenance": loaded.read("provenance", campaign / "provenance.json"),
    }
    checks = validate(inputs, campaign, args.mode)
    rows = {"scheduler": scheduler_rows(inputs["scheduler"]), "physics": physics_rows(inputs["physics"]),
            "integrated": integrated_rows(inputs["integrated"]),
            "realtime": realtime_rows(inputs["physics"], campaign, loaded)}
    require({name: len(value) for name, value in rows.items()} == {"scheduler": 9, "physics": 12, "integrated": 5, "realtime": 2}, "Unexpected table row counts")
    limitations = [
        "No data from another campaign are pooled. Pilot-test output is not main evidence.",
        "Scheduler penalized and accepted-only summaries are different estimands; no successful-only denominator substitution.",
        "Offline physics is a finite grid, not independent stochastic replication; no bootstrap or significance test is added here.",
        "Whole workload/profile groups execute in fixed order and use profile/load-specific seeds. Pairing is across methods within a group, not across loads, noise profiles or severities; between-group means do not isolate those factors causally.",
        "Healthy true detection and local-only supervisor metrics are N/A. Injected transport failure has expected supervisor absence.",
        "Integrated acceptance, application, full duration, horizon truncation, and observed fallback are distinct outcomes.",
        "Realtime simulation and wall delays have different origins/boundaries; no hard-real-time guarantee follows.",
        "This converter trusts the supplied completed audit reports and checks their identities/hashes; it does not rerun trace/physics analysis.",
        "Underlying JSON contains exact values; displayed table values are rounded and very small nonzero deficits use scientific notation.",
    ]
    report = {"schema_version": 1, "mode": args.mode, "main_results_eligible": args.mode == "main",
              "created_utc": datetime.now(timezone.utc).isoformat(), "campaign": str(campaign),
              "converter": {"path": str(Path(__file__).resolve()), "sha256": digest(Path(__file__).read_bytes())},
              "completion_checks": checks, "input_json": loaded.sources, "limitations": limitations,
              "tables": {name: {"tex_file": "table_" + name + ".tex", "caption": TABLES[name]["caption"],
                                 "rows": value} for name, value in rows.items()}}
    existing = output / "numeric_claims.json"
    if existing.is_file():
        old = json.loads(existing.read_text(encoding="utf-8"))
        require(old["mode"] == args.mode and Path(old["campaign"]).resolve() == campaign, "Refusing to mix output purposes or campaigns")
    rendered = {name: render_table(name, table_rows, args.mode) for name, table_rows in rows.items()}
    claim_text = json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    output.mkdir(parents=True, exist_ok=True)
    for name, text in rendered.items():
        (output / ("table_" + name + ".tex")).write_text(text, encoding="utf-8")
    (output / "numeric_claims.json").write_text(claim_text, encoding="utf-8")
    print(json.dumps({"mode": args.mode, "main_results_eligible": report["main_results_eligible"],
                      "rows": {name: len(value) for name, value in rows.items()}, "input_json_files": len(loaded.sources),
                      "output_dir": str(output)}))


if __name__ == "__main__":
    main()
