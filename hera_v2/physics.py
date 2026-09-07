"""Measured-state quadruped plant and deterministic safety experiment.

This module is intentionally independent of the historical root scripts.  It
uses one reset/settle phase, never resets during a measured trial, keeps
actuator degradation in the MuJoCo plant rather than rewriting controller
commands, and records all latency-relevant times with a monotonic clock.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field, fields, replace
import hashlib
import math
import time
from typing import Any, Callable, Literal, Mapping, Protocol, Sequence

import numpy as np

from .events import EventIdentity
from .presets import (
    ACTUATOR_KINDS,
    CANONICAL_LEGS,
    deterministic_stabilization_preset,
    nominal_trot_preset,
    standing_preset,
)


LEG_SUFFIX = {
    "FL": "front_left",
    "FR": "front_right",
    "BR": "back_right",
    "BL": "back_left",
}
JOINT_KINDS = ("yaw", "pitch", "knee", "ankle")
MEASUREMENT_SYNC = "mj_forward_post_integration"


class LayoutError(ValueError):
    """The loaded model does not have the expected named quadruped layout."""


class EpisodeTerminatedError(RuntimeError):
    """The environment ended during a measured trial; it must not be reset."""


class EventLoggerLike(Protocol):
    def emit(
        self,
        stage: str,
        identity: EventIdentity,
        details: Mapping[str, Any] | None = None,
    ) -> Any: ...


@dataclass(frozen=True, slots=True)
class ActuatorLayout:
    """Dynamic name-to-index map with a fail-fast canonical-order check."""

    names: tuple[str, ...]
    indices_by_leg: Mapping[str, tuple[int, int, int]]
    ctrl_ranges: tuple[tuple[float, float], ...]
    observed_leg_order: tuple[str, ...]

    @classmethod
    def from_physics(
        cls, physics: Any, *, require_canonical_model_order: bool = True
    ) -> "ActuatorLayout":
        model = physics.model
        names = tuple(model.id2name(index, "actuator") for index in range(model.nu))
        if any(name is None for name in names):
            raise LayoutError("Every actuator must have a name")

        expected_names = {
            leg: tuple(f"{kind}_{LEG_SUFFIX[leg]}" for kind in ACTUATOR_KINDS)
            for leg in CANONICAL_LEGS
        }
        all_expected = {name for group in expected_names.values() for name in group}
        unknown = set(names) - all_expected
        missing = all_expected - set(names)
        if unknown or missing or len(names) != len(all_expected):
            raise LayoutError(
                "Unexpected quadruped actuator names: "
                f"missing={sorted(missing)}, unknown={sorted(unknown)}, names={names}"
            )

        by_leg = {
            leg: tuple(names.index(name) for name in expected_names[leg])
            for leg in CANONICAL_LEGS
        }
        actuator_to_leg = {
            actuator_name: leg
            for leg, group in expected_names.items()
            for actuator_name in group
        }
        observed: list[str] = []
        for name in names:
            leg = actuator_to_leg[name]
            if not observed or observed[-1] != leg:
                observed.append(leg)
        observed_order = tuple(observed)

        canonical_names = tuple(
            name for leg in CANONICAL_LEGS for name in expected_names[leg]
        )
        if require_canonical_model_order and names != canonical_names:
            raise LayoutError(
                "Actuator order drifted from FL,FR,BR,BL / yaw,lift,extend. "
                f"Observed names: {names}"
            )

        ctrl_ranges_array = np.asarray(model.actuator_ctrlrange, dtype=float)
        if ctrl_ranges_array.shape != (len(names), 2):
            raise LayoutError(
                f"Expected actuator_ctrlrange shape {(len(names), 2)}, "
                f"got {ctrl_ranges_array.shape}"
            )
        return cls(
            names=names,
            indices_by_leg=by_leg,
            ctrl_ranges=tuple(tuple(float(value) for value in row) for row in ctrl_ranges_array),
            observed_leg_order=observed_order,
        )

    @property
    def size(self) -> int:
        return len(self.names)

    def indices(self, leg: str) -> tuple[int, int, int]:
        try:
            return self.indices_by_leg[leg]
        except KeyError as exc:
            raise ValueError(f"Unsupported leg: {leg!r}") from exc

    def action_from_leg_targets(
        self, targets: Mapping[str, Sequence[float]]
    ) -> np.ndarray:
        if set(targets) != set(CANONICAL_LEGS):
            raise ValueError(f"targets must contain exactly {CANONICAL_LEGS}")
        action = np.zeros(self.size, dtype=float)
        ranges = np.asarray(self.ctrl_ranges, dtype=float)
        for leg in CANONICAL_LEGS:
            values = np.asarray(targets[leg], dtype=float)
            if values.shape != (3,) or not np.all(np.isfinite(values)):
                raise ValueError(f"{leg} target must be three finite values")
            indices = np.asarray(self.indices(leg), dtype=int)
            lower, upper = ranges[indices, 0], ranges[indices, 1]
            if np.any(values < lower) or np.any(values > upper):
                raise ValueError(
                    f"{leg} target {values.tolist()} is outside control ranges "
                    f"{ranges[indices].tolist()}"
                )
            action[indices] = values
        return action

    def split(self, values: Sequence[float]) -> dict[str, np.ndarray]:
        array = np.asarray(values, dtype=float)
        if array.shape != (self.size,):
            raise ValueError(f"Expected shape {(self.size,)}, got {array.shape}")
        return {
            leg: array[np.asarray(self.indices(leg), dtype=int)].copy()
            for leg in CANONICAL_LEGS
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "model_order": list(self.names),
            "observed_leg_order": list(self.observed_leg_order),
            "indices_by_leg": {
                leg: list(self.indices_by_leg[leg]) for leg in CANONICAL_LEGS
            },
            "control_semantics": list(ACTUATOR_KINDS),
        }


class PlantActuatorFault:
    """Reversible plant-side actuator strength degradation.

    MuJoCo's general position servos use both a gain term and a length-bias
    term.  Scaling both preserves the target while reducing available effort.
    The controller action is never edited, and a preset load never restores
    the model parameters.
    """

    def __init__(self, physics: Any, layout: ActuatorLayout):
        self.physics = physics
        self.layout = layout
        self._nominal_gain = np.asarray(physics.model.actuator_gainprm).copy()
        self._nominal_bias = np.asarray(physics.model.actuator_biasprm).copy()
        self._active: dict[str, float] = {}

    @property
    def nominal_gainprm(self) -> np.ndarray:
        return self._nominal_gain.copy()

    @property
    def nominal_biasprm(self) -> np.ndarray:
        return self._nominal_bias.copy()

    @property
    def active(self) -> Mapping[str, float]:
        return dict(self._active)

    def inject(self, leg: str, *, strength_scale: float = 0.0) -> None:
        if not 0.0 <= strength_scale <= 1.0:
            raise ValueError("strength_scale must be in [0, 1]")
        indices = np.asarray(self.layout.indices(leg), dtype=int)
        self.physics.model.actuator_gainprm[indices] = (
            self._nominal_gain[indices] * strength_scale
        )
        self.physics.model.actuator_biasprm[indices] = (
            self._nominal_bias[indices] * strength_scale
        )
        self._active[leg] = float(strength_scale)
        self.physics.forward()

    def parameter_snapshot(self, leg: str) -> dict[str, Any]:
        """Copy actual selected-leg parameters, independently of later restore."""
        if leg not in self._active:
            raise ValueError(f"No active plant fault for {leg}")
        indices = np.asarray(self.layout.indices(leg), dtype=int)
        return {
            "leg": leg,
            "actuator_indices": indices.tolist(),
            "strength_scale": self._active[leg],
            "nominal_gainprm": self._nominal_gain[indices].tolist(),
            "nominal_biasprm": self._nominal_bias[indices].tolist(),
            "faulted_gainprm": np.asarray(self.physics.model.actuator_gainprm)[indices].tolist(),
            "faulted_biasprm": np.asarray(self.physics.model.actuator_biasprm)[indices].tolist(),
        }

    def restore(self, leg: str) -> None:
        indices = np.asarray(self.layout.indices(leg), dtype=int)
        self.physics.model.actuator_gainprm[indices] = self._nominal_gain[indices]
        self.physics.model.actuator_biasprm[indices] = self._nominal_bias[indices]
        self._active.pop(leg, None)
        self.physics.forward()

    def restore_all(self) -> None:
        self.physics.model.actuator_gainprm[:] = self._nominal_gain
        self.physics.model.actuator_biasprm[:] = self._nominal_bias
        self._active.clear()
        self.physics.forward()


@dataclass(frozen=True, slots=True)
class PhysicsMeasurements:
    monotonic_ns: int
    commanded_action: np.ndarray
    actuator_activation: np.ndarray
    actuator_length: np.ndarray
    actuator_velocity: np.ndarray
    actuator_force: np.ndarray
    joint_qpos_by_leg: Mapping[str, np.ndarray]
    joint_qvel_by_leg: Mapping[str, np.ndarray]
    toe_force_by_leg: Mapping[str, np.ndarray]
    imu_accel: np.ndarray
    imu_gyro: np.ndarray
    torso_upright: float
    torso_height: float
    torso_velocity: np.ndarray
    torso_position_world: np.ndarray = field(default_factory=lambda: np.zeros(3))
    torso_velocity_world: np.ndarray = field(default_factory=lambda: np.zeros(3))


class MeasurementExtractor:
    """Read model-named proprioception without consulting fault state."""

    def __init__(self, physics: Any, layout: ActuatorLayout):
        self.physics = physics
        self.layout = layout
        model = physics.model
        joint_names = {
            model.id2name(index, "joint") for index in range(model.njnt)
        }
        sensor_names = {
            model.id2name(index, "sensor") for index in range(model.nsensor)
        }
        self.joint_names_by_leg = {
            leg: tuple(f"{kind}_{LEG_SUFFIX[leg]}" for kind in JOINT_KINDS)
            for leg in CANONICAL_LEGS
        }
        required_joints = {
            name for group in self.joint_names_by_leg.values() for name in group
        }
        required_sensors = {"imu_accel", "imu_gyro", "velocimeter"} | {
            f"force_toe_{LEG_SUFFIX[leg]}" for leg in CANONICAL_LEGS
        }
        if not required_joints <= joint_names:
            raise LayoutError(
                f"Missing named joints: {sorted(required_joints - joint_names)}"
            )
        if not required_sensors <= sensor_names:
            raise LayoutError(
                f"Missing proprioceptive sensors: {sorted(required_sensors - sensor_names)}"
            )

    def capture(
        self, commanded_action: Sequence[float], *, monotonic_ns: int
    ) -> PhysicsMeasurements:
        action = np.asarray(commanded_action, dtype=float)
        if action.shape != (self.layout.size,):
            raise ValueError(f"Expected action shape {(self.layout.size,)}, got {action.shape}")
        physics = self.physics
        data = physics.data
        # actuator_length is the named MuJoCo transmission length derived from
        # qpos (joint angle for yaw, tendon length for lift/extend).
        qpos = {
            leg: np.asarray(
                [physics.named.data.qpos[name] for name in self.joint_names_by_leg[leg]],
                dtype=float,
            )
            for leg in CANONICAL_LEGS
        }
        qvel = {
            leg: np.asarray(
                [physics.named.data.qvel[name] for name in self.joint_names_by_leg[leg]],
                dtype=float,
            )
            for leg in CANONICAL_LEGS
        }
        toe_force = {
            leg: np.asarray(
                physics.named.data.sensordata[f"force_toe_{LEG_SUFFIX[leg]}"],
                dtype=float,
            ).copy()
            for leg in CANONICAL_LEGS
        }
        activation = np.asarray(data.act, dtype=float)
        if activation.shape != (self.layout.size,):
            # Stateless actuators have no activation state.  The current
            # quadruped uses filtered actuators, but ctrl is a safe fallback.
            activation = np.asarray(data.ctrl, dtype=float)
        return PhysicsMeasurements(
            monotonic_ns=int(monotonic_ns),
            commanded_action=action.copy(),
            actuator_activation=activation.copy(),
            actuator_length=np.asarray(data.actuator_length, dtype=float).copy(),
            actuator_velocity=np.asarray(data.actuator_velocity, dtype=float).copy(),
            actuator_force=np.asarray(data.actuator_force, dtype=float).copy(),
            joint_qpos_by_leg={leg: values.copy() for leg, values in qpos.items()},
            joint_qvel_by_leg={leg: values.copy() for leg, values in qvel.items()},
            toe_force_by_leg=toe_force,
            imu_accel=np.asarray(
                physics.named.data.sensordata["imu_accel"], dtype=float
            ).copy(),
            imu_gyro=np.asarray(
                physics.named.data.sensordata["imu_gyro"], dtype=float
            ).copy(),
            torso_upright=float(physics.torso_upright()),
            torso_height=float(physics.named.data.xpos["torso"][2]),
            torso_velocity=np.asarray(physics.torso_velocity(), dtype=float).copy(),
            torso_position_world=np.asarray(physics.named.data.xpos["torso"], dtype=float).copy(),
            torso_velocity_world=(
                np.asarray(physics.named.data.site_xmat["torso"], dtype=float).reshape(3, 3)
                @ np.asarray(physics.torso_velocity(), dtype=float)
            ),
        )


@dataclass(frozen=True, slots=True)
class ResidualDetectorConfig:
    enter_threshold: float = 0.58
    exit_threshold: float = 0.24
    enter_steps: int = 5
    exit_steps: int = 15
    minimum_expected_force: float = 8.0
    tracking_floor: float = 0.04
    tracking_full_scale: float = 0.32
    velocity_full_scale: float = 0.5
    gyro_full_scale: float = 5.0

    def __post_init__(self) -> None:
        if not 0 <= self.exit_threshold < self.enter_threshold <= 1:
            raise ValueError("Need 0 <= exit_threshold < enter_threshold <= 1")
        if self.enter_steps < 1 or self.exit_steps < 1:
            raise ValueError("debounce step counts must be positive")
        if self.minimum_expected_force < 0:
            raise ValueError("minimum_expected_force cannot be negative")


@dataclass(frozen=True, slots=True)
class DetectorNoiseConfig:
    """Independent zero-mean Gaussian *measurement* perturbations.

    Force std is a dimensionless fraction of max(abs(nominal predicted
    generalized actuator effort), detector.minimum_expected_force), per channel.
    Joint position/velocity use rad and rad/s, gyro uses rad/s, accel uses m/s².
    Command activation and transmission position/velocity remain noiseless;
    this is an explicitly partial sensor-noise sensitivity experiment, not a
    calibrated real-sensor model. Physical outcomes always use pristine data.
    """

    actuator_force_relative_std: float = 0.0
    joint_position_std_rad: float = 0.0
    joint_velocity_std_rad_s: float = 0.0
    gyro_std_rad_s: float = 0.0
    accel_std_m_s2: float = 0.0

    def __post_init__(self) -> None:
        for item in fields(self):
            value = float(getattr(self, item.name))
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{item.name} must be finite and nonnegative")


def copy_measurements(value: PhysicsMeasurements) -> PhysicsMeasurements:
    """Copy every array so hooks/noise cannot mutate physical ground truth."""
    return replace(value, **{
        item.name: (
            current.copy() if isinstance(current, np.ndarray)
            else {key: array.copy() for key, array in current.items()}
        )
        for item in fields(value)
        if isinstance((current := getattr(value, item.name)), (np.ndarray, Mapping))
    })


class DetectorMeasurementNoise:
    def __init__(self, config: DetectorNoiseConfig, *, seed: int):
        self.config = config
        self.seed = int(seed)
        self.rng = np.random.default_rng(self.seed)

    def apply(
        self, truth: PhysicsMeasurements, *, nominal_effort: np.ndarray,
        minimum_expected_force: float,
    ) -> PhysicsMeasurements:
        observed = copy_measurements(truth)
        config = self.config
        # Draw even at zero std so levels use the same underlying random draws.
        observed.actuator_force[:] += self.rng.standard_normal(observed.actuator_force.shape) * (
            config.actuator_force_relative_std
            * np.maximum(np.abs(nominal_effort), minimum_expected_force)
        )
        for leg in CANONICAL_LEGS:
            observed.joint_qpos_by_leg[leg][:] += self.rng.standard_normal(
                observed.joint_qpos_by_leg[leg].shape
            ) * config.joint_position_std_rad
            observed.joint_qvel_by_leg[leg][:] += self.rng.standard_normal(
                observed.joint_qvel_by_leg[leg].shape
            ) * config.joint_velocity_std_rad_s
        observed.imu_gyro[:] += self.rng.standard_normal(3) * config.gyro_std_rad_s
        observed.imu_accel[:] += self.rng.standard_normal(3) * config.accel_std_m_s2
        return observed


@dataclass(frozen=True, slots=True)
class LegResidual:
    leg: str
    score: float
    force_model_residual: float
    tracking_residual: float
    motion_residual: float
    kinematic_sensor_residual: float
    toe_force_norm: float
    imu_instability: float
    expected_force_l1: float


@dataclass(frozen=True, slots=True)
class DetectorUpdate:
    monotonic_ns: int
    residuals: Mapping[str, LegResidual]
    active_legs: tuple[str, ...]
    triggered_legs: tuple[str, ...]
    cleared_legs: tuple[str, ...]


class ResidualFaultDetector:
    """Debounced model residual with hysteresis and measured-state evidence.

    The nominal actuator model predicts effort from filtered command activation
    and qpos-derived actuator transmission length.  That prediction is compared
    with measured actuator force.  qvel, finite-difference qpos, toe force, and
    IMU readings are retained as independent proprioceptive components.  The
    detector has no access to :class:`PlantActuatorFault` or its active flags.
    """

    def __init__(
        self,
        layout: ActuatorLayout,
        nominal_gainprm: np.ndarray,
        nominal_biasprm: np.ndarray,
        *,
        control_timestep: float,
        config: ResidualDetectorConfig | None = None,
        nominal_forcelimited: Sequence[bool] | None = None,
        nominal_forcerange: Sequence[Sequence[float]] | None = None,
    ):
        self.layout = layout
        self.nominal_gainprm = np.asarray(nominal_gainprm, dtype=float).copy()
        self.nominal_biasprm = np.asarray(nominal_biasprm, dtype=float).copy()
        if self.nominal_gainprm.shape[0] != layout.size:
            raise ValueError("nominal_gainprm does not match actuator layout")
        if self.nominal_biasprm.shape[0] != layout.size:
            raise ValueError("nominal_biasprm does not match actuator layout")
        self.nominal_forcelimited = (
            np.zeros(layout.size, dtype=bool) if nominal_forcelimited is None
            else np.asarray(nominal_forcelimited, dtype=bool).copy()
        )
        self.nominal_forcerange = (
            np.tile([-np.inf, np.inf], (layout.size, 1)) if nominal_forcerange is None
            else np.asarray(nominal_forcerange, dtype=float).copy()
        )
        if self.nominal_forcelimited.shape != (layout.size,):
            raise ValueError("nominal_forcelimited does not match actuator layout")
        if self.nominal_forcerange.shape != (layout.size, 2):
            raise ValueError("nominal_forcerange does not match actuator layout")
        limited_ranges = self.nominal_forcerange[self.nominal_forcelimited]
        if (np.any(~np.isfinite(limited_ranges))
                or np.any(limited_ranges[:, 0] > limited_ranges[:, 1])):
            raise ValueError("Enabled actuator force limits must be finite and ordered")
        if control_timestep <= 0:
            raise ValueError("control_timestep must be positive")
        self.control_timestep = float(control_timestep)
        self.config = config or ResidualDetectorConfig()
        self._active = {leg: False for leg in CANONICAL_LEGS}
        self._high_count = {leg: 0 for leg in CANONICAL_LEGS}
        self._low_count = {leg: 0 for leg in CANONICAL_LEGS}
        self._previous_qpos: dict[str, np.ndarray] | None = None

    @property
    def active_legs(self) -> tuple[str, ...]:
        return tuple(leg for leg in CANONICAL_LEGS if self._active[leg])

    def _nominal_force(self, measurements: PhysicsMeasurements) -> np.ndarray:
        gain = self.nominal_gainprm
        bias = self.nominal_biasprm
        activation = measurements.actuator_activation
        length = measurements.actuator_length
        velocity = measurements.actuator_velocity
        # MuJoCo fixed/affine actuator model.  The quadruped uses columns 0,
        # 1 and 2; retaining all present terms makes the residual explicit.
        force = (
            gain[:, 0] * activation
            + bias[:, 0]
            + bias[:, 1] * length
            + bias[:, 2] * velocity
        )
        # MuJoCo clamps actuator effort after gain/bias evaluation. Activation
        # already reflects the plant's control clipping and filtering.
        limited = self.nominal_forcelimited
        force[limited] = np.clip(
            force[limited], self.nominal_forcerange[limited, 0],
            self.nominal_forcerange[limited, 1],
        )
        return force

    def update(self, measurements: PhysicsMeasurements) -> DetectorUpdate:
        expected_force = self._nominal_force(measurements)
        ranges = np.asarray(self.layout.ctrl_ranges, dtype=float)
        spans = np.maximum(ranges[:, 1] - ranges[:, 0], 1e-9)
        triggered: list[str] = []
        cleared: list[str] = []
        residuals: dict[str, LegResidual] = {}

        accel_deviation = abs(float(np.linalg.norm(measurements.imu_accel)) - 9.81) / 9.81
        gyro_ratio = float(np.linalg.norm(measurements.imu_gyro)) / self.config.gyro_full_scale
        upright_deficit = max(0.0, 0.8 - measurements.torso_upright) / 0.8
        imu_instability = float(np.clip(max(accel_deviation, gyro_ratio, upright_deficit), 0, 1))

        for leg in CANONICAL_LEGS:
            indices = np.asarray(self.layout.indices(leg), dtype=int)
            expected = expected_force[indices]
            measured = measurements.actuator_force[indices]
            expected_l1 = float(np.sum(np.abs(expected)))
            force_model_residual = float(
                np.clip(
                    np.sum(np.abs(measured - expected)) / max(expected_l1, 1e-9),
                    0,
                    1,
                )
            ) if expected_l1 >= self.config.minimum_expected_force else 0.0

            tracking = np.mean(
                np.abs(
                    measurements.commanded_action[indices]
                    - measurements.actuator_length[indices]
                )
                / spans[indices]
            )
            tracking_residual = float(
                np.clip(
                    (tracking - self.config.tracking_floor)
                    / max(
                        self.config.tracking_full_scale - self.config.tracking_floor,
                        1e-9,
                    ),
                    0,
                    1,
                )
            )

            target_error = (
                measurements.actuator_activation[indices]
                - measurements.actuator_length[indices]
            )
            progress_velocity = np.sign(target_error) * measurements.actuator_velocity[indices]
            motion_residual = float(
                np.clip(
                    1.0
                    - np.mean(np.maximum(progress_velocity, 0.0))
                    / self.config.velocity_full_scale,
                    0,
                    1,
                )
            )

            if self._previous_qpos is None:
                kinematic_sensor_residual = 0.0
            else:
                finite_difference = (
                    measurements.joint_qpos_by_leg[leg] - self._previous_qpos[leg]
                ) / self.control_timestep
                measured_qvel = measurements.joint_qvel_by_leg[leg]
                denominator = max(float(np.linalg.norm(measured_qvel)), 1.0)
                kinematic_sensor_residual = float(
                    np.clip(
                        np.linalg.norm(finite_difference - measured_qvel) / denominator,
                        0,
                        1,
                    )
                )

            # A model-effort mismatch is the principal localized evidence.
            # Tracking, measured motion, and IMU add bounded context and can
            # never independently cross the entry threshold.
            score = float(
                np.clip(
                    0.70 * force_model_residual
                    + 0.15 * tracking_residual
                    + 0.10 * motion_residual
                    + 0.03 * kinematic_sensor_residual
                    + 0.02 * imu_instability,
                    0,
                    1,
                )
            )
            residuals[leg] = LegResidual(
                leg=leg,
                score=score,
                force_model_residual=force_model_residual,
                tracking_residual=tracking_residual,
                motion_residual=motion_residual,
                kinematic_sensor_residual=kinematic_sensor_residual,
                toe_force_norm=float(np.linalg.norm(measurements.toe_force_by_leg[leg])),
                imu_instability=imu_instability,
                expected_force_l1=expected_l1,
            )

            if not self._active[leg]:
                self._low_count[leg] = 0
                self._high_count[leg] = (
                    self._high_count[leg] + 1
                    if score >= self.config.enter_threshold
                    else 0
                )
                if self._high_count[leg] >= self.config.enter_steps:
                    self._active[leg] = True
                    self._high_count[leg] = 0
                    triggered.append(leg)
            else:
                self._high_count[leg] = 0
                self._low_count[leg] = (
                    self._low_count[leg] + 1
                    if score <= self.config.exit_threshold
                    else 0
                )
                if self._low_count[leg] >= self.config.exit_steps:
                    self._active[leg] = False
                    self._low_count[leg] = 0
                    cleared.append(leg)

        self._previous_qpos = {
            leg: measurements.joint_qpos_by_leg[leg].copy() for leg in CANONICAL_LEGS
        }
        return DetectorUpdate(
            monotonic_ns=measurements.monotonic_ns,
            residuals=residuals,
            active_legs=self.active_legs,
            triggered_legs=tuple(triggered),
            cleared_legs=tuple(cleared),
        )


@dataclass(frozen=True, slots=True)
class PacingSample:
    target_ns: int
    actual_ns: int
    slept_ns: int
    lateness_ns: int


class RealtimePacer:
    """Absolute-deadline pacer that does not accumulate loop drift."""

    def __init__(
        self,
        period_s: float,
        *,
        clock_ns: Callable[[], int] = time.perf_counter_ns,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if period_s <= 0:
            raise ValueError("period_s must be positive")
        self.period_ns = int(round(period_s * 1_000_000_000))
        self.clock_ns = clock_ns
        self.sleep = sleep
        self.start_ns: int | None = None

    def start(self, start_ns: int | None = None) -> int:
        self.start_ns = int(self.clock_ns() if start_ns is None else start_ns)
        return self.start_ns

    def wait_for_step(self, step_index: int) -> PacingSample:
        if step_index < 0:
            raise ValueError("step_index cannot be negative")
        if self.start_ns is None:
            self.start()
        assert self.start_ns is not None
        target = self.start_ns + step_index * self.period_ns
        before = self.clock_ns()
        remaining = target - before
        slept = 0
        if remaining > 0:
            slept = remaining
            self.sleep(remaining / 1_000_000_000)
        actual = self.clock_ns()
        return PacingSample(
            target_ns=target,
            actual_ns=actual,
            slept_ns=max(0, slept),
            lateness_ns=max(0, actual - target),
        )


@dataclass(frozen=True, slots=True)
class ControlStep:
    step_index: int
    sim_time_s: float
    step_started_ns: int
    step_finished_ns: int
    pacing: PacingSample | None
    measurements: PhysicsMeasurements
    reward: float


def validate_actuator_force_model(model: Any) -> None:
    """Fail closed if a loaded model invalidates the nominal force equation."""
    from mujoco import mjtBias, mjtGain

    if np.any(np.asarray(model.actuator_gaintype) != int(mjtGain.mjGAIN_FIXED)):
        raise LayoutError("The nominal force predictor requires fixed actuator gains")
    if np.any(np.asarray(model.actuator_biastype) != int(mjtBias.mjBIAS_AFFINE)):
        raise LayoutError("The nominal force predictor requires affine actuator biases")
    if np.any(np.asarray(model.actuator_actearly)):
        raise LayoutError("The nominal force predictor does not support actearly actuators")


class QuadrupedTrial:
    """One-reset environment wrapper with no measured-phase reset path."""

    def __init__(
        self,
        env: Any,
        *,
        realtime: bool,
        clock_ns: Callable[[], int] = time.perf_counter_ns,
    ):
        self.env = env
        self.physics = env.physics
        self.layout = ActuatorLayout.from_physics(self.physics)
        validate_actuator_force_model(self.physics.model)
        self.extractor = MeasurementExtractor(self.physics, self.layout)
        self.control_timestep = float(env.control_timestep())
        self.clock_ns = clock_ns
        self.pacer = RealtimePacer(self.control_timestep, clock_ns=clock_ns) if realtime else None
        self.reset_count = 0
        self.settle_steps = 0
        self.trial_steps = 0
        self.initial_yaw_rad: float | None = None
        self.initial_noncontact_height: float | None = None
        self.settled_upright: float | None = None
        self.settled_torso_height: float | None = None
        self.settled_gyro_norm: float | None = None
        self._initialized = False

    def _place_upright_without_contact(self, yaw_rad: float) -> float:
        """Replace Move's random quaternion before any measured control step."""

        try:
            from dm_control.rl.control import PhysicsError
        except ImportError:  # pragma: no cover - QuadrupedTrial requires dm_control
            PhysicsError = RuntimeError  # type: ignore[assignment,misc]

        quaternion = np.asarray(
            [math.cos(yaw_rad / 2.0), 0.0, 0.0, math.sin(yaw_rad / 2.0)],
            dtype=float,
        )
        # Start embedded and move upward in 1 cm increments, matching the
        # dm_control suite's own non-contact search but with an upright pose.
        for attempt in range(10_001):
            height = attempt * 0.01
            try:
                with self.physics.reset_context():
                    self.physics.named.data.qpos["root"][:3] = (0.0, 0.0, height)
                    self.physics.named.data.qpos["root"][3:] = quaternion
                    self.physics.data.qvel[:] = 0.0
                    self.physics.data.ctrl[:] = 0.0
                    if self.physics.data.act.size:
                        self.physics.data.act[:] = 0.0
            except PhysicsError:
                continue
            if self.physics.data.ncon == 0:
                return height
        raise RuntimeError("Could not find a non-contacting deterministic upright pose")

    def initialize(self, *, settle_steps: int = 300, yaw_rad: float = 0.0) -> Any:
        if self._initialized:
            raise RuntimeError("A QuadrupedTrial can only be initialized once")
        if settle_steps < 0:
            raise ValueError("settle_steps cannot be negative")
        if not math.isfinite(yaw_rad):
            raise ValueError("yaw_rad must be finite")
        time_step = self.env.reset()
        self.reset_count += 1
        self.initial_yaw_rad = float(yaw_rad)
        self.initial_noncontact_height = self._place_upright_without_contact(yaw_rad)
        stand = standing_preset().to_action(self.layout)
        for _ in range(settle_steps):
            time_step = self.env.step(stand)
            if time_step.last():
                raise EpisodeTerminatedError("Environment ended during initial settle")
        self.physics.forward()
        self.settle_steps = settle_steps
        self.settled_upright = float(self.physics.torso_upright())
        self.settled_torso_height = float(self.physics.named.data.xpos["torso"][2])
        self.settled_gyro_norm = float(
            np.linalg.norm(self.physics.named.data.sensordata["imu_gyro"])
        )
        self._initialized = True
        if self.pacer is not None:
            self.pacer.start()
        return time_step

    def step(
        self,
        action: Sequence[float],
        *,
        before_step: Callable[[int], None] | None = None,
    ) -> ControlStep:
        if not self._initialized:
            raise RuntimeError("Call initialize() before step()")
        action_array = np.asarray(action, dtype=float)
        if action_array.shape != (self.layout.size,):
            raise ValueError(
                f"Expected action shape {(self.layout.size,)}, got {action_array.shape}"
            )
        pacing = self.pacer.wait_for_step(self.trial_steps) if self.pacer else None
        callback_ns = self.clock_ns()
        if before_step is not None:
            before_step(callback_ns)
        started_ns = self.clock_ns()
        time_step = self.env.step(action_array)
        # dm_control's legacy step finishes with mj_step1: qpos/qvel and
        # transmission kinematics are current, but actuator forces and some
        # sensors still reflect the preceding integration evaluation. Refresh
        # all derived quantities without advancing time/qpos/qvel/activation.
        # Include this synchronization cost in the measured completion time.
        self.physics.forward()
        finished_ns = self.clock_ns()
        if time_step.last():
            raise EpisodeTerminatedError(
                "Environment time limit reached during the measured phase; "
                "the trial was aborted instead of reset"
            )
        measurements = self.extractor.capture(action_array, monotonic_ns=finished_ns)
        result = ControlStep(
            step_index=self.trial_steps,
            sim_time_s=(self.trial_steps + 1) * self.control_timestep,
            step_started_ns=started_ns,
            step_finished_ns=finished_ns,
            pacing=pacing,
            measurements=measurements,
            reward=float(time_step.reward or 0.0),
        )
        self.trial_steps += 1
        return result


