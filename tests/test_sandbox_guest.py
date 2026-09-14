"""Regression tests for the sandbox agent-home setup."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from the_lab import sandbox_guest


class SandboxAgentHomeTests(unittest.TestCase):
    def test_prepare_agent_home_copies_codex_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            real_home = Path(tmp) / "real-home"
            real_codex = real_home / ".codex"
            real_codex.mkdir(parents=True)
            (real_codex / "config.toml").write_text('model = "gpt-test"\n')
            agent_home = Path(tmp) / "agent-home"
            agent_home.mkdir()

            env = {
                "HOME": str(real_home),
                "THE_LAB_SANDBOX_TARGET_UID": str(os.getuid()),
                "THE_LAB_SANDBOX_TARGET_GID": str(os.getgid()),
            }
            with patch.dict(os.environ, env, clear=False), patch(
                "tempfile.mkdtemp", return_value=str(agent_home)
            ):
                result = sandbox_guest._prepare_agent_home()

            self.assertEqual(result, str(agent_home))
            self.assertEqual(
                (agent_home / ".codex" / "config.toml").read_text(),
                'model = "gpt-test"\n',
            )


if __name__ == "__main__":
    unittest.main()
