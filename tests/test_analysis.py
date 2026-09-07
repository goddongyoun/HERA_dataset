from __future__ import annotations

import unittest

from hera_v2.analysis import analyze, estimate_remaining_seconds
from hera_v2.manifest import build_blocked_manifest


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.specs = build_blocked_manifest(
            study="scheduler",
            methods=("hera_preempt", "fifo_single"),
            legs=("FL",),
            batches=1,
            repeats_per_cell=2,
            master_seed=7,
            model="fake",
            deadline_s=10,
            fault_offset_s=0.1,
            duration_s=10,
            realtime=False,
        )

    def test_failures_enter_restricted_mean(self):
        rows = []
        for index, spec in enumerate(self.specs):
            success = index != 0
            rows.append({
                **spec.to_dict(),
                "status": "complete",
                "success": success,
                "event_time_s": 2.0 if success else None,
                "failure_reason": None if success else "deadline",
                "elapsed_s": 3.0,
            })
        report = analyze(rows)
        failed_method = self.specs[0].method
        method = report["methods"][failed_method]
        self.assertLess(method["deadline_success_rate"], 1.0)
        self.assertGreater(method["restricted_mean_to_deadline_s"], 2.0)

    def test_eta_uses_method_pilot(self):
        first = self.specs[0]
        rows = [{
            **first.to_dict(), "status": "complete", "success": True,
            "event_time_s": 1.0, "elapsed_s": 5.0,
        }]
        eta = estimate_remaining_seconds(self.specs, rows, overhead_fraction=0)
        self.assertEqual(len(self.specs) - 1, eta["remaining"])
        expected = sum(
            5.0 if spec.method == first.method
            else spec.deadline_s + spec.fault_offset_s + 5.0
            for spec in self.specs if spec.trial_id != first.trial_id
        )
        self.assertEqual(expected, eta["estimated_remaining_s"])

    def test_error_missing_and_late_success_are_not_dropped(self):
        specs = self.specs
        rows = [
            {**specs[0].to_dict(), "status": "error", "success": False},
            {**specs[1].to_dict(), "status": "complete", "success": True,
             "event_time_s": 11.0},
            {**specs[2].to_dict(), "status": "complete", "success": True,
             "event_time_s": 2.0},
        ]
        report = analyze(rows, [specs[3].trial_id], specs=specs)
        self.assertEqual(4, report["n_expected"])
        self.assertEqual(1, sum(group["n_success"] for group in report["methods"].values()))
        self.assertEqual(1, sum(group["n_infrastructure_error"] for group in report["methods"].values()))
        self.assertEqual(1, sum(group["n_missing"] for group in report["methods"].values()))

    def test_physical_metrics_are_reported_without_detection_dependency(self):
        row = {
            "study": "physics", "method": "no_reflex", "batch_id": "b1",
            "block_id": "r1", "fault_leg": "FL", "seed": 1,
            "trial_id": "t1", "deadline_s": 7.0,
            "status": "complete", "success": True, "event_time_s": .28,
            "result": {
                "pre_fault_eligible": True, "detector_correct": False,
                "reflex_applied_correctly": False, "fall_detected": False,
                "post_fault_min_upright": .9,
                "post_fault_min_torso_height": .2,
            },
        }
        report = analyze([row])
        physical = report["methods"]["no_reflex"]["physics_endpoints"]
        self.assertEqual(.9, physical["post_fault_min_upright"]["mean"])
        self.assertEqual(0, physical["correct_detection_rate"])
        self.assertEqual(1, report["methods"]["no_reflex"]["n_success"])

    def test_paired_contrast_uses_same_block_leg_seed(self):
        rows = []
        for spec in self.specs:
            rows.append({
                **spec.to_dict(), "status": "complete", "success": True,
                "event_time_s": 1.0 if spec.method == "hera_preempt" else 4.0,
            })
        report = analyze(rows)
        contrast = report["paired_contrasts"]["hera_preempt_minus_fifo_single"]
        self.assertEqual(2, contrast["n_manifest_pairs"])
        self.assertEqual(-3.0, contrast["metrics"]["deadline_penalized_latency_s"]["mean_difference"])


if __name__ == "__main__":
    unittest.main()
