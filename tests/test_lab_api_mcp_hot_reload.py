"""Hot-reload tests for the MCP bridge (the_lab/agent_skills/skills/lab_api_mcp.py).

Runs the bridge as a real subprocess talking JSON-RPC over stdio (as Claude
Code would), against a tiny fake Lab API (mutable /openapi.json + a
/api/v1/health probe), to exercise:

  - listChanged capability advertised at initialize
  - background tool-list refresh + notifications/tools/list_changed when the
    served OpenAPI spec changes
  - recovery after the fake API goes down and comes back with a new spec
  - self-reload (re-exec) when the bridge's own source file changes on disk,
    without the client re-sending `initialize`
  - stdout lines are never interleaved/corrupted (one lock guards all writes)

Run with: .venv/bin/python -m unittest tests.test_lab_api_mcp_hot_reload
"""
import http.server
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path


_BRIDGE_PATH = Path(__file__).parents[1] / "the_lab" / "agent_skills" / "skills" / "lab_api_mcp.py"

_SPEC_V1 = {
    "paths": {
        "/api/v1/instructions": {
            "get": {"summary": "Get instructions", "parameters": []},
        },
    },
}

_SPEC_V2 = {
    "paths": {
        "/api/v1/instructions": {
            "get": {"summary": "Get instructions", "parameters": []},
        },
        "/api/v1/orient": {
            "get": {"summary": "Orient", "parameters": []},
        },
    },
}


class _FakeLabAPI:
    """Minimal HTTP server serving a mutable OpenAPI spec + health probe."""

    def __init__(self, spec):
        self.spec = spec
        self._lock = threading.Lock()
        self._server = None
        self._thread = None
        self.port = None

    def set_spec(self, spec):
        with self._lock:
            self.spec = spec

    def start(self, port=0):
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass  # keep test output quiet

            def _reply(self, status, payload):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path.startswith("/api/v1/health"):
                    self._reply(200, {"status": "ok"})
                    return
                if self.path.startswith("/openapi.json"):
                    with outer._lock:
                        spec = outer.spec
                    self._reply(200, spec)
                    return
                # Generic fallback: any proxied tool call (e.g. instructions,
                # orient) just gets a trivial 200 so proxy_call succeeds.
                self._reply(200, {"ok": True, "path": self.path})

            def do_POST(self):
                self.do_GET()

        self._server = http.server.HTTPServer(("127.0.0.1", port), Handler)
        self._server.allow_reuse_address = True
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
            self._thread = None

    @property
    def api_url(self):
        return f"http://127.0.0.1:{self.port}/api/v1"


