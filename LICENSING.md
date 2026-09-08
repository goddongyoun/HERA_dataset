# HERA licensing

Copyright (c) 2026 Dongyeon Kim and Hyunjun Jung.

HERA uses separate licences for software and scientific material. These grants apply only to material for which the HERA authors hold the relevant rights, subject to any explicit third-party notice.

## Software: MIT

The project source code, tests, execution and analysis scripts, and associated software documentation are licensed under the [MIT License](LICENSE) (SPDX identifier: `MIT`).

This covers all project-authored `.py` and `.ps1` files, including files in `hera_v2/`, `tests/`, `paper/`, `tools/`, `docs/`, `results/`, or a frozen execution-source snapshot. A script is software regardless of the directory containing it. It also covers software configuration, dependency lists, package-verification metadata, `README.md`, `PROTOCOL.md`, `docs/` instructions, and `CITATION.cff`, except scientific material covered below.

Existing frozen execution-source snapshots are covered without changing their bytes or inserting per-file headers. Preserve the MIT copyright and permission notice when redistributing copies or substantial portions of this software.

## Scientific material: CC BY 4.0

The project-authored scientific data, experimental manifests, result exports, scientific reports and tables, and figures are licensed under the [Creative Commons Attribution 4.0 International Public License](LICENSE-DATA) (SPDX identifier: `CC-BY-4.0`). This includes:

- experimental manifests and matrix data in `runs/manifests/`;
- numeric results, provenance records, verification results, scientific reports, and table exports in `results/`, including their `.json`, `.md`, and `.tex` representations;
- rendered figures and their editable scientific artwork in `figures/`;
- raw trial records, traces, event records, summaries, and scientific metadata in a HERA data archive that includes this licensing notice.

Software files remain MIT-licensed even when bundled with a data archive or located inside one of the directories above. Documentation describing scientific datasets or findings follows CC BY 4.0; instructions for operating the software follow MIT. The text of each licence governs its respective material. A software archive's `MIT` metadata field does not replace the CC BY 4.0 licence for included scientific material, and a dataset archive's `CC-BY-4.0` field does not replace the MIT licence for included scripts.

For attribution, identify **Dongyeon Kim and Hyunjun Jung**, the **HERA** project, the relevant repository or dataset record, and the CC BY 4.0 licence, and indicate changes. The repository is <https://github.com/goddongyoun/HERA_dataset>. The published data version is **HERA (2026), Zenodo, <https://doi.org/10.5281/zenodo.22652578>**. This dataset DOI is not a journal article DOI or a separately assigned software DOI. Publication-status wording does not change the licensing scope stated above.

## Third-party material and exclusions

Separately installed dependencies retain their own licences. The HERA licences do not relicense third-party software, model weights, logos, or other assets. No Ollama model weights, third-party MDPI class files/logos, manuscript draft, or reviewer correspondence are bundled in this repository. Honour any applicable third-party notices and obtain those components separately under their respective terms.

These grants do not assert rights over materials that are not included in this release. No modification of historical experiment records or frozen-source contents was required to apply this licensing notice.

## Canonical licence texts

- MIT: <https://opensource.org/license/mit>
- CC BY 4.0 legal text: <https://creativecommons.org/licenses/by/4.0/legalcode.en>
- Canonical CC BY 4.0 plain text, reproduced in `LICENSE-DATA`: <https://creativecommons.org/licenses/by/4.0/legalcode.txt>
