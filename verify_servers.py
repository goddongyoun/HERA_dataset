"""Fail closed unless dedicated Ollama runners match the declared profiles."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any

import httpx


RUNNER_PATTERN = re.compile(
    r"finished setting up.*?runner\.name=(?P<model>\S+).*?"
    r"runner\.parallel=(?P<parallel>\d+).*?runner\.pid=(?P<runner_pid>\d+).*?"
    r"runner\.num_ctx=(?P<num_ctx>\d+)"
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _last_runner(log_path: Path, model: str) -> dict[str, Any]:
    match_value: re.Match[str] | None = None
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = RUNNER_PATTERN.search(line)
        if match and match.group("model").endswith(f"/{model}"):
            match_value = match
    if match_value is None:
        raise RuntimeError(f"No finished runner record for {model!r} in {log_path}")
    return {
        "model": match_value.group("model"),
        "parallel": int(match_value.group("parallel")),
        "runner_pid": int(match_value.group("runner_pid")),
        "num_ctx": int(match_value.group("num_ctx")),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--state",
        type=Path,
        default=Path(__file__).resolve().parent / "runs" / "servers" / "servers.current.json",
    )
    parser.add_argument("--model", default="llama3.2:3b")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    state = json.loads(args.state.read_text(encoding="utf-8-sig"))
    rows: list[dict[str, Any]] = []
    failures: list[str] = []
    for server in state["servers"]:
        log_path = Path(server["stderr"])
        runner = _last_runner(log_path, args.model)
        expected_parallel = int(server["parallel"])
        if runner["parallel"] != expected_parallel:
            failures.append(
                f"{server['name']}: expected parallel={expected_parallel}, "
                f"observed {runner['parallel']}"
            )
        if runner["num_ctx"] != int(server["context_length"]):
            failures.append(
                f"{server['name']}: expected num_ctx={server['context_length']}, "
                f"observed {runner['num_ctx']}"
            )
        host = str(server["host"]).rstrip("/")
        response = httpx.get(f"{host}/api/version", timeout=5.0)
        response.raise_for_status()
        rows.append(
            {
                **server,
                "api_version": response.json().get("version"),
                "runner": runner,
                "stderr_sha256_at_verification": _sha256(log_path),
                "stderr_size_at_verification": log_path.stat().st_size,
            }
        )

    payload = {
        "verified_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "requested_model": args.model,
        "state_file": str(args.state.resolve()),
        "state_sha256": _sha256(args.state),
        "valid": not failures,
        "failures": failures,
        "servers": rows,
    }
    rendered = json.dumps(payload, indent=2, sort_keys=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(rendered + "\n", encoding="utf-8")
    temporary.replace(args.output)
    print(rendered)
    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
