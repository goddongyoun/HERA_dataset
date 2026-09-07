from dataclasses import replace
import unittest
from make_paper_manifests import make_matrix
from hera_v2.analysis import analyze, estimate_remaining_seconds


class PaperMatrixTests(unittest.TestCase):
    def test_exact_prespecified_grid_and_pairing(self):
        matrix = make_matrix()
        self.assertEqual(1472, sum(map(len, matrix.values())))
        for name, rows in matrix.items():
            keys = {}
            for row in rows:
                keys.setdefault((row.block_id, row.fault_leg), []).append(row)
            for group in keys.values():
                self.assertEqual(1, len({r.seed for r in group}))
                self.assertEqual(1, len({str(r.metadata.get('physics')) for r in group}))
            if name.startswith('physics_') and name != 'physics_realtime':
                for leg in ('FL', 'FR', 'BR', 'BL'):
                    for method in ('local_reflex', 'no_reflex'):
                        self.assertEqual(set(range(20)), {r.metadata['physics']['gait_phase_offset_steps']
                            for r in rows if r.fault_leg == leg and r.method == method})

    def test_eta_cannot_mix_scheduler_and_live_physics(self):
        matrix = make_matrix(pilot=True)
        a = next(r for r in matrix['scheduler_t2048'] if r.method == 'hera_preempt')
        b = next(r for r in matrix['integrated'] if r.method == 'hera_preempt')
        rows = [{**a.to_dict(), 'status': 'complete', 'elapsed_s': 1.0}]
        eta = estimate_remaining_seconds([a, b], rows, overhead_fraction=0)
        self.assertEqual(b.duration_s + 1, eta['estimated_remaining_s'])

    def test_integrated_physical_success_is_not_a_latency_event(self):
        spec = make_matrix(pilot=True)['integrated'][0]
        report = analyze([{**spec.to_dict(), 'status': 'complete', 'success': True,
                           'event_time_s': None, 'result': {'physical_safe': True}}])
        group = report['methods'][spec.method]
        self.assertEqual(1.0, group['physical_safety_success_rate'])
        self.assertNotIn('deadline_penalized_mean_s', group)


if __name__ == '__main__':
    unittest.main()
