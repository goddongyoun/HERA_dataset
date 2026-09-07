from __future__ import annotations

import unittest

import numpy as np

from hera_v2.presets import (
    deterministic_stabilization_preset,
    nominal_trot_preset,
    standing_preset,
)


class _CanonicalLayout:
    indices_by_leg = {"FL": (0, 1, 2), "FR": (3, 4, 5), "BR": (6, 7, 8), "BL": (9, 10, 11)}

    def action_from_leg_targets(self, targets):
        action = np.zeros(12)
        for leg, indices in self.indices_by_leg.items():
            action[list(indices)] = targets[leg]
        return action


class PresetTests(unittest.TestCase):
    def setUp(self):
        self.layout = _CanonicalLayout()

    def test_standing_uses_yaw_lift_extend_semantics(self):
        expected = np.tile([0.0, 0.0, -0.4], 4)
        np.testing.assert_allclose(standing_preset().to_action(self.layout), expected)

    def test_trot_has_correct_diagonal_pairs_in_real_model_order(self):
        preset = nominal_trot_preset(phase_steps=2)
        phase_a = preset.action_at(0, self.layout).reshape(4, 3)
        phase_b = preset.action_at(2, self.layout).reshape(4, 3)
        # Canonical rows are FL, FR, BR, BL.  FL+BR match in A; FR+BL in B.
        np.testing.assert_allclose(phase_a[0], phase_a[2])
        np.testing.assert_allclose(phase_a[1], phase_a[3])
        np.testing.assert_allclose(phase_b[1], phase_b[3])
        self.assertFalse(np.allclose(phase_a[0], phase_a[1]))

    def test_local_stabilization_is_deterministic_and_leg_specific(self):
        first = deterministic_stabilization_preset("BR").to_action(self.layout)
        second = deterministic_stabilization_preset("BR").to_action(self.layout)
        np.testing.assert_array_equal(first, second)
        rows = first.reshape(4, 3)
        np.testing.assert_allclose(rows[2], [0.0, 0.0, -0.4])
        np.testing.assert_allclose(rows[0], [0.0, -0.08, -0.48])


if __name__ == "__main__":
    unittest.main()
