from __future__ import annotations

import unittest
from dataclasses import replace
from unittest.mock import patch

import numpy as np

from hera_v2.physics import (
    ActuatorLayout,
    DetectorMeasurementNoise,
    DetectorNoiseConfig,
    LayoutError,
    MEASUREMENT_SYNC,
    PhysicsMeasurements,
    PhysicsTrialSpec,
    PlantActuatorFault,
    QuadrupedTrial,
    RealtimePacer,
    ResidualDetectorConfig,
    ResidualFaultDetector,
    evaluate_pre_fault_eligibility,
    summarize_physical_safety,
    validate_control_override,
    validate_actuator_force_model,
    run_physics_trial,
)
from hera_v2.presets import nominal_trot_preset


CANONICAL_NAMES = (
    "yaw_front_left", "lift_front_left", "extend_front_left",
    "yaw_front_right", "lift_front_right", "extend_front_right",
    "yaw_back_right", "lift_back_right", "extend_back_right",
    "yaw_back_left", "lift_back_left", "extend_back_left",
)


class _FakeModel:
    def __init__(self, names=CANONICAL_NAMES):
        self._names = tuple(names)
        self.nu = len(self._names)
        self.actuator_ctrlrange = np.tile([-1.0, 1.0], (self.nu, 1))
        self.actuator_gainprm = np.zeros((self.nu, 10))
        self.actuator_gainprm[:, 0] = 1000.0
        self.actuator_biasprm = np.zeros((self.nu, 10))
        self.actuator_biasprm[:, 1] = -1000.0
        self.actuator_gaintype = np.zeros(self.nu, dtype=int)
        self.actuator_biastype = np.ones(self.nu, dtype=int)
        self.actuator_actearly = np.zeros(self.nu, dtype=bool)

    def id2name(self, index, kind):
        self.assert_kind = kind
        return self._names[index]


class _FakePhysics:
    def __init__(self, names=CANONICAL_NAMES):
        self.model = _FakeModel(names)
        self.forward_calls = 0

    def forward(self):
        self.forward_calls += 1


def _measurements(layout, *, failed_leg=None, timestamp=1):
    action = np.full(layout.size, 0.5)
    activation = action.copy()
    length = np.full(layout.size, 0.4)
    velocity = np.ones(layout.size)
    expected_force = 1000.0 * activation - 1000.0 * length
    force = expected_force.copy()
    if failed_leg is not None:
        force[list(layout.indices(failed_leg))] = 0.0
    return PhysicsMeasurements(
        monotonic_ns=timestamp,
        commanded_action=action,
        actuator_activation=activation,
        actuator_length=length,
        actuator_velocity=velocity,
        actuator_force=force,
        joint_qpos_by_leg={leg: np.zeros(4) for leg in ("FL", "FR", "BR", "BL")},
        joint_qvel_by_leg={leg: np.zeros(4) for leg in ("FL", "FR", "BR", "BL")},
        toe_force_by_leg={leg: np.ones(3) for leg in ("FL", "FR", "BR", "BL")},
        imu_accel=np.array([0.0, 0.0, 9.81]),
        imu_gyro=np.zeros(3),
        torso_upright=1.0,
        torso_height=0.5,
        torso_velocity=np.zeros(3),
    )


