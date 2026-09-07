# HERA

Research code and descriptive results for **HERA: Bioinspired Separation of Local Fault Response and Language-Model Scheduling in Simulated Quadruped Control**.

This package covers the fixed **1,472-trial** `main_001` campaign: 288 scheduler trials, 960 offline physics trials, 64 real-time physics audit trials, and 160 integrated trials. Earlier development/pilot and legacy 360-trial results are not pooled into this release.

Repository: [goddongyoun/HERA_dataset](https://github.com/goddongyoun/HERA_dataset).

**Release status:** the HERA code and result package is available in this repository; author metadata and licensing require author approval. No release tag or DOI deposit is claimed. The full raw-data archive for `main_001` is **not included or linked to a published deposit yet**. This checkout supports source inspection, regression tests and viewing result exports; full saved-data reanalysis requires the separate evidence archive.

The current tree contains the HERA package for `main_001` only. The former 360-trial `full/` and `summary/` directories and their separate README have been removed from the current tree to avoid confusing the two studies. They remain recoverable from [earlier Git history](https://github.com/goddongyoun/HERA_dataset/tree/ccf0e78f9b577afbf44bc0fe34be796b86238ef2); no history rewrite was performed. Their historical terminology, tests and claims are not the current conclusions.

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

## Lightweight checkout

The current files are small, but earlier commits contain a large legacy raw-data tree. A partial clone obtains the current package without downloading every historical file blob:

```powershell
git clone --filter=blob:none https://github.com/goddongyoun/HERA_dataset.git
cd HERA_dataset
```

No sparse checkout is required for the current tree. Browsing or checking out an older commit can download its historical data on demand. Removing files from the current tree does not erase the old commits or reclaim all historical Git storage.

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

The protocol heading and architecture figure metadata use the public project name HERA; their original and updated hashes are recorded in `release_manifest.json`. These naming-only changes do not alter the protocol's scientific conditions, figure content, execution source or results. Historical module names, archive paths and provenance identifiers are retained for reproducibility.

`CITATION.cff` contains draft creator metadata and the existing repository URL for the HERA software package. No DOI, release version or publication date is invented. After author approval and archival release, link the specific software release and the matching raw-data deposit for `main_001` in this README and in the manuscript. A DOI for the current code and result package must not be described as supplying the absent raw evidence.
