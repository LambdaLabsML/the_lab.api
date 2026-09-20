"""Focused cursor/resume tests for the MCP event-watch tools."""
import importlib.util
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch


_PATH = Path(__file__).parents[1] / "the_lab/agent_skills/skills/lab_api_mcp.py"
_SPEC = importlib.util.spec_from_file_location("lab_api_mcp_watch_test", _PATH)
mcp = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mcp)


class _MonkeyPatch:
    """Small pytest-monkeypatch subset so this focused test runs under unittest."""
    def __init__(self):
        self._undo = []

    def setattr(self, target, name, value):
        old = getattr(target, name)
        self._undo.append(lambda: setattr(target, name, old))
        setattr(target, name, value)

    def setenv(self, name, value):
        old = os.environ.get(name)
        self._undo.append(lambda: os.environ.pop(name, None) if old is None
                          else os.environ.__setitem__(name, old))
        os.environ[name] = value

    def undo(self):
        for undo in reversed(self._undo):
            undo()


class _Response:
    def __init__(self, data):
        self._data = json.dumps(data).encode()

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def test_implicit_matching_watch_resumes_and_explicit_zero_replays(monkeypatch):
    mcp._IMPLICIT_WATCH_CURSORS.clear()
    starts = []
    monkeypatch.setattr(mcp, "_current_event_cursor", lambda _deadline: 10)

    def watch(**kwargs):
        starts.append(kwargs["after_seq"])
        return {"type": "experiment_finished", "seq": kwargs["after_seq"] + 1}

    monkeypatch.setattr(mcp, "_watch", watch)
    assert json.loads(mcp._call_ws_tool("watch_events", {
        "types": ["experiment_finished"], "timeout": 1,
    }))["seq"] == 11
    mcp._call_ws_tool("watch_events", {"types": ["experiment_finished"], "timeout": 1})
    mcp._call_ws_tool("watch_events", {
        "types": ["experiment_finished"], "after_seq": 0, "timeout": 1,
    })
    assert starts == [10, 11, 0]


def test_longpoll_consumes_only_prefix_of_multi_match_batch(monkeypatch):
    requests = []
    responses = [
        {"events": [{"type": "done", "seq": 6}, {"type": "done", "seq": 7}], "seq": 7},
        {"events": [{"type": "done", "seq": 7}], "seq": 7},
    ]

    def urlopen(request, **_kwargs):
        requests.append(request.full_url)
        return _Response(responses.pop(0))

    monkeypatch.setattr(mcp.urllib.request, "urlopen", urlopen)
    deadline = __import__("time").monotonic() + 5
    first = mcp._events_longpoll(["done"], None, deadline, 5)
    second = mcp._events_longpoll(["done"], None, deadline, first["seq"])
    assert (first["seq"], second["seq"]) == (6, 7)
    assert "since=5" in requests[0]
    assert "since=6" in requests[1]


def test_longpoll_advances_past_filtered_event_and_keeps_agent_headers(monkeypatch):
    monkeypatch.setenv("THE_LAB_AGENT_ID", "agent-7")
    captured = {}

    def urlopen(request, **_kwargs):
        captured["headers"] = {k.lower(): v for k, v in request.header_items()}
        return _Response({"events": [
            {"type": "queue_changed", "seq": 6},
            {"type": "done", "seq": 7},
        ], "seq": 7})

    monkeypatch.setattr(mcp.urllib.request, "urlopen", urlopen)
    result = mcp._events_longpoll(["done"], None, __import__("time").monotonic() + 5, 5)
    assert result["seq"] == 7
    assert captured["headers"]["x-agent-id"] == "agent-7"
    assert captured["headers"]["x-mcp-proxy"] == "true"


def test_ws_reconnect_uses_consumed_cursor(monkeypatch):
    paths = []

    class Sock:
        def __init__(self, number):
            self.number = number

        def settimeout(self, _value):
            pass

        def close(self):
            pass

    def connect(_host, _port, path, _ssl):
        paths.append(path)
        return Sock(len(paths))

    def recv(sock):
        if sock.number == 1:
            if not getattr(sock, "seen", False):
                sock.seen = True
                return 1, json.dumps({"type": "other", "seq": 6}).encode()
            raise ConnectionResetError()
        return 1, json.dumps({"type": "done", "seq": 7}).encode()

    monkeypatch.setattr(mcp, "_ws_connect", connect)
    monkeypatch.setattr(mcp, "_ws_recv_frame", recv)
    result = mcp._watch(types=["done"], timeout=2, after_seq=5)
    assert result["seq"] == 7
    assert "since=5" in paths[0]
    assert "since=6" in paths[1]


def test_timeout_returns_cursor_after_nonmatching_event(monkeypatch):
    class Sock:
        def settimeout(self, _value):
            pass

        def close(self):
            pass

    calls = iter([
        (1, json.dumps({"type": "other", "seq": 9}).encode()),
        TimeoutError(), TimeoutError(),
    ])
    clock = iter([0, 0, 0, 0, 2])
    monkeypatch.setattr(mcp, "_ws_connect", lambda *_args: Sock())
    def recv(_sock):
        item = next(calls)
        if isinstance(item, Exception):
            raise item
        return item
    monkeypatch.setattr(mcp, "_ws_recv_frame", recv)
    with patch("time.monotonic", side_effect=lambda: next(clock)):
        result = mcp._watch(types=["done"], timeout=1, after_seq=5)
    assert result == {"timeout": True, "after_seq": 9}


class LabApiMcpWatchTests(unittest.TestCase):
    def _run(self, test):
        monkeypatch = _MonkeyPatch()
        try:
            test(monkeypatch)
        finally:
            monkeypatch.undo()

    def test_implicit_cursor(self):
        self._run(test_implicit_matching_watch_resumes_and_explicit_zero_replays)

    def test_longpoll_batch_prefix(self):
        self._run(test_longpoll_consumes_only_prefix_of_multi_match_batch)

    def test_longpoll_filter_and_headers(self):
        self._run(test_longpoll_advances_past_filtered_event_and_keeps_agent_headers)

    def test_reconnect_cursor(self):
        self._run(test_ws_reconnect_uses_consumed_cursor)

    def test_timeout_cursor(self):
        self._run(test_timeout_returns_cursor_after_nonmatching_event)


if __name__ == "__main__":
    unittest.main()
