# Data availability and release boundaries

## Current status

The `main_001` campaign completed all 1,472 manifested trials. Its full evidence exists locally, but **no public raw-evidence URL or DOI for this campaign is supplied by this repository**. The HERA package in [HERA_dataset](https://github.com/goddongyoun/HERA_dataset) is code plus result exports, not a complete raw-data deposit. A DOI for this repository alone must not be represented as providing data that are absent from it.

The separately prepared `HERA_dataset.zip` is 243,938,238 bytes and contains 7,466 payload files plus its complete manifest. It passed verification after extraction into a new location, including all 11 saved analyses, 76 regression tests, independent scheduler/physics/integration audits and exact comparison of all four tables. [DATA_RELEASE.json](DATA_RELEASE.json) records its SHA-256, included software commit and check results. The archive is ready for upload, but publication and unauthenticated download verification remain outstanding.

Included: frozen execution code, fixed manifests, report-generation tools, exported descriptive reports, table sources, PNG/SVG figures and hash provenance. Report exports replace workstation prefixes with `SOURCE_WORKSPACE` and device UUIDs with `GPU-REDACTED`; their original and derived hashes are in `release_manifest.json`. Scientific identifiers such as trial IDs, model names and source hashes are retained. Numerical values are not filtered or recomputed during export. Earlier Git commits are not rewritten by this metadata cleanup.

Excluded from the **HERA package**: raw trial directories, event/trace JSONL, worker/adopted summaries, server-state/prefix logs, large archive files, model weights, Python environments, earlier campaigns, manuscript/MDPI template bundles and reviewer correspondence.

The historical 360-trial `full/` and `summary/` directories and their separate README have been removed from the current tree. They remain in earlier Git commits; history has not been rewritten. An archive of the current tree contains the HERA package without those legacy files. The historical data are not the raw evidence for `main_001`, and their old claims must not be substituted for current results.

## Evidence required for full reanalysis

The future public raw-evidence deposit must preserve these original structures and their integrity relationships:

- `runs/main_001/campaign.json`, `progress.json`, `provenance.json`;
- `runs/main_001/manifests/` and `source_snapshot/`;
- all 11 saved `runs/main_001/analysis/<manifest>/analysis.json` files;
- complete adopted/worker attempt structure under `runs/main_001/trials/`, including both summary layers, events and traces;
- bounded server evidence needed to verify configuration and lifecycle claims.

The current raw campaign includes 1,184 trace JSONL files with 608,000 samples (898,417,644 bytes), and 1,472 event JSONL files (11,303,175 bytes). These are not the CSV files from the historical 360-trial release. Trace files alone are insufficient: the auditors check summary, event, trace, manifest and source hashes together.

The existing internal full-evidence ZIP also includes internal documents and workstation information. It is **not a publication-ready data deposit** and should not be uploaded wholesale. The separate public-data workflow builds `HERA_dataset.zip` using an explicit allowlist and omits manuscripts, reviewer correspondence and unrelated logs. See the [dataset guide](DATASET_README.md).

The public data license is CC BY 4.0; bundled software remains MIT under [LICENSING.md](../LICENSING.md). Author names, order and existing ORCIDs have been confirmed. These approvals are not evidence that a Zenodo record has been published.

The archive's `PUBLIC_DATA_MANIFEST.json` identifies every included file with original and public SHA-256 values, sizes and transformation descriptions. Measurement records and frozen execution source are preserved; selected workstation paths and device identifiers in metadata are removed from public derivatives. Historical server hashes and byte counts must not be confused with those of transformed public prefixes. The public verifier checks the archive and retained server evidence offline. Full fresh-location reanalysis is a separate release gate; a completed build alone is not a successful reanalysis.

## After publication

The repository URL is known; the release tag/commit citation, software version DOI, raw-data version DOI for `main_001`, archive filename, byte size and SHA-256 still need to be finalized for publication. Test unauthenticated downloading and full fresh-location reanalysis before claiming that all campaign data are publicly available. The paper should cite the exact versions used, not an unspecified latest state.
