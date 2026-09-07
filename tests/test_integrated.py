from __future__ import annotations

import asyncio
from dataclasses import replace
import inspect
import json
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

import numpy as np

from hera_v2 import integrated
from hera_v2.events import MemoryEventLogger
from hera_v2.manifest import TrialSpec
from hera_v2.ollama_backend import BackendChunk
from hera_v2.physics import PhysicsTrialSpec, run_physics_trial
from hera_v2.scheduler import EmergencyContext, canonical_emergency_preset
from hera_v2.schema import BackendRequest, ChatMessage, RequestType


def make_spec(method="hera_preempt"):
    return TrialSpec(
        trial_id=f"integrated-unit-{method}", batch_id="integrated-unit-b001",
        block_id="integrated-unit-b001-r01", order_index=0, study="integrated",
        method=method, fault_leg="FL", seed=470972406, model="fake",
        deadline_s=1.0, fault_offset_s=1.0, duration_s=2.0, realtime=True,
        ollama_host="http://unused.invalid", metadata={"physics": {"record_trace": True}},
    )


def command_for(leg="FL", duration=3):
    return canonical_emergency_preset(EmergencyContext(
        fault_label=f"detected {leg}", affected_channels=integrated.LEG_CHANNELS[leg],
        observed_values=(1.0, 2.0, 3.0), safe_action=integrated.support_action(leg),
        duration_steps=duration,
    ))


class CanonicalBackend:
    """No network: stream a mission, then serialize the supplied wire contract."""

    def __init__(self, *, mission_failure=False):
        self.mission_failure = mission_failure
        self.requests = []
        self.closed = False
        self.mission_streaming = False
        self.emergency_started = threading.Event()

    async def stream(self, request, on_stage=None):
        self.requests.append(request)
        if request.request_type is RequestType.MISSION and self.mission_failure:
            raise ConnectionError("mock mission transport unavailable")
        if on_stage is not None:
            notified = on_stage("server_observed", {"backend_tag": "forwarded"})
            if inspect.isawaitable(notified):
                await notified
        try:
            if request.request_type is RequestType.MISSION:
                self.mission_streaming = True
                yield BackendChunk(
                    request.request_id, request.request_type, request.generation,
                    "mission", metadata={"backend_tag": "forwarded"},
                )
                await asyncio.Event().wait()
            else:
                self.emergency_started.set()
                schema = request.response_schema
                phase = schema["$defs"]["ActionPhase"]["properties"]
                payload = {
                    key: schema["properties"][key]["const"]
                    for key in ("type", "skill_name", "stop_condition", "max_cycles")
                }
                payload["actions"] = [{
                    "actuator_targets": phase["actuator_targets"]["const"],
                    "duration_steps": phase["duration_steps"]["const"],
                }]
                yield BackendChunk(
                    request.request_id, request.request_type, request.generation,
                    json.dumps(payload), done=True,
                    metadata={"backend_tag": "forwarded", "eval_count": 117},
                )
        finally:
            if request.request_type is RequestType.MISSION:
                self.mission_streaming = False

    async def aclose(self):
        self.closed = True


class MailboxTests(unittest.TestCase):
    def test_only_accepted_command_can_override_and_duration_expires_to_local(self):
        mailbox = integrated.SupervisoryMailbox()
        self.assertIsNone(mailbox.action(0, "FL", None))
        self.assertIsNone(mailbox.action(1, None, None))
        command = command_for(duration=3)
        mailbox.accept(command, time.perf_counter_ns())
        # Waiting for a detector-selected leg must not consume command duration.
        self.assertIsNone(mailbox.action(9, None, None))
        self.assertIsNone(mailbox.first_step)
        for step in (10, 11, 12):
            actual, source = mailbox.action(step, "FL", None)
            np.testing.assert_array_equal(command.actions[0].actuator_targets, actual)
            self.assertEqual("validated_supervisor", source)
        self.assertEqual(3, mailbox.applied_steps)
        self.assertIsNone(mailbox.action(13, "FL", None))
        self.assertIsNone(mailbox.action(100, "FL", None))
        # Expiry does not open a new generation or permit duplicate acceptance.
        with self.assertRaisesRegex(ValueError, "duplicate"):
            mailbox.accept(command, time.perf_counter_ns())

    def test_real_cpu_plant_applies_canonical_command_after_acceptance_then_falls_back(self):
        mailbox = integrated.SupervisoryMailbox()
        selected = []

        def detection(leg, measurement, step):
            selected.append((leg, step))
            mailbox.accept(command_for(leg, duration=3), time.perf_counter_ns())

        result = run_physics_trial(
            PhysicsTrialSpec(
                trial_id="mailbox-cpu-unit", method="local_reflex", seed=470972406,
                fault_leg="FL", duration_s=2.0, fault_offset_s=1.0,
                realtime=False, record_trace=True,
            ),
            on_detection=detection, before_control=mailbox.action,
        )
        self.assertEqual(1, len(selected))
        applied = [row for row in result["trace"] if row["action_source"] == "validated_supervisor"]
        self.assertEqual(3, len(applied))
        self.assertEqual(selected[0][1] + 1, applied[0]["step"])
        self.assertGreaterEqual(applied[0]["control_started_ns"], mailbox.accepted_ns)
        expected = list(command_for(selected[0][0], 3).actions[0].actuator_targets)
        self.assertTrue(all(row["commanded_action"] == expected for row in applied))
        following = result["trace"][applied[-1]["step"] + 1]
        self.assertEqual("local_reflex", following["action_source"])
        self.assertTrue(result["fault_active_until_trial_end"])
        self.assertEqual(1, result["reset_count"])