@dataclass(frozen=True, slots=True)
class PhysicsTrialSpec:
    batch_id: str = "physics"
    run_id: str = "physics-run"
    trial_id: str = "physics-trial"
    method: Literal["local_reflex", "no_reflex"] = "local_reflex"
    fault_leg: str = "FL"
    seed: int = 0
    duration_s: float = 3.0
    fault_offset_s: float = 0.75
    realtime: bool = False
    fault_strength: float = 0.0
    fault_enabled: bool = True
    settle_steps: int = 300
    gait_phase_steps: int = 10
    initial_yaw_rad: float | None = None
    gait_phase_offset_steps: int | None = None
    minimum_pre_fault_samples: int = 50
    eligibility_window_steps: int = 50
    eligibility_upright_threshold: float = 0.95
    eligibility_upright_fraction: float = 0.95
    eligibility_gyro_threshold: float = 0.50
    eligibility_height_threshold: float = 0.35
    safe_upright_threshold: float = 0.80
    safe_height_threshold: float = 0.30
    safe_upright_fraction: float = 0.95
    fall_upright_threshold: float = 0.30
    fall_height_threshold: float = 0.18
    fall_debounce_steps: int = 5
    terminal_window_steps: int = 50
    record_trace: bool = True
    detector: ResidualDetectorConfig = ResidualDetectorConfig()
    detector_noise: DetectorNoiseConfig = DetectorNoiseConfig()
    detector_noise_seed: int | None = None
    lateral_push_force_n: float = 0.0
    lateral_push_start_s: float = 3.0
    lateral_push_duration_s: float = 0.2

    def __post_init__(self) -> None:
        for name in ("batch_id", "run_id", "trial_id"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} must not be blank")
        if isinstance(self.detector_noise, Mapping):
            object.__setattr__(self, "detector_noise", DetectorNoiseConfig(**dict(self.detector_noise)))
        if not isinstance(self.detector_noise, DetectorNoiseConfig):
            raise ValueError("detector_noise must be DetectorNoiseConfig or a matching mapping")
        if self.method not in {"local_reflex", "no_reflex"}:
            raise ValueError("method must be local_reflex or no_reflex")
        if self.fault_leg not in CANONICAL_LEGS:
            raise ValueError(f"Unsupported fault leg: {self.fault_leg!r}")
        if self.duration_s <= 0:
            raise ValueError("duration_s must be positive")
        if not 0 <= self.fault_offset_s < self.duration_s:
            raise ValueError("fault_offset_s must be within the measured trial")
        if not 0 <= self.fault_strength <= 1:
            raise ValueError("fault_strength must be in [0, 1]")
        if not isinstance(self.fault_enabled, bool):
            raise ValueError("fault_enabled must be boolean")
        if not math.isfinite(self.lateral_push_force_n) or abs(self.lateral_push_force_n) > 200:
            raise ValueError("lateral_push_force_n must be finite and within [-200, 200] N")
        if not math.isfinite(self.lateral_push_start_s) or self.lateral_push_start_s < 0:
            raise ValueError("lateral_push_start_s must be finite and nonnegative")
        if not math.isfinite(self.lateral_push_duration_s) or not 0 < self.lateral_push_duration_s <= 2:
            raise ValueError("lateral_push_duration_s must be in (0, 2] s")
        if self.lateral_push_force_n and self.lateral_push_start_s + self.lateral_push_duration_s > self.duration_s:
            raise ValueError("The full lateral push must fit within the measured horizon")
        if self.detector_noise_seed is not None and self.detector_noise_seed < 0:
            raise ValueError("detector_noise_seed cannot be negative")
        if self.settle_steps < 0 or self.gait_phase_steps < 1:
            raise ValueError("settle_steps/gait_phase_steps are invalid")
        if self.initial_yaw_rad is not None and not math.isfinite(self.initial_yaw_rad):
            raise ValueError("initial_yaw_rad must be finite")
        if self.gait_phase_offset_steps is not None and self.gait_phase_offset_steps < 0:
            raise ValueError("gait_phase_offset_steps cannot be negative")
        for name in (
            "minimum_pre_fault_samples",
            "eligibility_window_steps",
            "fall_debounce_steps",
            "terminal_window_steps",
        ):
            if int(getattr(self, name)) < 1:
                raise ValueError(f"{name} must be positive")
        for name in (
            "eligibility_upright_threshold",
            "eligibility_upright_fraction",
            "safe_upright_threshold",
            "safe_upright_fraction",
            "fall_upright_threshold",
        ):
            if not 0.0 <= float(getattr(self, name)) <= 1.0:
                raise ValueError(f"{name} must be in [0, 1]")
        for name in (
            "eligibility_gyro_threshold",
            "eligibility_height_threshold",
            "safe_height_threshold",
            "fall_height_threshold",
        ):
            if float(getattr(self, name)) < 0:
                raise ValueError(f"{name} cannot be negative")


