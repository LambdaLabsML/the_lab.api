"""Regression checks for API-side native Slurm ownership fencing."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from the_lab.runner import ExperimentRunner
from the_lab.store import Store


class SlurmLifecycleStoreTests(unittest.TestCase):
    def test_old_generation_cannot_terminalize_replacement(self):
        """A late gen-0 completion cannot overwrite a gen-1 running record."""
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            idea = store.create_idea("test", [], "main")
            store.save_idea(idea)
            exp = store.create_experiment(idea["id"], "run", meta={
                "slurm_job_id": "42", "slurm_native_requeue": True,
                "slurm_restart_count": 0, "slurm_state": "RUNNING",
            })
            label = exp["label"]
            store.update_experiment(label, status="running")

            # Sequence: RUNNING -> PREEMPTED -> empty queue/accounting
            # PREEMPTED -> PENDING -> RUNNING generation 1.
            current = store.get_experiment(label)
            meta = {**current["meta"], "slurm_restart_count": 1,
                    "slurm_state": "RUNNING"}
            self.assertIsNotNone(store.update_experiment_if(
                label, expected_status="running", expected_job_id="42",
                expected_generation=0, meta=meta))

            # Gen-0 output/completion and a manual replacement's old job are
            # both fenced; neither releases ownership by publishing terminal.
            self.assertIsNone(store.update_experiment_if(
                label, expected_status="running", expected_job_id="42",
                expected_generation=0, status="completed", metrics={"score": 9}))
            self.assertIsNone(store.update_experiment_if(
                label, expected_status="running", expected_job_id="old",
                expected_generation=1, status="completed"))
            self.assertEqual(store.get_experiment(label)["status"], "running")
            self.assertEqual(store.get_experiment(label)["meta"]["slurm_restart_count"], 1)

    def test_cancelled_record_stays_terminal_despite_old_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            idea = store.create_idea("test", [], "main")
            store.save_idea(idea)
            exp = store.create_experiment(idea["id"], "run", meta={
                "slurm_job_id": "42", "slurm_restart_count": 1,
            })
            store.update_experiment(exp["label"], status="cancelled")
            self.assertIsNone(store.update_experiment_if(
                exp["label"], expected_status="running", expected_job_id="42",
                expected_generation=1, status="completed"))
            self.assertEqual(store.get_experiment(exp["label"])["status"], "cancelled")


class _ExecutorSequence:
    def __init__(self, states, accounting, generations):
        self.states = iter(states)
        self.accounting = iter(accounting)
        self.generations = iter(generations)
        self.pulls = 0

    def poll_status(self, _job):
        return next(self.states)

    def accounting_status(self, _job):
        item = next(self.accounting)
        if isinstance(item, Exception):
            raise item
        return item

    def restart_count(self, _label):
        return next(self.generations)

    def pull_results(self, _label, local_dir):
        self.pulls += 1
        # Tests can inject a transient transfer failure before a stable pull.
        if hasattr(self, "pull_failures") and self.pulls <= self.pull_failures:
            raise RuntimeError("temporary rsync failure")
        (local_dir / "script.log").write_text(
            "" if getattr(self, "no_result", False) else '{"metrics":{"score":1}}\n')
        # The real executor returns the staged marker generation.
        return getattr(self, "pull_generation", 1 if self.pulls >= 3 else 0)

    def cleanup_remote(self, *_args, **_kwargs):
        pass


class SlurmLifecycleMonitorTests(unittest.IsolatedAsyncioTestCase):
    def _runner(self, store):
        runner = ExperimentRunner.__new__(ExperimentRunner)
        runner._store, runner._label_locks = store, {}
        runner._allocator, runner._tasks = Mock(), {}
        runner._finished_queue, runner._scheduler_wake = __import__('asyncio').Queue(), None
        runner._last_queue_broadcast = 0.0
        return runner

    def _running(self, store, idea, generation=0):
        exp = store.create_experiment(idea["id"], "x", meta={
            "slurm_job_id": "42", "slurm_native_requeue": True,
            "slurm_restart_count": generation, "slurm_state": "PENDING",
        })
        store.update_experiment(exp["label"], status="running")
        return exp

    async def test_actual_start_follows_allocation_and_resets_after_requeue(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp)); idea = store.create_idea("x", [], "main"); store.save_idea(idea)
            exp = self._running(store, idea); runner = self._runner(store)
            store.update_experiment(exp["label"], started_at="2026-01-01T00:00:00+00:00")
            evidence = {"state": "COMPLETED", "restart_count": 1, "exit_code": 0, "signal": 0,
                        "started_at": "2026-01-01T02:00:00+00:00", "finished_at": "2026-01-01T02:10:00+00:00"}
            executor = _ExecutorSequence(["RUNNING", "PENDING", "RUNNING", "COMPLETED"], [evidence], [0, 1, 1])
            executor.pull_generation = 1
            starts = iter(["2026-01-01T01:00:00+00:00", "2026-01-01T02:00:00+00:00"])
            executor.start_time = lambda _: next(starts)
            ws = Mock(); ws.broadcaster = Mock()
            async def no_sleep(_): pass
            with patch("the_lab.runner.asyncio.sleep", no_sleep):
                await runner._monitor_slurm_job_inner(exp["id"], executor, "42", exp["label"],
                    Path(tmp) / ".the_lab" / "experiments" / "1" / "1", ws)
            final = store.get_experiment(exp["label"])
            self.assertEqual(final["status"], "completed")
            self.assertEqual(final["actual_started_at"], "2026-01-01T01:00:00+00:00")
            self.assertEqual(final["attempt_started_at"], "2026-01-01T02:00:00+00:00")
            self.assertEqual(final["runtime_seconds"], 600)

    async def test_native_preemption_sequence_keeps_allocator_until_gen1_final(self):
        """RUNNING, PREEMPTED, empty/PREEMPTED, PENDING, RUNNING gen1, final."""
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp)); idea = store.create_idea("x", [], "main"); store.save_idea(idea)
            exp = self._running(store, idea)
            runner = self._runner(store)
            executor = _ExecutorSequence(
                ["RUNNING", "PREEMPTED", "", "PENDING", "RUNNING", "COMPLETED"],
                [{"state": "PREEMPTED", "restart_count": 0, "exit_code": 0, "signal": 0},
                 {"state": "COMPLETED", "restart_count": 1, "exit_code": 0, "signal": 0}],
                [0, 0, 1, 1],
            )
            ws = Mock(); ws.broadcaster = Mock(); ws.broadcaster.broadcast_soon = Mock()
            async def no_sleep(_): pass
            with patch("the_lab.runner.asyncio.sleep", no_sleep):
                await runner._monitor_slurm_job_inner(exp["id"], executor, "42", exp["label"],
                                                      Path(tmp) / ".the_lab" / "experiments" / "1" / "1", ws)
            done = store.get_experiment(exp["label"])
            self.assertEqual(done["status"], "completed")
            self.assertEqual(done["meta"]["slurm_restart_count"], 1)
            self.assertEqual(done["meta"]["slurm_state"], "COMPLETED")
            # Preemption and empty squeue never released the allocator.
            self.assertEqual(runner._allocator.release.call_count, 1)

    async def test_terminal_evidence_retries_then_rejects_old_generation_and_nonzero_exit(self):
        """Accounting/pull failures retain tracking; stale evidence cannot win."""
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp)); idea = store.create_idea("x", [], "main"); store.save_idea(idea)
            exp = self._running(store, idea, generation=1); runner = self._runner(store)
            executor = _ExecutorSequence(
                ["COMPLETED", "COMPLETED", "COMPLETED"],
                [RuntimeError("sacct temporarily down"),
                 {"state": "COMPLETED", "restart_count": 0, "exit_code": 0, "signal": 0},
                 {"state": "COMPLETED", "restart_count": 1, "exit_code": 7, "signal": 0}],
                [],
            )
            executor.pull_generation = 1
            ws = Mock(); ws.broadcaster = Mock(); ws.broadcaster.broadcast_soon = Mock()
            async def no_sleep(_): pass
            with patch("the_lab.runner.asyncio.sleep", no_sleep):
                await runner._monitor_slurm_job_inner(exp["id"], executor, "42", exp["label"],
                                                      Path(tmp) / ".the_lab" / "experiments" / "1" / "1", ws)
            self.assertEqual(store.get_experiment(exp["label"])["status"], "failed")
            self.assertIn("FAILED", store.get_experiment(exp["label"])["meta"]["slurm_state"])
            self.assertEqual(store.get_experiment(exp["label"])["metrics"], {"score": 1})
            self.assertEqual(runner._allocator.release.call_count, 1)

    async def test_failure_metrics_preserve_status_and_reject_stale_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root); idea = store.create_idea("x", [], "main"); store.save_idea(idea)
            exp = self._running(store, idea, generation=1); runner = self._runner(store)
            store.update_experiment(exp['label'], status='failed', error='worker crashed')
            (root / 'script.log').write_text('{"metrics":{"score":0.226155},"meta":{"slurm_job_id":"wrong"}}\n')
            self.assertIsNone(await runner._retain_slurm_failure_metrics(exp['id'], root, '42', 0))
            self.assertIsNone(store.get_experiment(exp['label']).get('metrics'))
            self.assertEqual(await runner._retain_slurm_failure_metrics(exp['id'], root, '42', 1),
                             {'score': 0.226155})
            saved = store.get_experiment(exp['label'])
            self.assertEqual(saved['status'], 'failed')
            self.assertEqual(saved['error'], 'worker crashed')
            self.assertEqual(saved['meta']['slurm_job_id'], '42')

    async def test_failure_without_result_does_not_invent_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root); idea = store.create_idea("x", [], "main"); store.save_idea(idea)
            exp = self._running(store, idea); runner = self._runner(store)
            (root / 'script.log').write_text('Traceback: failed before results\n')
            self.assertIsNone(await runner._retain_slurm_failure_metrics(exp['id'], root, '42', 0))
            self.assertIsNone(store.get_experiment(exp['label']).get('metrics'))

    async def test_transient_terminal_pull_failure_keeps_monitor_until_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp)); idea = store.create_idea("x", [], "main"); store.save_idea(idea)
            exp = self._running(store, idea); runner = self._runner(store)
            evidence = {"state": "COMPLETED", "restart_count": 0, "exit_code": 0, "signal": 0}
            executor = _ExecutorSequence(["COMPLETED", "COMPLETED"], [evidence, evidence], [])
            executor.pull_failures = 1
            ws = Mock(); ws.broadcaster = Mock(); ws.broadcaster.broadcast_soon = Mock()
            async def no_sleep(_): pass
            with patch("the_lab.runner.asyncio.sleep", no_sleep):
                await runner._monitor_slurm_job_inner(exp["id"], executor, "42", exp["label"],
                                                      Path(tmp) / ".the_lab" / "experiments" / "1" / "1", ws)
            self.assertEqual(executor.pulls, 2)
            self.assertEqual(store.get_experiment(exp["label"])["status"], "completed")

    async def test_terminal_accounting_learns_fast_restart_before_finalizing(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp)); idea = store.create_idea("x", [], "main"); store.save_idea(idea)
            exp = self._running(store, idea); runner = self._runner(store)
            evidence = {"state": "COMPLETED", "restart_count": 1, "exit_code": 0, "signal": 0}
            executor = _ExecutorSequence(["COMPLETED", "COMPLETED"], [evidence, evidence], [])
            executor.pull_generation = 1
            ws = Mock(); ws.broadcaster = Mock(); ws.broadcaster.broadcast_soon = Mock()
            async def no_sleep(_): pass
            with patch("the_lab.runner.asyncio.sleep", no_sleep):
                await runner._monitor_slurm_job_inner(exp["id"], executor, "42", exp["label"],
                                                      Path(tmp) / ".the_lab" / "experiments" / "1" / "1", ws)
            final = store.get_experiment(exp["label"])
            self.assertEqual(final["status"], "completed")
            self.assertEqual(final["meta"]["slurm_restart_count"], 1)

    async def test_no_result_completion_publishes_completed_slurm_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp)); idea = store.create_idea("x", [], "main"); store.save_idea(idea)
            exp = self._running(store, idea); runner = self._runner(store)
            evidence = {"state": "COMPLETED", "restart_count": 0, "exit_code": 0, "signal": 0}
            executor = _ExecutorSequence(["COMPLETED"], [evidence], [])
            executor.no_result = True
            ws = Mock(); ws.broadcaster = Mock(); ws.broadcaster.broadcast_soon = Mock()
            async def no_sleep(_): pass
            with patch("the_lab.runner.asyncio.sleep", no_sleep):
                await runner._monitor_slurm_job_inner(exp["id"], executor, "42", exp["label"],
                                                      Path(tmp) / ".the_lab" / "experiments" / "1" / "1", ws)
            self.assertEqual(store.get_experiment(exp["label"])["meta"]["slurm_state"], "COMPLETED")


if __name__ == "__main__":
    unittest.main()
