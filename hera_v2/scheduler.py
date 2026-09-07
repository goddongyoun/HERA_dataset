"""Policy-controlled LLM scheduling and runtime hooks for HERA v3 trials.

The three policies share the same request envelope, emergency prompt, JSON
schema, timeout, retry loop, and acceptance checks.  Only lane assignment and
whether the active mission stream is cancelled differ. The runtime supplies the
entire correct emergency command: this is a scheduling/formatting benchmark,
not a test of an LLM discovering a safe control action.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
import math
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Literal, Mapping, Sequence, cast

from .events import EventIdentity, EventLogger, EventStage
from .ollama_backend import BackendChunk, StreamingBackend
from .schema import (
    BackendRequest,
    ChatMessage,
    EmergencyPreset,
    RequestType,
    emergency_json_schema,
    parse_emergency_response,
)


SchedulerMethod = Literal["hera_preempt", "fifo_single", "reserved_slot"]
SUPPORTED_METHODS: tuple[SchedulerMethod, ...] = (
    "hera_preempt",
    "fifo_single",
    "reserved_slot",
)

MISSION_SYSTEM_PROMPT = (
    "You are the high-level planner for a quadruped robot. Produce a detailed "
    "plain-text mission plan. This mission request is intentionally long-running "
    "so scheduling behavior can be measured."
)

EMERGENCY_SYSTEM_PROMPT = """You serialize a runtime-specified emergency command.
Return only one JSON object that conforms exactly to the supplied JSON Schema.
The complete correct command, including duration, name, and stopping rule, is
supplied by the runtime. Reproduce it exactly; do not infer control semantics.
This is a scheduling and formatting benchmark, not a control-planning task.
Do not include Markdown or explanatory text outside the JSON."""


@dataclass(frozen=True, slots=True)
class EmergencyContext:
    """Runtime facts used to build the policy-independent emergency prompt."""

    fault_label: str
    affected_channels: tuple[int, ...]
    observed_values: tuple[float, ...]
    safe_action: tuple[float, ...]
    observation: str = "tracking residual exceeded the configured detector threshold"
    duration_steps: int = 1
    skill_name: str = "runtime_safe_stabilization"
    stop_condition: Literal["duration_steps_elapsed"] = "duration_steps_elapsed"

    def __post_init__(self) -> None:
        if not self.fault_label.strip():
            raise ValueError("fault_label must not be blank")
        if len(self.safe_action) != 12:
            raise ValueError("safe_action must contain exactly 12 actuator commands")
        if not self.affected_channels:
            raise ValueError("affected_channels must not be empty")
        if len(set(self.affected_channels)) != len(self.affected_channels):
            raise ValueError("affected_channels must not contain duplicates")
        if any(channel < 0 or channel >= 12 for channel in self.affected_channels):
            raise ValueError("affected_channels must be in [0, 11]")
        numeric_values = (*self.observed_values, *self.safe_action)
        if any(not math.isfinite(float(value)) for value in numeric_values):
            raise ValueError("observed_values and safe_action must be finite")
        if any(float(value) < -1.0 or float(value) > 1.0 for value in self.safe_action):
            raise ValueError("safe_action values must be in [-1, 1]")
        if type(self.duration_steps) is not int or not 1 <= self.duration_steps <= 10000:
            raise ValueError("duration_steps must be an integer in [1, 10000]")
        if not isinstance(self.skill_name, str) or not self.skill_name.strip() or len(self.skill_name) > 128:
            raise ValueError("skill_name must be a nonblank string of at most 128 characters")
        if self.stop_condition != "duration_steps_elapsed":
            raise ValueError("one-shot stop_condition must be duration_steps_elapsed")


@dataclass(frozen=True, slots=True)
class FaultObservation:
    """A measured detection delivered from the same perf_counter_ns clock domain."""

    context: EmergencyContext
    detected_monotonic_ns: int
    phase_origin_monotonic_ns: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.context, EmergencyContext):
            raise ValueError("FaultObservation context must be EmergencyContext")
        if type(self.detected_monotonic_ns) is not int or self.detected_monotonic_ns <= 0:
            raise ValueError("detected_monotonic_ns must be a positive integer")
        if self.phase_origin_monotonic_ns is not None:
            if type(self.phase_origin_monotonic_ns) is not int or not (
                0 < self.phase_origin_monotonic_ns <= self.detected_monotonic_ns
            ):
                raise ValueError("phase origin must be positive and no later than detection")


FaultHookValue = FaultObservation | EmergencyContext | None
FaultHook = Callable[[], Awaitable[FaultHookValue] | FaultHookValue]
MissionReadyHook = Callable[[], Awaitable[None] | None]
EmergencyAcceptedHook = Callable[[EmergencyPreset, int], Awaitable[None] | None]


@dataclass(frozen=True, slots=True)
class TrialSpec:
    batch_id: str
    run_id: str
    trial_id: str
    method: SchedulerMethod
    model: str
    mission_prompt: str
    emergency: EmergencyContext
    emergency_timeout_s: float = 20.0
    emergency_max_attempts: int = 3
    mission_barrier_timeout_s: float = 30.0
    cancel_grace_s: float = 3.0
    mission_temperature: float = 0.7
    emergency_temperature: float = 0.0
    seed: int = 0
    mission_num_predict: int = 2048
    emergency_num_predict: int = 512
    on_fault: FaultHook | None = field(default=None, repr=False, compare=False)
    on_mission_ready: MissionReadyHook | None = field(default=None, repr=False, compare=False)
    on_emergency_accepted: EmergencyAcceptedHook | None = field(default=None, repr=False, compare=False)
    require_active_mission_at_fault: bool = True

    def __post_init__(self) -> None:
        for name in ("batch_id", "run_id", "trial_id", "model", "mission_prompt"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} must not be blank")
        if self.method not in SUPPORTED_METHODS:
            raise ValueError(
                f"unsupported method {self.method!r}; expected one of {SUPPORTED_METHODS}"
            )
        if self.emergency_timeout_s <= 0:
            raise ValueError("emergency_timeout_s must be positive")
        if self.emergency_max_attempts < 1:
            raise ValueError("emergency_max_attempts must be at least 1")
        if self.mission_barrier_timeout_s <= 0:
            raise ValueError("mission_barrier_timeout_s must be positive")
        if self.cancel_grace_s <= 0:
            raise ValueError("cancel_grace_s must be positive")
        for name in ("mission_temperature", "emergency_temperature"):
            value = float(getattr(self, name))
            if value < 0.0 or value > 2.0:
                raise ValueError(f"{name} must be in [0, 2]")
        if self.seed < 0:
            raise ValueError("seed cannot be negative")
        if self.mission_num_predict < 1 or self.emergency_num_predict < 1:
            raise ValueError("num_predict values must be positive")


class RequestStatus(str, Enum):
    QUEUED = "queued"
    DISPATCHED = "dispatched"
    STREAMING = "streaming"
    COMPLETE = "complete"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    FAILED = "failed"
    REJECTED = "rejected"
    ACCEPTED = "accepted"


@dataclass(slots=True)
class RequestState:
    request: BackendRequest
    lane: str
    status: RequestStatus = RequestStatus.QUEUED
    queued_ns: int | None = None
    dispatched_ns: int | None = None
    server_observed_ns: int | None = None
    first_chunk_ns: int | None = None
    completed_ns: int | None = None
    first_chunk: asyncio.Event = field(default_factory=asyncio.Event)
    response_text: str = ""
    backend_metadata: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def public_summary(self) -> dict[str, Any]:
        return {
            "request_id": self.request.request_id,
            "request_type": self.request.request_type.value,
            "generation": self.request.generation,
            "attempt": self.request.attempt,
            "lane": self.lane,
            "status": self.status.value,
            "queued_ns": self.queued_ns,
            "dispatched_ns": self.dispatched_ns,
            "server_observed_ns": self.server_observed_ns,
            "first_chunk_ns": self.first_chunk_ns,
            "completed_ns": self.completed_ns,
            "response_chars": len(self.response_text),
            "backend_metadata": dict(self.backend_metadata),
            "error": self.error,
        }


class StaleResponseError(RuntimeError):
    pass


class MissionBarrierError(RuntimeError):
    pass


class MissionNotActiveAtFaultError(RuntimeError):
    pass


class RequestDeadlineError(TimeoutError):
    pass


def build_emergency_prompt(context: EmergencyContext) -> str:
    """Build the one common user prompt used by every scheduling condition."""

    affected = ", ".join(str(index) for index in context.affected_channels)
    observed = ", ".join(f"{float(value):.8g}" for value in context.observed_values)
    command = json.dumps(canonical_emergency_preset(context).model_dump(mode="json"), separators=(",", ":"))
    return (
        f"EMERGENCY condition: {context.fault_label}. "
        f"Detector observation: {context.observation}. "
        f"Affected actuator channels: [{affected}]. "
        f"Observed values: [{observed}]. "
        "Serialize exactly this runtime-provided canonical command: "
        f"{command}. The one-shot action expires after duration_steps control "
        "steps; duration_steps_elapsed is the only stopping rule. "
        "The safe command is given, not inferred."
    )


def canonical_emergency_preset(context: EmergencyContext) -> EmergencyPreset:
    """The complete runtime contract, independent of model output or policy."""

    return EmergencyPreset.model_validate({
        "type": "one-shot", "skill_name": context.skill_name,
        "actions": [{"actuator_targets": list(context.safe_action), "duration_steps": context.duration_steps}],
        "stop_condition": context.stop_condition, "max_cycles": None,
    })


def build_emergency_schema(context: EmergencyContext) -> dict[str, Any]:
    """Bind every command field to the runtime-provided canonical contract.

    This keeps the scheduling benchmark from accepting a merely well-shaped
    but semantically different actuator command.  The same context-specific
    schema is used by every scheduling policy in a paired scenario.
    """

    schema = copy.deepcopy(emergency_json_schema())
    canonical = canonical_emergency_preset(context).model_dump(mode="json")
    for name in ("type", "skill_name", "stop_condition", "max_cycles"):
        schema["properties"][name]["const"] = canonical[name]
    actions = schema["properties"]["actions"]
    actions["minItems"] = 1
    actions["maxItems"] = 1
    phase = schema["$defs"]["ActionPhase"]
    phase["properties"]["actuator_targets"]["const"] = [
        float(value) for value in context.safe_action
    ]
    phase["properties"]["duration_steps"]["const"] = context.duration_steps
    return schema


def validate_emergency_semantics(
    preset: EmergencyPreset, context: EmergencyContext
) -> None:
    expected = canonical_emergency_preset(context).model_dump(mode="json")
    actual = preset.model_dump(mode="json")
    if actual != expected:
        differences = [key for key in expected if actual.get(key) != expected[key]]
        raise ValueError(
            "emergency preset does not reproduce the runtime canonical contract: "
            + ", ".join(differences)
        )


def _coerce_spec(spec: TrialSpec | Mapping[str, Any]) -> TrialSpec:
    if isinstance(spec, TrialSpec):
        return spec
    values = dict(spec)
    emergency = values.get("emergency")
    if isinstance(emergency, Mapping):
        values["emergency"] = EmergencyContext(**dict(emergency))
    return TrialSpec(**values)


class _TrialScheduler:
    def __init__(
        self,
        spec: TrialSpec,
        backend: StreamingBackend,
        event_logger: EventLogger,
    ) -> None:
        self.spec = spec
        self.backend = backend
        self.log = event_logger
        self.generation = 0
        self.states: list[RequestState] = []
        self._single_lane = asyncio.Lock()
        self._mission_lane = asyncio.Lock()
        self._emergency_lane = asyncio.Lock()
        self.fault_ns: int | None = None
        self.deadline_ns: int | None = None
        self.emergency_context = spec.emergency
        self.fault_received_ns: int | None = None
        self.phase_origin_ns: int | None = None
        self.mission_ready_ns: int | None = None
        self.mission_active_at_fault: bool | None = None
        self.command_delivery_completed_ns: int | None = None

    def _trial_identity(self) -> EventIdentity:
        return EventIdentity(
            batch_id=self.spec.batch_id,
            run_id=self.spec.run_id,
            trial_id=self.spec.trial_id,
            request_id=None,
            request_type="trial",
            generation=self.generation,
        )

    def _request_identity(self, state: RequestState) -> EventIdentity:
        request = state.request
        return EventIdentity(
            batch_id=self.spec.batch_id,
            run_id=self.spec.run_id,
            trial_id=self.spec.trial_id,
            request_id=request.request_id,
            request_type=request.request_type.value,
            generation=request.generation,
        )

    def _emit_trial(
        self, stage: EventStage | str, details: Mapping[str, Any] | None = None
    ) -> int:
        return self.log.emit(stage, self._trial_identity(), details).monotonic_ns

    def _emit_request(
        self,
        state: RequestState,
        stage: EventStage | str,
        details: Mapping[str, Any] | None = None,
    ) -> int:
        return self.log.emit(stage, self._request_identity(state), details).monotonic_ns

    def _lane_for(self, request_type: RequestType) -> tuple[str, asyncio.Lock]:
        if self.spec.method == "reserved_slot":
            if request_type is RequestType.MISSION:
                return "mission", self._mission_lane
            return "emergency", self._emergency_lane
        return "single", self._single_lane

    def _new_request(
        self,
        request_type: RequestType,
        *,
        generation: int,
        attempt: int,
    ) -> RequestState:
        if request_type is RequestType.MISSION:
            messages = (
                ChatMessage(role="system", content=MISSION_SYSTEM_PROMPT),
                ChatMessage(role="user", content=self.spec.mission_prompt),
            )
            response_schema = None
            temperature = self.spec.mission_temperature
            num_predict = self.spec.mission_num_predict
            request_seed = self.spec.seed
        else:
            messages = (
                ChatMessage(role="system", content=EMERGENCY_SYSTEM_PROMPT),
                ChatMessage(
                    role="user", content=build_emergency_prompt(self.emergency_context)
                ),
            )
            response_schema = build_emergency_schema(self.emergency_context)
            temperature = self.spec.emergency_temperature
            num_predict = self.spec.emergency_num_predict
            request_seed = self.spec.seed + generation * 1000 + attempt
        lane_name, _ = self._lane_for(request_type)
        state = RequestState(
            request=BackendRequest(
                request_id=str(uuid.uuid4()),
                request_type=request_type,
                generation=generation,
                attempt=attempt,
                model=self.spec.model,
                messages=messages,
                response_schema=response_schema,
                temperature=temperature,
                seed=request_seed,
                num_predict=num_predict,
            ),
            lane=lane_name,
        )
        self.states.append(state)
        state.queued_ns = self._emit_request(
            state,
            EventStage.REQUEST_QUEUED,
            {"lane": lane_name, "attempt": attempt, "method": self.spec.method},
        )
        return state

    async def _backend_stage(
        self, state: RequestState, stage: str, details: Mapping[str, Any]
    ) -> None:
        # The typed chunk is authoritative for the first-chunk barrier.  A
        # backend callback alone cannot make an incorrectly tagged chunk valid.
        if stage == "first_chunk":
            return
        if stage == "server_observed" and state.server_observed_ns is None:
            state.server_observed_ns = self._emit_request(
                state, EventStage.SERVER_OBSERVED, details
            )
            return
        known = {
            "connection_opening": EventStage.CONNECTION_OPENING,
            "response_headers": EventStage.RESPONSE_HEADERS,
            "connection_closed": EventStage.CONNECTION_CLOSED,
        }
        mapped = known.get(stage)
        if mapped is not None:
            self._emit_request(state, mapped, details)

    def _verify_chunk_identity(
        self, state: RequestState, chunk: BackendChunk
    ) -> None:
        expected = state.request
        if (
            chunk.request_id != expected.request_id
            or chunk.request_type is not expected.request_type
            or chunk.generation != expected.generation
        ):
            state.status = RequestStatus.REJECTED
            state.error = "backend returned a stale or misrouted response identity"
            self._emit_request(
                state,
                EventStage.RESPONSE_REJECTED_STALE,
                {
                    "expected_request_id": expected.request_id,
                    "expected_type": expected.request_type.value,
                    "expected_generation": expected.generation,
                    "actual_request_id": chunk.request_id,
                    "actual_type": (
                        chunk.request_type.value
                        if isinstance(chunk.request_type, RequestType)
                        else str(chunk.request_type)
                    ),
                    "actual_generation": chunk.generation,
                },
            )
            raise StaleResponseError(state.error)

    async def _consume_request(
        self, state: RequestState, lane_lock: asyncio.Lock
    ) -> str:
        acquired = False
        chunks: list[str] = []
        saw_done = False
        try:
            await lane_lock.acquire()
            acquired = True
            state.status = RequestStatus.DISPATCHED
            state.dispatched_ns = self._emit_request(
                state,
                EventStage.REQUEST_DISPATCHED,
                {"lane": state.lane, "attempt": state.request.attempt},
            )

            async def on_stage(stage: str, details: Mapping[str, Any]) -> None:
                await self._backend_stage(state, stage, details)

            async for chunk in self.backend.stream(state.request, on_stage=on_stage):
                self._verify_chunk_identity(state, chunk)
                if state.first_chunk_ns is None:
                    state.status = RequestStatus.STREAMING
                    state.first_chunk_ns = self._emit_request(
                        state,
                        EventStage.FIRST_CHUNK,
                        {"done": chunk.done, "text_chars": len(chunk.text)},
                    )
                    state.first_chunk.set()
                chunks.append(chunk.text)
                state.response_text = "".join(chunks)
                state.backend_metadata.update(chunk.metadata)
                saw_done = saw_done or chunk.done
            if not saw_done:
                raise RuntimeError("backend stream ended without a terminal done frame")
            state.response_text = "".join(chunks)
            state.status = RequestStatus.COMPLETE
            state.completed_ns = self._emit_request(
                state,
                EventStage.RESPONSE_COMPLETE,
                {"response_chars": len(state.response_text),
                 "backend_metadata": dict(state.backend_metadata)},
            )
            return state.response_text
        except asyncio.CancelledError:
            state.status = RequestStatus.CANCELLED
            state.error = "request task cancelled"
            state.completed_ns = self._emit_request(
                state, EventStage.REQUEST_CANCELLED, {"lane": state.lane}
            )
            raise
        except StaleResponseError:
            state.completed_ns = time.perf_counter_ns()
            raise
        except Exception as exc:
            state.status = RequestStatus.FAILED
            state.error = f"{type(exc).__name__}: {exc}"
            state.completed_ns = self._emit_request(
                state,
                EventStage.REQUEST_FAILED,
                {"error_type": type(exc).__name__, "error": str(exc)},
            )
            raise
        finally:
            if acquired:
                lane_lock.release()

    def _start(self, state: RequestState) -> asyncio.Task[str]:
        _, lock = self._lane_for(state.request.request_type)
        return asyncio.create_task(
            self._consume_request(state, lock),
            name=f"{state.request.request_type.value}:{state.request.request_id}",
        )

    async def _wait_for_mission_barrier(
        self, state: RequestState, task: asyncio.Task[str]
    ) -> None:
        barrier_waiter = asyncio.create_task(state.first_chunk.wait())
        try:
            done, _ = await asyncio.wait(
                {barrier_waiter, task},
                timeout=self.spec.mission_barrier_timeout_s,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if state.first_chunk.is_set():
                self._emit_request(
                    state,
                    EventStage.MISSION_BARRIER_PASSED,
                    {"method": self.spec.method},
                )
                return
            if task in done:
                try:
                    task.result()
                except Exception as exc:
                    raise MissionBarrierError(
                        f"mission failed before a valid first chunk: {exc}"
                    ) from exc
                raise MissionBarrierError("mission completed without a response chunk")
            raise MissionBarrierError(
                f"mission first-chunk barrier timed out after "
                f"{self.spec.mission_barrier_timeout_s:.3f}s"
            )
        finally:
            barrier_waiter.cancel()
            await asyncio.gather(barrier_waiter, return_exceptions=True)

    async def _cancel_request(
        self,
        state: RequestState,
        task: asyncio.Task[str],
        *,
        reason: str,
        max_wait_s: float | None = None,
    ) -> bool:
        if task.done():
            # Consume a possible exception so a completed background mission
            # never becomes an unobserved-task warning.
            try:
                task.result()
            except (asyncio.CancelledError, Exception):
                pass
            return True
        state.status = RequestStatus.CANCEL_REQUESTED
        self._emit_request(
            state, EventStage.CANCEL_REQUESTED, {"reason": reason}
        )
        task.cancel()
        timeout_s = self.spec.cancel_grace_s
        if max_wait_s is not None:
            timeout_s = max(0.0, min(timeout_s, max_wait_s))
        until_ns = time.perf_counter_ns() + int(timeout_s * 1_000_000_000)
        if max_wait_s is not None and self.deadline_ns is not None:
            until_ns = min(until_ns, self.deadline_ns)
        closed = await self._wait_task_until(task, until_ns)
        if not closed:
            state.error = (
                f"request did not close within cancellation budget {timeout_s:.6f}s"
            )
            self._emit_request(
                state,
                EventStage.REQUEST_FAILED,
                {"error_type": "CancellationTimeout", "error": state.error},
            )
            return False
        try:
            task.result()
        except (asyncio.CancelledError, Exception):
            pass
        return True

    @staticmethod
    async def _wait_task_until(task: asyncio.Task[Any], until_ns: int) -> bool:
        # asyncio's timer clock can wake early on Windows. Durations and
        # deadline decisions use perf_counter_ns consistently with event logs.
        while not task.done():
            remaining_s = (until_ns - time.perf_counter_ns()) / 1_000_000_000
            if remaining_s <= 0:
                return False
            await asyncio.wait({task}, timeout=remaining_s)
        return True

    async def _run_with_deadline(
        self, state: RequestState, task: asyncio.Task[str]
    ) -> str:
        remaining_s = self._remaining_deadline_s()
        if self.deadline_ns is None:
            raise RuntimeError("fault deadline has not been initialized")
        finished = await self._wait_task_until(task, self.deadline_ns)
        if not finished:
            self._emit_request(
                state,
                EventStage.CANCEL_REQUESTED,
                {
                    "reason": "emergency_deadline",
                    "absolute_deadline_ns": self.deadline_ns,
                    "remaining_s_at_wait_start": remaining_s,
                },
            )
            task.cancel()
            closed = await self._wait_task_until(
                task, time.perf_counter_ns() + int(self.spec.cancel_grace_s * 1_000_000_000)
            )
            if closed:
                try:
                    task.result()
                except (asyncio.CancelledError, Exception):
                    pass
            raise RequestDeadlineError(
                "fault-to-accept absolute deadline expired"
            )
        return task.result()

    def _remaining_deadline_s(self) -> float:
        if self.deadline_ns is None:
            raise RuntimeError("fault deadline has not been initialized")
        return max(0.0, (self.deadline_ns - time.perf_counter_ns()) / 1_000_000_000)

    async def _emergency_attempts(
        self,
    ) -> tuple[EmergencyPreset | None, RequestState | None, list[str]]:
        errors: list[str] = []
        accepted_state: RequestState | None = None
        for attempt in range(1, self.spec.emergency_max_attempts + 1):
            if self._remaining_deadline_s() <= 0:
                errors.append("fault-to-accept absolute deadline expired before attempt")
                break
            state = self._new_request(
                RequestType.EMERGENCY,
                generation=self.generation,
                attempt=attempt,
            )
            task = self._start(state)
            try:
                response = await self._run_with_deadline(state, task)
                # Identity was checked on every chunk.  Parse exact JSON, then
                # validate with the Pydantic model that produced the wire schema.
                preset = parse_emergency_response(response)
                validate_emergency_semantics(preset, self.emergency_context)
                if self._remaining_deadline_s() <= 0:
                    raise RequestDeadlineError(
                        "fault-to-accept absolute deadline expired during validation"
                    )
                state.status = RequestStatus.ACCEPTED
                state.completed_ns = self._emit_request(
                    state,
                    EventStage.RESPONSE_ACCEPTED,
                    {"skill_name": preset.skill_name, "attempt": attempt},
                )
                accepted_state = state
                return preset, accepted_state, errors
            except asyncio.CancelledError:
                raise
            except StaleResponseError as exc:
                errors.append(f"attempt {attempt}: {exc}")
            except ValueError as exc:
                state.status = RequestStatus.REJECTED
                state.error = str(exc)
                self._emit_request(
                    state,
                    EventStage.RESPONSE_REJECTED_INVALID,
                    {"attempt": attempt, "error": str(exc)},
                )
                errors.append(f"attempt {attempt}: {exc}")
            except Exception as exc:
                state.error = state.error or f"{type(exc).__name__}: {exc}"
                errors.append(f"attempt {attempt}: {type(exc).__name__}: {exc}")
            if attempt < self.spec.emergency_max_attempts:
                self._emit_request(
                    state,
                    EventStage.RETRY_SCHEDULED,
                    {"completed_attempt": attempt, "next_attempt": attempt + 1},
                )
        return None, accepted_state, errors

    async def _invoke_fault(self, mission: RequestState, mission_task: asyncio.Task[str]) -> None:
        result: FaultHookValue = None
        if self.spec.on_fault is not None:
            result = self.spec.on_fault()
            if inspect.isawaitable(result):
                result = await result
        received_ns = time.perf_counter_ns()
        observed_ns: int | None = None
        if isinstance(result, FaultObservation):
            if result.detected_monotonic_ns > received_ns:
                raise ValueError("detection timestamp is in the future or a different clock domain")
            self.emergency_context = result.context
            observed_ns = result.detected_monotonic_ns
            self.phase_origin_ns = result.phase_origin_monotonic_ns
        elif isinstance(result, EmergencyContext):
            self.emergency_context = result
        elif result is not None:
            raise TypeError("on_fault must return None, EmergencyContext, or FaultObservation")
        self.mission_active_at_fault = not mission_task.done()
        if observed_ns is not None and mission.completed_ns is not None:
            self.mission_active_at_fault = mission.completed_ns > observed_ns
        if not self.mission_active_at_fault and self.spec.require_active_mission_at_fault:
            try:
                mission_task.result()
            except (asyncio.CancelledError, Exception):
                pass
            raise MissionNotActiveAtFaultError(
                "mission request was no longer active at the intended fault instant"
            )
        self.generation += 1
        logged_ns = self._emit_trial(
            EventStage.FAULT_TRIGGERED,
            {
                "method": self.spec.method,
                "fault_label": self.emergency_context.fault_label,
                "source_detection_monotonic_ns": observed_ns,
                "scheduler_received_monotonic_ns": received_ns,
                "phase_origin_monotonic_ns": self.phase_origin_ns,
                "fault_delivery_jitter_ms": (
                    (received_ns - observed_ns) / 1_000_000 if observed_ns is not None else 0.0
                ),
                "mission_active_at_fault": self.mission_active_at_fault,
                "require_active_mission_at_fault": self.spec.require_active_mission_at_fault,
            },
        )
        self.fault_ns = observed_ns if observed_ns is not None else logged_ns
        self.fault_received_ns = received_ns if observed_ns is not None else logged_ns
        self.deadline_ns = self.fault_ns + int(
            self.spec.emergency_timeout_s * 1_000_000_000
        )

    async def _deliver_emergency(self, preset: EmergencyPreset, accepted: RequestState) -> None:
        callback = self.spec.on_emergency_accepted
        if callback is None:
            return
        if accepted.completed_ns is None:
            raise RuntimeError("cannot deliver a command without an acceptance timestamp")
        self._emit_request(
            accepted, "emergency_delivery_started",
            {"accepted_monotonic_ns": accepted.completed_ns},
        )
        result = callback(preset, accepted.completed_ns)
        if inspect.isawaitable(result):
            await result
        self.command_delivery_completed_ns = self._emit_request(
            accepted, "emergency_delivery_completed",
            {"accepted_monotonic_ns": accepted.completed_ns},
        )

    def _result(
        self,
        *,
        status: str,
        mission: RequestState,
        preset: EmergencyPreset | None = None,
        accepted: RequestState | None = None,
        errors: Sequence[str] = (),
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema_version": 1,
            "batch_id": self.spec.batch_id,
            "run_id": self.spec.run_id,
            "trial_id": self.spec.trial_id,
            "method": self.spec.method,
            "status": status,
            "generation": self.generation,
            "mission_request_id": mission.request.request_id,
            "accepted_emergency_request_id": (
                accepted.request.request_id if accepted is not None else None
            ),
            "fault_monotonic_ns": self.fault_ns,
            "fault_deadline_monotonic_ns": self.deadline_ns,
            "fault_received_monotonic_ns": self.fault_received_ns,
            "fault_delivery_jitter_ms": (
                (self.fault_received_ns - self.fault_ns) / 1_000_000
                if self.fault_received_ns is not None and self.fault_ns is not None else None
            ),
            "phase_origin_monotonic_ns": self.phase_origin_ns,
            "mission_ready_monotonic_ns": self.mission_ready_ns,
            "mission_active_at_fault": self.mission_active_at_fault,
            "command_delivery_completed_monotonic_ns": self.command_delivery_completed_ns,
            "runtime_canonical_command": canonical_emergency_preset(self.emergency_context).model_dump(mode="json"),
            "benchmark_scope": "runtime-given canonical command serialization and scheduling; no model discovery of safe control",
            "emergency_preset": (
                preset.model_dump(mode="json") if preset is not None else None
            ),
            "errors": list(errors),
            "requests": [state.public_summary() for state in self.states],
        }
        if accepted is not None and self.fault_ns is not None:
            for field_name, timestamp in (
                ("fault_to_dispatch_ms", accepted.dispatched_ns),
                ("fault_to_server_observed_ms", accepted.server_observed_ns),
                ("fault_to_first_chunk_ms", accepted.first_chunk_ns),
                ("fault_to_accept_ms", accepted.completed_ns),
            ):
                result[field_name] = (
                    (timestamp - self.fault_ns) / 1_000_000
                    if timestamp is not None
                    else None
                )
            result["emergency_queue_ms"] = (
                (accepted.dispatched_ns - accepted.queued_ns) / 1_000_000
                if accepted.dispatched_ns is not None and accepted.queued_ns is not None
                else None
            )
        return result

    def _finish(self, result: dict[str, Any]) -> dict[str, Any]:
        self._emit_trial(
            EventStage.TRIAL_COMPLETED,
            {
                "method": self.spec.method,
                "status": result["status"],
                "accepted_emergency_request_id": result[
                    "accepted_emergency_request_id"
                ],
            },
        )
        return result

    async def run(self) -> dict[str, Any]:
        self._emit_trial(
            EventStage.TRIAL_STARTED,
            {
                "method": self.spec.method,
                "model": self.spec.model,
                "emergency_timeout_s": self.spec.emergency_timeout_s,
                "emergency_max_attempts": self.spec.emergency_max_attempts,
            },
        )
        mission = self._new_request(RequestType.MISSION, generation=0, attempt=1)
        mission_task = self._start(mission)
        outcome: dict[str, Any]

        async def execute_protocol() -> dict[str, Any]:
            try:
                await self._wait_for_mission_barrier(mission, mission_task)
            except MissionBarrierError as exc:
                return {
                    "status": "mission_barrier_failed",
                    "errors": [str(exc)],
                }

            try:
                self.mission_ready_ns = self._emit_trial("mission_ready", {"method": self.spec.method})
                if self.spec.on_mission_ready is not None:
                    ready = self.spec.on_mission_ready()
                    if inspect.isawaitable(ready):
                        await ready
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                return {"status": "mission_ready_hook_failed", "errors": [f"{type(exc).__name__}: {exc}"]}

            try:
                await self._invoke_fault(mission, mission_task)
            except MissionNotActiveAtFaultError as exc:
                return {
                    "status": "invalid_mission_not_active_at_fault",
                    "errors": [str(exc)],
                }
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                return {
                    "status": "fault_hook_failed",
                    "errors": [f"{type(exc).__name__}: {exc}"],
                }

            if self.spec.method == "hera_preempt":
                closed = await self._cancel_request(
                    mission,
                    mission_task,
                    reason="hera_fault_preemption",
                    max_wait_s=self._remaining_deadline_s(),
                )
                if not closed:
                    return {
                        "status": (
                            "deadline_exceeded"
                            if self._remaining_deadline_s() <= 0
                            else "preemption_failed"
                        ),
                        "errors": [mission.error or "mission connection did not close"],
                    }

            preset, accepted, errors = await self._emergency_attempts()
            if accepted is not None and self.deadline_ns is not None:
                within_deadline = (
                    accepted.completed_ns is not None
                    and self.fault_ns is not None
                    and accepted.completed_ns >= self.fault_ns
                    and accepted.completed_ns <= self.deadline_ns
                )
                if not within_deadline:
                    errors.append("accepted response fell outside the absolute deadline")
                    preset = None
                    accepted = None
            if preset is not None and accepted is not None:
                try:
                    await self._deliver_emergency(preset, accepted)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._emit_request(accepted, "emergency_delivery_failed", {"error_type": type(exc).__name__, "error": str(exc)})
                    return {
                        "status": "emergency_delivery_failed", "preset": preset,
                        "accepted": accepted, "errors": [*errors, f"{type(exc).__name__}: {exc}"],
                    }
            return {
                "status": "success" if preset is not None else "emergency_failed",
                "preset": preset,
                "accepted": accepted,
                "errors": errors,
            }

        try:
            outcome = await execute_protocol()
        finally:
            # Reserved-slot trials may finish while mission generation is still
            # streaming.  Cleanup is explicit and cannot leak into the next run.
            await self._cancel_request(
                mission, mission_task, reason="trial_cleanup"
            )
        # Request cleanup precedes the terminal event and the final snapshot, so
        # no request event can appear after TRIAL_COMPLETED and request statuses
        # cannot be reported as streaming after the trial has ended.
        result = self._result(mission=mission, **outcome)
        return self._finish(result)


async def run_scheduler_trial(
    spec: TrialSpec | Mapping[str, Any],
    backend: StreamingBackend,
    event_logger: EventLogger,
) -> dict[str, Any]:
    """Run one scheduling trial using a common, policy-controlled protocol.

    ``on_mission_ready`` runs after a valid typed mission chunk; ``on_fault``
    may await a measured detection and return a new context with its original
    timestamp. ``on_emergency_accepted`` receives the validated command and
    acceptance timestamp exactly once, after the absolute deadline check.
    Delivery failures do not retry an already accepted command. The function owns no
    backend lifetime; callers may reuse a backend across a randomized batch and
    close it after the batch.
    """

    scheduler = _TrialScheduler(_coerce_spec(spec), backend, event_logger)
    return await scheduler.run()


__all__ = [
    "EMERGENCY_SYSTEM_PROMPT",
    "EmergencyContext",
    "FaultObservation",
    "MISSION_SYSTEM_PROMPT",
    "RequestState",
    "RequestStatus",
    "SUPPORTED_METHODS",
    "SchedulerMethod",
    "TrialSpec",
    "build_emergency_prompt",
    "build_emergency_schema",
    "canonical_emergency_preset",
    "run_scheduler_trial",
    "validate_emergency_semantics",
]