def _coerce_physics_spec(value: PhysicsTrialSpec | Mapping[str, Any] | Any) -> PhysicsTrialSpec:
    if isinstance(value, PhysicsTrialSpec):
        return value
    if isinstance(value, Mapping):
        source = dict(value)
    else:
        source = {
            name: getattr(value, name)
            for name in (
                "batch_id",
                "run_id",
                "trial_id",
                "method",
                "fault_leg",
                "seed",
                "duration_s",
                "fault_offset_s",
                "realtime",
            )
            if hasattr(value, name)
        }
        metadata = getattr(value, "metadata", None)
        if isinstance(metadata, Mapping):
            source.update(metadata.get("physics", metadata))
    if isinstance(source.get("metadata"), Mapping):
        metadata = source.pop("metadata")
        source.update(metadata.get("physics", metadata))
    allowed = {field.name for field in fields(PhysicsTrialSpec)}
    kwargs = {key: item for key, item in source.items() if key in allowed}
    if "run_id" not in kwargs and "trial_id" in kwargs:
        kwargs["run_id"] = str(kwargs["trial_id"])
    detector = kwargs.get("detector")
    if isinstance(detector, Mapping):
        kwargs["detector"] = ResidualDetectorConfig(**dict(detector))
    noise = kwargs.get("detector_noise")
    if isinstance(noise, Mapping):
        kwargs["detector_noise"] = DetectorNoiseConfig(**dict(noise))
    return PhysicsTrialSpec(**kwargs)


