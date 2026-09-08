# Data availability and release boundaries

## Current status

The `main_001` campaign completed all 1,472 manifested trials. Its full evidence is publicly archived in the **HERA dataset**, published on 8 September 2026 at [10.5281/zenodo.22652578](https://doi.org/10.5281/zenodo.22652578). The [GitHub repository](https://github.com/goddongyoun/HERA_dataset) contains software and selected result exports, while the separate Zenodo archive supplies the complete raw-data deposit. The DOI identifies the dataset version, not a journal article or separate software release.

Download [HERA_dataset.zip](https://zenodo.org/api/records/22652578/files/HERA_dataset.zip/content) and [HERA_dataset.sha256](https://zenodo.org/api/records/22652578/files/HERA_dataset.sha256/content) without authentication. The ZIP is 243,938,238 bytes and contains 7,466 payload files plus its complete manifest. Its SHA-256 is `59c9fff6af4e71de9dc4b500d497e3d326df14abdf95b2ef07f93cc7866ec1a5`. The package passed verification after extraction into a new location, including all 11 saved analyses, 76 regression tests, independent scheduler/physics/integration audits and exact comparison of all four tables. The full publicly served ZIP was downloaded and hashed on 8 September 2026. [DATA_RELEASE.json](DATA_RELEASE.json) records that check separately from the later metadata recheck.

Included in the **GitHub tree**: frozen execution code, fixed manifests, report-generation tools, exported descriptive reports, table sources, PNG/SVG figures and hash provenance. Report exports replace workstation prefixes with `SOURCE_WORKSPACE` and device UUIDs with `GPU-REDACTED`; their original and derived hashes are in `release_manifest.json`. Scientific identifiers such as trial IDs, model names and source hashes are retained. Numerical values are not filtered or recomputed during export. Earlier Git commits are not rewritten by this metadata cleanup.

Excluded from the **GitHub tree**: raw trial directories, event/trace JSONL, worker/adopted summaries, server-state/prefix logs and large archive files. The published **data ZIP** includes the main-campaign raw records and bounded server evidence. Model weights, Python environments, earlier campaigns, manuscript/MDPI template bundles and reviewer correspondence are excluded from both the current Git tree and the data deposit.

The historical 360-trial `full/` and `summary/` directories and their separate README have been removed from the current tree. They remain in earlier Git commits; history has not been rewritten. An archive of the current tree contains the HERA package without those legacy files. The historical data are not the raw evidence for `main_001`, and their old claims must not be substituted for current results.

## Evidence required for full reanalysis

The published raw-evidence deposit preserves these structures and their integrity relationships:

- `runs/main_001/campaign.json`, `progress.json`, `provenance.json`;
- `runs/main_001/manifests/` and `source_snapshot/`;
- all 11 saved `runs/main_001/analysis/<manifest>/analysis.json` files;
- complete adopted/worker attempt structure under `runs/main_001/trials/`, including both summary layers, events and traces;
- bounded server evidence needed to verify configuration and lifecycle claims.

The current raw campaign includes 1,184 trace JSONL files with 608,000 samples (898,417,644 bytes), and 1,472 event JSONL files (11,303,175 bytes). These are not the CSV files from the historical 360-trial release. Trace files alone are insufficient: the auditors check summary, event, trace, manifest and source hashes together.

The existing internal full-evidence ZIP also includes internal documents and workstation information. It is **not a publication-ready data deposit** and should not be uploaded wholesale. The separate public-data workflow builds `HERA_dataset.zip` using an explicit allowlist and omits manuscripts, reviewer correspondence and unrelated logs. See the [dataset guide](DATASET_README.md).

The public data license is CC BY 4.0; bundled software remains MIT under [LICENSING.md](../LICENSING.md). The public record lists Dongyeon Kim and Hyunjun Jung in that order with their confirmed ORCIDs. Publication and anonymous access were verified separately from these metadata and licence approvals.

The archive's `PUBLIC_DATA_MANIFEST.json` identifies every included file with original and public SHA-256 values, sizes and transformation descriptions. Measurement records and frozen execution source are preserved; selected workstation paths and device identifiers in metadata are removed from public derivatives. Historical server hashes and byte counts must not be confused with those of transformed public prefixes. The public verifier checks the archive and retained server evidence offline. Full fresh-location reanalysis is a separate release gate; a completed build alone is not a successful reanalysis.

## Citation and immutable archive

Cite the specific dataset-version DOI [10.5281/zenodo.22652578](https://doi.org/10.5281/zenodo.22652578) and the included software snapshot `82ae54b209389ef4917b857bdd14f9baf14ca322`, rather than an unspecified latest checkout. No separate software DOI is claimed or required to identify this deposited dataset. Later repository documentation updates do not change the archived source, data or ZIP checksum. The archive's bundled documentation records its pre-publication preparation stage; the current publication status is recorded here and in [DATA_RELEASE.json](DATA_RELEASE.json).