class LayoutAndFaultTests(unittest.TestCase):
    def test_real_order_is_fl_fr_br_bl(self):
        physics = _FakePhysics()
        layout = ActuatorLayout.from_physics(physics)
        self.assertEqual(("FL", "FR", "BR", "BL"), layout.observed_leg_order)
        self.assertEqual((6, 7, 8), layout.indices("BR"))
        self.assertEqual((9, 10, 11), layout.indices("BL"))

    def test_dynamic_mapping_can_read_shuffled_model_but_strict_mode_rejects_it(self):
        shuffled = CANONICAL_NAMES[6:9] + CANONICAL_NAMES[0:6] + CANONICAL_NAMES[9:]
        physics = _FakePhysics(shuffled)
        with self.assertRaises(LayoutError):
            ActuatorLayout.from_physics(physics)
        layout = ActuatorLayout.from_physics(
            physics, require_canonical_model_order=False
        )
        self.assertEqual((0, 1, 2), layout.indices("BR"))
        action = layout.action_from_leg_targets(
            {leg: (0.0, 0.0, -0.4) for leg in ("FL", "FR", "BR", "BL")}
        )
        np.testing.assert_allclose(action[0:3], [0.0, 0.0, -0.4])

    def test_plant_fault_scales_gain_and_bias_and_only_explicit_restore_clears_it(self):
        physics = _FakePhysics()
        layout = ActuatorLayout.from_physics(physics)
        fault = PlantActuatorFault(physics, layout)
        original_gain = physics.model.actuator_gainprm.copy()
        original_bias = physics.model.actuator_biasprm.copy()
        controller_action = np.arange(12.0)

        fault.inject("BR", strength_scale=0.25)
        np.testing.assert_allclose(
            physics.model.actuator_gainprm[6:9], original_gain[6:9] * 0.25
        )
        np.testing.assert_allclose(
            physics.model.actuator_biasprm[6:9], original_bias[6:9] * 0.25
        )
        np.testing.assert_array_equal(controller_action, np.arange(12.0))
        self.assertEqual({"BR": 0.25}, fault.active)
        snapshot = fault.parameter_snapshot("BR")
        self.assertEqual([6, 7, 8], snapshot["actuator_indices"])
        self.assertEqual(.25, snapshot["strength_scale"])
        np.testing.assert_array_equal(snapshot["nominal_gainprm"], original_gain[6:9])
        np.testing.assert_array_equal(snapshot["faulted_gainprm"], original_gain[6:9] * .25)
        np.testing.assert_array_equal(snapshot["faulted_biasprm"], original_bias[6:9] * .25)

        # Preset/controller activity has no API path that restores the plant.
        self.assertEqual({"BR": 0.25}, fault.active)
        fault.restore("BR")
        np.testing.assert_array_equal(physics.model.actuator_gainprm, original_gain)
        np.testing.assert_array_equal(physics.model.actuator_biasprm, original_bias)
        self.assertEqual({}, fault.active)
        # Serialized evidence survives restoring the measured plant.
        np.testing.assert_array_equal(snapshot["faulted_gainprm"], original_gain[6:9] * .25)
        with self.assertRaises(ValueError):
            fault.parameter_snapshot("BR")

    def test_predictor_rejects_unsupported_gain_bias_or_early_activation(self):
        model = _FakeModel()
        validate_actuator_force_model(model)
        for name, value in (("actuator_gaintype", 1), ("actuator_biastype", 0), ("actuator_actearly", True)):
            original = getattr(model, name)[0]
            getattr(model, name)[0] = value
            with self.assertRaises(LayoutError):
                validate_actuator_force_model(model)
            getattr(model, name)[0] = original


class DetectorTests(unittest.TestCase):
    def setUp(self):
        self.physics = _FakePhysics()
        self.layout = ActuatorLayout.from_physics(self.physics)
        self.detector = ResidualFaultDetector(
            self.layout,
            self.physics.model.actuator_gainprm,
            self.physics.model.actuator_biasprm,
            control_timestep=0.02,
            config=ResidualDetectorConfig(enter_steps=3, exit_steps=4),
        )

    def test_debounce_localizes_force_model_residual(self):
        for index in range(5):
            update = self.detector.update(_measurements(self.layout, timestamp=index))
            self.assertEqual((), update.triggered_legs)
            self.assertEqual((), update.active_legs)

        self.assertEqual(
            (),
            self.detector.update(
                _measurements(self.layout, failed_leg="BL", timestamp=10)
            ).triggered_legs,
        )
        self.assertEqual(
            (),
            self.detector.update(
                _measurements(self.layout, failed_leg="BL", timestamp=11)
            ).triggered_legs,
        )
        triggered = self.detector.update(
            _measurements(self.layout, failed_leg="BL", timestamp=12)
        )
        self.assertEqual(("BL",), triggered.triggered_legs)
        self.assertEqual(("BL",), triggered.active_legs)
        self.assertGreater(triggered.residuals["BL"].force_model_residual, 0.99)
        self.assertLess(triggered.residuals["FL"].force_model_residual, 1e-12)

    def test_nominal_force_applies_only_enabled_force_limits(self):
        limited = np.zeros(12, dtype=bool)
        limited[:2] = True
        detector = ResidualFaultDetector(
            self.layout, self.physics.model.actuator_gainprm,
            self.physics.model.actuator_biasprm, control_timestep=.02,
            nominal_forcelimited=limited,
            nominal_forcerange=np.tile([-20., 30.], (12, 1)),
        )
        measurement = _measurements(self.layout)
        measurement.actuator_activation[1] = .1
        expected = detector._nominal_force(measurement)
        np.testing.assert_allclose(expected[:3], [30., -20., 100.])

    def test_hysteresis_requires_consecutive_healthy_samples_to_clear(self):
        for index in range(3):
            self.detector.update(
                _measurements(self.layout, failed_leg="FR", timestamp=index)
            )
        for index in range(3):
            update = self.detector.update(_measurements(self.layout, timestamp=10 + index))
            self.assertEqual(("FR",), update.active_legs)
        update = self.detector.update(_measurements(self.layout, timestamp=20))
        self.assertEqual(("FR",), update.cleared_legs)
        self.assertEqual((), update.active_legs)