def _paired_scenario_variation(
    spec: PhysicsTrialSpec, *, gait_cycle_steps: int
) -> tuple[float, int]:
    """Derive method-independent scenario variation from the paired seed."""

    digest = hashlib.sha256(f"hera-v2-physics|{spec.seed}".encode("ascii")).digest()
    unit = int.from_bytes(digest[:8], "big") / float(1 << 64)
    yaw_rad = (
        float(spec.initial_yaw_rad)
        if spec.initial_yaw_rad is not None
        else (2.0 * unit - 1.0) * math.pi
    )
    phase_offset = (
        int(spec.gait_phase_offset_steps)
        if spec.gait_phase_offset_steps is not None
        else int.from_bytes(digest[8:16], "big") % gait_cycle_steps
    )
    return yaw_rad, phase_offset % gait_cycle_steps


def evaluate_pre_fault_eligibility(
    samples: Sequence[PhysicsMeasurements],
    spec: PhysicsTrialSpec,
    *,
    detector_triggered_before_fault: bool = False,
) -> dict[str, Any]:
    """Evaluate whether a trial is interpretable before injecting the fault."""

    reasons: list[str] = []
    if len(samples) < spec.minimum_pre_fault_samples:
        reasons.append(
            f"insufficient_samples:{len(samples)}<{spec.minimum_pre_fault_samples}"
        )
    window = list(samples[-spec.eligibility_window_steps :])
    if not window:
        reasons.append("empty_eligibility_window")
        return {
            "eligible": False,
            "reasons": reasons,
            "sample_count": len(samples),
            "window_count": 0,
            "minimum_upright": None,
            "upright_fraction": None,
            "maximum_gyro_norm": None,
            "minimum_torso_height": None,
            "detector_triggered_before_fault": detector_triggered_before_fault,
        }

    upright = np.asarray([sample.torso_upright for sample in window], dtype=float)
    height = np.asarray([sample.torso_height for sample in window], dtype=float)
    gyro = np.asarray(
        [np.linalg.norm(sample.imu_gyro) for sample in window], dtype=float
    )
    upright_fraction = float(
        np.mean(upright >= spec.eligibility_upright_threshold)
    )
    if upright_fraction < spec.eligibility_upright_fraction:
        reasons.append(
            "upright_fraction_below_threshold:"
            f"{upright_fraction:.6f}<{spec.eligibility_upright_fraction:.6f}"
        )
    if float(np.max(gyro)) > spec.eligibility_gyro_threshold:
        reasons.append(
            "gyro_above_threshold:"
            f"{float(np.max(gyro)):.6f}>{spec.eligibility_gyro_threshold:.6f}"
        )
    if float(np.min(height)) < spec.eligibility_height_threshold:
        reasons.append(
            "torso_height_below_threshold:"
            f"{float(np.min(height)):.6f}<{spec.eligibility_height_threshold:.6f}"
        )
    return {
        "eligible": not reasons,
        "reasons": reasons,
        "sample_count": len(samples),
        "window_count": len(window),
        "minimum_upright": float(np.min(upright)),
        "mean_upright": float(np.mean(upright)),
        "upright_fraction": upright_fraction,
        "maximum_gyro_norm": float(np.max(gyro)),
        "minimum_torso_height": float(np.min(height)),
        "mean_torso_height": float(np.mean(height)),
        "detector_triggered_before_fault": detector_triggered_before_fault,
        "thresholds": {
            "minimum_samples": spec.minimum_pre_fault_samples,
            "window_steps": spec.eligibility_window_steps,
            "upright": spec.eligibility_upright_threshold,
            "upright_fraction": spec.eligibility_upright_fraction,
            "gyro_norm": spec.eligibility_gyro_threshold,
            "torso_height": spec.eligibility_height_threshold,
        },
    }


