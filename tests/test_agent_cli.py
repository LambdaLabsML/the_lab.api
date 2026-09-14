"""Focused coverage for agent CLI launch configuration."""

import json
import os
import unittest
from unittest.mock import patch

from the_lab.agent_cli import _api_curl_command, _build_launch_command


def _labapi_mcp_config():
    return json.dumps({
        "mcpServers": {
            "labapi": {
                "command": "python3",
                "args": ["/opt/the_lab/lab_api_mcp.py"],
                "env": {
                    "PYTHONUNBUFFERED": "1",
                    "THE_LAB_API_URL": "https://localhost:30033/api/v1",
                    "THE_LAB_API_INSECURE": "1",
                    "THE_LAB_USER": "admin",
                    "THE_LAB_PASSWORD": "secret-password",
                },
            },
        },
    })


class AgentCliMcpTests(unittest.TestCase):
    def test_api_curl_fallback_uses_inherited_basic_auth_without_exposing_values(self):
        with patch.dict(
            os.environ,
            {"THE_LAB_USER": "admin", "THE_LAB_PASSWORD": "secret-password"},
            clear=True,
        ):
            command = _api_curl_command("https://localhost:30033/api/v1", True)

        self.assertEqual(
            command,
            'curl -k -u "$THE_LAB_USER:$THE_LAB_PASSWORD" '
            "https://localhost:30033/api/v1/orient",
        )
        self.assertNotIn("secret-password", command)
        with patch.dict(os.environ, {}, clear=True):
            unauthenticated = _api_curl_command("http://localhost:8000/api/v1", False)
        self.assertEqual(unauthenticated, "curl http://localhost:8000/api/v1/orient")

    def test_codex_gets_launch_scoped_mcp_overrides_without_secret_values(self):
        cmd = _build_launch_command(
            "codex", "/usr/bin/codex", "prompt.md", None, False,
            mcp_config=_labapi_mcp_config(),
        )

        self.assertEqual(cmd[0], "/usr/bin/codex")
        self.assertIn("--yolo", cmd)
        self.assertEqual(cmd[-1], "prompt.md")
        self.assertIn("mcp_servers.labapi.command=\"python3\"", cmd)
        self.assertIn(
            "mcp_servers.labapi.args=[\"/opt/the_lab/lab_api_mcp.py\"]", cmd,
        )
        self.assertIn(
            "mcp_servers.labapi.env_vars=[\"PYTHONUNBUFFERED\", "
            "\"THE_LAB_API_URL\", \"THE_LAB_API_INSECURE\", "
            "\"THE_LAB_USER\", \"THE_LAB_PASSWORD\", \"THE_LAB_AGENT_ID\"]",
            cmd,
        )
        self.assertEqual(cmd.count("-c"), 3)
        self.assertNotIn("secret-password", " ".join(cmd))

    def test_claude_keeps_its_json_mcp_config_path(self):
        cmd = _build_launch_command(
            "claude", "/usr/bin/claude", "prompt.md", None, False,
            mcp_config=_labapi_mcp_config(), mcp_path="/tmp/lab-mcp.json",
        )

        self.assertEqual(
            cmd,
            [
                "/usr/bin/claude", "--dangerously-skip-permissions",
                "--mcp-config", "/tmp/lab-mcp.json", "--", "prompt.md",
            ],
        )
        self.assertNotIn("-c", cmd)


if __name__ == "__main__":
    unittest.main()