class PacerTests(unittest.TestCase):
    def test_absolute_deadlines_do_not_accumulate_drift(self):
        current = [1_000_000_000]

        def clock():
            return current[0]

        def sleep(seconds):
            current[0] += int(round(seconds * 1_000_000_000))

        pacer = RealtimePacer(0.02, clock_ns=clock, sleep=sleep)
        pacer.start()
        first = pacer.wait_for_step(0)
        second = pacer.wait_for_step(1)
        self.assertEqual(0, first.slept_ns)
        self.assertEqual(20_000_000, second.slept_ns)
        self.assertEqual(1_020_000_000, second.actual_ns)


class PhysicalEndpointTests(unittest.TestCase):
    def setUp(self):
        self.layout = ActuatorLayout.from_physics(_FakePhysics())
        self.sample = _measurements(self.layout)
        self.spec = PhysicsTrialSpec(duration_s=10, fault_offset_s=3)

    def test_eligibility_checks_state_without_conflating_detector_endpoint(self):
        result = evaluate_pre_fault_eligibility(
            [self.sample] * 150, self.spec, detector_triggered_before_fault=True
        )
        self.assertTrue(result["eligible"])
        self.assertTrue(result["detector_triggered_before_fault"])
        inverted = replace(self.sample, torso_upright=-1.0)
        self.assertFalse(evaluate_pre_fault_eligibility([inverted] * 150, self.spec)["eligible"])

    def test_safety_endpoint_is_same_for_both_methods_and_counts_sustained_fall(self):
        healthy = [(index, self.sample) for index in range(150, 500)]
        for method in ("local_reflex", "no_reflex"):
            spec = replace(self.spec, method=method)
            result = summarize_physical_safety(
                healthy, spec, control_timestep=0.02, pre_fault_eligible=True
            )
            self.assertTrue(result["safe"])
            self.assertEqual(1.0, result["terminal_safe_fraction"])
            fallen = healthy.copy()
            for index in range(100, 105):
                fallen[index] = (150 + index, replace(self.sample, torso_upright=0.1))
            result = summarize_physical_safety(
                fallen, spec, control_timestep=0.02, pre_fault_eligible=True
            )
            self.assertFalse(result["safe"])
            self.assertEqual(250, result["first_fall_step"])
            self.assertEqual(254, result["fall_recognized_step"])

    def test_noise_is_seeded_and_cannot_mutate_ground_truth(self):
        config = DetectorNoiseConfig(
            actuator_force_relative_std=.1, joint_position_std_rad=.01,
            joint_velocity_std_rad_s=.02, gyro_std_rad_s=.03, accel_std_m_s2=.1,
        )
        first = DetectorMeasurementNoise(config, seed=314)
        second = DetectorMeasurementNoise(config, seed=314)
        a = first.apply(self.sample, nominal_effort=np.ones(12) * 100, minimum_expected_force=8)
        b = second.apply(self.sample, nominal_effort=np.ones(12) * 100, minimum_expected_force=8)
        np.testing.assert_array_equal(a.actuator_force, b.actuator_force)
        self.assertFalse(np.array_equal(a.actuator_force, self.sample.actuator_force))
        a.imu_gyro[:] = 99
        a.torso_position_world[:] = 99
        np.testing.assert_array_equal(self.sample.imu_gyro, np.zeros(3))
        np.testing.assert_array_equal(self.sample.torso_position_world, np.zeros(3))
        np.testing.assert_array_equal(self.sample.actuator_force, np.full(12, 100))
        zero = DetectorMeasurementNoise(DetectorNoiseConfig(), seed=314).apply(
            self.sample, nominal_effort=np.ones(12) * 100, minimum_expected_force=8
        )
        np.testing.assert_array_equal(zero.actuator_force, self.sample.actuator_force)

    def test_override_rejects_invalid_values_and_reserved_sources(self):
        for action in (np.zeros(11), np.full(12, np.nan), np.full(12, 1.01)):
            with self.assertRaises(ValueError):
                validate_control_override((action, "supervisor"), self.layout)
        with self.assertRaises(ValueError):
            validate_control_override((np.zeros(12), "local_reflex"), self.layout)
        action, source = validate_control_override((np.zeros(12), "supervisor"), self.layout)
        self.assertEqual("supervisor", source)
        np.testing.assert_array_equal(action, np.zeros(12))

    def test_push_and_noise_configuration_reject_unbounded_or_nonfinite_inputs(self):
        for kwargs in (
            {"lateral_push_force_n": 201},
            {"lateral_push_force_n": float("nan")},
            {"lateral_push_duration_s": 3},
            {"lateral_push_force_n": 20, "lateral_push_start_s": 2.9},
        ):
            with self.assertRaises(ValueError):
                PhysicsTrialSpec(**kwargs)
        with self.assertRaises(ValueError):
            DetectorNoiseConfig(gyro_std_rad_s=float("nan"))


