import asyncio
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from the_lab.blocking import run_blocking
from the_lab.store import Store


class BlockingTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_write_keeps_loop_responsive_and_is_durable_on_return(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            idea = store.create_idea("test", [], "main")
            store.save_idea(idea)
            from the_lab import store as module
            original = module._write_json
            entered = threading.Event()

            def slow_write(path, data):
                entered.set()
                time.sleep(.15)
                original(path, data)

            with patch.object(module, "_write_json", slow_write):
                task = asyncio.create_task(run_blocking(store.update_idea, idea["id"], description="saved"))
                for _ in range(100):
                    if entered.is_set(): break
                    await asyncio.sleep(.005)
                self.assertTrue(entered.is_set())
                self.assertFalse(task.done())
                ticks = 0
                while not task.done():
                    await asyncio.sleep(.01)
                    ticks += 1
                await task
                self.assertGreater(ticks, 5)
            restored = Store(Path(directory))
            self.assertEqual(restored.get_idea(idea["id"])["description"], "saved")

    async def test_worker_concurrency_is_bounded(self):
        active = peak = 0
        lock = threading.Lock()

        def work():
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(.03)
            with lock: active -= 1

        with patch.dict(os.environ, {"THE_LAB_IO_WORKERS": "2"}):
            await asyncio.gather(*(run_blocking(work) for _ in range(12)))
        self.assertEqual(peak, 2)

    async def test_worker_exceptions_reach_caller(self):
        def fail(): raise OSError("disk unavailable")
        with self.assertRaisesRegex(OSError, "disk unavailable"):
            await run_blocking(fail)
