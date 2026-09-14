"""Route-level fencing regressions for native Slurm callbacks."""
from __future__ import annotations

from contextlib import ExitStack, asynccontextmanager
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from the_lab.routes import experiments
from the_lab.schemas import RequeueRequest, SlurmDoneRequest
from the_lab.store import Store


class _Request:
    def __init__(self, body: dict, headers: dict[str, str] | None = None, before_return=None):
        self._body = body
        self.headers = headers or {}
        self._before_return = before_return

    async def json(self):
        if self._before_return:
            self._before_return()
        return self._body


class _Runner:
    def __init__(self):
        self.wake_count = 0

    @asynccontextmanager
    async def label_lock(self, _label):
        yield

    def wake_scheduler(self):
        self.wake_count += 1


class SlurmCallbackRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root)
        idea = self.store.create_idea("callback test", [], "main")
        self.store.save_idea(idea)
        self.idea_id = idea["id"]
        self.runner = _Runner()
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(patch.object(experiments, "store", self.store))
        self.patches.enter_context(patch.object(experiments, "runner", self.runner))
        self.patches.enter_context(patch.object(experiments, "REPO_DIR", self.root))
        self.patches.enter_context(patch.object(
            experiments, "_resolve_exp", lambda ref: self.store.get_experiment(ref)))

    def _experiment(self, *, meta: dict | None = None, status: str = "running") -> dict:
        exp = self.store.create_experiment(self.idea_id, "run", meta=meta or {})
        self.store.update_experiment(exp["label"], status=status)
        return self.store.get_experiment(exp["label"])

    @staticmethod
    def _headers(generation: int) -> dict[str, str]:
        return {
            "X-The-Lab-Slurm-Job-ID": "42",
            "X-The-Lab-Restart-Count": str(generation),
        }

    async def test_native_stale_generation_progress_is_rejected_without_writing(self):
        exp = self._experiment(meta={
            "slurm_native_requeue": True, "slurm_job_id": "42", "slurm_restart_count": 1,
        })
        progress = self.root / exp["script"].replace(".sh", ".progress")
        progress.parent.mkdir(parents=True, exist_ok=True)
        progress.write_text('{"old": true}')

        with self.assertRaisesRegex(HTTPException, "stale Slurm progress generation") as raised:
            await experiments.post_experiment_progress(
                exp["label"], _Request({"step": 2}, self._headers(0)))
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(progress.read_text(), '{"old": true}')

    async def test_native_current_generation_progress_writes(self):
        exp = self._experiment(meta={
            "slurm_native_requeue": True, "slurm_job_id": "42", "slurm_restart_count": 1,
        })
        result = await experiments.post_experiment_progress(
            exp["label"], _Request({"step": 2}, self._headers(1)))
        progress = self.root / exp["script"].replace(".sh", ".progress")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(json.loads(progress.read_text()), {"step": 2})

    async def test_progress_rechecks_generation_after_body_await(self):
        exp = self._experiment(meta={
            "slurm_native_requeue": True, "slurm_job_id": "42", "slurm_restart_count": 0,
        })
        progress = self.root / exp["script"].replace(".sh", ".progress")
        progress.parent.mkdir(parents=True, exist_ok=True)
        progress.write_text('{"old": true}')

        def replace_generation():
            current = self.store.get_experiment(exp["label"])
            self.store.update_experiment(exp["label"], meta={
                **current["meta"], "slurm_restart_count": 1,
            })

        with self.assertRaisesRegex(HTTPException, "stale Slurm progress generation") as raised:
            await experiments.post_experiment_progress(
                exp["label"], _Request({"step": 2}, self._headers(0), replace_generation))
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(progress.read_text(), '{"old": true}')

    async def test_progress_rejects_terminal_cancelled_experiment(self):
        exp = self._experiment(meta={
            "slurm_native_requeue": True, "slurm_job_id": "42", "slurm_restart_count": 1,
        }, status="cancelled")
        with self.assertRaisesRegex(HTTPException, "no longer running") as raised:
            await experiments.post_experiment_progress(
                exp["label"], _Request({"step": 2}, self._headers(1)))
        self.assertEqual(raised.exception.status_code, 409)

    async def test_native_slurm_done_nonzero_cannot_fail_live_record(self):
        exp = self._experiment(meta={
            "slurm_native_requeue": True, "slurm_job_id": "42", "slurm_restart_count": 1,
        })
        result = await experiments.slurm_done(exp["label"], SlurmDoneRequest(
            exit_code=9, job_id="42", restart_count=1,
        ))
        self.assertEqual(result["status"], "running")
        self.assertEqual(self.store.get_experiment(exp["label"])["status"], "running")
        self.assertEqual(self.runner.wake_count, 0)

    async def test_requeue_leaves_native_and_legacy_live_slurm_jobs_running(self):
        for meta in (
            {"slurm_native_requeue": True, "slurm_job_id": "42", "slurm_restart_count": 0},
            {"slurm_job_id": "42"},
        ):
            with self.subTest(meta=meta):
                exp = self._experiment(meta=meta)
                result = await experiments.requeue_experiment(
                    exp["label"], RequeueRequest(job_id="42", restart_count=0))
                self.assertEqual(result["status"], "running")
                self.assertEqual(self.store.get_experiment(exp["label"])["status"], "running")
                self.assertFalse(self.store.list_experiments_by_status("queued"))
        self.assertEqual(self.runner.wake_count, 0)


if __name__ == "__main__":
    unittest.main()
