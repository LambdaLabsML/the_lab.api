import asyncio
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from the_lab import deps
from the_lab.routes import experiments
from the_lab.runner import ExperimentRunner
from the_lab.store import Store


class _Runner:
    def wake_scheduler(self):
        pass


class ExperimentRouteTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo_dir = Path(self.temp_dir.name)
        (self.repo_dir / ".git" / "info").mkdir(parents=True)
        self.store = Store(self.repo_dir)
        idea = self.store.create_idea("test idea", [], "idea/1")
        self.store.save_idea(idea)

        self.original_deps = experiments.store, experiments.runner, experiments.REPO_DIR, deps.store
        experiments.store = self.store
        experiments.runner = _Runner()
        experiments.REPO_DIR = self.repo_dir
        deps.store = self.store

        app = FastAPI()
        app.include_router(experiments.router)
        self.client = TestClient(app)

    def tearDown(self):
        experiments.store, experiments.runner, experiments.REPO_DIR, deps.store = self.original_deps
        self.temp_dir.cleanup()

    def test_create_rejects_missing_or_blank_script_without_creating_record(self):
        cases = [
            ({"description": "missing script"}, 422),
            ({"description": "empty script", "script_content": ""}, 400),
            ({"description": "blank script", "script_content": " \n"}, 400),
        ]
        for payload, status_code in cases:
            with self.subTest(payload=payload):
                response = self.client.post("/api/v1/ideas/1/experiments", json=payload)
                self.assertEqual(status_code, response.status_code)
                self.assertEqual([], self.store.list_all_experiments())

    def test_batch_create_rejects_missing_or_blank_script_without_creating_records(self):
        for script_content in (None, "", " \n"):
            body = {"metas": [{"description": "invalid script"}]}
            if script_content is not None:
                body["shared_script_content"] = script_content
            with self.subTest(script_content=script_content):
                response = self.client.post("/api/v1/ideas/1/experiments/batch", json=body)
                self.assertEqual(400, response.status_code)
                self.assertEqual([], self.store.list_all_experiments())

    def test_rerun_rejects_blank_script_without_creating_record(self):
        source = self.store.create_experiment(1, "blank script")
        script_path = self.repo_dir / source["script"]
        script_path.parent.mkdir(parents=True, exist_ok=True)
        script_path.write_text(" \n")

        response = self.client.post(f"/api/v1/experiments/{source['label']}/rerun")

        self.assertEqual(400, response.status_code)
        labels = [exp["label"] for exp in self.store.list_all_experiments()]
        self.assertEqual([source["label"]], labels)


class ExperimentRunnerTest(unittest.TestCase):
    def test_start_marks_missing_script_failed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_dir = Path(temp_dir)
            (repo_dir / ".git" / "info").mkdir(parents=True)
            store = Store(repo_dir)
            exp = store.create_experiment(1, "missing script")
            store.update_experiment(exp["label"], status="queued")
            runner = ExperimentRunner(store)

            result = asyncio.run(runner.start(exp["label"]))

            saved = store.get_experiment(exp["label"])
            self.assertEqual("error", result["status"])
            self.assertEqual("failed", saved["status"])
            self.assertIn("script not found", saved["error"])


if __name__ == "__main__":
    unittest.main()
