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
