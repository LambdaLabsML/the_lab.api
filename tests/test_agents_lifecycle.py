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
        live = dict(self.entry, agent_id="live1", pid=456)
        registry = {"abc12": dict(self.entry), "live1": live}
        with (
            patch.object(agents, "_read_registry", return_value=registry),
            patch.object(agents, "_pid_alive", side_effect=lambda pid: pid == 456),
            patch.object(agents, "_write_registry") as write_registry,
            patch.object(agents, "record_completed_agent") as record_completed,
            patch.object(agents.git_ops, "remove_worktree") as remove_worktree,
        ):
            removed = agents.prune_dead_agents(self.repo)

        self.assertEqual(removed, ["abc12"])
        record_completed.assert_called_once_with(self.repo, self.entry)
        write_registry.assert_called_once_with(self.repo, {"live1": live})
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


if __name__ == "__main__":
    unittest.main()
