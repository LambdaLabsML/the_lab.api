"""Shared-filesystem Slurm results are linked into experiment dirs, not copied."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from the_lab.executors.slurm import SlurmExecutor


class LinkResultsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name).resolve()
        self.jobs = root / "jobs"
        self.job = self.jobs / "inst1" / "12.3"
        (self.job / "worktree").mkdir(parents=True)
        (self.job / "artifacts").mkdir()
        (self.job / "artifacts" / "a.txt").write_text("a")
        (self.job / "script.log").write_text("log")
        (self.job / "experiment.json").write_text("{}")
        (self.job / ".the_lab_generation").write_text("2\n")
        self.exp = root / "repo" / ".the_lab" / "experiments" / "12" / "3"
        self.exp.mkdir(parents=True)
        (self.exp / "experiment.json").write_text('{"local": true}')
        # A copy left by the old pull mechanism:
        (self.exp / "artifacts").mkdir()
        (self.exp / "artifacts" / "old.txt").write_text("stale")
        self.ex = SlurmExecutor("local", {"remote_base": str(self.jobs)}, instance_id="inst1")

    def test_local_executor_links_instead_of_copying(self):
        self.assertTrue(self.ex.shares_filesystem)
        with patch("the_lab.executors.slurm.subprocess.run") as rsync:
            gen = self.ex.pull_results("12.3", self.exp)
        rsync.assert_not_called()
        self.assertEqual(gen, 2)
        for name in ("script.log", "artifacts", ".the_lab_generation"):
            self.assertTrue((self.exp / name).is_symlink(), name)
            self.assertEqual(os.readlink(self.exp / name), str(self.job / name))
        self.assertEqual((self.exp / "artifacts" / "a.txt").read_text(), "a")
        # Private / lab-owned entries are never linked or replaced.
        self.assertFalse((self.exp / "worktree").exists())
        self.assertEqual((self.exp / "experiment.json").read_text(), '{"local": true}')

    def test_new_entries_are_linked_and_repeat_calls_are_noops(self):
        self.ex.link_results("12.3", self.exp)
        (self.job / "script.output.html").write_text("<p>")
        self.ex.link_results("12.3", self.exp)
        self.assertEqual((self.exp / "script.output.html").read_text(), "<p>")
        before = os.lstat(self.exp / "script.log").st_ino
        self.ex.link_results("12.3", self.exp)
        self.assertEqual(os.lstat(self.exp / "script.log").st_ino, before)

    def test_shared_fs_flag_overrides_host_default(self):
        self.assertFalse(SlurmExecutor("local", {"remote_base": str(self.jobs), "shared_fs": False}).shares_filesystem)

    def test_stale_link_is_pruned_when_remote_entry_is_archived_away(self):
        """Restart/timing fix: once the wrapper archives a per-attempt file
        into attempts/<n>/ on restart, the top-level entry disappears from
        the job dir — the local symlink to it must be pruned too, or the
        dashboard keeps resolving a dangling link to the PREVIOUS attempt's
        file forever instead of seeing it move under attempts/."""
        (self.job / "chat_history.json").write_text("[]")
        self.ex.link_results("12.3", self.exp)
        self.assertTrue((self.exp / "chat_history.json").is_symlink())
        # Simulate a restart: the wrapper moves chat_history.json into
        # attempts/1/ and it's gone from the job root.
        (self.job / "chat_history.json").unlink()
        (self.job / "attempts" / "1").mkdir(parents=True)
        (self.job / "attempts" / "1" / "chat_history.json").write_text("[]")
        self.ex.link_results("12.3", self.exp)
        self.assertFalse((self.exp / "chat_history.json").exists())
        self.assertFalse((self.exp / "chat_history.json").is_symlink())
        # The still-present entries (including the new attempts/ dir) keep
        # their links.
        self.assertTrue((self.exp / "attempts").is_symlink())
        self.assertTrue((self.exp / "script.log").is_symlink())


class SharedJobFilesRouteTests(unittest.TestCase):
    def setUp(self):
        from the_lab import queue as queue_mod
        from the_lab.routes import experiments
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name).resolve()
        self.repo = root / "repo"; self.repo.mkdir()
        jobs = root / "jobs"
        job = jobs / "inst1" / "12.3"
        (job / "worktree").mkdir(parents=True)
        (job / "worktree" / "secret.py").write_text("x")
        (job / "chat_history.json").write_text('{"m": 1}')
        exp = self.repo / ".the_lab" / "experiments" / "12" / "3"; exp.mkdir(parents=True)
        os.symlink(job / "chat_history.json", exp / "chat_history.json")
        os.symlink(job / "worktree", exp / "wt")  # never created by link_results
        res = queue_mod.Resource(name="s", kind="slurm",
                                 executor_config={"ssh_host": "local", "remote_base": str(jobs)})
        store = type("S", (), {"instance_id": "inst1"})()
        for p in (patch.object(experiments, "REPO_DIR", self.repo), patch("the_lab.deps.REPO_DIR", self.repo),
                  patch.object(experiments, "store", store),
                  patch.object(queue_mod, "load_config", return_value=([res], {}))):
            p.start(); self.addCleanup(p.stop)
        app = FastAPI(); app.include_router(experiments.router)
        self.client = TestClient(app)

    def test_linked_results_are_served_private_entries_are_not(self):
        ok = self.client.get("/api/v1/files/.the_lab/experiments/12/3/chat_history.json")
        self.assertEqual((ok.status_code, ok.json()), (200, {"m": 1}))
        denied = self.client.get("/api/v1/files/.the_lab/experiments/12/3/wt/secret.py")
        self.assertEqual(denied.status_code, 403)


if __name__ == "__main__":
    unittest.main()
