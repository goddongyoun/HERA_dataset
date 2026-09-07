"""Write a fixed, versioned experiment matrix before paper-campaign execution."""
from __future__ import annotations
import argparse
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import random

from hera_v2.manifest import TrialSpec, write_manifest, validate_manifest

METHODS = ("hera_preempt", "fifo_single", "reserved_slot")
LEGS = ("FL", "FR", "BR", "BL")
NOISE = {"actuator_force_relative_std": .05, "joint_position_std_rad": .002,
         "joint_velocity_std_rad_s": .02, "gyro_std_rad_s": .01, "accel_std_m_s2": .05}
PROFILES = {
    "full_clean": {"fault_strength": 0.0},
    "partial_clean": {"fault_strength": .5},
    "healthy_clean": {"fault_enabled": False},
    "full_noisy": {"fault_strength": 0.0, "detector_noise": NOISE},
    "healthy_noisy": {"fault_enabled": False, "detector_noise": NOISE},
    "healthy_push": {"fault_enabled": False, "lateral_push_force_n": 40.0,
                     "lateral_push_start_s": 3.0, "lateral_push_duration_s": .2},
}


def seed(master: int, *parts: object) -> int:
    return int.from_bytes(hashlib.sha256("|".join(map(str, (master, *parts))).encode()).digest()[:4], "big") & 0x7fffffff


def make_matrix(master: int = 20260908, *, pilot: bool = False) -> dict[str, list[TrialSpec]]:
    result = {}
    def group(study, label, methods, blocks, phases, metadata, duration, fault_offset, realtime, tokens=1024):
        rows = []
        for block in range(blocks):
            cells = [(method, leg) for method in methods for leg in LEGS]
            random.Random(seed(master, label, block, "order")).shuffle(cells)
            batch = f"{label}-b{block+1:03d}"
            for within, (method, leg) in enumerate(cells):
                scenario_seed = seed(master, label, block, leg, "scenario")
                meta = json.loads(json.dumps(metadata))
                if phases is not None:
                    meta.setdefault("physics", {})["gait_phase_offset_steps"] = phases[block]
                trial_id = f"{batch}-o{within+1:02d}-{method}-{leg}-s{scenario_seed:010d}"
                rows.append(TrialSpec(
                    trial_id=trial_id, batch_id=batch, block_id=batch, order_index=len(rows),
                    study=study, method=method, fault_leg=leg, seed=scenario_seed,
                    model="none" if study == "physics" else "llama3.2:3b",
                    deadline_s=duration-fault_offset if study in {"physics", "integrated"} else 30.0,
                    fault_offset_s=fault_offset, duration_s=duration, realtime=realtime,
                    ollama_host="http://127.0.0.1:11436" if method == "reserved_slot" else "http://127.0.0.1:11435",
                    mission_num_predict=tokens, metadata=meta,
                ))
        validate_manifest(rows)
        result[label] = rows

    for tokens in (256, 1024, 2048):
        group("scheduler", f"scheduler_t{tokens}", METHODS, 1 if pilot else 8, None,
              {"contract_version": 3, "mission_token_budget": tokens, "emergency_max_attempts": 1,
               "emergency_num_predict": 512, "interpretation": "canonical command serialization benchmark"},
              30.0, .25, False, tokens)
    for profile, options in PROFILES.items():
        phases = [0, 10] if pilot else list(range(20))
        group("physics", f"physics_{profile}", ("local_reflex", "no_reflex"), len(phases), phases,
              {"physics": {"record_trace": True, **options}, "profile": profile,
               "inference_unit": "enumerated phase/leg scenario; not independent stochastic replication"},
              10.0, 3.0, False)
    phases = [0] if pilot else [0, 2, 5, 7, 10, 12, 15, 17]
    group("physics", "physics_realtime", ("local_reflex", "no_reflex"), len(phases), phases,
          {"physics": {"record_trace": True, "fault_strength": 0.0}, "profile": "realtime_audit"},
          10.0, 3.0, True)
    group("integrated", "integrated", ("local_only", *METHODS, "backend_failure"),
          1 if pilot else 8, phases,
          {"physics": {"record_trace": True, "fault_strength": 0.0}, "contract_version": 3,
           "purpose": "measured trigger, independent local reflex, validated supervisory hold application",
           "supervisor_duration_steps": 50}, 12.0, 3.0, True, 2048)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--pilot", action="store_true")
    parser.add_argument("--master-seed", type=int, default=20260908)
    args = parser.parse_args()
    if args.output_dir.exists():
        raise FileExistsError("Use a new immutable manifest directory")
    matrix = make_matrix(args.master_seed, pilot=args.pilot)
    args.output_dir.mkdir(parents=True)
    entries = []
    for label, rows in matrix.items():
        path = write_manifest(rows, args.output_dir / f"{label}.jsonl")
        entries.append({"name": label, "trials": len(rows), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    plan = {"version": 3, "pilot": args.pilot, "master_seed": args.master_seed,
            "total_trials": sum(e["trials"] for e in entries), "manifests": entries,
            "no_optional_stopping": True, "descriptive_not_confirmatory": True}
    (args.output_dir / "matrix.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
    print(json.dumps(plan, indent=2))


if __name__ == "__main__":
    main()