class FailureBackendTests(unittest.TestCase):
    def test_injected_failure_blocks_only_emergency_and_forwards_mission_metadata(self):
        async def scenario():
            calls = []
            callback_seen = []
            mission = BackendRequest(
                request_id="mission-1", request_type=RequestType.MISSION,
                generation=0, attempt=1, model="fake",
                messages=(ChatMessage(role="user", content="mission"),), temperature=0.0,
            )
            expected = BackendChunk(
                mission.request_id, mission.request_type, mission.generation,
                "mission-output", done=True, metadata={"eval_count": 7, "custom": "untouched"},
            )

            class Underlying:
                async def stream(self, request, on_stage=None):
                    calls.append(request)
                    await on_stage("server_observed", {"custom": "stage"})
                    yield expected

            async def callback(stage, details):
                callback_seen.append((stage, details))

            wrapper = integrated.EmergencyFailureBackend(Underlying())
            chunks = [chunk async for chunk in wrapper.stream(mission, on_stage=callback)]
            emergency = mission.model_copy(update={"request_id": "emergency-1", "request_type": RequestType.EMERGENCY, "generation": 1})
            with self.assertRaisesRegex(ConnectionError, "preregistered injected"):
                _ = [chunk async for chunk in wrapper.stream(emergency, on_stage=callback)]
            return calls, chunks, expected, callback_seen

        calls, chunks, expected, callback_seen = asyncio.run(scenario())
        self.assertEqual(1, len(calls))
        self.assertIs(expected, chunks[0])
        self.assertEqual({"eval_count": 7, "custom": "untouched"}, chunks[0].metadata)
        self.assertEqual([("server_observed", {"custom": "stage"})], callback_seen)


