import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from the_lab import agents


class AgentWorktreeLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.repo = Path("/repo")
        self.entry = {
            "agent_id": "abc12",
            "branch": "agent_init_abc12",
            "worktree": "/repo/.the_lab/agents/abc12",
            "pid": 123,
        }

    def test_launcher_retirement_preserves_worktree(self):
        registry = {"abc12": dict(self.entry)}
        with (
            patch.object(agents, "_read_registry", return_value=registry),
            patch.object(agents, "_write_registry") as write_registry,
            patch.object(agents, "record_completed_agent") as record_completed,
            patch.object(agents, "_archive_agent_data") as archive,
            patch.object(agents.git_ops, "remove_worktree") as remove_worktree,
        ):
            result = agents.unregister_agent(
                self.repo, "abc12", keep_worktree=True,
            )

        self.assertTrue(result)
        record_completed.assert_called_once_with(self.repo, self.entry)
        write_registry.assert_called_once_with(self.repo, {})
        archive.assert_not_called()
        remove_worktree.assert_not_called()

    def test_explicit_deletion_still_removes_worktree(self):
        registry = {"abc12": dict(self.entry)}
        with (
            patch.object(agents, "_read_registry", return_value=registry),
            patch.object(agents, "_write_registry"),
            patch.object(agents, "record_completed_agent"),
            patch.object(agents, "_archive_agent_data") as archive,
            patch.object(agents.git_ops, "remove_worktree") as remove_worktree,
            patch.object(agents.git_ops, "prune_worktrees"),
        ):
            result = agents.unregister_agent(self.repo, "abc12")

        self.assertTrue(result)
        archive.assert_called_once_with(self.repo, "abc12", self.entry)
        remove_worktree.assert_called_once_with(
            Path(self.entry["worktree"]), cwd=self.repo,
        )

    def test_dead_agent_pruning_preserves_worktree(self):
        here = socket.gethostname()
        dead = dict(self.entry, host=here)
        live = dict(self.entry, agent_id="live1", pid=456, host=here)
        # The server cannot check PIDs of agents on other machines (or of
        # entries that never recorded a host): those must survive a restart.
        remote = dict(self.entry, agent_id="far01", pid=999, host="other-node")
        unknown = dict(self.entry, agent_id="nohst", pid=998)
        registry = {"abc12": dead, "live1": live, "far01": remote, "nohst": unknown}
        with (
            patch.object(agents, "_read_registry", return_value=registry),
            patch.object(agents, "_pid_alive", side_effect=lambda pid: pid == 456),
            patch.object(agents, "_write_registry") as write_registry,
            patch.object(agents, "record_completed_agent") as record_completed,
            patch.object(agents.git_ops, "remove_worktree") as remove_worktree,
        ):
            removed = agents.prune_dead_agents(self.repo)

        self.assertEqual(removed, ["abc12"])
        record_completed.assert_called_once_with(self.repo, dead)
        write_registry.assert_called_once_with(
            self.repo, {"live1": live, "far01": remote, "nohst": unknown})
        remove_worktree.assert_not_called()

    def test_retained_past_agent_can_be_reactivated(self):
        past = dict(self.entry, completed_at="2026-09-24T12:00:00+00:00")
        with (
            patch.object(agents, "_read_registry", return_value={}),
            patch.object(agents, "_load_history", return_value=[past]),
            patch.object(Path, "is_dir", return_value=True),
            patch.object(agents, "_write_registry") as write_registry,
        ):
            result = agents.reactivate_agent(
                self.repo, "abc12", role="fixloop", pid=789,
            )

        self.assertEqual(result["agent_id"], "abc12")
        self.assertEqual(result["role"], "fixloop")
        self.assertEqual(result["pid"], 789)
        self.assertNotIn("completed_at", result)
        self.assertIn("resumed_at", result)
        written = write_registry.call_args.args[1]
        self.assertEqual(written["abc12"], result)


class ReactivationKeepsIdTests(unittest.TestCase):
    """Real git: resuming an agent whose worktree was cleaned up keeps its id."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name).resolve()
        git = lambda *a: subprocess.run(["git", *a], cwd=self.repo, check=True, capture_output=True)
        git("init", "-q", "-b", "main")
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init")
        self.git = git

    def _register(self):
        store = type("S", (), {"list_ideas": lambda self, **k: []})()
        return agents.register_agent(self.repo, store, role="worker", pid=1, host="h1")

    def test_missing_worktree_is_recreated_under_same_id(self):
        entry = self._register()
        self.assertEqual(entry["host"], "h1")
        agents.unregister_agent(self.repo, entry["agent_id"], keep_worktree=True)
        self.git("worktree", "remove", "--force", entry["worktree"])
        self.assertFalse(Path(entry["worktree"]).exists())

        again = agents.reactivate_agent(self.repo, entry["agent_id"], pid=2, host="h2")
        self.assertEqual(again["agent_id"], entry["agent_id"])
        self.assertEqual(again["worktree"], entry["worktree"])
        self.assertTrue((Path(again["worktree"]) / ".the_lab.agentid").exists())
        self.assertTrue(again["worktree_recreated"])
        self.assertEqual((again["pid"], again["host"]), (2, "h2"))
        self.assertNotIn("previous_pid", again)  # it was retired, not live

    def test_reactivating_a_live_agent_reports_the_previous_process(self):
        entry = self._register()
        again = agents.reactivate_agent(self.repo, entry["agent_id"], pid=3, host="h3")
        self.assertEqual((again["previous_pid"], again["previous_host"]), (1, "h1"))
        self.assertNotIn("worktree_recreated", again)


if __name__ == "__main__":
    unittest.main()
