"""Saved filters API and filter= / where= on the read endpoints."""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from the_lab import deps
from the_lab.routes import experiments, filters as filter_routes, overview
from the_lab.store import Store

DASH = {"X-The-Lab-Source": "dashboard"}


class FilterRoutesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.repo, check=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
                        "--allow-empty", "-m", "init"], cwd=self.repo, check=True)
        self.store = Store(self.repo)
        idea = self.store.create_idea("base", [], "main"); idea["branch"] = "main"; self.store.save_idea(idea)
        child = self.store.create_idea("child", [idea["id"]], "main"); child["branch"] = "main"; self.store.save_idea(child)
        self.idea, self.child = idea["id"], child["id"]
        self.labels = {}
        for name, idea_id, wall, score, tags in (("short", self.idea, 1800, 10.0, ["smoke"]),
                                                 ("long_a", self.idea, 9000, 30.0, []),
                                                 ("long_b", self.child, 10800, 20.0, [])):
            exp = self.store.create_experiment(idea_id, name, meta={"MAX_WALL_SECONDS": wall}, tags=tags)
            self.store.update_experiment(exp["id"], status="completed", metrics={"score": score},
                                         finished_at="2026-10-16T00:00:00+00:00")
            self.labels[name] = self.store.get_experiment(exp["id"])["label"]
        for mod in (deps, overview, experiments, filter_routes):
            for attr, val in (("store", self.store), ("REPO_DIR", self.repo)):
                if hasattr(mod, attr):
                    p = patch.object(mod, attr, val); p.start(); self.addCleanup(p.stop)
        app = FastAPI()
        for r in (filter_routes.router, overview.router, experiments.router):
            app.include_router(r)
        self.client = TestClient(app)

    def _create(self, **body):
        body.setdefault("as_user", "david")
        return self.client.post("/api/v1/filters", json=body, headers=DASH)

    def board_labels(self, **params):
        r = self.client.get("/api/v1/leaderboard", params={"metric": "score", "top": 10, **params})
        self.assertEqual(r.status_code, 200, r.text)
        return sorted(e["experiment_label"] if "experiment_label" in e else e["label"]
                      for e in r.json()["leaderboard"])

    def test_only_humans_create_filters(self):
        r = self.client.post("/api/v1/filters", json={"name": "long", "expr": ""}, headers={"X-Agent-Id": "a1"})
        self.assertEqual(r.status_code, 403)
        r = self.client.post("/api/v1/filters", json={"name": "long", "expr": ""})  # no dashboard header
        self.assertEqual(r.status_code, 403)
        r = self._create(name="long", expr="meta.MAX_WALL_SECONDS >= 9000", shared=True)
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(r.json()["owner"], "user:david")
        self.assertEqual([f["name"] for f in self.client.get("/api/v1/filters").json()["filters"]], ["long"])

    def test_leaderboard_by_saved_filter_and_where(self):
        self._create(name="long", expr="meta.MAX_WALL_SECONDS >= 9000", shared=True)
        everything = self.board_labels()
        self.assertEqual(len(everything), 3)
        self.assertEqual(self.board_labels(filter="long"), sorted([self.labels["long_a"], self.labels["long_b"]]))
        self.assertEqual(self.board_labels(where="tag = smoke"), [self.labels["short"]])
        self.assertEqual(self.board_labels(filter="long", where=f"idea under {self.child}"), [self.labels["long_b"]])
        self.assertEqual(self.board_labels(where="@long and metric.score > 25"), [self.labels["long_a"]])

    def test_bad_where_is_400_and_unknown_filter_404(self):
        r = self.client.get("/api/v1/leaderboard", params={"metric": "score", "where": "meta.X >="})
        self.assertEqual(r.status_code, 400)
        self.assertIn("filter:", r.json()["detail"])
        r = self.client.get("/api/v1/leaderboard", params={"metric": "score", "filter": "nope"})
        self.assertEqual(r.status_code, 404)

    def test_check_endpoint_validates_and_counts(self):
        ok = self.client.get("/api/v1/filters/check", params={"where": "meta.MAX_WALL_SECONDS >= 9000"}).json()
        self.assertTrue(ok["ok"]); self.assertEqual((ok["matched"], ok["total"]), (2, 3))
        bad = self.client.get("/api/v1/filters/check", params={"where": "status ="}).json()
        self.assertFalse(bad["ok"]); self.assertIn("position", bad["error"])

    def test_owner_edits_others_copy_shared_exclusions_by_any_human(self):
        f = self._create(name="long", expr="meta.MAX_WALL_SECONDS >= 9000", shared=True).json()
        r = self.client.patch(f"/api/v1/filters/{f['id']}", json={"expr": "", "as_user": "alice"}, headers=DASH)
        self.assertEqual(r.status_code, 403)
        r = self.client.patch(f"/api/v1/filters/{f['id']}", json={"add_exclude": [self.labels["long_a"]], "as_user": "alice"},
                              headers=DASH)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.board_labels(filter="long"), [self.labels["long_b"]])
        c = self.client.post(f"/api/v1/filters/{f['id']}/copy", json={"name": "my long", "as_user": "alice"}, headers=DASH)
        self.assertEqual(c.status_code, 201, c.text)
        self.assertEqual((c.json()["owner"], c.json()["copied_from"], c.json()["exclude"]),
                         ("user:alice", f["id"], [self.labels["long_a"]]))
        r = self.client.patch(f"/api/v1/filters/{c.json()['id']}", json={"expr": "tag = smoke", "as_user": "alice"}, headers=DASH)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.delete(f"/api/v1/filters/{f['id']}", params={"as_user": "bob"}, headers=DASH).status_code, 200)
        self.assertEqual(self.client.get("/api/v1/filters/long").status_code, 404)

    def test_experiments_list_and_match(self):
        r = self.client.get("/api/v1/experiments", params={"where": "meta.MAX_WALL_SECONDS < 9000"})
        self.assertEqual([e["label"] for e in r.json()["experiments"]], [self.labels["short"]])
        m = self.client.get("/api/v1/filters/match", params={"where": "metric.score >= 20"}).json()
        self.assertEqual(sorted(m["labels"]), sorted([self.labels["long_a"], self.labels["long_b"]]))

    def test_vocabulary_lists_keys_values_tags(self):
        self._create(name="long", expr="meta.MAX_WALL_SECONDS >= 9000")
        v = self.client.get("/api/v1/filters/vocabulary").json()
        meta = {m["key"]: m for m in v["meta"]}
        self.assertEqual(meta["MAX_WALL_SECONDS"]["count"], 3)
        self.assertEqual(sorted(meta["MAX_WALL_SECONDS"]["values"]), ["10800", "1800", "9000"])
        self.assertEqual([m["key"] for m in v["metrics"]], ["score"])
        self.assertEqual(v["tags"], [{"tag": "smoke", "count": 1}])
        self.assertEqual(v["statuses"], ["completed"])
        self.assertEqual(v["saved"], ["long"])


if __name__ == "__main__":
    unittest.main()
