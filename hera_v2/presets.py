"""Deterministic, model-order-safe control presets for the quadruped plant.

The dm_control quadruped does not expose twelve independent hip/knee position
actuators.  Its controls are, per leg, ``yaw``, ``lift`` tendon and ``extend``
tendon targets.  Keeping those semantics explicit prevents the rear-leg and
joint-label mistakes in the historical proof of concept.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol, Sequence

import numpy as np


CANONICAL_LEGS = ("FL", "FR", "BR", "BL")
ACTUATOR_KINDS = ("yaw", "lift", "extend")


class ActuatorLayoutLike(Protocol):
    """Minimal layout interface needed to convert named targets to an action."""

    def action_from_leg_targets(
        self, targets: Mapping[str, Sequence[float]]
    ) -> np.ndarray: ...


@dataclass(frozen=True, slots=True)
class PresetFrame:
    """One action held for an exact number of 50 Hz control steps."""

    name: str
    leg_targets: Mapping[str, tuple[float, float, float]]
    duration_steps: int

    def __post_init__(self) -> None:
        if self.duration_steps < 1:
            raise ValueError("duration_steps must be positive")
        if set(self.leg_targets) != set(CANONICAL_LEGS):
            raise ValueError(
                f"leg_targets must contain exactly {CANONICAL_LEGS}; "
                f"got {tuple(self.leg_targets)}"
            )
        for leg, target in self.leg_targets.items():
            if len(target) != len(ACTUATOR_KINDS):
                raise ValueError(f"{leg} must have yaw/lift/extend targets")
            if not np.all(np.isfinite(target)):
                raise ValueError(f"{leg} contains a non-finite target")

    def to_action(self, layout: ActuatorLayoutLike) -> np.ndarray:
        return layout.action_from_leg_targets(self.leg_targets)


@dataclass(frozen=True, slots=True)
class CyclicPreset:
    """A pure step-indexed preset; no hidden mutable phase state."""

    name: str
    frames: tuple[PresetFrame, ...]

    def __post_init__(self) -> None:
        if not self.frames:
            raise ValueError("A cyclic preset needs at least one frame")

    @property
    def cycle_steps(self) -> int:
        return sum(frame.duration_steps for frame in self.frames)

    def frame_at(self, step_index: int) -> PresetFrame:
        if step_index < 0:
            raise ValueError("step_index cannot be negative")
        offset = step_index % self.cycle_steps
        for frame in self.frames:
            if offset < frame.duration_steps:
                return frame
            offset -= frame.duration_steps
        raise AssertionError("unreachable cyclic preset state")

    def action_at(self, step_index: int, layout: ActuatorLayoutLike) -> np.ndarray:
        return self.frame_at(step_index).to_action(layout)


def _same_target(target: Sequence[float]) -> dict[str, tuple[float, float, float]]:
    values = tuple(float(value) for value in target)
    if len(values) != 3:
        raise ValueError("A leg target must be yaw/lift/extend")
    return {leg: values for leg in CANONICAL_LEGS}


def standing_preset(*, duration_steps: int = 1) -> PresetFrame:
    """Neutral stance using the model's yaw/lift/extend actuator semantics."""

    return PresetFrame(
        name="neutral_stand",
        leg_targets=_same_target((0.0, 0.0, -0.4)),
        duration_steps=duration_steps,
    )


def nominal_trot_preset(*, phase_steps: int = 10) -> CyclicPreset:
    """Return the corrected diagonal FL+BR / FR+BL open-loop gait.

    This is deliberately called a preset, not a CPG: it has no oscillator
    state, entrainment, or feedback adaptation.
    """

    swing = (0.0, 0.4, 0.3)
    push = (0.0, -0.3, -0.5)
    return CyclicPreset(
        name="corrected_diagonal_trot",
        frames=(
            PresetFrame(
                name="phase_FL_BR",
                leg_targets={"FL": swing, "FR": push, "BR": swing, "BL": push},
                duration_steps=phase_steps,
            ),
            PresetFrame(
                name="phase_FR_BL",
                leg_targets={"FL": push, "FR": swing, "BR": push, "BL": swing},
                duration_steps=phase_steps,
            ),
        ),
    )


def deterministic_stabilization_preset(
    fault_leg: str, *, duration_steps: int = 1
) -> PresetFrame:
    """Return a local, zero-inference tripod-support command.

    Healthy legs hold a slightly lower, compliant stance.  The failed leg is
    commanded to neutral but the plant-side degradation remains active, so
    this command cannot silently "repair" the simulated actuator.  This is a
    deterministic safety action and must not be described as an LLM-generated
    reflex or as guaranteed physical recovery.
    """

    if fault_leg not in CANONICAL_LEGS:
        raise ValueError(f"Unsupported fault leg: {fault_leg!r}")
    support = (0.0, -0.08, -0.48)
    failed = (0.0, 0.0, -0.4)
    targets = {leg: support for leg in CANONICAL_LEGS}
    targets[fault_leg] = failed
    return PresetFrame(
        name=f"local_tripod_stabilize_{fault_leg}",
        leg_targets=targets,
        duration_steps=duration_steps,
    )


__all__ = [
    "ACTUATOR_KINDS",
    "CANONICAL_LEGS",
    "CyclicPreset",
    "PresetFrame",
    "deterministic_stabilization_preset",
    "nominal_trot_preset",
    "standing_preset",
]
