"""Connect replay of the unread backlog is bounded, not dumped in full.

One real agent accumulated 1,448 unread messages and had its whole backlog
replayed on reconnect. A fresh (cursorless) connect must instead get a
bounded recent slice plus a summary of how many older messages exist, and a
``since=`` cursor must make subsequent connects/sweeps skip straight past
what was already seen instead of re-walking the backlog. Run as a
subprocess (like test_message_socket_delivery.py) to exercise the real app
module end to end, isolated from other tests' module-level lab state.
"""
import subprocess
import sys
import unittest


class MessageBacklogBoundingTests(unittest.TestCase):
    def test_cursorless_connect_is_bounded_with_a_backlog_note_then_since_skips_it(self):
        script = r'''
import os
from tempfile import TemporaryDirectory
with TemporaryDirectory() as tmp:
    os.environ["THE_LAB_REPO"] = tmp
    os.environ.pop("THE_LAB_USER", None)
    os.environ.pop("THE_LAB_PASSWORD", None)
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from the_lab import app as server, messages
    from the_lab.routes.messages import router
    server._AUTH_ENABLED = False
    server._DEMO = False
    app = FastAPI()

    @app.middleware("http")
    async def identify(request, call_next):
        request.state.agent_id = request.headers.get("X-Agent-Id")
        request.state.auth_tier = "admin"
        return await call_next(request)

    app.include_router(router)
    app.websocket("/api/v1/messages/ws")(server.messages_ws_endpoint)

    TOTAL = 35
    ids = [messages.add_message(server.REPO_DIR, from_agent="sender", from_role=None,
                                to="agent:reader", text=f"m{i}")["id"] for i in range(TOTAL)]

    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/messages/ws?agent_id=reader&ack=1") as ws:
            frame = ws.receive_json()
        assert frame["type"] == "init", frame
        assert len(frame["messages"]) == 20, frame
        assert [m["id"] for m in frame["messages"]] == list(reversed(ids[-20:])), frame
        assert frame.get("backlog", {}).get("older_unread") == TOTAL - 20, frame
        assert frame.get("cursor") == ids[-1], frame

        # Reconnecting with the reported cursor must not replay the bounded
        # recent slice again, and the older (never-delivered) backlog stays
        # untouched — explicit GET still sees it.
        with client.websocket_connect(
            f'/api/v1/messages/ws?agent_id=reader&ack=1&since={frame["cursor"]}'
        ) as ws2:
            frame2 = ws2.receive_json()
        assert frame2["messages"] == [], frame2
        assert "backlog" not in frame2, frame2

        # The older backlog was never claimed (ack mode defers claiming to
        # an explicit POST .../delivered) — it stays fully visible on
        # request, which is what lets the agent catch up on its own terms
        # instead of having it dumped through the socket.
        pending = client.get("/api/v1/messages?unread=1&peek=1&limit=50",
                             headers={"X-Agent-Id": "reader"}).json()
        assert pending["total"] == TOTAL, pending

    # Default (non-ack, non-peek) claim mode: the bounded recent slice IS
    # claimed on the snapshot (old behavior for that slice), but the older
    # backlog beyond it is left alone rather than claimed page-by-page.
    other_ids = [messages.add_message(server.REPO_DIR, from_agent="sender", from_role=None,
                                      to="agent:other", text=f"n{i}")["id"] for i in range(TOTAL)]
    with TestClient(app) as client2:
        with client2.websocket_connect("/api/v1/messages/ws?agent_id=other") as ws3:
            frame3 = ws3.receive_json()
        assert len(frame3["messages"]) == 20, frame3
        assert frame3.get("backlog", {}).get("older_unread") == TOTAL - 20, frame3
        still_undelivered = client2.get(
            "/api/v1/messages?undelivered=1&peek=1&limit=50", headers={"X-Agent-Id": "other"},
        ).json()
        assert still_undelivered["total"] == TOTAL - 20, still_undelivered
'''
        result = subprocess.run([sys.executable, "-c", script],
                                capture_output=True, text=True, timeout=25)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
