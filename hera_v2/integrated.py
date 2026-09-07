"""Live plant/LLM integration with a nonblocking deterministic safety path.

The supervisory payload deliberately reaffirms a trusted support command. This
is an integration/latency and failure-containment test, NOT a demonstration of
LLM-discovered locomotion or extra physical benefit over the same local reflex.
"""
from __future__ import annotations

import asyncio
import threading
from typing import Any

import numpy as np

from .events import EventIdentity, EventLogger
from .manifest import TrialSpec
from .ollama_backend import OllamaBackend
from .physics import PhysicsMeasurements, PhysicsTrialSpec, run_physics_trial
from .scheduler import EmergencyContext, FaultObservation, TrialSpec as SchedulerSpec, run_scheduler_trial


LEG_CHANNELS = {"FL": (0, 1, 2), "FR": (3, 4, 5), "BR": (6, 7, 8), "BL": (9, 10, 11)}


def support_action(leg: str) -> tuple[float, ...]:
    return tuple(x for name in ("FL", "FR", "BR", "BL")
                 for x in ((0.0, 0.0, -0.4) if name == leg else (0.0, -0.08, -0.48)))


class EmergencyFailureBackend:
    """Explicit failure injection: real mission, failing emergency transport."""
    def __init__(self, backend: Any):
        self.backend = backend

    async def stream(self, request: Any, on_stage=None):
        if request.request_type.value == "emergency":
            raise ConnectionError("preregistered injected emergency transport failure")
        async for chunk in self.backend.stream(request, on_stage=on_stage):
            yield chunk


class SupervisoryMailbox:
    """Single accepted generation; finite command duration then local fallback."""
    def __init__(self):
        self.lock = threading.Lock()
        self.command: Any = None
        self.accepted_ns: int | None = None
        self.first_step: int | None = None
        self.applied_ns: int | None = None
        self.applied_steps = 0

    def accept(self, preset: Any, accepted_ns: int) -> None:
        with self.lock:
            if self.command is not None:
                raise ValueError("duplicate supervisory acceptance")
            self.command, self.accepted_ns = preset, accepted_ns

    def action(self, step: int, selected_leg: str | None, measurement: Any):
        with self.lock:
            if self.command is None or selected_leg is None:
                return None
            phase = self.command.actions[0]
            if self.first_step is None:
                self.first_step = step
            if step - self.first_step >= phase.duration_steps:
                return None
            self.applied_steps += 1
            return np.asarray(phase.actuator_targets, dtype=float), "validated_supervisor"


