from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from hera_v2.events import EventIdentity, JsonlEventLogger


class JsonlEventLoggerTests(unittest.TestCase):
    def test_preserves_existing_lines_and_writes_complete_identity_and_clocks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            path.write_text('{"preexisting":true}\n', encoding="utf-8")
            logger = JsonlEventLogger(path)
            identity = EventIdentity(
                batch_id="batch-1",
                run_id="run-1",
                trial_id="trial-1",
                request_id="request-1",
                request_type="emergency",
                generation=2,
            )
            first = logger.emit("request_queued", identity, {"attempt": 1})
            second = logger.emit("request_dispatched", identity, {"lane": "single"})

            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual({"preexisting": True}, json.loads(lines[0]))
            records = [json.loads(line) for line in lines[1:]]
            self.assertEqual(2, len(records))
            # Windows' monotonic source can report the same tick for adjacent
            # writes; sequence is the strict local ordering tie-breaker.
            self.assertLessEqual(first.monotonic_ns, second.monotonic_ns)
            self.assertLess(first.sequence, second.sequence)
            for record in records:
                self.assertEqual("batch-1", record["batch_id"])
                self.assertEqual("run-1", record["run_id"])
                self.assertEqual("trial-1", record["trial_id"])
                self.assertEqual("request-1", record["request_id"])
                self.assertEqual("emergency", record["request_type"])
                self.assertEqual(2, record["generation"])
                self.assertIsInstance(record["monotonic_ns"], int)
                self.assertIsNotNone(datetime.fromisoformat(record["utc"]))


if __name__ == "__main__":
    unittest.main()