class DmControlIntegrationTests(unittest.TestCase):
    def make_trial(self):
        try:
            from dm_control import suite
        except ImportError:
            self.skipTest("dm_control is unavailable")
        env = suite.load("quadruped", "walk", task_kwargs={"random": 470972406, "time_limit": 30})
        trial = QuadrupedTrial(env, realtime=False)
        trial.initialize(settle_steps=300, yaw_rad=.75)
        return trial

    def test_post_step_forward_does_not_advance_state_and_precedes_completion_clock(self):
        trial = self.make_trial()
        order = []
        original_forward = trial.physics.forward
        original_step = trial.env.step
        original_capture = trial.extractor.capture
        original_clock = trial.clock_ns
        observed = []

        def synchronized_forward():
            order.append("forward")
            before_time = float(trial.physics.data.time)
            before = {name: getattr(trial.physics.data, name).copy() for name in ("qpos", "qvel", "act")}
            original_forward()
            self.assertEqual(before_time, float(trial.physics.data.time))
            for name, values in before.items():
                np.testing.assert_array_equal(values, getattr(trial.physics.data, name))
            observed.append(before_time)

        def step(action):
            order.append("env.step")
            return original_step(action)

        def clock():
            order.append("clock")
            return original_clock()

        def capture(*args, **kwargs):
            order.append("capture")
            return original_capture(*args, **kwargs)

        with patch.object(trial.physics, "forward", synchronized_forward), patch.object(
            trial.env, "step", step
        ), patch.object(trial.extractor, "capture", capture), patch.object(trial, "clock_ns", clock):
            result = trial.step(nominal_trot_preset().action_at(11, trial.layout))
        self.assertEqual(["clock", "clock", "env.step", "forward", "clock", "capture"], order)
        self.assertEqual(1, len(observed))
        self.assertEqual(result.step_finished_ns, result.measurements.monotonic_ns)
        self.assertAlmostEqual(.02, result.sim_time_s)

    def test_dynamic_synchronized_force_matches_healthy_and_half_strength_model(self):
        trial = self.make_trial()
        model = trial.physics.model
        self.assertTrue(np.all(model.actuator_gaintype == 0))
        self.assertTrue(np.all(model.actuator_biastype == 1))
        self.assertFalse(np.any(model.actuator_forcelimited))
        fault = PlantActuatorFault(trial.physics, trial.layout)
        detector = ResidualFaultDetector(
            trial.layout, fault.nominal_gainprm, fault.nominal_biasprm,
            control_timestep=trial.control_timestep,
            nominal_forcelimited=model.actuator_forcelimited,
            nominal_forcerange=model.actuator_forcerange,
        )
        gait = nominal_trot_preset()
        fault_indices = list(trial.layout.indices("FL"))
        healthy_count = 0
        partial_count = 0
        for index in range(200):
            if index == 100:
                fault.inject("FL", strength_scale=.5)
            measurement = trial.step(gait.action_at(index + 11, trial.layout)).measurements
            expected = detector._nominal_force(measurement)
            actual_expected = expected.copy()
            if index >= 100:
                actual_expected[fault_indices] *= .5
            np.testing.assert_allclose(measurement.actuator_force, actual_expected, rtol=1e-11, atol=1e-8)
            update = detector.update(measurement)
            for leg, residual in update.residuals.items():
                if residual.expected_force_l1 < detector.config.minimum_expected_force:
                    self.assertEqual(0, residual.force_model_residual)
                elif leg == "FL" and index >= 100:
                    self.assertAlmostEqual(.5, residual.force_model_residual, places=10)
                    partial_count += 1
                else:
                    self.assertLess(residual.force_model_residual, 1e-10)
                    healthy_count += 1
        self.assertGreater(healthy_count, 100)
        self.assertGreater(partial_count, 20)
        self.assertEqual(.58, detector.config.enter_threshold)
        fault.restore_all()

    def test_synchronized_real_model_force_limit_matches_prediction(self):
        trial = self.make_trial()
        model = trial.physics.model
        # Exercise a bounded test-only force clamp; campaign model is unchanged.
        model.actuator_forcelimited[0] = True
        model.actuator_forcerange[0] = [-5., 7.]
        detector = ResidualFaultDetector(
            trial.layout, model.actuator_gainprm, model.actuator_biasprm,
            control_timestep=trial.control_timestep,
            nominal_forcelimited=model.actuator_forcelimited,
            nominal_forcerange=model.actuator_forcerange,
        )
        saturated = False
        for index in range(40):
            action = nominal_trot_preset().action_at(index + 11, trial.layout)
            action[0] = .8
            measurement = trial.step(action).measurements
            np.testing.assert_allclose(measurement.actuator_force, detector._nominal_force(measurement), rtol=1e-11, atol=1e-8)
            saturated = saturated or abs(measurement.actuator_force[0] - 7.) < 1e-10
        self.assertTrue(saturated)

    def test_short_trial_has_one_reset_persistent_fault_and_monotonic_events(self):
        try:
            import dm_control  # noqa: F401
        except ImportError:
            self.skipTest("dm_control is unavailable")

        events = []
        result = run_physics_trial(
            PhysicsTrialSpec(
                trial_id="unit-smoke-BR",
                method="local_reflex",
                fault_leg="BR",
                duration_s=1.2,
                fault_offset_s=0.3,
                settle_steps=300,
                record_trace=False,
            ),
            events.append,
        )
        self.assertTrue(result["detected"])
        self.assertTrue(result["fault_active_until_trial_end"])
        self.assertEqual(1, result["reset_count"])
        self.assertEqual(60, result["measured_steps"])
        self.assertEqual([6, 7, 8], result["actuator_layout"]["indices_by_leg"]["BR"])
        self.assertEqual([], result["false_triggers"])
        monotonic = [event["monotonic_ns"] for event in events]
        self.assertEqual(monotonic, sorted(monotonic))
        local_event = next(event for event in events if event["event_type"] == "local_reflex_applied")
        self.assertTrue(local_event["plant_fault_still_active"])
        self.assertEqual(MEASUREMENT_SYNC, result["measurement_sync"])
        snapshot = result["fault_parameter_snapshot"]
        self.assertEqual("BR", snapshot["leg"])
        self.assertEqual([6, 7, 8], snapshot["actuator_indices"])
        self.assertEqual(0, snapshot["strength_scale"])
        np.testing.assert_array_equal(snapshot["faulted_gainprm"], np.zeros((3, 10)))
        np.testing.assert_array_equal(snapshot["faulted_biasprm"], np.zeros((3, 10)))
        injected = next(event for event in events if event["event_type"] == "plant_fault_injected")
        self.assertEqual(snapshot, injected["fault_parameter_snapshot"])
        self.assertEqual((result["reference_step"] + result["gait_phase_offset_steps"]) % 20, result["injection_phase_steps"])

    def test_previous_inverted_seed_starts_upright_and_pairing_is_method_independent(self):
        try:
            import dm_control  # noqa: F401
        except ImportError:
            self.skipTest("dm_control is unavailable")
        paired = []
        for method in ("local_reflex", "no_reflex"):
            result = run_physics_trial(PhysicsTrialSpec(
                trial_id=f"upright-regression-{method}", method=method,
                seed=889603654, duration_s=2.0, fault_offset_s=1.0,
                record_trace=False,
            ))
            self.assertGreater(result["settled_upright"], 0.99)
            self.assertTrue(result["pre_fault_eligible"])
            self.assertEqual(1, result["reset_count"])
            self.assertIsNone(result["stability_latency_ms"])
            paired.append(result)
        self.assertEqual(paired[0]["initial_yaw_rad"], paired[1]["initial_yaw_rad"])
        self.assertEqual(paired[0]["gait_phase_offset_steps"], paired[1]["gait_phase_offset_steps"])

    def test_sim_latency_uses_post_integration_detection_timestamp(self):
        result = run_physics_trial(PhysicsTrialSpec(
            seed=470972406, fault_leg="FL", duration_s=2.0, fault_offset_s=1.0,
            record_trace=True,
        ))
        detected = result["trace"][result["detect_step"]]
        expected = detected["sim_time_s"] - result["fault_step"] * .02
        self.assertAlmostEqual(expected, result["detect_latency_sim_s"])
        self.assertAlmostEqual(
            .02, result["detect_latency_sim_s"] - (result["detect_step"] - result["fault_step"]) * .02
        )
        self.assertEqual(0, result["reflex_dispatch_latency_sim_s"])
        self.assertGreaterEqual(result["reflex_monotonic_ns"], result["detect_monotonic_ns"])

    def test_healthy_reference_preserves_horizon_and_marks_detection_not_applicable(self):
        result = run_physics_trial(PhysicsTrialSpec(
            fault_enabled=False, duration_s=2, fault_offset_s=1, record_trace=True,
            initial_yaw_rad=.75, gait_phase_offset_steps=17,
        ))
        self.assertFalse(result["fault_injected"])
        self.assertIsNone(result["fault_parameter_snapshot"])
        self.assertIsNone(result["detector_correct"])
        self.assertIsNone(result["detected"])
        self.assertIsNone(result["detect_latency_sim_s"])
        self.assertIsNone(result["fault_monotonic_ns"])
        self.assertIsNotNone(result["reference_monotonic_ns"])
        self.assertEqual(50, result["physical_safety"]["post_fault_samples"])
        self.assertTrue(result["success"])
        self.assertFalse(any(row["fault_active"] for row in result["trace"]))
        self.assertEqual(17, result["gait_phase_offset_steps"])
        self.assertEqual(.75, result["initial_yaw_rad"])

    def test_trace_recomputes_physical_endpoint_and_movement_without_noisy_sensor_data(self):
        spec = PhysicsTrialSpec(
            method="no_reflex", duration_s=2, fault_offset_s=1,
            detector_noise=DetectorNoiseConfig(actuator_force_relative_std=.15, gyro_std_rad_s=.2),
            lateral_push_force_n=10, lateral_push_start_s=1.2,
            lateral_push_duration_s=.1, record_trace=True,
        )
        result = run_physics_trial(spec)
        clean = run_physics_trial(replace(spec, detector_noise=DetectorNoiseConfig()))
        # With no reflex response, detector-only noise has no path into plant
        # dynamics or physical outcomes, including during the external push.
        self.assertEqual(clean["upright_deficit_integral"], result["upright_deficit_integral"])
        self.assertEqual(
            [row["torso_position_world_m"] for row in clean["trace"]],
            [row["torso_position_world_m"] for row in result["trace"]],
        )
        post = [row for row in result["trace"] if row["reference_reached"]]
        self.assertEqual(50, len(post))
        self.assertAlmostEqual(
            sum(max(0, 1-row["torso_upright"]) * .02 for row in post),
            result["upright_deficit_integral"],
        )
        self.assertEqual(min(row["torso_upright"] for row in post), result["post_fault_min_upright"])
        self.assertEqual(min(row["torso_height"] for row in post), result["post_fault_min_torso_height"])
        self.assertAlmostEqual(sum(row["torso_speed"] for row in post)/len(post), result["mean_speed_m_s"])
        self.assertAlmostEqual(sum(row["imu_gyro_norm"] for row in post)/len(post), result["mean_gyro_rad_s"])
        upright_fraction = sum(row["torso_upright"] >= .8 for row in post)/len(post)
        terminal_safe = sum(row["torso_upright"] >= .8 and row["torso_height"] >= .3 for row in post[-50:])/50
        self.assertEqual(upright_fraction, result["post_fault_upright_fraction"])
        self.assertEqual(terminal_safe, result["terminal_safe_fraction"])
        consecutive = 0
        fall = False
        for row in post:
            consecutive = consecutive+1 if row["torso_upright"] < .3 or row["torso_height"] < .18 else 0
            fall = fall or consecutive >= 5
        self.assertEqual(fall, result["fall_detected"])
        self.assertEqual(result["pre_fault_eligible"] and not fall and upright_fraction >= .95 and terminal_safe >= .95, result["success"])
        self.assertTrue(any(row["detector_observed_gyro_norm"] != row["imu_gyro_norm"] for row in post))
        self.assertEqual(5, result["lateral_push"]["applied_steps"])
        self.assertAlmostEqual(1., result["lateral_push"]["impulse_n_s"])

    def test_hooks_apply_exact_validated_action_and_detection_uses_copied_observation(self):
        selected = []
        command = np.tile([0., 0., -.4], 4)
        def detection(leg, measurement, step):
            selected.append((leg, step))
            measurement.imu_gyro[:] = 1e6
        def controller(step, leg, last_measurement):
            if last_measurement is not None:
                last_measurement.imu_gyro[:] = 1e6
            return (command, "supervisor_test") if step < 3 else None
        result = run_physics_trial(
            PhysicsTrialSpec(duration_s=2, fault_offset_s=1),
            on_detection=detection, before_control=controller,
        )
        self.assertEqual(1, len(selected))
        self.assertEqual("FL", selected[0][0])
        self.assertEqual(3, result["action_source_steps"]["supervisor_test"])
        for row in result["trace"][:3]:
            self.assertEqual(command.tolist(), row["commanded_action"])
            self.assertLessEqual(row["control_started_ns"], row["monotonic_ns"])
        self.assertLess(max(row["imu_gyro_norm"] for row in result["trace"]), 1e6)
        self.assertAlmostEqual(1., sum(result["action_source_duty_fraction"].values()))

    def test_healthy_false_trigger_is_recorded_and_hook_uses_detector_selected_leg(self):
        original_update = ResidualFaultDetector.update
        count = [0]
        selected = []
        def inject_detector_output(detector, measurement):
            update = original_update(detector, measurement)
            count[0] += 1
            return replace(update, triggered_legs=("BR",), active_legs=("BR",)) if count[0] == 5 else update
        with patch.object(ResidualFaultDetector, "update", inject_detector_output):
            result = run_physics_trial(
                PhysicsTrialSpec(fault_enabled=False, fault_leg="FL", duration_s=2, fault_offset_s=1),
                on_detection=lambda leg, measurement, step: selected.append(leg),
            )
        self.assertEqual(["BR"], selected)
        self.assertEqual(1, result["false_trigger_count"])
        self.assertEqual("BR", result["false_triggers"][0]["leg"])
        self.assertIsNone(result["detector_correct"])
        self.assertIsNone(result["reflex_applied_correctly"])
        self.assertTrue(result["false_reflex_applied"])
        self.assertEqual("BR", result["detector_selected_leg"])


if __name__ == "__main__":
    unittest.main()
