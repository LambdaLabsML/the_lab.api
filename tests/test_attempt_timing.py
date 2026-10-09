"""Restart/timing fix: attempt number + attempt_started_at are additive,
computed fields (Store._enrich_experiment), and started_at keeps its old
dispatch-time semantics for backwards compatibility."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from the_lab import deps
from the_lab.routes import experiments, overview
from the_lab.store import Store
from the_lab import ws as ws_mod


class AttemptFieldComputationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name))
        idea = self.store.create_idea("x", [], "main"); self.store.save_idea(idea)
        self.exp = self.store.create_experiment(idea["id"], "run")
        self.label = self.exp["label"]

    def test_fresh_experiment_is_attempt_one(self):
        exp = self.store.update_experiment(self.label, status="queued")
        self.assertEqual(exp["attempt"], 1)
        # No restart yet: best-available signal falls back to started_at,
        # which is itself still unset before dispatch.
        self.assertIsNone(exp["attempt_started_at"])

    def test_dispatch_sets_attempt_started_at_from_started_at(self):
        exp = self.store.update_experiment(self.label, status="running", started_at="2026-01-01T00:00:00+00:00")
        self.assertEqual(exp["attempt"], 1)
        self.assertEqual(exp["attempt_started_at"], "2026-01-01T00:00:00+00:00")
        # started_at itself is untouched — still the original dispatch time,
        # for backwards compatibility with existing readers.
        self.assertEqual(exp["started_at"], "2026-01-01T00:00:00+00:00")

    def test_restart_advances_attempt_and_attempt_started_at_without_moving_started_at(self):
        self.store.update_experiment(self.label, status="running", started_at="2026-01-01T00:00:00+00:00")
        exp = self.store.update_experiment(self.label, meta={
            "slurm_restart_count": 1, "slurm_last_restart_at": "2026-01-01T02:00:00+00:00",
        })
        self.assertEqual(exp["attempt"], 2)
        self.assertEqual(exp["attempt_started_at"], "2026-01-01T02:00:00+00:00")
        # started_at (original dispatch/queue-inclusive time) never moves —
        # this is the field every existing caller already reads.
        self.assertEqual(exp["started_at"], "2026-01-01T00:00:00+00:00")

    def test_second_restart_advances_to_attempt_three(self):
        self.store.update_experiment(self.label, status="running", started_at="2026-01-01T00:00:00+00:00")
        self.store.update_experiment(self.label, meta={
            "slurm_restart_count": 1, "slurm_last_restart_at": "2026-01-01T02:00:00+00:00",
        })
        exp = self.store.update_experiment(self.label, meta={
            "slurm_restart_count": 2, "slurm_last_restart_at": "2026-01-01T04:00:00+00:00",
        })
        self.assertEqual(exp["attempt"], 3)
        self.assertEqual(exp["attempt_started_at"], "2026-01-01T04:00:00+00:00")

    def test_reload_from_disk_recomputes_attempt_fields(self):
        self.store.update_experiment(self.label, status="running", started_at="2026-01-01T00:00:00+00:00",
                                     meta={"slurm_restart_count": 1, "slurm_last_restart_at": "2026-01-01T02:00:00+00:00"})
        reloaded = self.store.reload_experiment(self.label)
        self.assertEqual(reloaded["attempt"], 2)
        self.assertEqual(reloaded["attempt_started_at"], "2026-01-01T02:00:00+00:00")


class AttemptFieldExposureTests(unittest.TestCase):
    """The additive fields reach API responses and ws/chart-data payloads."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        self.store = Store(self.repo)
        idea = self.store.create_idea("x", [], "main"); self.store.save_idea(idea)
        self.idea_id = idea["id"]
        exp = self.store.create_experiment(idea["id"], "run")
        self.label = exp["label"]
        self.store.update_experiment(self.label, status="running", started_at="2026-01-01T00:00:00+00:00",
                                     meta={"slurm_restart_count": 1, "slurm_last_restart_at": "2026-01-01T02:00:00+00:00"})
        exp = self.store.get_experiment(self.label)
        progress_path = self.repo / exp["script"].replace(".sh", ".progress")
        progress_path.parent.mkdir(parents=True, exist_ok=True)
        progress_path.write_text('{"step": 1}')
        for p in (patch.object(experiments, "store", self.store), patch.object(experiments, "REPO_DIR", self.repo),
                  patch.object(deps, "store", self.store), patch.object(deps, "REPO_DIR", self.repo),
                  patch.object(overview, "store", self.store), patch.object(overview, "REPO_DIR", self.repo)):
            p.start(); self.addCleanup(p.stop)
        overview._chart_data_last = None
        app = FastAPI(); app.include_router(experiments.router); app.include_router(overview.router)
        self.client = TestClient(app)

    def test_get_experiment_exposes_attempt_fields(self):
        resp = self.client.get(f"/api/v1/experiments/{self.label}")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["attempt"], 2)
        self.assertEqual(body["attempt_started_at"], "2026-01-01T02:00:00+00:00")
        # Existing field kept exactly as before.
        self.assertEqual(body["started_at"], "2026-01-01T00:00:00+00:00")

    def test_chart_data_includes_attempt_fields(self):
        resp = self.client.get("/api/v1/chart-data")
        self.assertEqual(resp.status_code, 200)
        [running] = resp.json()["running"]
        self.assertEqual(running["attempt"], 2)
        self.assertEqual(running["attempt_started_at"], "2026-01-01T02:00:00+00:00")

    def test_ws_experiment_payload_includes_attempt_fields(self):
        exp = self.store.get_experiment(self.label)
        payload = ws_mod.experiment_payload(exp)
        self.assertEqual(payload["attempt"], 2)
        self.assertEqual(payload["attempt_started_at"], "2026-01-01T02:00:00+00:00")


if __name__ == "__main__":
    unittest.main()


class RunPctTests(unittest.TestCase):
    """run_pct: wall-clock progress next to the solver's games-picked pct."""

    def test_from_budget_and_max_wall(self):
        from the_lab.deps import with_run_pct
        src = {"pct_complete": 100.0, "budget_remaining_s": 4500}
        out = with_run_pct(src, {"MAX_WALL_SECONDS": "9000"})
        self.assertEqual(out["run_pct"], 50.0)
        self.assertNotIn("run_pct", src)           # input (often cached) untouched
        self.assertEqual(with_run_pct({"budget_remaining_s": -5}, {"MAX_WALL_SECONDS": 9000})["run_pct"], 100.0)

    def test_missing_inputs_leave_progress_alone(self):
        from the_lab.deps import with_run_pct
        self.assertNotIn("run_pct", with_run_pct({"pct_complete": 40}, {"MAX_WALL_SECONDS": 9000}))
        self.assertNotIn("run_pct", with_run_pct({"budget_remaining_s": 10}, {}))
        self.assertNotIn("run_pct", with_run_pct({"budget_remaining_s": 10}, {"MAX_WALL_SECONDS": "x"}))