def summarize_physical_safety(
    samples: Sequence[tuple[int, PhysicsMeasurements]],
    spec: PhysicsTrialSpec,
    *,
    control_timestep: float,
    pre_fault_eligible: bool,
) -> dict[str, Any]:
    """Summarize plant safety independently of detector/reflex endpoints."""

    if not samples:
        return {
            "evaluable": False,
            "safe": False,
            "invalid_reason": "no_post_fault_samples",
        }
    upright = np.asarray([sample.torso_upright for _, sample in samples], dtype=float)
    height = np.asarray([sample.torso_height for _, sample in samples], dtype=float)
    terminal_count = min(spec.terminal_window_steps, len(samples))
    terminal_upright = upright[-terminal_count:]
    terminal_height = height[-terminal_count:]

    bad = (upright < spec.fall_upright_threshold) | (
        height < spec.fall_height_threshold
    )
    consecutive = 0
    first_fall_index: int | None = None
    recognized_fall_index: int | None = None
    for index, is_bad in enumerate(bad):
        consecutive = consecutive + 1 if bool(is_bad) else 0
        if consecutive >= spec.fall_debounce_steps:
            first_fall_index = index - spec.fall_debounce_steps + 1
            recognized_fall_index = index
            break

    upright_fraction = float(np.mean(upright >= spec.safe_upright_threshold))
    terminal_mean_upright = float(np.mean(terminal_upright))
    terminal_mean_height = float(np.mean(terminal_height))
    terminal_safe_fraction = float(np.mean(
        (terminal_upright >= spec.safe_upright_threshold)
        & (terminal_height >= spec.safe_height_threshold)
    ))
    enough_terminal_samples = len(samples) >= spec.terminal_window_steps
    evaluable = bool(pre_fault_eligible and enough_terminal_samples)
    safe = bool(
        evaluable
        and first_fall_index is None
        and upright_fraction >= spec.safe_upright_fraction
        and terminal_safe_fraction >= spec.safe_upright_fraction
    )
    first_fall_step = (
        samples[first_fall_index][0] if first_fall_index is not None else None
    )
    recognized_fall_step = (
        samples[recognized_fall_index][0]
        if recognized_fall_index is not None
        else None
    )
    invalid_reason = None
    if not pre_fault_eligible:
        invalid_reason = "invalid_pre_fault_state"
    elif not enough_terminal_samples:
        invalid_reason = (
            f"insufficient_post_fault_samples:{len(samples)}<"
            f"{spec.terminal_window_steps}"
        )
    return {
        "evaluable": evaluable,
        "safe": safe,
        "invalid_reason": invalid_reason,
        "fall": first_fall_index is not None,
        "first_fall_step": first_fall_step,
        "fall_recognized_step": recognized_fall_step,
        "post_fault_samples": len(samples),
        "minimum_upright": float(np.min(upright)),
        "mean_upright": float(np.mean(upright)),
        "minimum_torso_height": float(np.min(height)),
        "mean_torso_height": float(np.mean(height)),
        "upright_fraction": upright_fraction,
        "terminal_safe_fraction": terminal_safe_fraction,
        "integrated_upright_deficit_s": float(
            np.sum(np.maximum(0.0, 1.0 - upright)) * control_timestep
        ),
        "integrated_height_deficit_m_s": float(
            np.sum(np.maximum(0.0, spec.safe_height_threshold - height))
            * control_timestep
        ),
        "terminal_window": {
            "samples": terminal_count,
            "mean_upright": terminal_mean_upright,
            "minimum_upright": float(np.min(terminal_upright)),
            "mean_torso_height": terminal_mean_height,
            "minimum_torso_height": float(np.min(terminal_height)),
        },
        "thresholds": {
            "safe_upright": spec.safe_upright_threshold,
            "safe_height": spec.safe_height_threshold,
            "safe_upright_fraction": spec.safe_upright_fraction,
            "fall_upright": spec.fall_upright_threshold,
            "fall_height": spec.fall_height_threshold,
            "fall_debounce_steps": spec.fall_debounce_steps,
            "terminal_window_steps": spec.terminal_window_steps,
        },
    }


