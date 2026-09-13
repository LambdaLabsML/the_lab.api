import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from the_lab.runner import ExperimentRunner
from the_lab.store import Store
from the_lab.ws import Broadcaster


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_subscribe_before_replay_keeps_intervening_event(self):
        b = Broadcaster()
        b.capture_loop()
        b.broadcast({"type": "one"})
        q = b.subscribe()
        replay = b.replay_since(0)
        b.broadcast({"type": "two"})
        seen = {x["seq"] for x in replay}
        queued = await q.get()
        seen.add(queued["seq"])
        self.assertEqual(seen, {1, 2})

    async def test_overflow_emits_resync(self):
        b = Broadcaster()
        b.capture_loop()
        q = b.subscribe()
        for i in range(b._QUEUE_MAX + 5):
            b.broadcast({"type": "event", "i": i})
        frames = []
        while not q.empty():
            frames.append(q.get_nowait())
        self.assertTrue(any(x.get("type") == "resync" and x.get("reason") == "overflow" for x in frames))

    async def test_cancelled_state_cannot_be_completed_by_running_cas(self):
        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td))
            idea = store.create_idea("x", [], "main")
            store.save_idea(idea)
            exp = store.create_experiment(idea["id"], "x")
            store.update_experiment(exp["id"], status="running")
            store.update_experiment(exp["id"], status="cancelled")
            self.assertIsNone(store.transition_experiment(exp["id"], {"running"}, status="completed"))
            self.assertEqual(store.get_experiment(exp["id"])["status"], "cancelled")

    async def test_cancel_during_start_setup_never_launches_child(self):
        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td))
            idea = store.create_idea("x", [], "main")
            store.save_idea(idea)
            exp = store.create_experiment(idea["id"], "x")
            script = Path(td) / exp["script"]
            script.parent.mkdir(parents=True, exist_ok=True)
            script.write_text("#!/bin/sh\nexit 0\n")
            runner = ExperimentRunner(store)
            entered = asyncio.Event()
            release = asyncio.Event()

            async def slow_read(_path):
                entered.set()
                await release.wait()
                return ""

            with patch.object(runner, "_read_text_off_loop", slow_read), \
                 patch("the_lab.runner.asyncio.create_subprocess_exec", new=AsyncMock()) as spawn, \
                 patch("the_lab.runner.load_sandbox_config", return_value={}), \
                 patch("the_lab.runner.gpu_host_state", return_value={}):
                task = asyncio.create_task(runner.start(exp["id"]))
                await entered.wait()
                await runner.cancel(exp["id"])
                release.set()
                result = await task
            self.assertEqual(result["status"], "error")
            self.assertEqual(store.get_experiment(exp["id"])["status"], "cancelled")
            spawn.assert_not_awaited()

    async def test_launch_exception_leaves_failed_not_starting(self):
        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td))
            idea = store.create_idea("x", [], "main")
            store.save_idea(idea)
            exp = store.create_experiment(idea["id"], "x")
            script = Path(td) / exp["script"]
            script.parent.mkdir(parents=True, exist_ok=True)
            script.write_text("#!/bin/sh\nexit 0\n")
            runner = ExperimentRunner(store)
            with patch("the_lab.runner.asyncio.create_subprocess_exec", new=AsyncMock(side_effect=OSError("no spawn"))), \
                 patch("the_lab.runner.load_sandbox_config", return_value={}), \
                 patch("the_lab.runner.gpu_host_state", return_value={}):
                result = await runner.start(exp["id"])
            self.assertEqual(result["status"], "error")
            self.assertEqual(store.get_experiment(exp["id"])["status"], "failed")

    async def test_setup_fault_before_spawn_leaves_failed(self):
        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td))
            idea = store.create_idea("x", [], "main")
            store.save_idea(idea)
            exp = store.create_experiment(idea["id"], "x")
            script = Path(td) / exp["script"]
            script.parent.mkdir(parents=True, exist_ok=True)
            script.write_text("#!/bin/sh\nexit 0\n")
            runner = ExperimentRunner(store)
            with patch("the_lab.runner.load_sandbox_config", side_effect=OSError("sandbox config denied")), \
                 patch("the_lab.runner.asyncio.create_subprocess_exec", new=AsyncMock()) as spawn:
                result = await runner.start(exp["id"])
            self.assertEqual(result["status"], "error")
            self.assertEqual(store.get_experiment(exp["id"])["status"], "failed")
            spawn.assert_not_awaited()

    async def test_restart_reconciles_incomplete_start_without_relaunch(self):
        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td))
            idea = store.create_idea("x", [], "main")
            store.save_idea(idea)
            exp = store.create_experiment(idea["id"], "x")
            store.update_experiment(exp["id"], status="starting")
            ExperimentRunner(store)
            restored = store.get_experiment(exp["id"])
            self.assertEqual(restored["status"], "failed")
            self.assertIn("launch outcome unknown", restored["status_msg"])
