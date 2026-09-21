"""Regression test: the bridge must find the API on a non-default port.

`the-lab . --port 9009` is a documented flag, but `the-lab init` writes a
.mcp.json with no THE_LAB_API_URL, so a client that launches the bridge from
that file (Claude Code opened in the project, an IDE, any MCP host) used to get
the hardcoded port 8000. The bridge then starts anyway and advertises only its
two static tools, so the agent runs blind with nothing but a stderr warning.

The server already records where it bound in .the_lab/sandbox/runtime.json.
This test puts that file next to a stub spec server on a random port, runs the
bridge from the project directory with no environment override, and asserts the
spec-derived tools are there.
"""
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "the_lab" / "agent_skills" / "skills" / "lab_api_mcp.py"
BRIDGE_PYTHON = os.environ.get("LAB_MCP_PYTHON") or sys.executable
SPEC_SCRIPT = "import json; from the_lab.app import app; print(json.dumps(app.openapi()))"


def _real_openapi_spec():
    # Importing the app initialises a project in the cwd, so do it in a
    # throwaway directory and a separate process.
    with tempfile.TemporaryDirectory() as tmp:
        out = subprocess.run(
            [sys.executable, "-c", SPEC_SCRIPT],
            cwd=tmp, capture_output=True, text=True, timeout=120,
            env=dict(os.environ, PYTHONPATH=str(ROOT)),
        )
    if out.returncode != 0:
        raise RuntimeError("could not build OpenAPI spec:\n" + out.stderr)
    return out.stdout.strip().splitlines()[-1].encode()


class BridgeReadsRuntimePort(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = _real_openapi_spec()

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                found = self.path == "/openapi.json"
                body = spec if found else b"{}"
                self.send_response(200 if found else 404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _tools_from_bridge(self, cwd: Path) -> set:
        msgs = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                        "clientInfo": {"name": "test", "version": "0"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ]
        env = {k: v for k, v in os.environ.items() if k != "THE_LAB_API_URL"}
        proc = subprocess.run(
            [BRIDGE_PYTHON, str(BRIDGE)],
            input="".join(json.dumps(m) + os.linesep for m in msgs),
            capture_output=True, text=True, timeout=60, cwd=str(cwd), env=env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        replies = {r.get("id"): r for r in map(json.loads, proc.stdout.splitlines())}
        return {t["name"] for t in replies.get(2, {}).get("result", {}).get("tools", [])}

    def test_port_comes_from_runtime_json(self):
        port = self.server.server_address[1]
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            runtime = project / ".the_lab" / "sandbox"
            runtime.mkdir(parents=True)
            (runtime / "runtime.json").write_text(json.dumps(
                {"api_scheme": "http", "api_host": "127.0.0.1", "api_port": port}
            ), encoding="utf-8")

            # Launched from a subdirectory: the bridge walks up to the project.
            nested = project / "benchmark"
            nested.mkdir()
            tools = self._tools_from_bridge(nested)

        for name in ("orient", "create_idea", "list_ideas", "get_instructions"):
            self.assertIn(name, tools, f"bridge did not reach the API on port {port}")


if __name__ == "__main__":
    unittest.main()