def _emit_event(
    logger: EventLoggerLike | Callable[[Mapping[str, Any]], Any] | None,
    event_type: str,
    *,
    monotonic_ns: int,
    identity: EventIdentity,
    trial_start_ns: int,
    **payload: Any,
) -> None:
    if logger is None:
        return
    fields_payload = {
        "batch_id": identity.batch_id,
        "run_id": identity.run_id,
        "trial_id": identity.trial_id,
        "monotonic_ns": int(monotonic_ns),
        "elapsed_ns": int(monotonic_ns - trial_start_ns),
        **payload,
    }
    if hasattr(logger, "emit"):
        # The shared event logger owns its EventRecord clock and identity.  The
        # physics sample clock is retained in details for exact stage deltas.
        logger.emit(event_type, identity, fields_payload)  # type: ignore[union-attr]
    elif callable(logger):
        logger({"event_type": event_type, **fields_payload})
    else:
        raise TypeError(
            "event_logger must be callable or expose emit(stage, identity, details)"
        )


def summarize_movement(
    samples: Sequence[PhysicsMeasurements], *, initial_yaw_rad: float,
    terminal_window_steps: int, control_timestep: float,
    reference_position_world: np.ndarray | None = None,
) -> dict[str, Any]:
    if not samples:
        return {"samples": 0}
    speed = np.asarray([np.linalg.norm(m.torso_velocity) for m in samples])
    gyro = np.asarray([np.linalg.norm(m.imu_gyro) for m in samples])
    heading = np.array([math.cos(initial_yaw_rad), math.sin(initial_yaw_rad), 0.0])
    lateral = np.array([-math.sin(initial_yaw_rad), math.cos(initial_yaw_rad), 0.0])
    velocity_world = np.asarray([m.torso_velocity_world for m in samples])
    positions = np.asarray([m.torso_position_world for m in samples])
    origin = positions[0] if reference_position_world is None else reference_position_world
    displacement = positions[-1] - origin
    return {
        "samples": len(samples),
        "mean_speed_m_s": float(np.mean(speed)),
        "terminal_mean_speed_m_s": float(np.mean(speed[-terminal_window_steps:])),
        "mean_gyro_rad_s": float(np.mean(gyro)),
        "terminal_mean_gyro_rad_s": float(np.mean(gyro[-terminal_window_steps:])),
        "mean_forward_velocity_m_s": float(np.mean(velocity_world @ heading)),
        "mean_lateral_velocity_m_s": float(np.mean(velocity_world @ lateral)),
        "forward_displacement_m": float(displacement @ heading),
        "lateral_displacement_m": float(displacement @ lateral),
        "world_displacement_m": displacement.tolist(),
        "speed_integral_m": float(np.sum(speed) * control_timestep),
        "direction_reference": "fixed initial-yaw forward/lateral axes; not current body axes",
    }


def summarize_detector_excursions(
    history: Sequence[tuple[int, DetectorUpdate]], *, reference_step: int,
    threshold: float,
) -> dict[str, Any]:
    result = {}
    for label, selected in (
        ("pre_reference", [entry for entry in history if entry[0] < reference_step]),
        ("post_reference", [entry for entry in history if entry[0] >= reference_step]),
    ):
        by_leg = {}
        for leg in CANONICAL_LEGS:
            above = [update.residuals[leg].score >= threshold for _, update in selected]
            current = maximum = 0
            for flag in above:
                current = current + 1 if flag else 0
                maximum = max(maximum, current)
            by_leg[leg] = {
                "above_threshold_samples": sum(above),
                "above_threshold_fraction": sum(above) / len(above) if above else None,
                "maximum_consecutive_samples": maximum,
                "detector_active_fraction": (
                    sum(leg in update.active_legs for _, update in selected) / len(selected)
                    if selected else None
                ),
            }
        result[label] = {"samples": len(selected), "threshold": threshold, "by_leg": by_leg}
    return result


def validate_control_override(
    override: tuple[Sequence[float], str], layout: ActuatorLayout,
) -> tuple[np.ndarray, str]:
    if not isinstance(override, tuple) or len(override) != 2:
        raise ValueError("before_control must return None or (action, source) tuple")
    action, source = override
    action = np.asarray(action, dtype=float)
    if action.shape != (layout.size,) or not np.all(np.isfinite(action)):
        raise ValueError("control override must contain one finite value per actuator")
    limits = np.asarray(layout.ctrl_ranges)
    if np.any(action < limits[:, 0]) or np.any(action > limits[:, 1]):
        raise ValueError("control override is outside model actuator control ranges")
    if not isinstance(source, str) or not source.strip() or len(source) > 128:
        raise ValueError("control override source must be a nonempty string of at most 128 characters")
    if source in {"local_reflex", "nominal_trot"}:
        raise ValueError("control override source must not use a built-in controller label")
    return action.copy(), source


