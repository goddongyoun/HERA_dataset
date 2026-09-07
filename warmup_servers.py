"""Unload and identically warm dedicated Ollama endpoints before a batch."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from typing import Any

import httpx

from hera_v2.scheduler import (
    EMERGENCY_SYSTEM_PROMPT,
    MISSION_SYSTEM_PROMPT,
    EmergencyContext,
    build_emergency_prompt,
    build_emergency_schema,
)


MISSION_WARMUP_PROMPT = (
    "Calibration request. Produce a numbered quadruped inspection plan containing "
    "navigation, sensing, risk, and fallback actions."
)


def _emergency_context() -> EmergencyContext:
    return EmergencyContext(
        fault_label="calibration actuator degradation",
        affected_channels=(0, 1, 2),
        observed_values=(0.0, 0.0, 0.0),
        safe_action=(
            0.0, 0.0, -0.4,
            0.0, -0.08, -0.48,
            0.0, -0.08, -0.48,
            0.0, -0.08, -0.48,
        ),
    )


async def _post(client: httpx.AsyncClient, url: str, payload: dict[str, Any]) -> dict[str, Any]:
    response = await client.post(url, json=payload)
    response.raise_for_status()
    value = response.json()
    if value.get("error"):
        raise RuntimeError(str(value["error"]))
    return value


async def warm_host(host: str, model: str, *, unload: bool) -> dict[str, Any]:
    host = host.rstrip("/")
    started = time.perf_counter()
    async with httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=5.0)) as client:
        if unload:
            await _post(
                client,
                f"{host}/api/generate",
                {"model": model, "prompt": "", "stream": False, "keep_alive": 0},
            )
        mission = await _post(
            client,
            f"{host}/api/chat",
            {
                "model": model,
                "messages": [
                    {"role": "system", "content": MISSION_SYSTEM_PROMPT},
                    {"role": "user", "content": MISSION_WARMUP_PROMPT},
                ],
                "stream": False,
                "think": False,
                "keep_alive": "30m",
                "options": {"temperature": 0.0, "seed": 0, "num_predict": 8},
            },
        )
        emergency_context = _emergency_context()
        emergency = await _post(
            client,
            f"{host}/api/chat",
            {
                "model": model,
                "messages": [
                    {"role": "system", "content": EMERGENCY_SYSTEM_PROMPT},
                    {"role": "user", "content": build_emergency_prompt(emergency_context)},
                ],
                "format": build_emergency_schema(emergency_context),
                "stream": False,
                "think": False,
                "keep_alive": "30m",
                "options": {"temperature": 0.0, "seed": 1, "num_predict": 256},
            },
        )
    return {
        "host": host,
        "elapsed_s": time.perf_counter() - started,
        "mission_load_duration_ns": mission.get("load_duration"),
        "mission_prompt_eval_count": mission.get("prompt_eval_count"),
        "emergency_eval_count": emergency.get("eval_count"),
    }


async def _main_async(args: argparse.Namespace) -> list[dict[str, Any]]:
    return await asyncio.gather(
        *(warm_host(host, args.model, unload=args.unload) for host in args.hosts)
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hosts", nargs="+", required=True)
    parser.add_argument("--model", default="llama3.2:3b")
    parser.add_argument("--unload", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    rows = asyncio.run(_main_async(args))
    payload = {
        "completed_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "model": args.model,
        "unloaded_first": args.unload,
        "hosts": rows,
    }
    rendered = json.dumps(payload, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix(args.output.suffix + ".tmp")
        temporary.write_text(rendered + "\n", encoding="utf-8")
        temporary.replace(args.output)
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
