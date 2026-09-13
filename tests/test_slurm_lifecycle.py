import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from the_lab.runner import ExperimentRunner
from the_lab.store import Store


class _Broadcaster:
    def __init__(self):
        self.events = []

    def broadcast_soon(self, event):
        self.events.append(event)


class _WS:
    def __init__(self):
        self.broadcaster = _Broadcaster()


class _Executor:
    def __init__(self, states, *, exit_status=None, accounting=None, pull_error=False):
        self.states = iter(states)
        self.exit_status = exit_status
        self.accounting = accounting
        self.pull_error = pull_error
        self.pulls = self.cleanups = 0

    def poll_status(self, _job_id):
        item = next(self.states)
        if isinstance(item, Exception):
            raise item
        return item

    def get_exit_status(self, _label, _job_id=None):
        return self.exit_status

    def accounting_status(self, _job_id):
        value = self.accounting() if callable(self.accounting) else self.accounting
        return value

    def pull_results(self, _label, _dir):
        self.pulls += 1
        if self.pull_error and self.pulls == 1:
            raise RuntimeError("rsync unavailable")

    def cleanup_remote(self, *_args, **_kwargs):
        self.cleanups += 1


class SlurmLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.td.name))
        idea = self.store.create_idea("x", [], "main")
        self.store.save_idea(idea)
        self.exp = self.store.create_experiment(idea["id"], "x")
        self.store.update_experiment(
            self.exp["id"], status="running",
            meta={"slurm_job_id": "42", "assigned_resource": "slurm"},
        )
        self.runner = ExperimentRunner(self.store)
        self.local_dir = Path(self.td.name) / self.exp["script"]
        self.local_dir.parent.mkdir(parents=True, exist_ok=True)
        self.local_dir.parent.joinpath("script.log").write_text("")
        self.ws = _WS()

    def tearDown(self):
        self.td.cleanup()

    async def _monitor(self, executor):
        async def no_sleep(_seconds):
            return None
        with patch("the_lab.runner.asyncio.sleep", no_sleep):
            await self.runner._monitor_slurm_job_inner(
                self.exp["id"], executor, "42", self.exp["label"],
                self.local_dir.parent, self.ws,
            )

    async def test_poll_failure_retries_and_exit_127_never_completes(self):
        executor = _Executor(
            [RuntimeError("ssh down"), ""],
            exit_status={"job_id": "42", "exit_code": 127, "preempted": False},
            accounting=None,
        )
        await self._monitor(executor)
        got = self.store.get_experiment(self.exp["id"])
        self.assertEqual(got["status"], "failed")
        self.assertIn("127", got["error"])
        self.assertEqual(executor.cleanups, 1)

    async def test_accounting_cancelled_suffix_is_terminal(self):
        executor = _Executor(
            [""], exit_status=None,
            accounting={"job_id": "42", "state": "CANCELLED by 1008", "exit_code": 0},
        )
        await self._monitor(executor)
        self.assertEqual(self.store.get_experiment(self.exp["id"])["status"], "failed")

    async def test_old_attempt_cannot_release_new_attempt_allocator(self):
        self.store.update_experiment(self.exp["id"], meta={"slurm_job_id": "43"})
        self.runner._allocator.release = MagicMock()
        executor = _Executor([""], exit_status={"job_id": "42", "exit_code": 0, "preempted": False})
        await self.runner._finalize_slurm_terminal(
            self.exp["id"], executor, "42", self.exp["label"], self.local_dir.parent,
            self.ws, "COMPLETED", 0,
        )
        self.runner._allocator.release.assert_not_called()
        self.assertEqual(executor.cleanups, 0)

    async def test_attempt_change_during_log_read_cannot_clean_or_release(self):
        self.runner._allocator.release = MagicMock()

        async def replace_attempt(_path):
            self.store.update_experiment(self.exp["id"], meta={"slurm_job_id": "43"})
            return ""

        self.runner._read_text_off_loop = replace_attempt
        executor = _Executor([""], exit_status={"job_id": "42", "exit_code": 0, "preempted": False})
        await self.runner._finalize_slurm_terminal(
            self.exp["id"], executor, "42", self.exp["label"], self.local_dir.parent,
            self.ws, "COMPLETED", 0,
        )
        self.assertEqual(self.store.get_experiment(self.exp["id"])["meta"]["slurm_job_id"], "43")
        self.runner._allocator.release.assert_not_called()
        self.assertEqual(executor.cleanups, 0)

    async def test_metrics_win_over_warning_text_after_verified_zero_exit(self):
        (self.local_dir.parent / "script.log").write_text(
            'warning: command not found in optional probe\n{"metrics":{"score": 9}}\n')
        executor = _Executor([""], exit_status={"job_id": "42", "exit_code": 0, "preempted": False})
        await self.runner._finalize_slurm_terminal(
            self.exp["id"], executor, "42", self.exp["label"], self.local_dir.parent,
            self.ws, "COMPLETED", 0,
        )
        got = self.store.get_experiment(self.exp["id"])
        self.assertEqual(got["status"], "completed")
        self.assertEqual(got["metrics"]["score"], 9)

    async def test_empty_queue_waits_for_delayed_accounting(self):
        answers = iter([None, {"job_id": "42", "state": "COMPLETED", "exit_code": 0}])
        executor = _Executor(
            ["", ""],
            exit_status=None,
            accounting=lambda: next(answers),
        )
        await self._monitor(executor)
        self.assertEqual(self.store.get_experiment(self.exp["id"])["status"], "completed")
        self.assertEqual(executor.cleanups, 1)

    async def test_transfer_failure_keeps_monitor_and_remote_until_retry(self):
        executor = _Executor(
            ["", ""],
            exit_status={"job_id": "42", "exit_code": 0, "preempted": False},
            accounting={"job_id": "42", "state": "COMPLETED", "exit_code": 0},
            pull_error=True,
        )
        await self._monitor(executor)
        got = self.store.get_experiment(self.exp["id"])
        self.assertEqual(got["status"], "completed")
        self.assertEqual(executor.pulls, 2)
        self.assertEqual(executor.cleanups, 1)
        self.assertNotIn("slurm_sync_error", got["meta"])

    async def test_callback_race_cancel_cannot_be_resurrected(self):
        executor = _Executor([""], exit_status={"job_id": "42", "exit_code": 0, "preempted": False})
        self.store.update_experiment(self.exp["id"], status="cancelled")
        await self.runner._finalize_slurm_terminal(
            self.exp["id"], executor, "42", self.exp["label"], self.local_dir.parent,
            self.ws, "COMPLETED", 0,
        )
        self.assertEqual(self.store.get_experiment(self.exp["id"])["status"], "cancelled")
        self.assertEqual(executor.cleanups, 0)

    async def test_structured_progress_is_preserved_and_uses_idea_id(self):
        progress = self.local_dir.parent / "script.progress"
        progress.write_text('{"pct_complete": 23, "source": "script"}')
        (self.local_dir.parent / "script.log").write_text("[  1] move=LEFT  score=3  restarts=0 latency=2ms  t_remaining=1s\n")
        await self.runner._publish_live_slurm_update(
            self.exp["id"], self.exp["label"], self.local_dir.parent, self.ws)
        self.assertIn('"source": "script"', progress.read_text())
        self.assertTrue(self.ws.broadcaster.events)
        self.assertTrue(all(e["idea_id"] == self.exp["idea_id"] for e in self.ws.broadcaster.events))
