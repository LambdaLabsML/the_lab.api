"""`the-lab messages` must flush each NDJSON line immediately when stdout is
a pipe, not just when it's a TTY.

An agent ran it as a background watcher through a pipe and saw zero output
for 30 minutes (buffered, then killed on timeout). This runs the actual CLI
as a real subprocess talking to a minimal hand-rolled WebSocket server over
a real socket, and reads stdout through a pipe line-by-line while the
process is still running — proving each line is visible before the next
event even arrives, not just after the process exits.
"""
import base64
import hashlib
import json
import os
import queue
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest


def _ws_accept_response(request_head: bytes) -> bytes:
    key = b""
    for line in request_head.split(b"\r\n"):
        if line.lower().startswith(b"sec-websocket-key:"):
            key = line.split(b":", 1)[1].strip()
    accept = base64.b64encode(
        hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest()
    ).decode()
    return (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
    ).encode()


def _ws_text_frame(obj) -> bytes:
    payload = json.dumps(obj).encode()
    assert len(payload) < 126, "test payload must fit a short WS frame"
    return bytes([0x81, len(payload)]) + payload  # unmasked server->client frame


def _serve_one_connection(server_sock, frames_with_delay):
    conn, _ = server_sock.accept()
    try:
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = conn.recv(4096)
            if not chunk:
                return
            buf += chunk
        head = buf.split(b"\r\n\r\n", 1)[0]
        conn.sendall(_ws_accept_response(head))
        for payload, delay in frames_with_delay:
            if delay:
                time.sleep(delay)
            conn.sendall(_ws_text_frame(payload))
        time.sleep(0.3)  # keep the socket open briefly so the client can read calmly
    finally:
        conn.close()


class CliMessagesPipeFlushTests(unittest.TestCase):
    def test_each_line_is_readable_through_a_pipe_before_the_next_event_arrives(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]

        frames = [
            ({"type": "init", "messages": [{"id": 1, "text": "hello"}], "notifications": []}, 0),
            ({"type": "message", "messages": [{"id": 2, "text": "world"}]}, 1.0),
        ]
        thread = threading.Thread(target=_serve_one_connection, args=(srv, frames), daemon=True)
        thread.start()
        self.addCleanup(srv.close)

        state_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(state_tmp.cleanup)
        env = {**os.environ, "THE_LAB_STATE_DIR": state_tmp.name}
        proc = subprocess.Popen(
            [sys.executable, "-m", "the_lab.cli", "messages",
             "--url", f"http://127.0.0.1:{port}/api/v1",
             "--peek", "--timeout", "5", "--agent-id", "pipe-test-agent"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1, env=env,
        )
        self.addCleanup(lambda: (proc.kill(), proc.wait()) if proc.poll() is None else None)

        lines: "queue.Queue[str]" = queue.Queue()

        def _reader():
            for line in proc.stdout:
                lines.put(line)

        reader_thread = threading.Thread(target=_reader, daemon=True)
        reader_thread.start()

        # The first line must show up almost immediately — if stdout were
        # buffered instead of flushed, this would block until the process
        # exits (or the whole timeout elapses) instead.
        first = lines.get(timeout=3)
        self.assertIn('"id": 1', first)

        # The second line only exists after the server's 1s delay; getting
        # it well under the --timeout/process-exit horizon, from a process
        # that is still running, confirms incremental (not exit-time) flush.
        t0 = time.monotonic()
        second = lines.get(timeout=3)
        self.assertLess(time.monotonic() - t0, 2.5)
        self.assertIn('"id": 2', second)

        proc.terminate()
        proc.wait(timeout=5)
        proc.stdout.close()


if __name__ == "__main__":
    unittest.main()
