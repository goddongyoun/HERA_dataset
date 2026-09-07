"""Deterministic, exact trial manifests for revision-v2 experiments.

The historical runners selected results with filename globs and timestamp
ranges.  This module makes the manifest the only source of truth: every trial
is named before execution, its randomized order is recorded, and consumers
reject duplicate or malformed entries.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
import hashlib
import json
import math
from pathlib import Path
import random
from typing import Any, Iterable, Mapping, Sequence


CANONICAL_LEGS = ("FL", "FR", "BR", "BL")


@dataclass(frozen=True, slots=True)
class TrialSpec:
    trial_id: str
    batch_id: str
    block_id: str
    order_index: int
    study: str
    method: str
    fault_leg: str
    seed: int
    model: str
    deadline_s: float
    fault_offset_s: float
    duration_s: float
    realtime: bool
    ollama_host: str = "http://127.0.0.1:11434"
    mission_num_predict: int = 1024
    metadata: dict[str, Any] | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "TrialSpec":
        allowed = {field.name for field in fields(cls)}
        unknown = set(value) - allowed
        if unknown:
            raise ValueError(f"Unknown TrialSpec field(s): {sorted(unknown)}")
        return cls(**dict(value))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def canonical_json_bytes(value: Any) -> bytes:
    """Encode experiment identity data in one stable, strict JSON form."""

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def trial_spec_sha256(spec: TrialSpec) -> str:
    """Return the identity hash for every parameter of one manifested trial."""

    return hashlib.sha256(canonical_json_bytes(spec.to_dict())).hexdigest()


def manifest_file_sha256(path: str | Path) -> str:
    """Hash the exact manifest bytes used by a campaign."""

    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _stable_seed(master_seed: int, *parts: object) -> int:
    payload = "|".join([str(master_seed), *(str(part) for part in parts)])
    digest = hashlib.sha256(payload.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") & 0x7FFFFFFF


def build_blocked_manifest(
    *,
    study: str,
    methods: Sequence[str],
    legs: Sequence[str] = CANONICAL_LEGS,
    batches: int,
    repeats_per_cell: int,
    master_seed: int,
    model: str,
    deadline_s: float,
    fault_offset_s: float,
    duration_s: float,
    realtime: bool,
    ollama_host: str = "http://127.0.0.1:11434",
    mission_num_predict: int = 1024,
    metadata: Mapping[str, Any] | None = None,
) -> list[TrialSpec]:
    """Create randomized complete blocks.

    Each ``batch x repeat`` block contains every ``method x leg`` condition
    exactly once.  The seeded shuffle is reproducible, but no method is tied to
    a fixed run position.  Simulation seeds are condition-independent within a
    block/leg so methods can be paired on the same scenario.
    """
    if not study.strip():
        raise ValueError("study must be non-empty")
    if batches < 1 or repeats_per_cell < 1:
        raise ValueError("batches and repeats_per_cell must be positive")
    if not methods or len(set(methods)) != len(methods):
        raise ValueError("methods must be non-empty and unique")
    if not legs or len(set(legs)) != len(legs):
        raise ValueError("legs must be non-empty and unique")
    invalid_legs = set(legs) - set(CANONICAL_LEGS)
    if invalid_legs:
        raise ValueError(f"Unsupported leg(s): {sorted(invalid_legs)}")
    if deadline_s <= 0 or duration_s <= 0:
        raise ValueError("deadline_s and duration_s must be positive")
    if fault_offset_s < 0:
        raise ValueError("fault_offset_s cannot be negative")

    manifest: list[TrialSpec] = []
    global_order = 0
    for batch_index in range(batches):
        batch_id = f"{study}-b{batch_index + 1:03d}"
        for repeat_index in range(repeats_per_cell):
            block_id = f"{batch_id}-r{repeat_index + 1:02d}"
            cells = [(method, leg) for method in methods for leg in legs]
            rng = random.Random(_stable_seed(master_seed, study, batch_index, repeat_index, "order"))
            rng.shuffle(cells)
            for block_order, (method, leg) in enumerate(cells):
                scenario_seed = _stable_seed(master_seed, study, batch_index, repeat_index, leg, "scenario")
                trial_id = (
                    f"{block_id}-o{block_order + 1:02d}-"
                    f"{method}-{leg}-s{scenario_seed:010d}"
                )
                manifest.append(
                    TrialSpec(
                        trial_id=trial_id,
                        batch_id=batch_id,
                        block_id=block_id,
                        order_index=global_order,
                        study=study,
                        method=method,
                        fault_leg=leg,
                        seed=scenario_seed,
                        model=model,
                        deadline_s=float(deadline_s),
                        fault_offset_s=float(fault_offset_s),
                        duration_s=float(duration_s),
                        realtime=bool(realtime),
                        ollama_host=ollama_host.rstrip("/"),
                        mission_num_predict=int(mission_num_predict),
                        metadata=dict(metadata or {}),
                    )
                )
                global_order += 1
    validate_manifest(manifest)
    return manifest


def validate_manifest(specs: Sequence[TrialSpec]) -> None:
    if not specs:
        raise ValueError("Manifest is empty")
    ids = [spec.trial_id for spec in specs]
    if len(ids) != len(set(ids)):
        duplicates = sorted({trial_id for trial_id in ids if ids.count(trial_id) > 1})
        raise ValueError(f"Duplicate trial_id(s): {duplicates}")
    order = [spec.order_index for spec in specs]
    if order != list(range(len(specs))):
        raise ValueError(
            "manifest row order must match order_index exactly (0..N-1)"
        )
    for spec in specs:
        if spec.fault_leg not in CANONICAL_LEGS:
            raise ValueError(f"Invalid fault_leg in {spec.trial_id}: {spec.fault_leg}")
        numeric = (spec.deadline_s, spec.duration_s, spec.fault_offset_s)
        if not all(math.isfinite(float(value)) for value in numeric):
            raise ValueError(f"Non-finite timing value in {spec.trial_id}")
        if spec.deadline_s <= 0 or spec.duration_s <= 0:
            raise ValueError(f"Invalid duration in {spec.trial_id}")
        if spec.fault_offset_s < 0:
            raise ValueError(f"Invalid fault offset in {spec.trial_id}")
        if spec.mission_num_predict < 1:
            raise ValueError(f"Invalid mission_num_predict in {spec.trial_id}")


def write_manifest(specs: Sequence[TrialSpec], path: str | Path) -> Path:
    validate_manifest(specs)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = "\n".join(json.dumps(spec.to_dict(), sort_keys=True) for spec in specs) + "\n"
    target.write_text(payload, encoding="utf-8")
    return target


def load_manifest(path: str | Path) -> list[TrialSpec]:
    source = Path(path)
    specs: list[TrialSpec] = []
    for line_number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON at {source}:{line_number}: {exc}") from exc
        specs.append(TrialSpec.from_mapping(raw))
    validate_manifest(specs)
    return specs


def pending_trials(specs: Iterable[TrialSpec], results_dir: str | Path) -> list[TrialSpec]:
    base = Path(results_dir)
    pending: list[TrialSpec] = []
    for spec in specs:
        summary_path = base / spec.trial_id / "summary.json"
        if not summary_path.exists():
            pending.append(spec)
            continue
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pending.append(spec)
            continue
        if (
            summary.get("trial_id") != spec.trial_id
            or summary.get("status") != "complete"
            or summary.get("trial_spec_sha256") != trial_spec_sha256(spec)
        ):
            pending.append(spec)
    return pending
