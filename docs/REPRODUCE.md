# Reproduction guide

All commands below are run from the repository root. Use CPython 3.12 and install `requirements-analysis.txt` in a dedicated environment. The recorded experiment platform was Windows x64, Ollama 0.33.2, `llama3.2:3b`, dm-control 1.0.37 and MuJoCo 3.5.0. The full package list is in `requirements-environment.txt`; installing unrelated packages from that historical workstation inventory is not required for this package.

## 1. Checks possible with this checkout alone

```powershell
python -B tools/verify_package.py
python -B -m unittest discover -s tests -v
python -B paper/build_architecture_figure.py --output-dir paper/reproduced/architecture
```

The first command verifies 30 frozen execution files plus the copied/exported payload listed in `release_manifest.json`. It does not certify a raw-data archive. The regression tests are simulator/software checks, not a new main experiment or a statistical replication. They need no live Ollama backend. The architecture illustration needs no raw data.

## 2. Obtain the full immutable evidence

**This step is currently blocked for outside readers: the raw archive has not yet been published.** See [data availability](DATA_AVAILABILITY.md). Do not substitute result exports for raw trial evidence.

Once the public `HERA_dataset.zip` archive is available, download it and check its published SHA-256. Extract it into a separate `evidence/` directory, not over the repository. Its top-level folder is `HERA/`, so the campaign is `evidence/HERA/runs/main_001`. Do not substitute an older internal manuscript/evidence ZIP or let an archive overwrite this checkout's files.

The full campaign must include the files listed in `DATA_AVAILABILITY.md`, not just traces. The public archive retains measurement records, manifested conditions and frozen execution source unchanged; selected path/device metadata are disclosed public derivatives. `PUBLIC_DATA_MANIFEST.json` records original and public hashes. Verify the extracted archive before reanalysis:

```powershell
python -B evidence/HERA/tools/verify_public_data.py --root evidence/HERA
```

Original server-prefix byte counts and digests are historical provenance; public derivatives are checked separately by the archive verifier. Do not run the historical server-log collector to recreate old evidence. No live Ollama server is needed for archive verification.

## 3. Recalculate and audit saved results

No live model server or new experiment campaign is needed for these commands after the full evidence has been restored. In PowerShell:

```powershell
$heraCampaign = (Resolve-Path 'evidence/HERA/runs/main_001').Path
python -B paper/verify_saved_analysis.py --campaign-dir $heraCampaign --output-dir paper/reproduced/analysis
python -B paper/build_scheduler_report.py --campaign-dir $heraCampaign --output-dir paper/reproduced/scheduler
python -B paper/build_physics_report.py --campaign-dir $heraCampaign --output-dir paper/reproduced/physics
python -B paper/build_integrated_audit.py --campaign-dir $heraCampaign --output-dir paper/reproduced/integrated
python -B paper/audit_scheduler_independent.py --campaign-dir $heraCampaign --scheduler-report paper/reproduced/scheduler/scheduler_report.json --output-dir paper/reproduced/scheduler_independent
python -B paper/build_result_tables.py --mode main --campaign-dir $heraCampaign --scheduler-report paper/reproduced/scheduler/scheduler_report.json --physics-report paper/reproduced/physics/report.json --integrated-audit paper/reproduced/integrated/integrated_audit.json --output-dir paper/generated/tables_reproduced
```

Stop if any command fails. All outputs are separate from the immutable campaign. The table converter requires a named directory under `paper/generated/`, reads realtime summaries directly, and checks the current campaign path recorded in its input reports. Therefore regenerate the three reports before the tables. The privacy-processed JSON previews in `results/` must not be used as converter inputs.

Expected historical checks: all 11 saved analyses match exactly; independent scheduler checks cover 288 trials; the physics audit covers 1,184 physical/integrated trials and 608,000 trace samples; the integrated command audit covers 160 trials. Rendering and floating-point behavior can depend on the installed environment. Do not manufacture exact-match claims if a new environment differs.

## 4. Running new experiments is a separate workflow

Read `PROTOCOL.md`, `docs/DEVIATIONS.md`, `start_dedicated_servers.ps1` and `run_campaign.py --help` before starting. New runs need model availability, two dedicated local Ollama servers (single-slot and parallel-2 on the same GPU), warmup/verification and an appropriate host-load plan. These requirements do not apply to saved-data reanalysis.

Do not resume or overwrite the historical `main_001` campaign. Use a new campaign directory and freshly created server-state file. The main execution used the 11 original manifests in the order listed by `runs/manifests/main_001/matrix.json`, with `--trial-timeout-s 120`. Keep matrix/manifest bytes and execution source unchanged for a like-for-like replication. Do not launch all studies concurrently; this changes scheduling and timing conditions.

The public project name is HERA. The inherited `hera_v2` module name, some CLI descriptions and archive paths are historical identifiers retained for reproducibility, not a substitution of earlier results. This package preserves the final `main_001` execution snapshot exactly. Running the new experiment workflow is not necessary merely to inspect or upload this repository.
