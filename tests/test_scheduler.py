from __future__ import annotations

import asyncio
import inspect
import json
import time
import unittest
from dataclasses import replace
from collections.abc import Callable, Mapping
from typing import Any

from hera_v2.events import EventStage, MemoryEventLogger
from hera_v2.ollama_backend import BackendChunk
from hera_v2.schema import (
    BackendRequest,
    ChatMessage,
    RequestType,
    emergency_json_schema,
    parse_emergency_response,
)
from hera_v2.scheduler import (
    EmergencyContext, FaultObservation, TrialSpec, build_emergency_schema,
    canonical_emergency_preset, run_scheduler_trial,
)


def _valid_emergency_payload(targets: list[float] | None = None) -> str:
    return json.dumps(
        {
            "type": "one-shot",
            "skill_name": "runtime_safe_stabilization",
            "actions": [
                {
                    "actuator_targets": targets or [0.0, 0.0, -0.4] * 4,
                    "duration_steps": 1,
                }
            ],
            "stop_condition": "duration_steps_elapsed",
            "max_cycles": None,
        }
    )


async def _wait_until(
    predicate: Callable[[], bool], *, description: str, turns: int = 1000
) -> None:
    for _ in range(turns):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError(f"timed out waiting for {description}")


class FakeStreamingBackend:
    """Controllable backend that exposes scheduler ordering without real I/O."""

    def __init__(
        self,
        *,
        corrupt_emergency_identity: str | None = None,
        emergency_payload: str | None = None,
    ) -> None:
        self.corrupt_emergency_identity = corrupt_emergency_identity
        self.emergency_payload = emergency_payload or _valid_emergency_payload()
        self.release_mission = asyncio.Event()
        self.mission_first_chunk = asyncio.Event()
        self.emergency_started = asyncio.Event()
        self.requests: list[BackendRequest] = []
        self.trace: list[tuple[str, str, str, int]] = []
        self._active: dict[str, RequestType] = {}
        self.max_active = 0
        self.emergency_saw_active_mission = False

    @staticmethod
    async def _notify(callback, stage: str, details: Mapping[str, Any]) -> None:
        if callback is None:
            return
        result = callback(stage, details)
        if inspect.isawaitable(result):
            await result

    async def stream(self, request: BackendRequest, on_stage=None):
        kind = request.request_type.value
        self.requests.append(request)
        self._active[request.request_id] = request.request_type
        self.max_active = max(self.max_active, len(self._active))
        if request.request_type is RequestType.EMERGENCY:
            self.emergency_started.set()
            self.emergency_saw_active_mission = any(
                active_type is RequestType.MISSION
                for active_type in self._active.values()
            )
        self.trace.append(("start", kind, request.request_id, request.generation))

        cancelled = False
        try:
            await self._notify(on_stage, "server_observed", {"fake": True})
            await self._notify(on_stage, "first_chunk", {"fake": True})

            if request.request_type is RequestType.MISSION:
                self.trace.append(
                    ("first_chunk", kind, request.request_id, request.generation)
                )
                self.mission_first_chunk.set()
                yield BackendChunk(
                    request_id=request.request_id,
                    request_type=request.request_type,
                    generation=request.generation,
                    text="mission-first-chunk",
                )
                await self.release_mission.wait()
                yield BackendChunk(
                    request_id=request.request_id,
                    request_type=request.request_type,
                    generation=request.generation,
                    text="mission-complete",
                    done=True,
                )
                return

            response_request_id = request.request_id
            response_type = request.request_type
            response_generation = request.generation
            if self.corrupt_emergency_identity == "request_id":
                response_request_id = f"stale-{request.request_id}"
            elif self.corrupt_emergency_identity == "request_type":
                response_type = RequestType.MISSION
            elif self.corrupt_emergency_identity == "generation":
                response_generation = max(0, request.generation - 1)

            self.trace.append(
                ("first_chunk", kind, request.request_id, request.generation)
            )
            yield BackendChunk(
                request_id=response_request_id,
                request_type=response_type,
                generation=response_generation,
                text=self.emergency_payload,
                done=True,
            )
        except asyncio.CancelledError:
            cancelled = True
            self.trace.append(
                ("cancelled", kind, request.request_id, request.generation)
            )
            raise
        finally:
            self._active.pop(request.request_id, None)
            self.trace.append(("closed", kind, request.request_id, request.generation))
            await self._notify(
                on_stage, "connection_closed", {"cancelled": cancelled, "fake": True}
            )


