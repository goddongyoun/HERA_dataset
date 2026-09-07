from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path

from hera_v2.manifest import build_blocked_manifest, write_manifest


SCHEDULER_METHODS = ("hera_preempt", "fifo_single", "reserved_slot")
PHYSICS_METHODS = ("local_reflex", "no_reflex")


def main() -> int:
    parser = argparse.ArgumentParser(description="Create immutable HERA v2 randomized manifests")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--label", default="main")
    parser.add_argument("--batches", type=int, default=8)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--master-seed", type=int, default=20260907)
    parser.add_argument("--model", default="llama3.2:3b")
    parser.add_argument("--legs", nargs="+", default=["FL", "FR", "BR", "BL"])
    parser.add_argument("--single-host", default="http://127.0.0.1:11435")
    parser.add_argument("--reserved-host", default="http://127.0.0.1:11436")
    parser.add_argument("--scheduler-deadline", type=float, default=30.0)
    parser.add_argument("--scheduler-fault-offset", type=float, default=0.25)
    parser.add_argument("--mission-num-predict", type=int, default=1024)
    parser.add_argument("--physics-duration", type=float, default=10.0)
    parser.add_argument("--physics-fault-offset", type=float, default=3.0)
    parser.add_argument("--physics-realtime", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--physics-trace", action=argparse.BooleanOptionalAction, default=True)
    args = parser.parse_args()

    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    scheduler = build_blocked_manifest(
        study="scheduler",
        methods=SCHEDULER_METHODS,
        legs=args.legs,
        batches=args.batches,
        repeats_per_cell=args.repeats,
        master_seed=args.master_seed,
        model=args.model,
        deadline_s=args.scheduler_deadline,
        fault_offset_s=args.scheduler_fault_offset,
        duration_s=args.scheduler_deadline,
        realtime=False,
        ollama_host=args.single_host,
        mission_num_predict=args.mission_num_predict,
        metadata={
            "emergency_max_attempts": 1,
            "emergency_num_predict": 512,
            "protocol": "mission-first-chunk barrier; identical emergency schema/prompt/retry",
        },
    )
    scheduler = [
        replace(
            spec,
            ollama_host=args.reserved_host if spec.method == "reserved_slot" else args.single_host,
            metadata={
                **(spec.metadata or {}),
                "server_profile": "parallel_2_reserved" if spec.method == "reserved_slot" else "parallel_1_single",
            },
        )
        for spec in scheduler
    ]
    physics = build_blocked_manifest(
        study="physics",
        methods=PHYSICS_METHODS,
        legs=args.legs,
        batches=args.batches,
        repeats_per_cell=args.repeats,
        master_seed=args.master_seed + 1,
        model="none",
        deadline_s=max(0.001, args.physics_duration - args.physics_fault_offset),
        fault_offset_s=args.physics_fault_offset,
        duration_s=args.physics_duration,
        realtime=args.physics_realtime,
        metadata={
            "physics": {
                "fault_strength": 0.0,
                "settle_steps": 300,
                "record_trace": args.physics_trace,
            },
            "protocol": "plant-side gain/bias fault; persistent through measured trial",
        },
    )
    scheduler_path = write_manifest(scheduler, output / f"{args.label}_scheduler.jsonl")
    physics_path = write_manifest(physics, output / f"{args.label}_physics.jsonl")
    print(f"scheduler={scheduler_path} trials={len(scheduler)}")
    print(f"physics={physics_path} trials={len(physics)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
