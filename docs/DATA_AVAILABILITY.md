# Data availability and release boundaries

## Current status

The `main_001` campaign completed all 1,472 manifested trials. Its full evidence exists locally, but **no public v3 raw-evidence URL or DOI is supplied by this repository**. The v3 addition to [HERA_dataset](https://github.com/goddongyoun/HERA_dataset) is code plus result exports, not a complete v3 raw-data deposit. A DOI for this repository alone must not be represented as providing data that are absent from it.

Included: frozen execution code, fixed manifests, report-generation tools, exported descriptive reports, table sources, PNG/SVG figures and hash provenance. Report exports replace workstation prefixes with `SOURCE_WORKSPACE`; their original and derived hashes are in `release_manifest.json`. Identifiers such as trial IDs, model names and hashes are retained. Numerical values are not filtered or recomputed during export.

Excluded from the **v3 addition**: raw trial directories, event/trace JSONL, worker/adopted summaries, server-state/prefix logs, large archive files, model weights, Python environments, earlier campaigns, manuscript/MDPI template bundles and reviewer correspondence.

The repository's pre-existing Git history, `full/` and `summary/` are retained. Those directories contain the historical 360-trial study, not the raw evidence for `main_001`. Their contents and tree identities are unchanged by this addition. The legacy README is preserved separately; its historical claims are not current v3 results. A sparse local checkout may omit `full/`, but GitHub/Zenodo repository archives still include tracked legacy content unless the release scope is explicitly changed.

## Evidence required for full reanalysis

The future public raw-evidence deposit must preserve these original structures and their integrity relationships:

- `runs/main_001/campaign.json`, `progress.json`, `provenance.json`;
- `runs/main_001/manifests/` and `source_snapshot/`;
- all 11 saved `runs/main_001/analysis/<manifest>/analysis.json` files;
- complete adopted/worker attempt structure under `runs/main_001/trials/`, including both summary layers, events and traces;
- bounded server evidence needed to verify configuration and lifecycle claims.

The current raw campaign includes 1,184 trace JSONL files with 608,000 samples (898,417,644 bytes), and 1,472 event JSONL files (11,303,175 bytes). These are not the CSV files from the historical 360-trial release. Trace files alone are insufficient: the auditors check summary, event, trace, manifest and source hashes together.

The existing internal full-evidence ZIP also includes internal documents and workstation information. It is **not a publication-ready data deposit** and should not be uploaded wholesale. Prepare a separate allowlisted archive, review its privacy and licensing, and preserve the original scientific records. If redaction changes hash-linked evidence, provide an explicit transformation map and revalidate the public package; never silently replace values while retaining old hashes.

## After publication

The repository URL is known; the v3 release tag/commit citation, software version DOI, v3 raw-data version DOI, archive filename, byte size and SHA-256 still need to be finalized for publication. Test unauthenticated downloading and full fresh-location reanalysis before claiming that all v3 data are publicly available. The paper should cite the exact versions used, not an unspecified latest state.