def _make_spec(
    method: str,
    *,
    on_fault=None,
    emergency_max_attempts: int = 1,
) -> TrialSpec:
    return TrialSpec(
        batch_id="batch-test",
        run_id=f"run-{method}",
        trial_id=f"trial-{method}",
        method=method,  # type: ignore[arg-type]
        model="fake-model",
        mission_prompt="Keep producing a mission plan until released.",
        emergency=EmergencyContext(
            fault_label="front-left tracking fault",
            affected_channels=(0, 1, 2),
            observed_values=(0.1, -0.2, 0.3),
            safe_action=tuple([0.0, 0.0, -0.4] * 4),
        ),
        emergency_timeout_s=1.0,
        emergency_max_attempts=emergency_max_attempts,
        mission_barrier_timeout_s=1.0,
        cancel_grace_s=1.0,
        on_fault=on_fault,
    )


def _trace_index(trace, action: str, request_type: str) -> int:
    return next(
        index
        for index, item in enumerate(trace)
        if item[0] == action and item[1] == request_type
    )


class SchedulerPolicyTests(unittest.TestCase):
    def test_full_canonical_contract_rejects_semantic_contradictions(self):
        mutations = {
            "duration": lambda payload: payload["actions"][0].update(duration_steps=60),
            "skill": lambda payload: payload.update(skill_name="invented_skill"),
            "stop_zero": lambda payload: payload.update(stop_condition="all_actuator_targets_zero"),
            "stop_max": lambda payload: payload.update(stop_condition="max_cycles"),
            "cycles": lambda payload: payload.update(max_cycles=1),
            "tiny_target_change": lambda payload: payload["actions"][0]["actuator_targets"].__setitem__(0, 1e-9),
            "numeric_string": lambda payload: payload["actions"][0]["actuator_targets"].__setitem__(0, "0"),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                payload = json.loads(_valid_emergency_payload())
                mutate(payload)

                async def scenario():
                    backend = FakeStreamingBackend(emergency_payload=json.dumps(payload))
                    logger = MemoryEventLogger()
                    deliveries = []
                    spec = replace(_make_spec("hera_preempt"), on_emergency_accepted=lambda *args: deliveries.append(args))
                    result = await run_scheduler_trial(spec, backend, logger)
                    return result, deliveries

                result, deliveries = asyncio.run(scenario())
                self.assertEqual("emergency_failed", result["status"])
                self.assertIsNone(result["accepted_emergency_request_id"])
                self.assertEqual([], deliveries)

    def test_runtime_hooks_bind_measured_context_and_original_detection_clock(self):
        async def scenario():
            backend = FakeStreamingBackend()
            logger = MemoryEventLogger()
            spec = _make_spec("hera_preempt")
            context = replace(spec.emergency, safe_action=tuple([0.1, -0.2, -0.3] * 4), duration_steps=50)
            backend.emergency_payload = canonical_emergency_preset(context).model_dump_json()
            calls = []
            detection = 0

            async def ready():
                self.assertTrue(any(item.stage == "mission_barrier_passed" for item in logger.records))
                calls.append("ready")
                await asyncio.sleep(0)

            async def fault():
                nonlocal detection
                self.assertEqual(["ready"], calls)
                detection = time.perf_counter_ns() - 10_000_000
                calls.append("fault")
                return FaultObservation(context, detection, detection - 1_000_000)

            async def accepted(preset, accepted_ns):
                self.assertEqual(["ready", "fault"], calls)
                calls.append(("accepted", preset.model_dump(mode="json"), accepted_ns))
                await asyncio.sleep(0)

            result = await run_scheduler_trial(
                replace(spec, on_mission_ready=ready, on_fault=fault, on_emergency_accepted=accepted),
                backend, logger,
            )
            return result, backend, calls, detection, context, logger

        result, backend, calls, detection, context, logger = asyncio.run(scenario())
        self.assertEqual("success", result["status"])
        self.assertEqual(detection, result["fault_monotonic_ns"])
        self.assertEqual(detection + 1_000_000_000, result["fault_deadline_monotonic_ns"])
        self.assertEqual(detection - 1_000_000, result["phase_origin_monotonic_ns"])
        self.assertGreaterEqual(result["fault_delivery_jitter_ms"], 10.0)
        self.assertTrue(result["mission_active_at_fault"])
        request = next(request for request in backend.requests if request.request_type is RequestType.EMERGENCY)
        self.assertEqual(build_emergency_schema(context), request.response_schema)
        self.assertEqual(result["emergency_preset"], calls[2][1])
        accepted_request = next(row for row in result["requests"] if row["request_type"] == "emergency")
        self.assertEqual(accepted_request["completed_ns"], calls[2][2])
        self.assertLessEqual(calls[2][2], result["command_delivery_completed_monotonic_ns"])
        stages = [row.stage for row in logger.records]
        self.assertLess(stages.index("response_accepted"), stages.index("emergency_delivery_started"))
        self.assertEqual("trial_completed", stages[-1])

    def test_context_only_and_sync_hooks_preserve_compatibility(self):
        async def scenario():
            backend = FakeStreamingBackend()
            logger = MemoryEventLogger()
            spec = _make_spec("reserved_slot")
            deliveries = []
            ready_calls = []
            result = await run_scheduler_trial(replace(
                spec, on_mission_ready=lambda: ready_calls.append(True),
                on_fault=lambda: spec.emergency,
                on_emergency_accepted=lambda preset, stamp: deliveries.append((preset, stamp)),
            ), backend, logger)
            return result, ready_calls, deliveries

        result, ready_calls, deliveries = asyncio.run(scenario())
        self.assertEqual("success", result["status"])
        self.assertEqual([True], ready_calls)
        self.assertEqual(1, len(deliveries))
        self.assertEqual(0.0, result["fault_delivery_jitter_ms"])

    def test_delivery_failure_is_reported_without_repeating_accepted_command(self):
        async def scenario():
            backend = FakeStreamingBackend()
            logger = MemoryEventLogger()
            calls = []

            def fail_delivery(preset, stamp):
                calls.append(stamp)
                raise RuntimeError("plant command queue failed")

            spec = replace(_make_spec("hera_preempt", emergency_max_attempts=3), on_emergency_accepted=fail_delivery)
            result = await run_scheduler_trial(spec, backend, logger)
            return result, calls, backend

        result, calls, backend = asyncio.run(scenario())
        self.assertEqual("emergency_delivery_failed", result["status"])
        self.assertIsNotNone(result["accepted_emergency_request_id"])
        self.assertEqual(1, len(calls))
        self.assertEqual(1, sum(request.request_type is RequestType.EMERGENCY for request in backend.requests))

    def test_detection_delivery_delay_consumes_absolute_deadline(self):
        async def scenario():
            backend = FakeStreamingBackend()
            spec = _make_spec("hera_preempt")
            spec = replace(spec, emergency_timeout_s=.01, on_fault=lambda: FaultObservation(spec.emergency, time.perf_counter_ns() - 100_000_000))
            result = await run_scheduler_trial(spec, backend, MemoryEventLogger())
            return result, backend

        result, backend = asyncio.run(scenario())
        self.assertNotEqual("success", result["status"])
        self.assertFalse(backend.emergency_started.is_set())
        self.assertGreaterEqual(result["fault_delivery_jitter_ms"], 100.0)

    def test_future_detection_timestamp_is_rejected(self):
        async def scenario():
            backend = FakeStreamingBackend()
            spec = _make_spec("hera_preempt")
            spec = replace(spec, on_fault=lambda: FaultObservation(spec.emergency, time.perf_counter_ns() + 1_000_000_000))
            return await run_scheduler_trial(spec, backend, MemoryEventLogger()), backend

        result, backend = asyncio.run(scenario())
        self.assertEqual("fault_hook_failed", result["status"])
        self.assertFalse(backend.emergency_started.is_set())
        self.assertIn("clock domain", result["errors"][0])

    def test_integrated_trial_may_allow_mission_completed_before_detection(self):
        async def scenario():
            backend = FakeStreamingBackend()
            spec = _make_spec("hera_preempt")

            async def fault():
                backend.release_mission.set()
                await asyncio.sleep(.01)

            return await run_scheduler_trial(replace(
                spec, on_fault=fault, require_active_mission_at_fault=False,
            ), backend, MemoryEventLogger())

        result = asyncio.run(scenario())
        self.assertEqual("success", result["status"])
        self.assertFalse(result["mission_active_at_fault"])

    def test_finished_mission_cannot_enter_contention_trial(self):
        async def scenario():
            backend = FakeStreamingBackend()
            logger = MemoryEventLogger()

            async def finish_before_fault():
                backend.release_mission.set()
                await asyncio.sleep(.01)

            result = await run_scheduler_trial(
                _make_spec("hera_preempt", on_fault=finish_before_fault), backend, logger
            )
            return backend, logger, result

        backend, logger, result = asyncio.run(scenario())
        self.assertEqual("invalid_mission_not_active_at_fault", result["status"])
        self.assertFalse(backend.emergency_started.is_set())
        self.assertFalse(any(record.stage == "fault_triggered" for record in logger.records))
        self.assertEqual("trial_completed", logger.records[-1].stage)

    def test_preemption_cancellation_consumes_fault_deadline(self):
        class SlowCancellationBackend(FakeStreamingBackend):
            async def stream(self, request, on_stage=None):
                try:
                    async for chunk in super().stream(request, on_stage):
                        yield chunk
                except asyncio.CancelledError:
                    if request.request_type is RequestType.MISSION:
                        await asyncio.sleep(.08)
                    raise

        async def scenario():
            backend = SlowCancellationBackend()
            logger = MemoryEventLogger()
            spec = replace(_make_spec("hera_preempt"), emergency_timeout_s=.02)
            result = await run_scheduler_trial(spec, backend, logger)
            return backend, logger, result

        backend, logger, result = asyncio.run(scenario())
        self.assertEqual("deadline_exceeded", result["status"])
        self.assertFalse(backend.emergency_started.is_set())
        self.assertEqual("trial_completed", logger.records[-1].stage)

    def test_fifo_waits_behind_active_mission_and_preserves_order(self):
        async def scenario():
            backend = FakeStreamingBackend()
            logger = MemoryEventLogger()
            fault_seen = asyncio.Event()

            async def on_fault() -> None:
                fault_seen.set()

            trial = asyncio.create_task(
                run_scheduler_trial(
                    _make_spec("fifo_single", on_fault=on_fault), backend, logger
                )
            )
            await asyncio.wait_for(fault_seen.wait(), timeout=1.0)
            await _wait_until(
                lambda: any(
                    record.stage == EventStage.REQUEST_QUEUED.value
                    and record.request_type == RequestType.EMERGENCY.value
                    for record in logger.records
                ),
                description="the FIFO emergency request to enter the queue",
            )

            self.assertFalse(backend.emergency_started.is_set())
            emergency_records = [
                record
                for record in logger.records
                if record.request_type == RequestType.EMERGENCY.value
            ]
            self.assertTrue(emergency_records)
            self.assertFalse(
                any(
                    record.stage == EventStage.REQUEST_DISPATCHED.value
                    for record in emergency_records
                )
            )

            backend.release_mission.set()
            result = await asyncio.wait_for(trial, timeout=1.0)
            return backend, logger, result

        backend, _, result = asyncio.run(scenario())
        self.assertEqual("success", result["status"])
        self.assertLess(
            _trace_index(backend.trace, "first_chunk", "mission"),
            _trace_index(backend.trace, "closed", "mission"),
        )
        self.assertLess(
            _trace_index(backend.trace, "closed", "mission"),
            _trace_index(backend.trace, "start", "emergency"),
        )
        self.assertEqual(1, backend.max_active)

    def test_hera_cancels_active_mission_before_emergency_dispatch(self):
        async def scenario():
            backend = FakeStreamingBackend()
            logger = MemoryEventLogger()
            result = await run_scheduler_trial(
                _make_spec("hera_preempt"), backend, logger
            )
            return backend, logger, result

        backend, logger, result = asyncio.run(scenario())
        self.assertEqual("success", result["status"])
        self.assertLess(
            _trace_index(backend.trace, "first_chunk", "mission"),
            _trace_index(backend.trace, "closed", "mission"),
        )
        self.assertLess(
            _trace_index(backend.trace, "closed", "mission"),
            _trace_index(backend.trace, "start", "emergency"),
        )
        self.assertEqual(1, backend.max_active)

        requests = {request["request_type"]: request for request in result["requests"]}
        self.assertEqual("cancelled", requests["mission"]["status"])
        self.assertEqual("accepted", requests["emergency"]["status"])
        stages = [record.stage for record in logger.records]
        self.assertIn(EventStage.CANCEL_REQUESTED.value, stages)
        self.assertIn(EventStage.REQUEST_CANCELLED.value, stages)
        self.assertIn(EventStage.RESPONSE_ACCEPTED.value, stages)

        emergency_request = next(
            request
            for request in backend.requests
            if request.request_type is RequestType.EMERGENCY
        )
        self.assertIsNotNone(emergency_request.response_schema)
        self.assertIn("actions", emergency_request.response_schema["properties"])

        for record in logger.records:
            self.assertEqual("batch-test", record.batch_id)
            self.assertTrue(record.run_id)
            self.assertTrue(record.trial_id)
            self.assertGreaterEqual(record.generation, 0)
            if record.request_type == "trial":
                self.assertIsNone(record.request_id)
            else:
                self.assertTrue(record.request_id)
                self.assertIn(record.request_type, {"mission", "emergency"})

    def test_reserved_slot_starts_emergency_while_mission_is_active(self):
        async def scenario():
            backend = FakeStreamingBackend()
            logger = MemoryEventLogger()
            result = await run_scheduler_trial(
                _make_spec("reserved_slot"), backend, logger
            )
            return backend, logger, result

        backend, logger, result = asyncio.run(scenario())
        self.assertEqual("success", result["status"])
        self.assertTrue(backend.emergency_saw_active_mission)
        self.assertEqual(2, backend.max_active)
        self.assertLess(
            _trace_index(backend.trace, "start", "emergency"),
            _trace_index(backend.trace, "closed", "mission"),
        )
        dispatched = {
            record.request_type: record.details["lane"]
            for record in logger.records
            if record.stage == EventStage.REQUEST_DISPATCHED.value
        }
        self.assertEqual("mission", dispatched["mission"])
        self.assertEqual("emergency", dispatched["emergency"])
        mission = next(
            request for request in result["requests"]
            if request["request_type"] == "mission"
        )
        self.assertEqual("cancelled", mission["status"])
        self.assertEqual(EventStage.TRIAL_COMPLETED.value, logger.records[-1].stage)

    def test_rejects_schema_valid_but_semantically_different_safe_action(self):
        payload = _valid_emergency_payload([0.0] * 12)

        async def scenario():
            backend = FakeStreamingBackend(emergency_payload=payload)
            logger = MemoryEventLogger()
            result = await run_scheduler_trial(
                _make_spec("hera_preempt"), backend, logger
            )
            return logger, result

        logger, result = asyncio.run(scenario())
        self.assertEqual("emergency_failed", result["status"])
        self.assertIn("does not reproduce", result["errors"][0])
        self.assertTrue(
            any(
                record.stage == EventStage.RESPONSE_REJECTED_INVALID.value
                for record in logger.records
            )
        )

    def test_rejects_wrong_or_stale_backend_identity(self):
        for corrupted_field in ("request_id", "request_type", "generation"):
            with self.subTest(corrupted_field=corrupted_field):
                async def scenario():
                    backend = FakeStreamingBackend(
                        corrupt_emergency_identity=corrupted_field
                    )
                    logger = MemoryEventLogger()
                    result = await run_scheduler_trial(
                        _make_spec("hera_preempt"), backend, logger
                    )
                    return logger, result

                logger, result = asyncio.run(scenario())
                self.assertEqual("emergency_failed", result["status"])
                self.assertIsNone(result["accepted_emergency_request_id"])
                emergency = next(
                    request
                    for request in result["requests"]
                    if request["request_type"] == "emergency"
                )
                self.assertEqual("rejected", emergency["status"])
                self.assertIn("stale or misrouted", emergency["error"])
                self.assertTrue(
                    any(
                        record.stage
                        == EventStage.RESPONSE_REJECTED_STALE.value
                        and record.request_id == emergency["request_id"]
                        for record in logger.records
                    )
                )
                self.assertFalse(
                    any(
                        record.stage == EventStage.RESPONSE_ACCEPTED.value
                        for record in logger.records
                    )
                )


class SchedulerSchemaTests(unittest.TestCase):
    def test_runtime_contract_constrains_every_command_field(self):
        context = replace(_make_spec("hera_preempt").emergency, duration_steps=50)
        schema = build_emergency_schema(context)
        canonical = canonical_emergency_preset(context).model_dump(mode="json")
        for name in ("type", "skill_name", "stop_condition", "max_cycles"):
            self.assertEqual(canonical[name], schema["properties"][name]["const"])
        phase = schema["$defs"]["ActionPhase"]
        self.assertEqual(50, phase["properties"]["duration_steps"]["const"])
        self.assertEqual(list(context.safe_action), phase["properties"]["actuator_targets"]["const"])
        self.assertEqual(1, schema["properties"]["actions"]["minItems"])
        self.assertEqual(1, schema["properties"]["actions"]["maxItems"])
        with self.assertRaisesRegex(ValueError, "stop_condition"):
            replace(context, stop_condition="max_cycles")

    def test_emergency_schema_is_strict_and_request_ids_are_required(self):
        schema = emergency_json_schema()
        self.assertTrue(
            {"type", "skill_name", "actions", "stop_condition"}
            <= set(schema["required"])
        )
        parsed = parse_emergency_response(_valid_emergency_payload())
        self.assertEqual("one-shot", parsed.type)
        self.assertEqual(12, len(parsed.actions[0].actuator_targets))

        malformed = json.loads(_valid_emergency_payload())
        malformed["actions"][0]["actuator_targets"] = [0.0] * 11
        with self.assertRaisesRegex(ValueError, "emergency schema"):
            parse_emergency_response(json.dumps(malformed))

        out_of_range = json.loads(_valid_emergency_payload())
        out_of_range["actions"][0]["actuator_targets"][0] = 1.01
        with self.assertRaisesRegex(ValueError, "emergency schema"):
            parse_emergency_response(json.dumps(out_of_range))

        request_kwargs = {
            "request_type": RequestType.EMERGENCY,
            "generation": 0,
            "attempt": 1,
            "model": "fake",
            "messages": (ChatMessage(role="user", content="emergency"),),
            "response_schema": schema,
            "temperature": 0.0,
        }
        with self.assertRaises(ValueError):
            BackendRequest(request_id="", **request_kwargs)
        with self.assertRaises(ValueError):
            BackendRequest(request_id="request-1", generation=-1, **{
                key: value for key, value in request_kwargs.items() if key != "generation"
            })


if __name__ == "__main__":
    unittest.main()
