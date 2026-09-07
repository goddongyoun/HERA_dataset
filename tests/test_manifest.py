from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
import json
from pathlib import Path

from hera_v2.manifest import (
    build_blocked_manifest,
    load_manifest,
    trial_spec_sha256,
    write_manifest,
)


class ManifestTests(unittest.TestCase):
    def make_manifest(self):
        return build_blocked_manifest(
            study="scheduler",
            methods=("hera_preempt", "fifo_single", "reserved_slot"),
            batches=2,
            repeats_per_cell=2,
            master_seed=20260907,
            model="qwen3.5:9b",
            deadline_s=30,
            fault_offset_s=0.25,
            duration_s=30,
            realtime=False,
        )

    def test_complete_blocks_and_paired_seeds(self):
        specs = self.make_manifest()
        self.assertEqual(2 * 2 * 3 * 4, len(specs))
        by_block = {}
        for spec in specs:
            by_block.setdefault(spec.block_id, []).append(spec)
        for block in by_block.values():
            self.assertEqual(12, len(block))
            self.assertEqual(12, len({(s.method, s.fault_leg) for s in block}))
            for leg in ("FL", "FR", "BR", "BL"):
                self.assertEqual(1, len({s.seed for s in block if s.fault_leg == leg}))

    def test_reproducible_but_not_fixed_method_order(self):
        first = self.make_manifest()
        second = self.make_manifest()
        self.assertEqual(first, second)
        sequence = [spec.method for spec in first]
        self.assertNotEqual(sorted(sequence), sequence)

    def test_round_trip(self):
        specs = self.make_manifest()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.jsonl"
            write_manifest(specs, path)
            self.assertEqual(specs, load_manifest(path))

    def test_trial_hash_covers_all_spec_fields(self):
        original = self.make_manifest()[0]
        changed = replace(original, deadline_s=original.deadline_s + 1)
        self.assertNotEqual(trial_spec_sha256(original), trial_spec_sha256(changed))
        self.assertEqual(trial_spec_sha256(original), trial_spec_sha256(original))

    def test_load_rejects_rows_that_do_not_follow_order_index(self):
        specs = self.make_manifest()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "reordered.jsonl"
            rows = [spec.to_dict() for spec in specs]
            rows[0], rows[1] = rows[1], rows[0]
            path.write_text(
                "\n".join(json.dumps(row) for row in rows) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "row order"):
                load_manifest(path)


if __name__ == "__main__":
    unittest.main()
