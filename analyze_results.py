from __future__ import annotations

import argparse
import json
from pathlib import Path

from hera_v2.analysis import analyze, estimate_remaining_seconds, load_trial_summaries, write_trial_csv
from hera_v2.manifest import load_manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze exactly the trials in a HERA v2 manifest")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--results-dir", type=Path, required=True)
    parser.add_argument("--require-all", action="store_true")
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()

    specs = load_manifest(args.manifest)
    rows, missing = load_trial_summaries(specs, args.results_dir, require_all=args.require_all)
    report = analyze(rows, missing, specs=specs)
    report["eta"] = estimate_remaining_seconds(specs, rows)
    output_dir = args.output_dir or args.manifest.parent / "analysis"
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "analysis.json").write_text(
        json.dumps(report, indent=2, sort_keys=True), encoding="utf-8"
    )
    write_trial_csv(rows, output_dir / "trial_summary.csv")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