class _BridgeClient:
    """Drives the MCP bridge subprocess as a JSON-RPC-over-stdio client."""

    def __init__(self, script_path, env_overrides):
        env = os.environ.copy()
        env.update(env_overrides)
        env["PYTHONUNBUFFERED"] = "1"
        self.proc = subprocess.Popen(
            [sys.executable, str(script_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
        )
        self._lines = queue.Queue()
        self._stderr_lines = []
        self._next_id = 1
        self._out_thread = threading.Thread(target=self._pump_stdout, daemon=True)
        self._out_thread.start()
        self._err_thread = threading.Thread(target=self._pump_stderr, daemon=True)
        self._err_thread.start()

    def _pump_stdout(self):
        try:
            for line in self.proc.stdout:
                self._lines.put(line)
        except Exception:
            pass
        self._lines.put(None)

    def _pump_stderr(self):
        try:
            for line in self.proc.stderr:
                self._stderr_lines.append(line)
        except Exception:
            pass

    def send_request(self, method, params=None):
        msg_id = self._next_id
        self._next_id += 1
        self._write({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}})
        return msg_id

    def send_notification(self, method, params=None):
        self._write({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def _write(self, msg):
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def recv_raw(self, timeout=10):
        """Return one raw stdout line (still newline-free), or raise."""
        try:
            line = self._lines.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError(
                f"no stdout line within {timeout}s (stderr so far: {''.join(self._stderr_lines)[-2000:]})"
            )
        if line is None:
            raise EOFError(f"bridge closed stdout (stderr: {''.join(self._stderr_lines)[-2000:]})")
        return line

    def recv(self, timeout=10):
        return json.loads(self.recv_raw(timeout=timeout))

    def recv_matching(self, predicate, timeout=10):
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("no matching message within timeout")
            msg = self.recv(timeout=remaining)
            if predicate(msg):
                return msg

    def initialize(self, timeout=10):
        req_id = self.send_request("initialize", {"protocolVersion": "2024-11-05"})
        return self.recv_matching(lambda m: m.get("id") == req_id, timeout=timeout)

    def tools_list(self, timeout=10):
        req_id = self.send_request("tools/list")
        return self.recv_matching(lambda m: m.get("id") == req_id, timeout=timeout)

    def tools_call(self, name, arguments=None, timeout=10):
        req_id = self.send_request("tools/call", {"name": name, "arguments": arguments or {}})
        return self.recv_matching(lambda m: m.get("id") == req_id, timeout=timeout)

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


def _tool_names(tools_list_result):
    return {t["name"] for t in tools_list_result["result"]["tools"]}


class HotReloadTests(unittest.TestCase):
    def setUp(self):
        self.api = _FakeLabAPI(dict(_SPEC_V1))
        self.api.start()
        self.addCleanup(self.api.stop)
        self._clients = []

    def tearDown(self):
        for client in self._clients:
            client.close()

    def _client(self, extra_env=None, script_path=None):
        env = {
            "THE_LAB_API_URL": self.api.api_url,
            "LAB_MCP_REFRESH_S": "1",
        }
        env.update(extra_env or {})
        c = _BridgeClient(script_path or _BRIDGE_PATH, env)
        self._clients.append(c)
        return c

    # -- initialize / listChanged capability -----------------------------

    def test_initialize_advertises_list_changed_and_tools_list_returns_tools(self):
        client = self._client()
        init = client.initialize()
        self.assertTrue(init["result"]["capabilities"]["tools"]["listChanged"])

        listing = client.tools_list()
        names = _tool_names(listing)
        self.assertIn("get_instructions", names)
        self.assertIn("watch_events", names)
        self.assertIsNone(client.proc.poll(), "bridge exited unexpectedly")

    # -- tool-list refresh notification -----------------------------------

    def test_tool_added_on_server_triggers_list_changed_notification(self):
        client = self._client()
        client.initialize()
        before = _tool_names(client.tools_list())
        self.assertNotIn("orient", before)

        self.api.set_spec(dict(_SPEC_V2))

        client.recv_matching(
            lambda m: m.get("method") == "notifications/tools/list_changed",
            timeout=15,
        )

        after = _tool_names(client.tools_list())
        self.assertIn("orient", after)
        self.assertIsNone(client.proc.poll())

    # -- server down then up with a changed spec ---------------------------

    def test_server_down_then_up_with_changed_spec_recovers_and_notifies(self):
        client = self._client()
        client.initialize()
        before = _tool_names(client.tools_list())
        self.assertNotIn("orient", before)

        port = self.api.port
        self.api.stop()
        time.sleep(2)  # let a couple of refresh ticks see the server down
        self.assertIsNone(client.proc.poll(), "bridge must not exit while API is down")

        self.api = _FakeLabAPI(dict(_SPEC_V2))
        self.api.start(port=port)
        # Re-registering cleanup for the new server instance.
        self.addCleanup(self.api.stop)

        client.recv_matching(
            lambda m: m.get("method") == "notifications/tools/list_changed",
            timeout=20,
        )

        after = _tool_names(client.tools_list())
        self.assertIn("orient", after)
        self.assertIsNone(client.proc.poll(), "bridge must not exit across a server restart")

    # -- self reload --------------------------------------------------------

    def test_self_reload_execs_without_new_initialize_and_notifies(self):
        tmpdir = tempfile.mkdtemp(prefix="lab-mcp-reload-")
        self.addCleanup(shutil.rmtree, tmpdir, ignore_errors=True)
        copy_path = Path(tmpdir) / "lab_api_mcp_copy.py"
        original_source = _BRIDGE_PATH.read_text()
        copy_path.write_text(original_source)

        client = self._client(script_path=copy_path)
        client.initialize()
        baseline = client.tools_list()
        baseline_watch_desc = next(
            t["description"] for t in baseline["result"]["tools"] if t["name"] == "watch_events"
        )
        self.assertNotIn("RELOAD-MARKER", baseline_watch_desc)
        pid_before = client.proc.pid

        # Simulate "the bridge's own code changed on disk": append a
        # top-level statement (runs at import time, before main()'s stdin
        # loop) that mutates a tool description — a change observable
        # without needing a fresh `initialize`.
        marker_stmt = (
            '\n_WS_TOOLS[0]["description"] = _WS_TOOLS[0]["description"] + '
            '"\\n[RELOAD-MARKER]"\n'
        )
        new_source = original_source.replace(
            "def _call_ws_tool(name: str, args: dict) -> str:",
            marker_stmt + "\n\ndef _call_ws_tool(name: str, args: dict) -> str:",
            1,
        )
        self.assertNotEqual(new_source, original_source)
        copy_path.write_text(new_source)

        # The bridge re-execs only between messages with no unread input (an
        # exec while a request sits in its buffer would lose that request), so
        # the reload happens right after the next request it answers. Give the
        # refresh thread a moment to notice the change, then send one request:
        # it is still answered (by the old code), then the bridge re-execs.
        time.sleep(2.5)
        before_reload = client.tools_call("get_instructions", {})
        self.assertIn("content", before_reload["result"])
        client.recv_matching(
            lambda m: m.get("method") == "notifications/tools/list_changed",
            timeout=15,
        )

        # No new `initialize` was sent — tools/list and tools/call must work.
        reloaded = client.tools_list()
        reloaded_watch_desc = next(
            t["description"] for t in reloaded["result"]["tools"] if t["name"] == "watch_events"
        )
        self.assertIn("RELOAD-MARKER", reloaded_watch_desc)

        call_result = client.tools_call("get_instructions", {})
        self.assertIn("content", call_result["result"])

        self.assertIsNone(client.proc.poll(), "bridge must still be running after self-reload")
        # Same PID is fine (and expected) for os.execv — just confirm it is
        # in fact still this same OS process, not a crashed-and-respawned one.
        self.assertEqual(client.proc.pid, pid_before)

    # -- no interleaving of stdout lines -------------------------------------

    def test_responses_and_notifications_are_never_interleaved(self):
        client = self._client()
        client.initialize()
        client.tools_list()

        # Change the spec so a notification fires in the middle of a burst
        # of concurrent tool calls.
        self.api.set_spec(dict(_SPEC_V2))

        sent_ids = []
        for _ in range(25):
            sent_ids.append(client.send_request("tools/call", {
                "name": "get_instructions", "arguments": {},
            }))
            time.sleep(0.02)

        seen_ids = set()
        seen_notification = False
        deadline = time.monotonic() + 20
        while (len(seen_ids) < len(sent_ids) or not seen_notification) and time.monotonic() < deadline:
            raw = client.recv_raw(timeout=max(0.1, deadline - time.monotonic()))
            stripped = raw.strip()
            self.assertTrue(stripped, "stdout produced a blank line")
            # Every single line must parse as exactly one complete JSON
            # object — interleaving would corrupt this.
            msg = json.loads(stripped)
            self.assertIsInstance(msg, dict)
            if msg.get("method") == "notifications/tools/list_changed":
                seen_notification = True
                continue
            self.assertIn(msg.get("id"), sent_ids)
            seen_ids.add(msg["id"])

        self.assertEqual(seen_ids, set(sent_ids))
        self.assertTrue(seen_notification, "expected a list_changed notification during the burst")
        self.assertIsNone(client.proc.poll())


if __name__ == "__main__":
    unittest.main()
