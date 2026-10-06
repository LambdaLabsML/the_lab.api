"""Hot-endpoint performance fixes: HEAD-file branch reads, incremental cost
parsing, and conditional GET for /chart-data."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from the_lab import git_ops
from the_lab.routes import agents as agent_routes
from the_lab.routes import overview
from the_lab.store import Store


class ReadHeadBranchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        run = lambda *a, cwd=self.repo: subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)
        run("init", "-q", "-b", "main")
        run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "i")
        run("branch", "idea/7")
        run("worktree", "add", "-q", str(self.repo / "wt"), "idea/7")
        self.run_git = run

    def test_matches_git_for_main_checkout_and_linked_worktree(self):
        for path in (self.repo, self.repo / "wt"):
            self.assertEqual(git_ops.read_head_branch(path), git_ops.get_current_branch(cwd=path))

    def test_detached_or_missing_returns_none(self):
        self.run_git("checkout", "-q", "--detach", cwd=self.repo / "wt")
        self.assertIsNone(git_ops.read_head_branch(self.repo / "wt"))
        self.assertIsNone(git_ops.read_head_branch(self.repo / "nope"))


class IncrementalCostParseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.file = Path(self.tmp.name) / "s.jsonl"
        agent_routes._session_parse_cache.clear()

    def line(self, mid, out, typ="assistant", model="claude-opus-5-5"):
        return json.dumps({"type": typ, "timestamp": "t", "message": {
            "id": mid, "model": model, "usage": {"output_tokens": out}}}) + "\n"

    def parse(self):
        return agent_routes._parse_session_file(self.file, self.file.stat())

    def test_appends_parse_only_the_tail_and_dedupe_blocks(self):
        # Two content blocks of message m1 repeat its usage: count once.
        self.file.write_text(self.line("m1", 10) + self.line("m1", 10))
        self.assertEqual(self.parse()["out"], 10)
        with self.file.open("a") as fh:
            fh.write(self.line("m2", 5))
        state = self.parse()
        self.assertEqual(state["out"], 15)
        self.assertEqual(state["offset"], self.file.stat().st_size)

    def test_cost_uses_each_messages_model_price(self):
        # 1M output tokens: Opus 5.5 $20 + Fable 5.1 $50; duplicate block not billed.
        self.file.write_text(self.line("m1", 1_000_000) + self.line("m1", 1_000_000)
                             + self.line("m2", 1_000_000, model="claude-fable-5-1"))
        self.assertAlmostEqual(self.parse()["cost"], 70.0)

    def test_partial_last_line_waits_for_newline(self):
        full = self.line("m1", 10)
        self.file.write_text(self.line("m0", 1) + full[:-10])
        self.assertEqual(self.parse()["out"], 1)
        with self.file.open("a") as fh:
            fh.write(full[-10:])
        self.assertEqual(self.parse()["out"], 11)

    def test_truncated_file_is_reparsed(self):
        self.file.write_text(self.line("m1", 10) + self.line("m2", 10))
        self.assertEqual(self.parse()["out"], 20)
        self.file.write_text(self.line("m9", 3))
        self.assertEqual(self.parse()["out"], 3)


class ChartDataConditionalGetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        self.store = Store(self.repo)
        idea = self.store.create_idea("x", [], "main"); self.store.save_idea(idea)
        self.label = self.store.create_experiment(idea["id"], "run")["label"]
        self.store.update_experiment(self.label, status="completed", metrics={"score": 1.0})
        for p in (patch.object(overview, "store", self.store), patch.object(overview, "REPO_DIR", self.repo),
                  patch("the_lab.deps.REPO_DIR", self.repo)):
            p.start(); self.addCleanup(p.stop)
        overview._chart_data_last = None
        app = FastAPI(); app.include_router(overview.router)
        self.client = TestClient(app)

    def test_backlog_fields_projection(self):
        with patch.object(overview, "get_current_branch", return_value="main"), \
                patch("the_lab.agents.list_agents", return_value=[]):
            full = self.client.get("/api/v1/backlog").json()
            slim = self.client.get("/api/v1/backlog?fields=current_branch,active_count").json()
        self.assertEqual(full["active_count"], len(full["active_ideas"]))
        self.assertEqual(slim, {"current_branch": "main", "active_count": full["active_count"]})

    def test_unchanged_data_returns_304_and_changes_invalidate(self):
        first = self.client.get("/api/v1/chart-data")
        etag = first.headers["etag"]
        self.assertEqual(len(first.json()["experiments"]), 1)
        again = self.client.get("/api/v1/chart-data", headers={"If-None-Match": etag})
        self.assertEqual((again.status_code, again.content), (304, b""))
        self.store.update_experiment(self.label, metrics={"score": 2.0})
        changed = self.client.get("/api/v1/chart-data", headers={"If-None-Match": etag})
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(changed.json()["experiments"][0]["metrics"]["score"], 2.0)



class FileRevalidationTests(unittest.TestCase):
    def setUp(self):
        from the_lab.routes import experiments
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name).resolve()
        (self.repo / "data").mkdir()
        self.file = self.repo / "data" / "history.json"
        self.file.write_text('{"a": 1}')
        for p in (patch.object(experiments, "REPO_DIR", self.repo), patch("the_lab.deps.REPO_DIR", self.repo)):
            p.start(); self.addCleanup(p.stop)
        app = FastAPI(); app.include_router(experiments.router)
        self.client = TestClient(app)

    def test_etag_304_head_and_change(self):
        first = self.client.get("/api/v1/files/data/history.json")
        self.assertEqual(first.status_code, 200, first.text)
        etag = first.headers["etag"]
        self.assertEqual(first.headers["cache-control"], "no-cache")
        same = self.client.get("/api/v1/files/data/history.json", headers={"If-None-Match": etag})
        self.assertEqual((same.status_code, same.content), (304, b""))
        head = self.client.head("/api/v1/files/data/history.json")
        self.assertEqual((head.status_code, head.headers["content-length"]), (200, "8"))
        self.file.write_text('{"a": 1, "b": 2}')
        changed = self.client.get("/api/v1/files/data/history.json", headers={"If-None-Match": etag})
        self.assertEqual(changed.status_code, 200)


if __name__ == "__main__":
    unittest.main()
