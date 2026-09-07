from __future__ import annotations

from dataclasses import replace
from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from hera_v2.manifest import (
    build_blocked_manifest,
    manifest_file_sha256,
    trial_spec_sha256,
    write_manifest,
)
import run_campaign


class RunnerIntegrityTests(unittest.TestCase):
    def make_spec(self):
        return build_blocked_manifest(
            study="scheduler",
            methods=("hera_preempt",),
            legs=("FL",),
            batches=1,
            repeats_per_cell=1,
            master_seed=11,
            model="fake",
            deadline_s=10,
            fault_offset_s=0.1,
            duration_s=10,
            realtime=False,
        )[0]

    def test_campaign_snapshots_are_content_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_path = write_manifest([self.make_spec()], root / "manifest.jsonl")
            sources = run_campaign._load_source_manifests([source_path])
            prepared = run_campaign._prepare_manifest_snapshots(root / "campaign", sources)
            self.assertEqual(manifest_file_sha256(source_path), prepared[0]["sha256"])
            snapshot = prepared[0]["path"]
            self.assertEqual(source_path.read_bytes(), snapshot.read_bytes())

            changed = replace(self.make_spec(), deadline_s=11.0)
            write_manifest([changed], source_path)
            changed_sources = run_campaign._load_source_manifests([source_path])
            with self.assertRaisesRegex(RuntimeError, "do not match"):
                run_campaign._prepare_manifest_snapshots(root / "campaign", changed_sources)

    def test_attempt_directories_are_never_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            trial_dir = Path(directory) / "trial"
            first_id, first = run_campaign._next_attempt_dir(trial_dir)
            second_id, second = run_campaign._next_attempt_dir(trial_dir)
            self.assertEqual("attempt-0001", first_id)
            self.assertEqual("attempt-0002", second_id)
            self.assertNotEqual(first, second)

    def test_complete_requires_exact_hashes_and_untampered_attempt(self):
        spec = self.make_spec()
        manifest_hash = "a" * 64
        with tempfile.TemporaryDirectory() as directory:
            results = Path(directory)
            trial_dir = results / spec.trial_id
            attempt_id, attempt_dir = run_campaign._next_attempt_dir(trial_dir)
            events = attempt_dir / "events.jsonl"
            events.write_text("{}\n", encoding="utf-8")
            event_payload = events.read_bytes()
            worker = {
                **spec.to_dict(),
                "schema_version": 1,
                "trial_spec_sha256": trial_spec_sha256(spec),
                "manifest_sha256": manifest_hash,
                "attempt_id": attempt_id,
                "status": "complete",
                "success": True,
                "event_time_s": 1.0,
                "failure_reason": None,
                "elapsed_s": 2.0,
                "environment": {},
                "result": {},
                "artifacts": {
                    "events": {
                        "file": "events.jsonl",
                        "sha256": hashlib.sha256(event_payload).hexdigest(),
                        "bytes": len(event_payload),
                        "rows": 1,
                    }
                },
            }
            worker_path = attempt_dir / "summary.json"
            worker_path.write_text(json.dumps(worker), encoding="utf-8")
            adopted = {
                **worker,
                "adopted_attempt_id": attempt_id,
                "worker_summary_sha256": hashlib.sha256(worker_path.read_bytes()).hexdigest(),
                "parent_wall_cycle_s": 2.3,
            }
            trial_dir.mkdir(parents=True, exist_ok=True)
            (trial_dir / "summary.json").write_text(json.dumps(adopted), encoding="utf-8")

            self.assertTrue(run_campaign._complete(spec, results, manifest_hash))
            self.assertFalse(
                run_campaign._complete(replace(spec, deadline_s=11.0), results, manifest_hash)
            )
            events.write_text("tampered\n", encoding="utf-8")
            self.assertFalse(run_campaign._complete(spec, results, manifest_hash))

    def test_lock_rejects_live_pid_and_recovers_dead_pid(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runner.lock"
            path.write_text(json.dumps({"pid": -1, "token": "stale"}), encoding="utf-8")
            token = run_campaign._acquire_lock(path)
            self.assertEqual(os.getpid(), run_campaign._read_lock(path)["pid"])
            with self.assertRaisesRegex(RuntimeError, "live PID"):
                run_campaign._acquire_lock(path)
            run_campaign._release_lock(path, token)
            self.assertFalse(path.exists())

    @unittest.skipUnless(os.name == "nt", "Windows process-handle probe")
    def test_windows_liveness_probe_never_uses_kill_or_terminates_child(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"])
        try:
            with mock.patch.object(run_campaign.os, "kill", side_effect=AssertionError("unsafe probe")):
                self.assertTrue(run_campaign._pid_is_alive(os.getpid()))
                self.assertTrue(run_campaign._pid_is_alive(child.pid))
                self.assertIsNone(child.poll())
            child.terminate()
            child.wait(timeout=5)
            self.assertFalse(run_campaign._pid_is_alive(child.pid))
        finally:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=5)

    def test_pilot_estimates_and_lifecycle_are_in_initial_eta(self):
        spec = self.make_spec()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            estimates = root / "pilot.json"
            estimates.write_text('{"hera_preempt": 4.0}', encoding="utf-8")
            parsed = run_campaign._load_pilot_estimates(str(estimates))
            self.assertEqual({"hera_preempt": 4.0}, parsed)
            self.assertEqual(parsed, run_campaign._load_pilot_estimates(estimates.read_text()))
            state = run_campaign._progress(
                campaign_dir=root, all_specs=[spec],
                manifest_hash_for_trial={spec.trial_id: "a" * 64},
                results_dir=root / "trials", started_monotonic=time.perf_counter(),
                current=None, failures=[], pilot_method_seconds=parsed,
                scheduler_batch_warmup=True,
            )
            self.assertAlmostEqual(64.4, state["estimated_remaining_s"])
            self.assertEqual(1, state["scheduler_lifecycle"]["remaining_batches"])
            warmed = run_campaign._progress(
                campaign_dir=root, all_specs=[spec],
                manifest_hash_for_trial={spec.trial_id: "a" * 64},
                results_dir=root / "trials", started_monotonic=time.perf_counter(),
                current=None, failures=[], pilot_method_seconds=parsed,
                scheduler_batch_warmup=True,
                prepared_batches={run_campaign._batch_key(spec)},
                lifecycle_records=[{"status": "complete", "elapsed_s": 20.0}],
            )
            self.assertAlmostEqual(4.4, warmed["estimated_remaining_s"])
            self.assertEqual(20.0, warmed["scheduler_lifecycle"]["elapsed_s"])
        for bad in ('[]', '{"hera_preempt": -1}', '{"hera_preempt": true}', '{"hera_preempt": NaN}'):
            with self.subTest(bad=bad), self.assertRaises((ValueError, OSError)):
                run_campaign._load_pilot_estimates(bad)

    def test_scheduler_batches_warm_once_and_frozen_manifests_are_analyzed(self):
        specs = build_blocked_manifest(
            study="scheduler", methods=("hera_preempt", "fifo_single"), legs=("FL",),
            batches=2, repeats_per_cell=1, master_seed=19, model="fake",
            deadline_s=10, fault_offset_s=0.1, duration_s=10, realtime=False,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = write_manifest(specs, root / "source.jsonl")
            campaign = root / "campaign"
            commands = []

            def fake_run(command, **kwargs):
                commands.append(command)
                if Path(command[1]).name == "trial_worker.py":
                    bound_manifest = Path(command[command.index("--manifest") + 1])
                    trial_id = command[command.index("--trial-id") + 1]
                    spec = next(item for item in specs if item.trial_id == trial_id)
                    attempt_id = command[command.index("--attempt-id") + 1]
                    output = Path(command[command.index("--output-dir") + 1])
                    events = output / "events.jsonl"
                    events.write_text("{}\n", encoding="utf-8")
                    summary = {
                        **spec.to_dict(), "schema_version": 1,
                        "trial_spec_sha256": trial_spec_sha256(spec),
                        "manifest_sha256": manifest_file_sha256(bound_manifest),
                        "attempt_id": attempt_id, "status": "complete", "success": True,
                        "event_time_s": 1.0, "elapsed_s": 2.0,
                        "environment": {"source_sha256": "source-hash"}, "result": {},
                        "artifacts": {"events": {"file": "events.jsonl", "sha256": hashlib.sha256(events.read_bytes()).hexdigest()}},
                    }
                    (output / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
                elif Path(command[1]).name == "analyze_results.py":
                    output = Path(command[command.index("--output-dir") + 1])
                    (output / "analysis.json").write_text("{}", encoding="utf-8")
                    (output / "trial_summary.csv").write_text("trial_id\n", encoding="utf-8")
                else:
                    self.fail(f"Unexpected subprocess: {command}")
                return subprocess.CompletedProcess(command, 0)

            args = ["run_campaign.py", str(manifest), "--campaign-dir", str(campaign)]
            with mock.patch.object(sys, "argv", args), \
                 mock.patch.object(run_campaign, "bind_campaign_source", return_value={"source_sha256": "source-hash"}), \
                 mock.patch.object(run_campaign, "source_digest", return_value="source-hash"), \
                 mock.patch.object(run_campaign, "prepare_scheduler_batch", return_value={"elapsed_s": 1.0}) as warmup, \
                 mock.patch.object(run_campaign.subprocess, "run", side_effect=fake_run), \
                 redirect_stdout(io.StringIO()):
                self.assertEqual(0, run_campaign.main())
                self.assertEqual(2, warmup.call_count)
                self.assertEqual([2, 2], [len(call.args[0]) for call in warmup.call_args_list])
                # A finished campaign resumes without new worker attempts or warmups.
                self.assertEqual(0, run_campaign.main())
                self.assertEqual(2, warmup.call_count)
            worker_calls = [call for call in commands if Path(call[1]).name == "trial_worker.py"]
            analyzer_calls = [call for call in commands if Path(call[1]).name == "analyze_results.py"]
            self.assertEqual(4, len(worker_calls))
            self.assertEqual(2, len(analyzer_calls))
            self.assertTrue(all("--require-all" in call for call in analyzer_calls))
            self.assertTrue(all(Path(call[2]).parent == campaign / "manifests" for call in analyzer_calls))
            progress = json.loads((campaign / "progress.json").read_text())
            self.assertEqual("complete", progress["runner_status"])
            self.assertEqual("complete", progress["analysis"]["status"])
            self.assertEqual(4, progress["completed_trials"])
            adopted = json.loads((campaign / "trials" / specs[0].trial_id / "summary.json").read_text())
            self.assertIn("parent_wall_elapsed_s", adopted)

    def test_auto_analysis_failure_is_not_reported_as_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepared = [{"path": root / "manifest.jsonl", "sha256": "a" * 64}]
            with mock.patch.object(run_campaign.subprocess, "run", return_value=subprocess.CompletedProcess([], 2)):
                report = run_campaign._run_final_analyses(prepared, root / "trials", root)
            self.assertEqual("failed", report["status"])
            self.assertEqual(2, report["manifests"][0]["returncode"])


if __name__ == "__main__":
    unittest.main()
