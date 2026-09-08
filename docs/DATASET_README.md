# HERA

Public-data package for **HERA: Bioinspired Separation of Local Fault Response and Language-Model Scheduling in Simulated Quadruped Control**.

This package covers the fixed `main_001` campaign of 1,472 trials: 288 scheduler trials, 960 offline physical trials, 64 paced physical-audit trials, and 160 integrated trials. Earlier development/pilot and legacy results are excluded. Packaging these existing observations does not run a new experiment.

Authors, in order: Dongyeon Kim (ORCID 0009-0006-8048-2696), Hyunjun Jung (ORCID 0000-0002-6717-1395). Repository: https://github.com/goddongyoun/HERA_dataset.

## Publication and licenses

This guide describes the separately prepared `HERA_dataset.zip`. A local ZIP or a reserved DOI is not a published record. Use only the actual published Zenodo URL and dataset-version DOI after publication; no DOI is invented here. The GitHub source archive alone does not contain these raw data.

Scientific data, reports and figures are licensed under **CC BY 4.0** (`LICENSE-DATA`). Bundled software and its associated software documentation are **MIT** (`LICENSE`). Consult `LICENSING.md` for the file/type boundaries; the data license does not replace the software license. Third-party dependencies and models retain their own licenses and are not bundled.

## Contents

- `runs/main_001/`: fixed campaign metadata, 11 manifest snapshots and saved analyses, frozen source snapshot, adopted/worker trial summaries, trial event logs, physical control traces and relevant batch lifecycle records.
- 1,472 trial event files and 1,184 physical/integrated trace files containing 608,000 samples.
- `paper/generated/server_main/`: bounded historical server evidence for 32 inference batches and 64 log prefixes, not live server access or proof of immediate GPU-slot release.
- Root scripts, `hera_v2/`, `tests/`, `paper/` and `tools/`: software for integrity checks and reanalysis. The historical module name is retained for source identity.
- `results/` and `figures/`: descriptive result previews and figures. Regenerate report inputs from the restored campaign before making tables.
- `PUBLIC_DATA_MANIFEST.json`: complete file inventory, source/public hashes, transformations and the exact included software commit.
- `docs/SOFTWARE_README.md`: README of the included software checkout; statements about raw-data absence there refer to Git, not to this separate data ZIP.

Manuscripts, journal templates, reviewer correspondence, internal handoff documents, credentials, models, virtual environments, unrelated campaigns and unnecessary console logs are not included. Do not substitute an earlier internal evidence ZIP.

## Privacy and provenance

Measured values, trial IDs, condition order, timestamps, original manifests and frozen execution source are preserved. Selected workstation paths and device identifiers in metadata are replaced by public markers. These files are public derivatives, not byte-identical originals; the manifest records both identities without publishing the removed identifiers.

Historical server-prefix hashes, sizes and offsets describe the captured original evidence. Public-prefix integrity is verified against separate public hashes and parsed control settings. Do not silently treat a transformed log as the original or infer GPU-compute timing from configuration logs.

## Verify and reproduce

Extract the ZIP into a new directory, then open a terminal in the extracted `HERA` folder. Use CPython 3.12. The recorded analysis environment and minimal requirements are included. No live Ollama server or model download is needed for these saved-data checks.

```powershell
python -B tools/verify_public_data.py --root .
python -B tools/verify_package.py
python -B -m unittest discover -s tests -v
python -B paper/verify_saved_analysis.py --campaign-dir runs/main_001 --output-dir paper/reproduced/analysis
python -B paper/build_scheduler_report.py --campaign-dir runs/main_001 --output-dir paper/reproduced/scheduler
python -B paper/build_physics_report.py --campaign-dir runs/main_001 --output-dir paper/reproduced/physics
python -B paper/build_integrated_audit.py --campaign-dir runs/main_001 --output-dir paper/reproduced/integrated
python -B paper/audit_scheduler_independent.py --campaign-dir runs/main_001 --scheduler-report paper/reproduced/scheduler/scheduler_report.json --output-dir paper/reproduced/scheduler_independent
python -B paper/build_result_tables.py --mode main --campaign-dir runs/main_001 --scheduler-report paper/reproduced/scheduler/scheduler_report.json --physics-report paper/reproduced/physics/report.json --integrated-audit paper/reproduced/integrated/integrated_audit.json --output-dir paper/generated/tables_reproduced
```

Install `requirements-analysis.txt` in a dedicated environment before running the tests and analysis generators. The package and raw-archive integrity verifiers use the Python standard library. Stop on any failure. Outputs must remain outside `runs/main_001`; do not alter source data to make a check pass.

Expected checks: 30 frozen execution files with source fingerprint `73b25c2272adadd37b4cfad44bc5cde3b8e2821d13f886f2b1996f8ca463f1c5`; 76 regression tests; all 11 saved analyses match; scheduler audits cover 288 trials; physical audit covers 1,184 traces and 608,000 samples; integrated audit covers 160 trials. Floating-point and rendering behavior may differ across environments; report differences rather than claim exact agreement without checking. The historical server collector is not a portable reanalysis command; the public archive verifier instead reads the included server evidence offline.

Alternatively, run `python -B tools/reproduce_public_data.py --root .` in a fresh extraction. This orchestrates the same saved-data audits, compares all four regenerated table files and their exact numerical claims with the included reference, and writes `paper/reproduced/public_data_checks/REANALYSIS_REPORT.json`. It refuses existing output directories and never starts a new experiment. Inspect its `passed` flag and every reported check; do not infer success from the presence of an output file alone.

## Interpretation limits

Preemption improved fully validated supervisory-command acceptance time in the tested configuration, not every latency endpoint. Complete faults were detected but every tested half-strength fault was missed. Fixed-horizon physical safety was at ceiling; local support improved mean upright deficit with adverse cases and reduced motion speed. All five integrated conditions had identical paired physical outcomes. These data do not establish additional LLM physical benefit, general diagnosis, restored locomotion or hardware/hard-real-time safety.

When citing the data, identify the actual dataset-version DOI and the matching software commit/release. Do not use the manuscript's DOI as the dataset DOI or substitute a software-only DOI for this raw-data deposit.
