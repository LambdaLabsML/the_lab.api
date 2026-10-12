"""POST /ideas/{id}/reparent: move an idea in the graph, optionally rebasing its branch."""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from the_lab import deps
from the_lab.routes import ideas
from the_lab.store import Store


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


class ReparentIdeaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "commit", "-q", "--allow-empty", "-m", "init")
        self.store = Store(self.repo)
        # 1 (a.txt) and 2 (b.txt) branch off main; 3 (c.txt) is a child of 1.
        self.a = self._idea("A", [], "main", "a.txt")
        self.b = self._idea("B", [], "main", "b.txt")
        self.c = self._idea("C", [self.a], f"idea/{self.a}", "c.txt")
        for p in (patch.object(ideas, "store", self.store), patch.object(ideas, "REPO_DIR", self.repo),
                  patch.object(deps, "store", self.store), patch.object(deps, "REPO_DIR", self.repo)):
            p.start(); self.addCleanup(p.stop)
        app = FastAPI(); app.include_router(ideas.router)
        self.client = TestClient(app)

    def _idea(self, desc, parents, base, filename, content="x\n"):
        idea = self.store.create_idea(desc, parents, branch="")
        idea["branch"] = f"idea/{idea['id']}"
        self.store.save_idea(idea)
        _git(self.repo, "branch", idea["branch"], base)
        self._commit_on(idea["branch"], filename, content)
        return idea["id"]

    def _commit_on(self, branch, filename, content):
        _git(self.repo, "checkout", "-q", branch)
        (self.repo / filename).write_text(content)
        _git(self.repo, "add", filename)
        _git(self.repo, "commit", "-q", "-m", f"{branch}: {filename}")
        _git(self.repo, "checkout", "-q", "main")

    def _files(self, branch):
        return set(_git(self.repo, "ls-tree", "--name-only", branch).split())

    def test_rebase_moves_own_commits_onto_new_parent_and_keeps_old_head(self):
        old_head = _git(self.repo, "rev-parse", f"idea/{self.c}")
        resp = self.client.post(f"/api/v1/ideas/{self.c}/reparent", json={"parent_ids": [self.b], "note": "fits B"})
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["parent_ids"], [self.b])
        self.assertEqual(body["old_parent_ids"], [self.a])
        self.assertEqual(body["rebase"]["commits"], 1)
        # C now has B's file and its own, not A's.
        self.assertEqual(self._files(f"idea/{self.c}"), {"b.txt", "c.txt"})
        self.assertEqual(self.store.get_idea(self.c)["parent_ids"], [self.b])
        # Old head kept reachable for experiments pinned to it.
        self.assertEqual(_git(self.repo, "rev-parse", body["rebase"]["backup_ref"]), old_head)
        note = self.store.get_notes(self.c)[-1]["text"]
        self.assertIn("#1 → #2", note.replace(str(self.a), "1").replace(str(self.b), "2"))
        self.assertIn("fits B", note)
        # The main checkout was never moved.
        self.assertEqual(_git(self.repo, "symbolic-ref", "--short", "HEAD"), "main")

    def test_conflict_aborts_and_changes_nothing(self):
        self._commit_on(f"idea/{self.b}", "c.txt", "B's version\n")
        before = _git(self.repo, "rev-parse", f"idea/{self.c}")
        resp = self.client.post(f"/api/v1/ideas/{self.c}/reparent", json={"parent_ids": [self.b]})
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertIn("c.txt", resp.json()["detail"]["conflicts"])
        self.assertEqual(_git(self.repo, "rev-parse", f"idea/{self.c}"), before)
        self.assertEqual(self.store.get_idea(self.c)["parent_ids"], [self.a])
        self.assertEqual(_git(self.repo, "worktree", "list").count("\n"), 0)  # temp worktree removed

    def test_graph_only_move_leaves_git_alone(self):
        before = _git(self.repo, "rev-parse", f"idea/{self.c}")
        resp = self.client.post(f"/api/v1/ideas/{self.c}/reparent",
                                json={"parent_ids": [self.a, self.b], "rebase": False})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIsNone(resp.json()["rebase"])
        self.assertEqual(self.store.get_idea(self.c)["parent_ids"], [self.a, self.b])
        self.assertEqual(_git(self.repo, "rev-parse", f"idea/{self.c}"), before)

    def test_cycle_and_self_are_rejected(self):
        self.assertEqual(self.client.post(f"/api/v1/ideas/{self.a}/reparent",
                                          json={"parent_ids": [self.c], "rebase": False}).status_code, 400)
        self.assertEqual(self.client.post(f"/api/v1/ideas/{self.a}/reparent",
                                          json={"parent_ids": [self.a], "rebase": False}).status_code, 400)
        self.assertEqual(self.client.post(f"/api/v1/ideas/{self.a}/reparent",
                                          json={"parent_ids": [999], "rebase": False}).status_code, 404)

    def test_rebase_refused_while_branch_is_checked_out(self):
        wt = Path(self.tmp.name) / "agent-wt"
        _git(self.repo, "worktree", "add", "-q", str(wt), f"idea/{self.c}")
        resp = self.client.post(f"/api/v1/ideas/{self.c}/reparent", json={"parent_ids": [self.b]})
        self.assertEqual(resp.status_code, 409, resp.text)
        self.assertEqual(self.store.get_idea(self.c)["parent_ids"], [self.a])

    def test_root_move_rebases_onto_default_branch(self):
        resp = self.client.post(f"/api/v1/ideas/{self.c}/reparent", json={"parent_ids": []})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self._files(f"idea/{self.c}"), {"c.txt"})
        self.assertEqual(self.store.get_idea(self.c)["parent_ids"], [])


if __name__ == "__main__":
    unittest.main()
