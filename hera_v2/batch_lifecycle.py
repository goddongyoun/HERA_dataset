"""Recorded model reload, common warmup, and server verification per batch."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid

from .manifest import TrialSpec


def prepare_scheduler_batch(
    specs: list[TrialSpec], campaign_dir: Path, server_state: Path
) -> dict:
    models = {spec.model for spec in specs}
    batches = {spec.batch_id for spec in specs}
    if len(models) != 1 or len(batches) != 1:
        raise ValueError("Batch lifecycle requires one scheduler model and batch")
    model = next(iter(models))
    batch = next(iter(batches))
    state = json.loads(server_state.read_text(encoding="utf-8-sig"))
    hosts = sorted({str(server["host"]).rstrip("/") for server in state["servers"]})
    if not {spec.ollama_host.rstrip("/") for spec in specs} <= set(hosts):
        raise ValueError("Manifest endpoint is not among the dedicated servers")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    directory = campaign_dir / "batches" / batch / f"{stamp}-{uuid.uuid4().hex[:8]}"
    directory.mkdir(parents=True, exist_ok=False)
    root = Path(__file__).resolve().parent.parent
    started = time.perf_counter()
    commands = [
        [sys.executable, str(root / "warmup_servers.py"), "--hosts", *hosts,
         "--model", model, "--unload", "--output", str(directory / "warmup.json")],
        [sys.executable, str(root / "verify_servers.py"), "--state", str(server_state),
         "--model", model, "--output", str(directory / "verification.json")],
    ]
    with (directory / "stdout.log").open("wb") as stdout, (directory / "stderr.log").open("wb") as stderr:
        for command in commands:
            subprocess.run(command, stdout=stdout, stderr=stderr, timeout=240, check=True)
    result = {
        "batch_id": batch, "model": model, "hosts": hosts,
        "directory": str(directory), "elapsed_s": time.perf_counter() - started,
        "completed_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    (directory / "lifecycle.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result
