"""Focused reliability coverage for the lightweight messages CLI client."""
import os
import socket
import io
import unittest
from argparse import Namespace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from the_lab.cli import (
    _MessageDeduper, _client_api, _effective_agent_id, _ws_handshake,
    _ws_recv_frame, _cmd_messages_poll, cmd_messages,
)


class _SocketWithCoalescedFrame:
    def __init__(self, response: bytes):
        self.response = response

    def sendall(self, _data):
        pass

    def recv(self, size):
        out, self.response = self.response[:size], self.response[size:]
        return out

    def settimeout(self, _timeout):
        pass

    def close(self):
        pass


class CliMessagesTests(unittest.TestCase):
    def setUp(self):
        # Messages-cursor state (see _cursor_state_path) defaults to
        # ~/.the_lab — point it at a throwaway directory so these tests
        # never touch the real home directory.
        self._state_tmp = TemporaryDirectory()
        self.addCleanup(self._state_tmp.cleanup)
        self.state_dir = self._state_tmp.name

    def test_url_env_is_used_but_explicit_url_wins_and_normalizes(self):
        args = Namespace(url=None, https=False, port=8123)
        with patch.dict(os.environ, {"THE_LAB_API_URL": "https://service.example/lab"}, clear=True):
            base, ctx = _client_api(args)
            self.assertEqual(base, "https://service.example/lab/api/v1")
            self.assertIsNone(ctx)  # remote TLS stays verified
            args.url = "http://override.example:9/api/v1/"
            self.assertEqual(_client_api(args)[0], "http://override.example:9/api/v1")

    def test_identity_precedence_includes_worktree_marker(self):
        with TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            marker = Path(tmp) / ".the_lab.agentid"
            marker.write_text("marker-id\n")
            old = Path.cwd()
            os.chdir(tmp)
            try:
                self.assertEqual(_effective_agent_id(), ("marker-id", "worktree marker", "marker-id"))
                os.environ["THE_LAB_AGENT_ID"] = "env-id"
                self.assertEqual(_effective_agent_id()[0:2], ("env-id", "environment"))
                self.assertEqual(_effective_agent_id("flag-id")[0:2], ("flag-id", "explicit"))
            finally:
                os.chdir(old)

    def test_deduper_suppresses_replayed_snapshot_and_is_bounded(self):
        deduper = _MessageDeduper(limit=2)
        self.assertEqual(deduper.fresh([{"id": 1}, {"id": 2}]), [{"id": 1}, {"id": 2}])
        self.assertEqual(deduper.fresh([{"id": 2}, {"id": 3}]), [{"id": 3}])
        # id 1 was evicted after the bounded window rolled forward.
        self.assertEqual(deduper.fresh([{"id": 1}]), [{"id": 1}])

    def test_handshake_preserves_coalesced_first_frame(self):
        import base64
        import hashlib

        key = b"x" * 16
        key64 = base64.b64encode(key).decode()
        accept = base64.b64encode(hashlib.sha1(
            (key64 + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
        ).digest()).decode()
        payload = b'{}'
        frame = bytes([0x81, len(payload)]) + payload
        raw = ("HTTP/1.1 101 Switching Protocols\r\n"
               f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode() + frame
        fake = _SocketWithCoalescedFrame(raw)
        with patch("socket.create_connection", return_value=fake), patch("os.urandom", return_value=key):
            ws = _ws_handshake("localhost", 80, "/ws", False, None)
        self.assertEqual(_ws_recv_frame(ws), (1, payload))

    def test_messages_ws_prints_before_acknowledging_delivery(self):
        socket_obj = MagicMock()
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b"{}"
        stdout = io.StringIO()
        event = {"type": "init", "messages": [{"id": 41, "text": "hello"}], "notifications": []}
        with patch.dict(os.environ, {"THE_LAB_AGENT_ID": "agent-x", "THE_LAB_STATE_DIR": self.state_dir}, clear=True), \
             patch("sys.argv", ["the-lab", "messages", "--once", "--url", "http://example.test/lab/api/v1"]), \
             patch("the_lab.cli._ws_handshake", return_value=socket_obj) as handshake, \
             patch("the_lab.cli._ws_recv_frame", return_value=(1, __import__("json").dumps(event).encode())), \
             patch("urllib.request.urlopen", return_value=response) as urlopen, \
             patch("sys.stdout", stdout):
            with self.assertRaises(SystemExit) as stopped:
                cmd_messages()
        self.assertEqual(stopped.exception.code, 0)
        self.assertIn('"id": 41', stdout.getvalue())
        self.assertIn("/lab/api/v1/messages/ws?", handshake.call_args.args[2])
        self.assertIn("ack=1", handshake.call_args.args[2])
        request = urlopen.call_args.args[0]
        self.assertTrue(request.full_url.endswith("/lab/api/v1/messages/41/delivered"))
        self.assertEqual(request.method, "POST")

    def test_peek_never_requests_or_sends_delivery_ack(self):
        socket_obj = MagicMock()
        stdout = io.StringIO()
        event = {"type": "init", "messages": [{"id": 42}], "notifications": []}
        with patch.dict(os.environ, {"THE_LAB_AGENT_ID": "agent-x", "THE_LAB_STATE_DIR": self.state_dir}, clear=True), \
             patch("sys.argv", ["the-lab", "messages", "--once", "--peek", "--url", "http://host/api/v1"]), \
             patch("the_lab.cli._ws_handshake", return_value=socket_obj) as handshake, \
             patch("the_lab.cli._ws_recv_frame", return_value=(1, __import__("json").dumps(event).encode())), \
             patch("urllib.request.urlopen") as urlopen, patch("sys.stdout", stdout):
            with self.assertRaises(SystemExit):
                cmd_messages()
        self.assertIn("peek=1", handshake.call_args.args[2])
        self.assertNotIn("ack=1", handshake.call_args.args[2])
        urlopen.assert_not_called()

    def test_messages_reconnect_replays_without_duplicate_stdout(self):
        socket_obj = MagicMock()
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b"{}"
        stdout = io.StringIO()
        payload = __import__("json").dumps({
            "type": "init", "messages": [{"id": 99, "text": "replay"}], "notifications": [],
        }).encode()
        outcomes = iter([(1, payload), ConnectionResetError(), (1, payload)])

        def receive(_sock):
            try:
                item = next(outcomes)
            except StopIteration:
                raise TimeoutError()
            if isinstance(item, BaseException):
                raise item
            return item

        with patch.dict(os.environ, {"THE_LAB_AGENT_ID": "agent-x", "THE_LAB_STATE_DIR": self.state_dir}, clear=True), \
             patch("sys.argv", ["the-lab", "messages", "--timeout", "0.02", "--url", "http://host/api/v1"]), \
             patch("the_lab.cli._ws_handshake", return_value=socket_obj) as handshake, \
             patch("the_lab.cli._ws_recv_frame", side_effect=receive), \
             patch("urllib.request.urlopen", return_value=response), patch("time.sleep"), \
             patch("sys.stdout", stdout):
            with self.assertRaises(SystemExit) as stopped:
                cmd_messages()
        self.assertEqual(stopped.exception.code, 0)
        self.assertGreaterEqual(handshake.call_count, 2)
        self.assertEqual(stdout.getvalue().count('"id": 99'), 1)

    def test_poll_fallback_stays_listening_and_deduplicates_batches(self):
        args = Namespace(peek=False, notifications=False, once=False, timeout=10, poll=1)
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b"{}"
        stdout = io.StringIO()
        batches = iter([
            {"messages": [{"id": 1, "text": "one"}]},
            {"messages": [{"id": 1, "text": "one"}, {"id": 2, "text": "two"}]},
            {"messages": []},
        ])
        # deadline setup, first loop check/sleep, second loop check/sleep,
        # then expiry after the third request.
        clocks = iter([0, 0, 0, 0, 0, 11])
        with patch.dict(os.environ, {"THE_LAB_STATE_DIR": self.state_dir}), \
             patch("the_lab.cli._api_get_json", side_effect=lambda *_: next(batches)), \
             patch("urllib.request.urlopen", return_value=response) as urlopen, \
             patch("time.monotonic", side_effect=lambda: next(clocks)), patch("time.sleep"), \
             patch("sys.stdout", stdout):
            with self.assertRaises(SystemExit) as stopped:
                _cmd_messages_poll(args, "http://host/api/v1", None, "agent-x", {"X-Agent-Id": "agent-x"})
        self.assertEqual(stopped.exception.code, 0)
        lines = stdout.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn('"id": 1', lines[0])
        self.assertIn('"id": 2', lines[1])
        self.assertNotIn('"id": 1', lines[1])
        self.assertEqual(urlopen.call_count, 3)  # 1, then replayed 1 + new 2

    def test_poll_fallback_peek_does_not_ack(self):
        args = Namespace(peek=True, notifications=False, once=True, timeout=10, poll=1)
        stdout = io.StringIO()
        with patch.dict(os.environ, {"THE_LAB_STATE_DIR": self.state_dir}), \
             patch("the_lab.cli._api_get_json", return_value={"messages": [{"id": 7}]}), \
             patch("urllib.request.urlopen") as urlopen, patch("sys.stdout", stdout):
            with self.assertRaises(SystemExit) as stopped:
                _cmd_messages_poll(args, "http://host/api/v1", None, "agent-x", {"X-Agent-Id": "agent-x"})
        self.assertEqual(stopped.exception.code, 0)
        self.assertIn('"id": 7', stdout.getvalue())
        urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
