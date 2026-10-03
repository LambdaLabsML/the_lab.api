"""Codex-specific resume parsing and rollout lookup coverage."""

import argparse
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from the_lab import agent_cli
from unittest.mock import patch

from the_lab.agent_cli import (
    _build_launch_command,
    _extract_codex_resume_request,
    _read_codex_listing_session,
    _read_codex_session_meta,
    _resume_command,
)


SESSION_ID = "01a0ae9d-4100-73b0-a862-8970975350d8"


class CodexResumeTests(unittest.TestCase):
    def test_real_launcher_parser_accepts_reported_command_without_launching(self):
        class Parsed(Exception):
            pass

        def inspect(args, extra):
            session_id, forwarded = _extract_codex_resume_request(args, extra)
            self.assertEqual(session_id, SESSION_ID)
            self.assertEqual(forwarded, [])
            self.assertIsNone(args.command)
            self.assertEqual(args.role, "fixloop_v2")
            self.assertEqual(args.port, 30033)
            self.assertEqual(args.sandbox, "off")
            raise Parsed

        argv = ["the-lab-agent", "--agent", "codex", "--role", "fixloop_v2",
                "--port", "30033", "--sandbox", "off", "resume", SESSION_ID]
        with patch("sys.argv", argv), patch.object(
                agent_cli, "_extract_codex_resume_request", side_effect=inspect):
            with self.assertRaises(Parsed):
                agent_cli.main()

    def _launcher_positionals(self, argv):
        # This is the same broad positional shape main uses.  It demonstrates
        # why native `resume ID` needs post-argparse normalization.
        parser = argparse.ArgumentParser()
        parser.add_argument("command", nargs="?", default=None)
        parser.add_argument("prompt_file", nargs="?", default="PROMPT.md")
        parser.add_argument("--agent", default="claude")
        parser.add_argument("--resume")
        return parser.parse_known_args(argv)

    def test_native_resume_positionals_become_codex_resume_id(self):
        args, extra = self._launcher_positionals(
            ["--agent", "codex", "resume", SESSION_ID],
        )
        session_id, forwarded = _extract_codex_resume_request(args, extra)

        self.assertEqual(session_id, SESSION_ID)
        self.assertEqual(forwarded, [])
        self.assertIsNone(args.command)
        self.assertEqual(args.prompt_file, "PROMPT.md")

    def test_old_resume_flag_is_normalized_for_codex(self):
        args, extra = self._launcher_positionals(
            ["--agent", "codex", "loop", "--resume", SESSION_ID, "--all"],
        )
        session_id, forwarded = _extract_codex_resume_request(args, extra)

        self.assertEqual(session_id, SESSION_ID)
        self.assertEqual(forwarded, ["--all"])
        self.assertEqual(args.command, "loop")

    def test_missing_native_resume_id_is_rejected_by_normalizer(self):
        args, extra = self._launcher_positionals(["--agent", "codex", "resume"])
        session_id, forwarded = _extract_codex_resume_request(args, extra)
        self.assertEqual(session_id, "")
        self.assertEqual(forwarded, [])

    def test_codex_resume_command_keeps_id_and_explicit_new_worktree(self):
        cmd = _build_launch_command(
            "codex", "/usr/bin/codex", "loop prompt", None, False,
            mcp_config=None, resume_session_id=SESSION_ID,
            resume_cwd="/repo/.the_lab/agents/new-agent",
        )
        self.assertEqual(cmd, [
            "/usr/bin/codex", "resume", "--yolo", "--cd",
            "/repo/.the_lab/agents/new-agent", SESSION_ID, "loop prompt",
        ])

    def test_rollout_metadata_reads_initial_cwd_from_effective_codex_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            rollout = Path(tmp) / "sessions" / "2026" / "09" / "17" / f"rollout-x-{SESSION_ID}.jsonl"
            rollout.parent.mkdir(parents=True)
            rollout.write_text(json.dumps({
                "type": "session_meta",
                "payload": {"id": SESSION_ID, "cwd": "/old/.the_lab/agents/original"},
            }) + "\n" + "not json needed\n")

            meta = _read_codex_session_meta(SESSION_ID, Path(tmp))

        self.assertEqual(meta, {"id": SESSION_ID, "cwd": "/old/.the_lab/agents/original"})

    def test_rollout_metadata_honors_codex_home_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            rollout = Path(tmp) / "sessions" / f"rollout-{SESSION_ID}.jsonl"
            rollout.parent.mkdir()
            rollout.write_text(json.dumps({
                "type": "session_meta",
                "payload": {"session_id": SESSION_ID, "cwd": "/old/worktree"},
            }) + "\n")
            with patch.dict("os.environ", {"CODEX_HOME": tmp}, clear=False):
                meta = _read_codex_session_meta(SESSION_ID)
        self.assertEqual(meta["cwd"], "/old/worktree")

    def test_codex_listing_uses_latest_resumed_agent_and_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            rollout = Path(tmp) / f"rollout-{SESSION_ID}.jsonl"
            rollout.write_text("\n".join([
                json.dumps({
                    "timestamp": "2026-09-20T10:00:00Z",
                    "type": "session_meta",
                    "payload": {
                        "id": SESSION_ID,
                        "cwd": "/repo/.the_lab/agents/old01",
                        "source": "cli",
                    },
                }),
                json.dumps({
                    "timestamp": "2026-09-24T10:00:00Z",
                    "type": "event_msg",
                    "payload": {"environment_context": {
                        "cwd": "file:///repo/.the_lab/agents/new02",
                    }},
                }),
            ]) + "\n")

            row = _read_codex_listing_session(rollout)

        self.assertEqual(row["agent_id"], "new02")
        self.assertEqual(row["cwd"], "/repo/.the_lab/agents/new02")
        self.assertEqual(row["provider"], "codex")
        self.assertEqual(
            _resume_command(row),
            f"the-lab-agent --agent codex resume {SESSION_ID}",
        )

    def test_list_combines_providers_newest_first_and_hides_codex_subagents(self):
        claude_id = "11111111-1111-1111-1111-111111111111"
        codex_id = "22222222-2222-2222-2222-222222222222"
        subagent_id = "33333333-3333-3333-3333-333333333333"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude_root = root / "claude"
            project = claude_root / "project"
            project.mkdir(parents=True)
            claude = project / f"{claude_id}.jsonl"
            claude.write_text(json.dumps({
                "type": "user", "timestamp": "2026-09-23T10:00:00Z",
                "cwd": "/repo/.the_lab/agents/cla01", "message": {},
            }) + "\n")

            codex_root = root / "codex"
            codex_root.mkdir()
            codex = codex_root / f"rollout-{codex_id}.jsonl"
            codex.write_text(json.dumps({
                "type": "session_meta", "timestamp": "2026-09-24T10:00:00Z",
                "payload": {"id": codex_id, "cwd": "/repo/.the_lab/agents/cod01", "source": "cli"},
            }) + "\n")
            subagent = codex_root / f"rollout-{subagent_id}.jsonl"
            subagent.write_text(json.dumps({
                "type": "session_meta", "timestamp": "2026-09-24T11:00:00Z",
                "payload": {"id": subagent_id, "cwd": "/repo/.the_lab/agents/cod01", "source": {"subagent": {}}},
            }) + "\n")
            os.utime(claude, (1_000, 1_000))
            os.utime(codex, (2_000, 2_000))
            os.utime(subagent, (3_000, 3_000))

            stdout = io.StringIO()
            with patch.object(agent_cli, "_SESSION_ROOT", claude_root), patch.object(
                agent_cli, "_codex_session_root", return_value=codex_root,
            ), contextlib.redirect_stdout(stdout):
                agent_cli.cmd_list_sessions(["--json"])

        result = json.loads(stdout.getvalue())
        self.assertEqual(
            [(row["provider"], row["agent_id"], row["session_id"]) for row in result["sessions"]],
            [("codex", "cod01", codex_id), ("claude", "cla01", claude_id)],
        )
        self.assertIn("--agent codex resume", result["sessions"][0]["resume_command"])
        self.assertIn("--agent claude loop --resume", result["sessions"][1]["resume_command"])


if __name__ == "__main__":
    unittest.main()