def run_physics_trial(
    spec: PhysicsTrialSpec | Mapping[str, Any] | Any,
    event_logger: EventLoggerLike | Callable[[Mapping[str, Any]], Any] | None = None,
    *,
    on_detection: Callable[[str, PhysicsMeasurements, int], None] | None = None,
    before_control: Callable[[int, str | None, PhysicsMeasurements | None], tuple[Sequence[float], str] | None] | None = None,
) -> dict[str, Any]:
    """Run a persistent-fault physical control trial.

    ``spec`` may be :class:`PhysicsTrialSpec`, a mapping, or the package's
    manifest ``TrialSpec``.  Manifest-only fields are ignored and physics
    overrides can be supplied under ``metadata["physics"]``.

    Hooks are synchronous, nonblocking integration points; a caller must enqueue
    asynchronous supervisor work, never wait for inference in either callback.
    on_detection receives the first detector-selected leg, a copied noisy
    observation, and step. before_control receives the latest copied noisy
    observation (None before step 0); None preserves the immediately available
    built-in action, while (action, source) applies a range-validated override.
    """

    resolved = _coerce_physics_spec(spec)
    try:
        from dm_control import suite
    except ImportError as exc:  # pragma: no cover - depends on experiment host
        raise RuntimeError("dm_control is required for a physics trial") from exc

    control_timestep = 0.02
    measured_steps = int(math.ceil(resolved.duration_s / control_timestep))
    # Include settle time and a safety margin so a measured-phase LAST cannot
    # occur.  If it nevertheless does, QuadrupedTrial aborts instead of reset.
    time_limit = (resolved.settle_steps + measured_steps + 10) * control_timestep
    env = suite.load(
        "quadruped",
        "walk",
        task_kwargs={"random": int(resolved.seed), "time_limit": time_limit},
    )
    trial = QuadrupedTrial(env, realtime=resolved.realtime)
    fault = PlantActuatorFault(trial.physics, trial.layout)
    detector = ResidualFaultDetector(
        trial.layout,
        fault.nominal_gainprm,
        fault.nominal_biasprm,
        control_timestep=trial.control_timestep,
        config=resolved.detector,
        nominal_forcelimited=trial.physics.model.actuator_forcelimited,
        nominal_forcerange=trial.physics.model.actuator_forcerange,
    )
    nominal = nominal_trot_preset(phase_steps=resolved.gait_phase_steps)
    local_presets = {
        leg: deterministic_stabilization_preset(leg) for leg in CANONICAL_LEGS
    }
    fault_step = min(
        measured_steps - 1,
        int(math.ceil(resolved.fault_offset_s / trial.control_timestep)),
    )

    initial_yaw_rad, gait_phase_offset_steps = _paired_scenario_variation(
        resolved, gait_cycle_steps=nominal.cycle_steps
    )
    noise_seed = resolved.detector_noise_seed
    if noise_seed is None:
        noise_seed = int.from_bytes(hashlib.sha256(
            f"hera-v3-detector-noise|{resolved.seed}".encode("ascii")
        ).digest()[:8], "big")
    measurement_noise = DetectorMeasurementNoise(resolved.detector_noise, seed=noise_seed)
    trial.initialize(settle_steps=resolved.settle_steps, yaw_rad=initial_yaw_rad)
    initial_measurement = trial.extractor.capture(
        standing_preset().to_action(trial.layout), monotonic_ns=trial.clock_ns()
    )
    torso_id = trial.physics.model.name2id("torso", "body")
    original_external_force = trial.physics.data.xfrc_applied[torso_id].copy()
    push_start_step = int(math.ceil(resolved.lateral_push_start_s / trial.control_timestep))
    push_end_step = int(math.ceil(
        (resolved.lateral_push_start_s + resolved.lateral_push_duration_s)
        / trial.control_timestep
    ))
    push_direction = np.array([-math.sin(initial_yaw_rad), math.cos(initial_yaw_rad), 0.0])
    trial_start_ns = trial.clock_ns()
    physics_identity = EventIdentity(
        batch_id=resolved.batch_id,
        run_id=resolved.run_id,
        trial_id=resolved.trial_id,
        request_id=None,
        request_type="physics",
        generation=0,
    )
    _emit_event(
        event_logger,
        "physics_trial_started",
        monotonic_ns=trial_start_ns,
        identity=physics_identity,
        trial_start_ns=trial_start_ns,
        method=resolved.method,
        seed=resolved.seed,
        initial_yaw_rad=initial_yaw_rad,
        gait_phase_offset_steps=gait_phase_offset_steps,
        injection_phase_steps=(fault_step + gait_phase_offset_steps) % nominal.cycle_steps,
        measurement_sync=MEASUREMENT_SYNC,
        detector_noise_seed=noise_seed,
        detector_noise=asdict(resolved.detector_noise),
        fault_enabled=resolved.fault_enabled,
        settled_upright=trial.settled_upright,
        settled_torso_height=trial.settled_torso_height,
        reset_count=trial.reset_count,
        actuator_layout=trial.layout.as_dict(),
    )

    fault_ns: int | None = None
    fault_parameter_snapshot: dict[str, Any] | None = None
    reference_ns: int | None = None
    detect_ns: int | None = None
    detect_step: int | None = None
    detector_selected_leg: str | None = None
    detector_selected_ns: int | None = None
    reflex_ns: int | None = None
    reflex_step: int | None = None
    false_triggers: list[dict[str, Any]] = []
    trace: list[dict[str, Any]] = []
    pre_fault_samples: list[PhysicsMeasurements] = []
    post_fault_samples: list[tuple[int, PhysicsMeasurements]] = []
    pre_fault_eligibility: dict[str, Any] | None = None
    last_observed_measurement: PhysicsMeasurements | None = None
    detector_history: list[tuple[int, DetectorUpdate]] = []
    action_sources: Counter[str] = Counter()
    hook_durations_ns: list[int] = []
    push_steps = 0
    pacing_lateness: list[int] = []
    fault_active_until_trial_end = False

    try:
        for step_index in range(measured_steps):
            reflex_active = (
                resolved.method == "local_reflex"
                and detector_selected_leg is not None
            )
            action = (
                local_presets[detector_selected_leg].to_action(trial.layout)
                if reflex_active
                else nominal.action_at(step_index + gait_phase_offset_steps, trial.layout)
            )
            action_source = "local_reflex" if reflex_active else "nominal_trot"
            if before_control is not None:
                hook_started_ns = trial.clock_ns()
                override = before_control(
                    step_index, detector_selected_leg,
                    copy_measurements(last_observed_measurement) if last_observed_measurement is not None else None,
                )
                hook_durations_ns.append(trial.clock_ns() - hook_started_ns)
                if override is not None:
                    action, action_source = validate_control_override(override, trial.layout)
                    reflex_active = False
            action_sources[action_source] += 1

            def before_step(now_ns: int, *, index: int = step_index) -> None:
                nonlocal fault_ns, reference_ns, pre_fault_eligibility, push_steps, fault_parameter_snapshot
                pushing = bool(resolved.lateral_push_force_n) and push_start_step <= index < push_end_step
                trial.physics.data.xfrc_applied[torso_id] = original_external_force
                if pushing:
                    trial.physics.data.xfrc_applied[torso_id, :3] += (
                        resolved.lateral_push_force_n * push_direction
                    )
                    push_steps += 1
                if resolved.lateral_push_force_n and index in {push_start_step, push_end_step}:
                    _emit_event(
                        event_logger, "lateral_push_started" if pushing else "lateral_push_finished",
                        monotonic_ns=now_ns, identity=physics_identity,
                        trial_start_ns=trial_start_ns, step=index,
                        force_world_n=(resolved.lateral_push_force_n * push_direction).tolist() if pushing else [0., 0., 0.],
                    )
                if index != fault_step:
                    return
                pre_fault_eligibility = evaluate_pre_fault_eligibility(
                    pre_fault_samples,
                    resolved,
                    detector_triggered_before_fault=detector_selected_leg is not None,
                )
                _emit_event(
                    event_logger,
                    "pre_fault_eligibility_evaluated",
                    monotonic_ns=now_ns,
                    identity=physics_identity,
                    trial_start_ns=trial_start_ns,
                    step=index,
                    **pre_fault_eligibility,
                )
                # Ineligible trials remain in the denominator.  There is no
                # reset, resampling, or threshold adjustment after this gate.
                if resolved.fault_enabled:
                    fault.inject(
                        resolved.fault_leg,
                        strength_scale=resolved.fault_strength,
                    )
                    fault_ns = trial.clock_ns()
                    fault_parameter_snapshot = fault.parameter_snapshot(resolved.fault_leg)
                reference_ns = trial.clock_ns() if fault_ns is None else fault_ns
                _emit_event(
                    event_logger,
                    "plant_fault_injected" if resolved.fault_enabled else "healthy_reference_reached",
                    monotonic_ns=reference_ns,
                    identity=physics_identity,
                    trial_start_ns=trial_start_ns,
                    step=index,
                    fault_leg=resolved.fault_leg,
                    strength_scale=resolved.fault_strength,
                    fault_enabled=resolved.fault_enabled,
                    fault_parameter_snapshot=fault_parameter_snapshot,
                    injection_phase_steps=(index + gait_phase_offset_steps) % nominal.cycle_steps,
                    sim_time_s=index * trial.control_timestep,
                )

            control_step = trial.step(action, before_step=before_step)
            if control_step.pacing is not None:
                pacing_lateness.append(control_step.pacing.lateness_ns)
            if reflex_active and reflex_ns is None:
                reflex_ns = control_step.step_started_ns
                reflex_step = step_index
                _emit_event(
                    event_logger,
                    "local_reflex_applied",
                    monotonic_ns=reflex_ns,
                    identity=physics_identity,
                    trial_start_ns=trial_start_ns,
                    step=step_index,
                    detected_leg=detector_selected_leg,
                    preset=local_presets[detector_selected_leg].name,
                    plant_fault_still_active=resolved.fault_leg in fault.active,
                )
            observed_measurement = measurement_noise.apply(
                control_step.measurements,
                nominal_effort=detector._nominal_force(control_step.measurements),
                minimum_expected_force=resolved.detector.minimum_expected_force,
            )
            last_observed_measurement = observed_measurement
            update = detector.update(observed_measurement)
            detector_history.append((step_index, update))

            for leg in update.triggered_legs:
                if detector_selected_leg is None:
                    # The controller selects a response from measured detector
                    # output only; it never reads PlantActuatorFault.active or
                    # the ground-truth injected leg to choose an action.
                    detector_selected_leg = leg
                    detector_selected_ns = update.monotonic_ns
                    if on_detection is not None:
                        hook_started_ns = trial.clock_ns()
                        on_detection(leg, copy_measurements(observed_measurement), step_index)
                        hook_durations_ns.append(trial.clock_ns() - hook_started_ns)
                if leg == resolved.fault_leg and fault_ns is not None:
                    if detect_ns is not None:
                        continue
                    detect_ns = update.monotonic_ns
                    detect_step = step_index
                    _emit_event(
                        event_logger,
                        "fault_detected",
                        monotonic_ns=detect_ns,
                        identity=physics_identity,
                        trial_start_ns=trial_start_ns,
                        step=step_index,
                        fault_leg=leg,
                        score=update.residuals[leg].score,
                        sim_time_s=control_step.sim_time_s,
                    )
                else:
                    false_trigger = {
                        "leg": leg,
                        "step": step_index,
                        "monotonic_ns": update.monotonic_ns,
                    }
                    false_triggers.append(false_trigger)
                    _emit_event(
                        event_logger,
                        "detector_false_trigger",
                        monotonic_ns=update.monotonic_ns,
                        identity=physics_identity,
                        trial_start_ns=trial_start_ns,
                        leg=leg,
                        step=step_index,
                    )

            measurement = control_step.measurements
            if reference_ns is not None:
                post_fault_samples.append((step_index, measurement))
            else:
                pre_fault_samples.append(measurement)

            if resolved.record_trace:
                trace.append(
                    {
                        "step": step_index,
                        "sim_time_s": control_step.sim_time_s,
                        "monotonic_ns": measurement.monotonic_ns,
                        "control_started_ns": control_step.step_started_ns,
                        "commanded_action": action.tolist(),
                        "action_source": action_source,
                        "fault_active": resolved.fault_leg in fault.active,
                        "detector_active_legs": list(update.active_legs),
                        "detector_scores": {
                            leg: update.residuals[leg].score for leg in CANONICAL_LEGS
                        },
                        "force_model_residuals": {
                            leg: update.residuals[leg].force_model_residual
                            for leg in CANONICAL_LEGS
                        },
                        "torso_upright": measurement.torso_upright,
                        "torso_height": measurement.torso_height,
                        "imu_gyro_norm": float(np.linalg.norm(measurement.imu_gyro)),
                        "imu_accel_norm": float(np.linalg.norm(measurement.imu_accel)),
                        "torso_speed": float(np.linalg.norm(measurement.torso_velocity)),
                        "torso_velocity_body_m_s": measurement.torso_velocity.tolist(),
                        "torso_velocity_world_m_s": measurement.torso_velocity_world.tolist(),
                        "torso_position_world_m": measurement.torso_position_world.tolist(),
                        "reference_reached": reference_ns is not None,
                        "lateral_push_active": bool(resolved.lateral_push_force_n) and push_start_step <= step_index < push_end_step,
                        "detector_observed_fault_leg_actuator_force": trial.layout.split(observed_measurement.actuator_force)[resolved.fault_leg].tolist(),
                        "detector_observed_gyro_norm": float(np.linalg.norm(observed_measurement.imu_gyro)),
                        "fault_leg_qpos": measurement.joint_qpos_by_leg[
                            resolved.fault_leg
                        ].tolist(),
                        "fault_leg_qvel": measurement.joint_qvel_by_leg[
                            resolved.fault_leg
                        ].tolist(),
                        "fault_leg_actuator_force": trial.layout.split(
                            measurement.actuator_force
                        )[resolved.fault_leg].tolist(),
                    }
                )

        fault_active_until_trial_end = resolved.fault_leg in fault.active
        end_ns = trial.clock_ns()
        if pre_fault_eligibility is None:
            pre_fault_eligibility = evaluate_pre_fault_eligibility(
                pre_fault_samples, resolved,
                detector_triggered_before_fault=detector_selected_leg is not None,
            )
        physical_safety = summarize_physical_safety(
            post_fault_samples, resolved,
            control_timestep=trial.control_timestep,
            pre_fault_eligible=pre_fault_eligibility["eligible"],
        )
        detector_correct = bool(
            detect_ns is not None
            and detector_selected_leg == resolved.fault_leg
            and not false_triggers
        ) if resolved.fault_enabled else None
        reflex_applied_correctly = (
            bool(
                reflex_ns is not None
                and detector_selected_leg == resolved.fault_leg
                and fault_ns is not None
                and reflex_ns >= fault_ns
            )
            if resolved.method == "local_reflex" and resolved.fault_enabled else None
        )
        post_measurements = [measurement for _, measurement in post_fault_samples]
        pre_movement = summarize_movement(
            pre_fault_samples, initial_yaw_rad=initial_yaw_rad,
            terminal_window_steps=resolved.terminal_window_steps,
            control_timestep=trial.control_timestep,
            reference_position_world=initial_measurement.torso_position_world,
        )
        post_movement = summarize_movement(
            post_measurements, initial_yaw_rad=initial_yaw_rad,
            terminal_window_steps=resolved.terminal_window_steps,
            control_timestep=trial.control_timestep,
            reference_position_world=(pre_fault_samples[-1].torso_position_world if pre_fault_samples else initial_measurement.torso_position_world),
        )
        # The common physical endpoint is evaluated over the complete fixed
        # horizon and is independent of either method's detector/reflex result.
        success = bool(physical_safety["safe"])
        result = {
            "trial_id": resolved.trial_id,
            "method": resolved.method,
            "fault_leg": resolved.fault_leg,
            "seed": resolved.seed,
            "fault_enabled": resolved.fault_enabled,
            "fault_parameter_snapshot": fault_parameter_snapshot,
            "measurement_sync": MEASUREMENT_SYNC,
            "nominal_actuator_model": {
                "gain_type": "fixed",
                "bias_type": "affine",
                "activation_timing": "current activation; actearly disabled",
                "forcelimited": np.asarray(trial.physics.model.actuator_forcelimited, dtype=bool).tolist(),
                "forcerange": np.asarray(trial.physics.model.actuator_forcerange, dtype=float).tolist(),
                "force_prediction": "clip(gain[0]*activation + bias[0] + bias[1]*length + bias[2]*velocity) only for force-limited actuators",
            },
            "reference_step": fault_step,
            "reference_monotonic_ns": reference_ns,
            "fault_monotonic_ns": fault_ns,
            "detect_monotonic_ns": detect_ns,
            "reflex_monotonic_ns": reflex_ns,
            "detector_noise_seed": noise_seed,
            "detector_noise": asdict(resolved.detector_noise),
            "measurement_noise_scope": "detector and hook observation copies only; physical outcomes use noiseless ground truth",
            "initial_yaw_rad": initial_yaw_rad,
            "gait_phase_offset_steps": gait_phase_offset_steps,
            "injection_phase_steps": (fault_step + gait_phase_offset_steps) % nominal.cycle_steps,
            "initial_noncontact_height": trial.initial_noncontact_height,
            "settled_upright": trial.settled_upright,
            "settled_torso_height": trial.settled_torso_height,
            "settled_gyro_norm": trial.settled_gyro_norm,
            "success": success,
            "physical_safe": success,
            "physical_safety_evaluable": physical_safety["evaluable"],
            "physical_safety": physical_safety,
            "pre_fault_eligible": pre_fault_eligibility["eligible"],
            "pre_fault_eligibility": pre_fault_eligibility,
            "detector_correct": detector_correct,
            "detector_selected_leg": detector_selected_leg,
            "reflex_applied_correctly": reflex_applied_correctly,
            "false_reflex_applied": bool(reflex_ns is not None and (not resolved.fault_enabled or detector_selected_leg != resolved.fault_leg or fault_ns is None or reflex_ns < fault_ns)),
            "fall_detected": physical_safety.get("fall", False),
            "fault_injected": fault_ns is not None,
            "detected": detect_ns is not None if resolved.fault_enabled else None,
            "stabilized": success,
            "fault_step": fault_step,
            "detect_step": detect_step,
            "reflex_step": reflex_step,
            "stable_step": None,
            "detect_latency_ms": (
                (detect_ns - fault_ns) / 1_000_000
                if detect_ns is not None and fault_ns is not None
                else None
            ),
            "detect_latency_sim_s": (
                # Fault precedes this step's integration; detection observes
                # the end-of-step sample, which is (detect_step + 1) * dt.
                (detect_step + 1 - fault_step) * trial.control_timestep
                if detect_step is not None
                else None
            ),
            "reflex_dispatch_latency_sim_s": (
                (reflex_step - detect_step - 1) * trial.control_timestep
                if reflex_step is not None and detect_step is not None else None
            ),
            "fault_to_reflex_sim_s": (
                (reflex_step - fault_step) * trial.control_timestep
                if reflex_step is not None and fault_ns is not None else None
            ),
            "reflex_dispatch_latency_ms": (
                (reflex_ns - detector_selected_ns) / 1_000_000
                if reflex_ns is not None and detector_selected_ns is not None
                else None
            ),
            # Kept for compatibility only: fixed-horizon safety is not a
            # recovery event, so it must not be plotted as response latency.
            "stability_latency_ms": None,
            "stability_latency_sim_s": None,
            "reset_count": trial.reset_count,
            "settle_steps": trial.settle_steps,
            "measured_steps": trial.trial_steps,
            "fault_active_until_trial_end": fault_active_until_trial_end,
            "false_triggers": false_triggers,
            "false_trigger_count": len(false_triggers),
            "residual_threshold_excursions": summarize_detector_excursions(
                detector_history, reference_step=fault_step,
                threshold=resolved.detector.enter_threshold,
            ),
            "movement": {"pre_reference": pre_movement, "post_reference": post_movement},
            "mean_speed_m_s": post_movement.get("mean_speed_m_s"),
            "terminal_mean_speed_m_s": post_movement.get("terminal_mean_speed_m_s"),
            "mean_gyro_rad_s": post_movement.get("mean_gyro_rad_s"),
            "terminal_mean_gyro_rad_s": post_movement.get("terminal_mean_gyro_rad_s"),
            "forward_displacement_m": post_movement.get("forward_displacement_m"),
            "lateral_displacement_m": post_movement.get("lateral_displacement_m"),
            "action_source_steps": dict(action_sources),
            "action_source_duty_fraction": {name: count / trial.trial_steps for name, count in action_sources.items()},
            "lateral_push": {
                "force_n": resolved.lateral_push_force_n,
                "force_world_n": (resolved.lateral_push_force_n * push_direction).tolist(),
                "applied_steps": push_steps,
                "applied_duration_s": push_steps * trial.control_timestep,
                "impulse_n_s": resolved.lateral_push_force_n * push_steps * trial.control_timestep,
                "reference": "fixed initial-yaw lateral axis; applied to torso COM in world frame",
            },
            "maximum_hook_duration_ms": max(hook_durations_ns) / 1e6 if hook_durations_ns else None,
            "hook_overrun_count": sum(value > trial.control_timestep * 1e9 for value in hook_durations_ns),
            "post_fault_min_upright": physical_safety.get("minimum_upright"),
            "post_fault_min_torso_height": physical_safety.get("minimum_torso_height"),
            "post_fault_upright_fraction": physical_safety.get("upright_fraction"),
            "terminal_safe_fraction": physical_safety.get("terminal_safe_fraction"),
            "upright_deficit_integral": physical_safety.get("integrated_upright_deficit_s"),
            "max_pacing_lateness_ms": (
                max(pacing_lateness) / 1_000_000 if pacing_lateness else None
            ),
            "actuator_layout": trial.layout.as_dict(),
            "spec": {
                **asdict(resolved),
                "detector": asdict(resolved.detector),
            },
            "trace": trace,
        }
        _emit_event(
            event_logger,
            "physics_trial_finished",
            monotonic_ns=end_ns,
            identity=physics_identity,
            trial_start_ns=trial_start_ns,
            success=success,
            pre_fault_eligible=pre_fault_eligibility["eligible"],
            detector_correct=detector_correct,
            reflex_applied_correctly=reflex_applied_correctly,
            fall_detected=physical_safety.get("fall", False),
            measured_steps=trial.trial_steps,
            reset_count=trial.reset_count,
            fault_active_until_trial_end=fault_active_until_trial_end,
        )
        return result
    finally:
        # Restoration occurs only after the measured trial (or after an abort),
        # never when a preset arrives or a detector event fires.
        fault.restore_all()
        trial.physics.data.xfrc_applied[torso_id] = original_external_force


__all__ = [
    "ActuatorLayout",
    "ControlStep",
    "DetectorUpdate",
    "DetectorNoiseConfig",
    "DetectorMeasurementNoise",
    "EpisodeTerminatedError",
    "LayoutError",
    "LegResidual",
    "MeasurementExtractor",
    "MEASUREMENT_SYNC",
    "PhysicsMeasurements",
    "PhysicsTrialSpec",
    "PlantActuatorFault",
    "PacingSample",
    "QuadrupedTrial",
    "RealtimePacer",
    "ResidualDetectorConfig",
    "ResidualFaultDetector",
    "copy_measurements",
    "evaluate_pre_fault_eligibility",
    "summarize_physical_safety",
    "summarize_movement",
    "summarize_detector_excursions",
    "validate_control_override",
    "validate_actuator_force_model",
    "run_physics_trial",
]