class IntegratedLifecycleTests(unittest.TestCase):
    def fake_plant(self, *, expect_supervisor=False, backend=None):
        started = threading.Event()
        before_acceptance = []

        def run(spec, logger, *, on_detection, before_control):
            started.set()
            if backend is not None and not backend.mission_failure:
                self.assertTrue(backend.mission_streaming)
            before_acceptance.append(before_control(0, None, None))
            measurement = SimpleNamespace(
                monotonic_ns=time.perf_counter_ns(), actuator_force=np.arange(12, dtype=float),
                imu_gyro=np.zeros(3),
            )
            on_detection("FL", measurement, 1)
            trace = []
            for step in range(2, 302):
                override = before_control(step, "FL", measurement)
                stamp = time.perf_counter_ns()
                action, source = override if override is not None else (integrated.support_action("FL"), "local_reflex")
                trace.append({
                    "step": step, "control_started_ns": stamp,
                    "action_source": source, "commanded_action": list(action),
                })
                if not expect_supervisor or any(row["action_source"] == "validated_supervisor" for row in trace):
                    break
                time.sleep(.001)
            return {"trace": trace, "physical_safe": True, "success": True}

        return run, started, before_acceptance

    def test_live_scheduler_receives_measured_context_and_application_follows_acceptance(self):
        async def scenario():
            backend = CanonicalBackend()
            plant, started, before = self.fake_plant(expect_supervisor=True, backend=backend)
            logger = MemoryEventLogger()
            with mock.patch.object(integrated, "OllamaBackend", return_value=backend), \
                 mock.patch.object(integrated, "run_physics_trial", side_effect=plant):
                result = await asyncio.wait_for(integrated.run_integrated_trial(make_spec(), logger, logger), timeout=2)
            return result, backend, started, before, logger

        result, backend, started, before, logger = asyncio.run(scenario())
        self.assertTrue(started.is_set())
        self.assertEqual([None], before)
        self.assertTrue(backend.closed)
        self.assertEqual("success", result["scheduler"]["status"])
        application = result["supervisory_application"]
        self.assertTrue(application["applied"])
        self.assertGreaterEqual(application["accept_to_apply_ms"], 0)
        self.assertEqual(50, application["command"]["actions"][0]["duration_steps"])
        self.assertEqual("duration_steps_elapsed", application["command"]["stop_condition"])
        emergency = next(request for request in backend.requests if request.request_type is RequestType.EMERGENCY)
        self.assertIn("Observed values: [0, 1, 2]", emergency.messages[1].content)
        self.assertIn("measured residual on FL", emergency.messages[1].content)
        self.assertTrue(any(row.stage == "emergency_delivery_completed" for row in logger.records))

    def test_injected_emergency_transport_failure_preserves_local_control(self):
        async def scenario():
            backend = CanonicalBackend()
            plant, started, _ = self.fake_plant(backend=backend)
            logger = MemoryEventLogger()
            with mock.patch.object(integrated, "OllamaBackend", return_value=backend), \
                 mock.patch.object(integrated, "run_physics_trial", side_effect=plant):
                result = await asyncio.wait_for(integrated.run_integrated_trial(make_spec("backend_failure"), logger, logger), timeout=2)
            return result, backend, started

        result, backend, started = asyncio.run(scenario())
        self.assertTrue(started.is_set())
        self.assertTrue(result["physical_safe"])
        self.assertTrue(result["injected_backend_failure"])
        self.assertFalse(result["supervisory_application"]["accepted"])
        self.assertFalse(result["supervisory_application"]["applied"])
        self.assertEqual("emergency_failed", result["scheduler"]["status"])
        self.assertIn("preregistered injected", result["scheduler"]["errors"][0])
        self.assertTrue(all(row["action_source"] == "local_reflex" for row in result["trace"]))
        self.assertFalse(backend.emergency_started.is_set())
        self.assertTrue(backend.closed)

    def test_mission_transport_failure_still_releases_physics_start_barrier(self):
        async def scenario():
            backend = CanonicalBackend(mission_failure=True)
            plant, started, _ = self.fake_plant(backend=backend)
            logger = MemoryEventLogger()
            with mock.patch.object(integrated, "OllamaBackend", return_value=backend), \
                 mock.patch.object(integrated, "run_physics_trial", side_effect=plant):
                result = await asyncio.wait_for(integrated.run_integrated_trial(make_spec(), logger, logger), timeout=2)
            return result, backend, started

        result, backend, started = asyncio.run(scenario())
        self.assertTrue(started.is_set())
        self.assertTrue(result["physical_safe"])
        self.assertEqual("mission_barrier_failed", result["scheduler"]["status"])
        self.assertFalse(result["supervisory_application"]["applied"])
        self.assertTrue(backend.closed)

    def test_scheduler_exception_still_returns_local_physical_outcome(self):
        async def scenario():
            backend = CanonicalBackend()
            plant, started, _ = self.fake_plant()
            logger = MemoryEventLogger()
            with mock.patch.object(integrated, "OllamaBackend", return_value=backend), \
                 mock.patch.object(integrated, "run_physics_trial", side_effect=plant), \
                 mock.patch.object(integrated, "run_scheduler_trial", new=mock.AsyncMock(side_effect=RuntimeError("scheduler unavailable"))):
                result = await asyncio.wait_for(integrated.run_integrated_trial(make_spec(), logger, logger), timeout=2)
            return result, started

        result, started = asyncio.run(scenario())
        self.assertTrue(started.is_set())
        self.assertTrue(result["physical_safe"])
        self.assertIn("scheduler unavailable", result["scheduler_error"])
        self.assertIsNone(result["scheduler"])

    def test_backend_construction_failure_still_returns_local_physical_outcome(self):
        async def scenario():
            plant, started, _ = self.fake_plant()
            logger = MemoryEventLogger()
            with mock.patch.object(integrated, "OllamaBackend", side_effect=RuntimeError("client setup failed")), \
                 mock.patch.object(integrated, "run_physics_trial", side_effect=plant):
                result = await asyncio.wait_for(integrated.run_integrated_trial(make_spec(), logger, logger), timeout=2)
            return result, started

        result, started = asyncio.run(scenario())
        self.assertTrue(started.is_set())
        self.assertTrue(result["physical_safe"])
        self.assertIn("client setup failed", result["scheduler_error"])
        self.assertFalse(result["supervisory_application"]["applied"])

    def test_disabling_trace_is_rejected_before_plant_start(self):
        async def scenario():
            spec = replace(make_spec(), metadata={"physics": {"record_trace": False}})
            logger = MemoryEventLogger()
            with mock.patch.object(integrated, "run_physics_trial") as plant:
                with self.assertRaisesRegex(ValueError, "record_trace=True"):
                    await integrated.run_integrated_trial(spec, logger, logger)
                plant.assert_not_called()

        asyncio.run(scenario())

    def test_local_only_never_constructs_a_backend(self):
        async def scenario():
            plant, started, _ = self.fake_plant()
            logger = MemoryEventLogger()
            with mock.patch.object(integrated, "OllamaBackend", side_effect=AssertionError("unexpected model backend")), \
                 mock.patch.object(integrated, "run_physics_trial", side_effect=plant):
                result = await asyncio.wait_for(integrated.run_integrated_trial(make_spec("local_only"), logger, logger), timeout=2)
            return result, started

        result, started = asyncio.run(scenario())
        self.assertTrue(started.is_set())
        self.assertTrue(result["physical_safe"])
        self.assertIsNone(result["scheduler"])
        self.assertFalse(result["supervisory_application"]["accepted"])


if __name__ == "__main__":
    unittest.main()
