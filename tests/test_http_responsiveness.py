"""Exercise the real middleware/route stack against an isolated empty lab."""
import asyncio
import importlib
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import httpx


class HttpResponsivenessTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        with patch.dict(os.environ, {"THE_LAB_REPO": cls.directory.name, "THE_LAB_DEMO": "0",
                                    "THE_LAB_USER": "", "THE_LAB_PASSWORD": ""}):
            cls.module = importlib.import_module("the_lab.app")
        idea = cls.module.store.create_idea("test", [], "main")
        cls.module.store.save_idea(idea)
        cls.idea_id = idea["id"]

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    async def test_health_responds_during_slow_experiment_creation(self):
        from the_lab import store as store_module
        original = store_module._write_json
        entered = threading.Event()

        def slow_write(path, data):
            entered.set()
            time.sleep(.2)
            original(path, data)

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.module.app),
                                    base_url="http://test") as client:
            with patch.object(store_module, "_write_json", slow_write):
                task = asyncio.create_task(client.post(
                    f"/api/v1/ideas/{self.idea_id}/experiments",
                    json={"description": "slow write", "script_content": "echo test"},
                ))
                for _ in range(200):
                    if entered.is_set(): break
                    await asyncio.sleep(.005)
                self.assertTrue(entered.is_set())
                health = await asyncio.wait_for(client.get("/api/v1/health"), timeout=.15)
                self.assertEqual(health.status_code, 200)
                self.assertFalse(task.done())
                response = await task
                self.assertEqual(response.status_code, 201, response.text)
                self.assertEqual(response.json()["status"], "queued")

    async def test_chart_projection_history_and_live_cache_invalidation(self):
        from the_lab.routes import overview
        from the_lab import cache, jsonio
        from pathlib import Path
        store = self.module.store
        completed = []
        for score in range(3):
            exp = store.create_experiment(self.idea_id, "long description " * 100)
            store.update_experiment(exp["id"], status="completed", metrics={"score": score, "other": 99},
                                    finished_at=f"2026-01-0{score+1}T00:00:00Z")
            completed.append(exp["id"])
        live = store.create_experiment(self.idea_id, "live")
        store.update_experiment(live["id"], status="running")
        progress_path = Path(self.directory.name) / live["script"].replace(".sh", ".progress")
        jsonio.write_json(progress_path, {"score": 4, "pct_complete": 10})
        cache.clear()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.module.app), base_url="http://test",
                                    headers={"X-The-Lab-Source": "dashboard"}) as client:
            with patch.object(overview, "_chart_rows", wraps=overview._chart_rows) as build:
                first = (await client.get("/api/v1/chart-data?limit=2&metrics=score&compact=true")).json()
                self.assertEqual(first["history"], {"loaded": 2, "total": 3, "has_more": True})
                self.assertEqual(first["experiments"][0]["metrics"], {"score": 2})
                self.assertLessEqual(len(first["experiments"][0]["description"]), 160)
                self.assertIn("other", first["metric_keys"])
                jsonio.write_json(progress_path, {"score": 5, "pct_complete": 20})
                store.update_experiment(live["id"], metrics={"score": 5})
                second = (await client.get("/api/v1/chart-data?metrics=score")).json()
                self.assertEqual(len(second["experiments"]), 3)
                self.assertEqual(second["running"][0]["metrics"], {"score": 5})
                completed_builds = [call for call in build.call_args_list if call.args[0] and call.args[0][0]["status"] == "completed"]
                self.assertEqual(len(completed_builds), 1)
                store.update_experiment(completed[-1], metrics={"score": 8})
                third = (await client.get("/api/v1/chart-data?metrics=score")).json()
                self.assertEqual(third["experiments"][0]["metrics"], {"score": 8})

    async def test_dashboard_does_not_build_notifications(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.module.app), base_url="http://test") as client:
            with patch.object(self.module, "build_notifications", side_effect=AssertionError("unnecessary work")):
                response = await client.get("/api/v1/ideas", headers={"X-The-Lab-Source": "dashboard"})
                self.assertEqual(response.status_code, 200)
