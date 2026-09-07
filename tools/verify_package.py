"""Check public-package payload hashes and frozen execution-source identity.

Standard library only; no simulator, LLM, network or filesystem writes.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from hera_v2.provenance import source_digest, source_files


def main() -> int:
    manifest = json.loads((ROOT / "release_manifest.json").read_text(encoding="utf-8"))
    errors = []
    paths = set()
    for item in manifest["files"]:
        relative = item["path"]
        path = (ROOT / relative).resolve()
        if relative in paths or not path.is_relative_to(ROOT):
            errors.append("Invalid or duplicate payload path: " + relative)
            continue
        paths.add(relative)
        if not path.is_file():
            errors.append("Missing payload: " + relative)
            continue
        payload = path.read_bytes()
        if len(payload) != item["bytes"] or hashlib.sha256(payload).hexdigest() != item["sha256"]:
            errors.append("Payload hash/size mismatch: " + relative)
    source_hash = source_digest(ROOT)
    source_count = len(source_files(ROOT))
    if source_hash != manifest["experiment_source_sha256"]:
        errors.append("Frozen execution-source fingerprint mismatch")
    if source_count != manifest["experiment_source_file_count"]:
        errors.append("Frozen execution-source file count mismatch")
    print(json.dumps({"ok": not errors, "payload_files_checked": len(paths),
                      "execution_source_files": source_count, "execution_source_sha256": source_hash,
                      "raw_evidence_checked": False, "errors": errors}, indent=2))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
