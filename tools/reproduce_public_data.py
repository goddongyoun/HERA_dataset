"""Reanalyse a restored HERA data archive without running new experiments.

Writes only new report directories outside the immutable campaign. Every
subprocess uses the current Python interpreter; no server is started or queried.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time


def read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve(strict=True)
    campaign = root / "runs/main_001"
    output = root / "paper/reproduced/public_data_checks"
    tables = root / "paper/generated/public_data_tables"
    if not (root / "PUBLIC_DATA_MANIFEST.json").is_file():
        parser.error("Expected an extracted public data archive, not a software-only checkout")
    initial_manifest = read(root / "PUBLIC_DATA_MANIFEST.json")
    initial_manifest_sha256 = digest(root / "PUBLIC_DATA_MANIFEST.json")
    if output.exists() or tables.exists():
        parser.error("Reanalysis output exists; extract into a fresh location (no overwrite)")
    output.mkdir(parents=True)
    records = []
    started = time.perf_counter()

    def run(name: str, script: str, *arguments: str) -> dict:
        command = [sys.executable, "-B", str(root / script), *map(str, arguments)]
        print(json.dumps({"started": name}), flush=True)
        begin = time.perf_counter()
        result = subprocess.run(command, cwd=root, capture_output=True,
                                text=True, encoding="utf-8", timeout=1200)
        (output / (name + ".stdout.log")).write_text(result.stdout, encoding="utf-8")
        (output / (name + ".stderr.log")).write_text(result.stderr, encoding="utf-8")
        record = {"check": name, "returncode": result.returncode,
                  "elapsed_s": time.perf_counter() - begin,
                  "stdout_sha256": digest(output / (name + ".stdout.log")),
                  "stderr_sha256": digest(output / (name + ".stderr.log"))}
        print(json.dumps(record), flush=True)
        return record

    def require_success(items):
        records.extend(items)
        failed = [item["check"] for item in items if item["returncode"] != 0]
        if failed:
            raise ValueError("Failed commands: " + ", ".join(failed))

    checks = {}
    errors = []
    try:
        require_success([run("archive_integrity", "tools/verify_public_data.py", "--root", root),
                         run("software_integrity", "tools/verify_package.py")])
        jobs = [
            ("qualification", "paper/run_qualification_checks.py", "--output-dir", output / "qualification"),
            ("saved_analysis", "paper/verify_saved_analysis.py", "--campaign-dir", campaign,
             "--output-dir", output / "analysis"),
            ("scheduler", "paper/build_scheduler_report.py", "--campaign-dir", campaign,
             "--output-dir", output / "scheduler"),
            ("physics", "paper/build_physics_report.py", "--campaign-dir", campaign,
             "--output-dir", output / "physics"),
            ("integrated", "paper/build_integrated_audit.py", "--campaign-dir", campaign,
             "--output-dir", output / "integrated"),
        ]
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(run, *job) for job in jobs]
            require_success([future.result() for future in futures])
        require_success([run("independent_scheduler", "paper/audit_scheduler_independent.py",
                             "--campaign-dir", campaign, "--scheduler-report", output / "scheduler/scheduler_report.json",
                             "--output-dir", output / "scheduler_independent")])
        require_success([run("tables", "paper/build_result_tables.py", "--mode", "main",
                             "--campaign-dir", campaign, "--scheduler-report", output / "scheduler/scheduler_report.json",
                             "--physics-report", output / "physics/report.json",
                             "--integrated-audit", output / "integrated/integrated_audit.json",
                             "--output-dir", tables)])
        qualification = read(output / "qualification/qualification.json")
        saved = read(output / "analysis/verification.json")
        scheduler = read(output / "scheduler/scheduler_report.json")
        independent = read(output / "scheduler_independent/scheduler_independent_audit.json")
        physics = read(output / "physics/report.json")
        integrated = read(output / "integrated/integrated_audit.json")
        claims = read(tables / "numeric_claims.json")
        original_claims = read(root / "results/tables_main/numeric_claims.json")
        checks = {
            "qualification_passed": qualification["passed"] and qualification["source_unchanged"],
            "tests_run": qualification["tests_run"],
            "execution_source_sha256": qualification["source_sha256"],
            "all_saved_analyses_match": saved["all_match"],
            "saved_analyses_checked": len(saved["manifests"]),
            "scheduler_trials": scheduler["n_trials"],
            "scheduler_audit_errors": len(scheduler["canonical_and_raw_timing_audit_errors"]),
            "independent_scheduler_valid": independent["valid"],
            "independent_scheduler_trials": independent["n_trials"],
            "physics_expected": physics["audit"]["expected"],
            "physics_verified": physics["audit"]["verified"],
            "physics_trace_samples": physics["audit"]["trace_rows"],
            "physics_audit_errors": len(physics["audit"]["errors"]),
            "integrated_expected": integrated["n_expected"],
            "integrated_audited": integrated["n_audited"],
            "integrated_audit_errors": len(integrated["audit_errors"]),
            "table_claims_exact": claims["tables"] == original_claims["tables"],
            "table_completion_checks_exact": claims["completion_checks"] == original_claims["completion_checks"],
            "tables_exact_text": all((tables / ("table_" + name + ".tex")).read_text(encoding="utf-8")
                                      == (root / "results/tables_main" / ("table_" + name + ".tex")).read_text(encoding="utf-8")
                                      for name in ("scheduler", "physics", "integrated", "realtime")),
        }
        expected = {"qualification_passed": True, "tests_run": 76,
                    "execution_source_sha256": "73b25c2272adadd37b4cfad44bc5cde3b8e2821d13f886f2b1996f8ca463f1c5",
                    "all_saved_analyses_match": True, "saved_analyses_checked": 11,
                    "scheduler_trials": 288, "scheduler_audit_errors": 0,
                    "independent_scheduler_valid": True, "independent_scheduler_trials": 288,
                    "physics_expected": 1184, "physics_verified": 1184,
                    "physics_trace_samples": 608000, "physics_audit_errors": 0,
                    "integrated_expected": 160, "integrated_audited": 160, "integrated_audit_errors": 0,
                    "table_claims_exact": True, "table_completion_checks_exact": True,
                    "tables_exact_text": True}
        errors.extend(key for key, value in expected.items() if checks.get(key) != value)
        # The strict archive verifier deliberately rejects extra files. Reports
        # now exist in the explicitly separate output directories, so check all
        # original payload bytes again without pretending this is a fresh ZIP.
        unchanged = digest(root / "PUBLIC_DATA_MANIFEST.json") == initial_manifest_sha256
        for item in initial_manifest["files"]:
            path = root / item["path"]
            unchanged = unchanged and path.is_file() and path.stat().st_size == item["bytes"] and digest(path) == item["sha256"]
        checks["archive_payload_unchanged_after_reanalysis"] = unchanged
        if not unchanged:
            errors.append("Archive input bytes changed during reanalysis")
    except Exception as exc:
        errors.append(str(exc))
    report = {"schema_version": 1, "created_utc": datetime.now(timezone.utc).isoformat(),
              "passed": not errors, "campaign_id": "main_001",
              "archive_manifest_sha256": digest(root / "PUBLIC_DATA_MANIFEST.json"),
              "elapsed_s": time.perf_counter() - started, "new_experiments_run": False,
              "checks": checks, "commands": records, "errors": errors}
    (output / "REANALYSIS_REPORT.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2), flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
