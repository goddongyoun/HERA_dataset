# HERA v3 — code and results for `main_001`

Research code and descriptive results for **HERA: Bioinspired Separation of Local Fault Response and Language-Model Scheduling in Simulated Quadruped Control**.

This package covers the fixed **1,472-trial** revision-3 campaign: 288 scheduler trials, 960 offline physics trials, 64 real-time physics audit trials, and 160 integrated trials. Earlier development/pilot and legacy 360-trial results are not pooled into this release.

Repository: [goddongyoun/HERA_dataset](https://github.com/goddongyoun/HERA_dataset).

**Release status:** the v3 code and result package has been prepared for release; author metadata and licensing require author approval. No v3 release tag or DOI deposit is claimed by this preparation. The full **v3** raw-data archive is **not included or linked to a published deposit yet**. This checkout supports source inspection, regression tests and viewing result exports; full v3 saved-data reanalysis requires the separate evidence archive.

The existing `full/` and `summary/` directories are retained unchanged as **legacy 360-trial evidence**, not as raw data for the new 1,472-trial campaign. Their historical documentation is preserved in [the legacy README](docs/LEGACY_DATASET_README.md). Its old terminology, statistical tests, figure numbers and claims describe the previous submission and are not the current v3 conclusions. Do not combine the legacy and v3 results.

## Start here

- [Reproduction instructions](docs/REPRODUCE.md)
- [Data availability and excluded files](docs/DATA_AVAILABILITY.md)
- [GitHub upload and Zenodo DOI guide — 한국어](docs/GITHUB_ZENODO_KO.md)
- [Frozen protocol](PROTOCOL.md) and [deviations](docs/DEVIATIONS.md)
- [Licensing status](LICENSING.md)

## What the results support

HERA preemption reduced fully validated supervisory-command acceptance time relative to FIFO on the tested host and backend. Across the three workload caps, mean times were about 0.486–0.512 s for HERA, 1.476–11.101 s for FIFO, and 0.543–0.575 s for the parallel-slot baseline. The parallel-slot baseline had earlier dispatch/first-frame endpoints in the audited comparison; this is not superiority at every endpoint.

Local reflex actions reduced mean upright-deficit integral under full-strength-loss faults, with adverse cases retained, but reduced movement speed. All 1,184 physical/integrated trials met the tested fixed-horizon safety criterion, so this endpoint does not separate methods. Half-strength faults were missed in the tested partial-fault profile. Integrated supervisory commands reaffirmed the same action as the local reflex and showed **no additional physical benefit** in these trials.

These are descriptive, single-platform simulation results, not hardware safety guarantees, broad statistical generalization, autonomous LLM-discovered recovery, or restored locomotion. See the protocol and full report exports for definitions and limitations.

## Contents

| Location | Contents |
|---|---|
| Root Python/PowerShell files, `hera_v2/`, `tests/` | Byte-identical execution source from the final campaign snapshot; the historical module name is preserved |
| `runs/manifests/main_001/` | Fixed matrix and 11 original ordered manifests |
| `paper/` | Eight analysis, audit, table and figure-generation tools |
| `results/` | Report JSON/Markdown exports, four LaTeX tables, historical qualification/reanalysis records and provenance previews |
| `figures/` | PNG/SVG architecture and result figures, including supplementary plots |
| `tools/verify_package.py` | Local package and execution-source integrity check; no model inference |
| `release_manifest.json` | Original and public-copy SHA-256 values, transformations and payload inventory |
| `full/`, `summary/` | Preserved historical 360-trial data; outside the v3 payload manifest and not changed by the v3 addition |

## Lightweight checkout

The existing legacy `full/` tree is large. To obtain the current code and result exports without downloading those historical raw files, use a partial clone and sparse checkout:

```powershell
git clone --filter=blob:none --no-checkout https://github.com/goddongyoun/HERA_dataset.git
cd HERA_dataset
git sparse-checkout init --cone
git sparse-checkout set --skip-checks docs figures hera_v2 paper results runs tests tools summary
git checkout master
```

The v3 files must first be pushed to the remote before a fresh remote clone can retrieve them. Sparse checkout changes local materialization only: the legacy files remain tracked in Git and are still part of the repository tree. It does not remove them from a GitHub or Zenodo release archive.

## Quick check

Run from this folder using CPython 3.12. Creating a virtual environment is recommended.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-analysis.txt
.\.venv\Scripts\python.exe -B tools/verify_package.py
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v
```

The package verifier uses only the Python standard library. The tests additionally need the experiment dependencies; they do not require an Ollama server or the full saved-data archive. Installing packages requires network access. This package was validated on Windows; other platforms are not certified by that validation.

Experiment source fingerprint (30 execution files):

```text
73b25c2272adadd37b4cfad44bc5cde3b8e2821d13f886f2b1996f8ca463f1c5
```

Git attributes preserve code and manifest bytes. Do not rename `hera_v2`, add new root-level Python/PowerShell files, or format the frozen execution files if you need to preserve this fingerprint. Put new utilities under `tools/`.

## Report provenance and citation

Report exports under `results/` replace historical workstation prefixes with `SOURCE_WORKSPACE`; numeric/boolean/null values and list ordering are unchanged. These are explicitly identified **public derivatives**, not byte-identical raw evidence or portable inputs for the report generators. Regenerate reports from the immutable evidence at its new location before generating tables. Source and public-copy hashes are recorded separately.

`CITATION.cff` contains draft creator metadata and the existing repository URL for this v3 software package. No DOI, release version or publication date is invented. After author approval and publication, link the specific software release and the matching v3 raw-data deposit in this README and in the manuscript. A repository snapshot can also contain the preserved legacy data; its DOI must not be described as supplying the absent v3 raw evidence.
