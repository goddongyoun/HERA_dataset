"""Snapshot experiment code and identify it consistently in every trial."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import subprocess
import sys
import time
from datetime import datetime, timezone


def source_files(root: Path) -> list[Path]:
    return sorted(
        [*root.glob("*.py"), *root.glob("*.ps1"),
         *(root / "hera_v2").glob("*.py"), *(root / "tests").glob("*.py")],
        key=lambda path: path.relative_to(root).as_posix(),
    )


def source_digest(root: Path | None = None) -> str:
    root = root or Path(__file__).resolve().parent.parent
    digest = hashlib.sha256()
    for path in source_files(root):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def bind_campaign_source(campaign_dir: Path) -> dict:
    root = Path(__file__).resolve().parent.parent
    fingerprint = source_digest(root)
    target = campaign_dir / "provenance.json"
    if target.exists():
        value = json.loads(target.read_text(encoding="utf-8"))
        if value.get("source_sha256") != fingerprint:
            raise RuntimeError("Campaign code changed; use a new campaign directory")
        return value
    snapshots = campaign_dir / "source_snapshot"
    entries = []
    for path in source_files(root):
        relative = path.relative_to(root)
        destination = snapshots / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        payload = path.read_bytes()
        if destination.exists() and destination.read_bytes() != payload:
            raise RuntimeError(f"Conflicting source snapshot: {destination}")
        destination.write_bytes(payload)
        entries.append({"file": relative.as_posix(), "sha256": hashlib.sha256(payload).hexdigest()})
    for name in ("README.md", "PROTOCOL.md", "requirements-core.txt"):
        path = root / name
        if path.exists():
            (snapshots / name).write_bytes(path.read_bytes())
    packages = sorted(
        {f"{distribution.metadata['Name']}=={distribution.version}"
         for distribution in importlib.metadata.distributions()}
    )
    (snapshots / "requirements-environment.txt").write_text("\n".join(packages) + "\n", encoding="utf-8")
    gpu = subprocess.run(
        ["nvidia-smi", "--query-gpu=name,uuid,driver_version,memory.total", "--format=csv,noheader"],
        capture_output=True, text=True, timeout=10, check=False,
    )
    value = {
        "captured_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source_sha256": fingerprint, "source_files": entries,
        "python": sys.version, "platform": platform.platform(),
        "latency_clock": vars(time.get_clock_info("perf_counter")),
        "gpu": gpu.stdout.strip(), "packages": packages,
    }
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
    temporary.replace(target)
    return value
