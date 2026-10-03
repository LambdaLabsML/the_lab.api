"""Reloading experiments from disk: store methods, routes, and admin gating."""
import asyncio
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from the_lab import app as app_mod
from the_lab.routes import experiments, operational
from the_lab.store import Store


class _Runner:
    def label_lock(self, _label):
        return asyncio.Lock()


class ReloadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root)
        idea = self.store.create_idea("reload test", [], "main")
        self.store.save_idea(idea)
        self.idea_id = idea["id"]
        exp = self.store.create_experiment(self.idea_id, "run")
        self.label = exp["label"]
        self.store.update_experiment(self.label, status="completed", metrics={"score": 1.0})
        self.path = self.store._exp_json_path(self.idea_id, exp["seq"])
        patches = ExitStack()
        self.addCleanup(patches.close)
        patches.enter_context(patch.object(experiments, "store", self.store))
        patches.enter_context(patch.object(experiments, "runner", _Runner()))
        patches.enter_context(patch.object(operational, "store", self.store))

    def _edit(self, **fields):
        data = json.loads(self.path.read_text())
        data.update(fields)
        self.path.write_text(json.dumps(data))

    def test_hand_edit_invisible_until_reload(self):
        self._edit(metrics={"score": 0.5})
        self.assertEqual(self.store.get_experiment(self.label)["metrics"], {"score": 1.0})
        v = self.store.version
        exp = self.store.reload_experiment(self.label)
        self.assertEqual(exp["metrics"], {"score": 0.5})
        self.assertEqual(self.store.get_experiment(self.label)["metrics"], {"score": 0.5})
        self.assertGreater(self.store.version, v)

    def test_identity_comes_from_path_not_contents(self):
        self._edit(label="999.9", idea_id=999)
        exp = self.store.reload_experiment(self.label)
        self.assertEqual((exp["label"], exp["idea_id"]), (self.label, self.idea_id))

    def test_invalid_file_leaves_cache_untouched(self):
        self.path.write_text("{not json")
        with self.assertRaises(ValueError):
            self.store.reload_experiment(self.label)
        self.path.write_text("[1, 2]")
        with self.assertRaises(ValueError):
            self.store.reload_experiment(self.label)
        self.assertEqual(self.store.get_experiment(self.label)["metrics"], {"score": 1.0})

    def test_missing_or_malformed_label(self):
        for ref in ("nope", f"{self.idea_id}.99"):
            with self.assertRaises(KeyError):
                self.store.reload_experiment(ref)

    def test_reload_all_adds_removes_and_keeps_active(self):
        running = self.store.create_experiment(self.idea_id, "live")
        self.store.update_experiment(running["label"], status="running")
        # Add a new experiment file by hand, delete the running one's file.
        new_dir = self.path.parent.parent / "7"
        new_dir.mkdir()
        (new_dir / "experiment.json").write_text(json.dumps({"status": "completed", "metrics": {"score": 3}}))
        self.store._exp_json_path(self.idea_id, running["seq"]).unlink()
        next_idea = self.store._next_idea_id

        result = self.store.reload_all()
        new_label = f"{self.idea_id}.7"
        self.assertEqual(result["added"], [new_label])
        self.assertEqual(result["removed"], [])
        self.assertEqual(result["kept_active_missing_on_disk"], [running["label"]])
        self.assertEqual(self.store.get_experiment(new_label)["metrics"], {"score": 3})
        self.assertIn(new_label, {e["label"] for e in self.store.list_experiments(self.idea_id)})
        self.assertEqual(self.store._next_idea_id, next_idea)

    def test_reload_all_invalid_file_leaves_state(self):
        self.path.write_text("{broken")
        with self.assertRaises(ValueError):
            self.store.reload_all()
        self.assertEqual(self.store.get_experiment(self.label)["metrics"], {"score": 1.0})

    async def test_routes(self):
        self._edit(metrics={"score": 0.25})
        with patch("the_lab.ws.broadcaster.broadcast_soon") as bc:
            exp = await experiments.reload_experiment(self.label)
        self.assertEqual(exp["metrics"], {"score": 0.25})
        self.assertEqual(bc.call_args[0][0]["type"], "experiment_reloaded")

        with self.assertRaises(HTTPException) as cm:
            await experiments.reload_experiment(f"{self.idea_id}.99")
        self.assertEqual(cm.exception.status_code, 404)
        self.path.write_text("{bad")
        with self.assertRaises(HTTPException) as cm:
            await experiments.reload_experiment(self.label)
        self.assertEqual(cm.exception.status_code, 422)
        with self.assertRaises(HTTPException) as cm:
            operational.reload_from_disk()
        self.assertEqual(cm.exception.status_code, 422)

    def test_reload_routes_are_admin_only(self):
        self.assertTrue(app_mod._is_admin_only("POST", "/api/v1/reload"))
        self.assertTrue(app_mod._is_admin_only("POST", "/api/v1/experiments/12.3/reload"))
        # Neighbouring routes keep their existing classification.
        self.assertFalse(app_mod._is_admin_only("POST", "/api/v1/experiments/12.3/pull"))
        self.assertTrue(app_mod._is_admin_only("PUT", "/api/v1/prompts/worker"))
        self.assertFalse(app_mod._is_admin_only("GET", "/api/v1/prompts/worker"))


if __name__ == "__main__":
    unittest.main()