async def run_integrated_trial(spec: TrialSpec, logger: EventLogger, physics_logger: Any) -> dict[str, Any]:
    identity = EventIdentity(spec.batch_id, spec.trial_id, spec.trial_id, None, "integrated", 0)
    logger.emit("integrated_trial_started", identity, {"method": spec.method})
    ready = threading.Event()
    detected = threading.Event()
    state: dict[str, Any] = {}
    mailbox = SupervisoryMailbox()
    metadata = spec.metadata or {}
    physics_options = dict(metadata.get("physics", {}))
    if physics_options.get("record_trace", True) is not True:
        raise ValueError("integrated trials require record_trace=True for application audit")
    physics_options["record_trace"] = True
    physics_spec = PhysicsTrialSpec(
        batch_id=spec.batch_id, run_id=spec.trial_id, trial_id=spec.trial_id,
        method="local_reflex", fault_leg=spec.fault_leg, seed=spec.seed,
        duration_s=spec.duration_s, fault_offset_s=spec.fault_offset_s,
        realtime=True, **physics_options,
    )

    def on_detection(leg: str, measurement: PhysicsMeasurements, step: int) -> None:
        if detected.is_set():
            return
        channels = LEG_CHANNELS[leg]
        context = EmergencyContext(
            fault_label=f"measured residual on {leg}", affected_channels=channels,
            observed_values=tuple(float(measurement.actuator_force[i]) for i in channels),
            safe_action=support_action(leg), duration_steps=50,
            observation=f"measured residual trigger at step {step}; gyro_norm={np.linalg.norm(measurement.imu_gyro):.6g}",
        )
        state.update(context=context, detection_ns=measurement.monotonic_ns, detected_leg=leg)
        detected.set()

    def plant() -> dict[str, Any]:
        if not ready.wait(timeout=35.0):
            raise TimeoutError("plant start barrier did not release")
        return run_physics_trial(
            physics_spec, physics_logger, on_detection=on_detection,
            before_control=mailbox.action,
        )

    plant_task = asyncio.create_task(asyncio.to_thread(plant))
    initial_context = EmergencyContext(
        fault_label="awaiting physical detector", affected_channels=LEG_CHANNELS[spec.fault_leg],
        observed_values=(0.0, 0.0, 0.0), safe_action=support_action(spec.fault_leg),
        duration_steps=50,
    )

    async def on_fault():
        if not await asyncio.to_thread(detected.wait, spec.duration_s + 5.0):
            raise TimeoutError("no measured detector event within plant horizon")
        return FaultObservation(context=state["context"], detected_monotonic_ns=state["detection_ns"])

    async def on_ready():
        ready.set()

    scheduler_result: dict[str, Any] | None = None
    scheduler_error: str | None = None
    backend = None
    try:
        if spec.method == "local_only":
            ready.set()
        else:
            method = "hera_preempt" if spec.method == "backend_failure" else spec.method
            scheduler_spec = SchedulerSpec(
                batch_id=spec.batch_id, run_id=spec.trial_id, trial_id=spec.trial_id,
                method=method, model=spec.model,
                mission_prompt=(f"Scenario seed {spec.seed}; sector {spec.fault_leg}. "
                                "Write 160 numbered inspection steps, each with navigation, sensing, risk, and fallback. Continue to step 160."),
                emergency=initial_context, emergency_timeout_s=spec.deadline_s,
                emergency_max_attempts=1, mission_barrier_timeout_s=30,
                cancel_grace_s=5, mission_temperature=0.0, emergency_temperature=0.0,
                seed=spec.seed, mission_num_predict=spec.mission_num_predict,
                emergency_num_predict=512, on_mission_ready=on_ready, on_fault=on_fault,
                on_emergency_accepted=mailbox.accept, require_active_mission_at_fault=False,
            )
            try:
                backend = OllamaBackend(spec.ollama_host)
                selected_backend = EmergencyFailureBackend(backend) if spec.method == "backend_failure" else backend
                scheduler_result = await run_scheduler_trial(scheduler_spec, selected_backend, logger)
            except Exception as exc:
                scheduler_error = f"{type(exc).__name__}: {exc}"
            finally:
                # Even total inference unavailability cannot prevent local control.
                ready.set()
        physical = await plant_task
    finally:
        ready.set()
        if backend is not None:
            await backend.aclose()
        if not plant_task.done():
            await plant_task

    trace = physical.get("trace", [])
    applied = [row for row in trace if row["action_source"] == "validated_supervisor"]
    first_applied = applied[0] if applied else None
    accepted_ns = mailbox.accepted_ns
    # Trace measurement timestamps follow integration; explicit control-start
    # timestamps (provided by physics) define application time.
    applied_ns = int(first_applied["control_started_ns"]) if first_applied else None
    application = {
        "accepted": accepted_ns is not None,
        "applied": applied_ns is not None,
        "accepted_monotonic_ns": accepted_ns,
        "applied_monotonic_ns": applied_ns,
        "first_applied_step": first_applied["step"] if first_applied else None,
        "applied_steps": len(applied),
        "accept_to_apply_ms": (applied_ns - accepted_ns) / 1e6 if applied_ns is not None else None,
        "detect_to_apply_ms": (applied_ns - state["detection_ns"]) / 1e6 if applied_ns is not None else None,
        "command": mailbox.command.model_dump(mode="json") if mailbox.command is not None else None,
        "same_as_local_support": True,
        "interpretation": "bounded supervisory reaffirmation; not LLM-generated physical recovery",
    }
    if applied_ns is not None:
        if accepted_ns is None or applied_ns < accepted_ns:
            raise RuntimeError("supervisor command applied before validation")
        expected = np.asarray(mailbox.command.actions[0].actuator_targets)
        for row in applied:
            if not np.array_equal(np.asarray(row["commanded_action"]), expected):
                raise RuntimeError("actual plant action differs from accepted payload")
    raw = {**physical, "method": spec.method, "scheduler": scheduler_result,
           "scheduler_error": scheduler_error, "supervisory_application": application,
           "supervisory_success": bool(application["applied"]),
           "injected_backend_failure": spec.method == "backend_failure"}
    logger.emit("integrated_trial_finished", identity,
                {"physical_safe": raw["physical_safe"], "supervisory_applied": application["applied"]})
    return raw
