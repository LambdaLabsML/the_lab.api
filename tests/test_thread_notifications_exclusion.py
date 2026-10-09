"""get_thread responses are excluded from the general _notifications
injection in the response middleware.

A single thread's history can already be the largest response an agent
fetches; padding it with notifications it will see again on its next
ordinary call added size for no benefit, so the middleware special-cases
GET /api/v1/threads/{id}. Run as a subprocess (like
test_message_socket_delivery.py) so the real app module's middleware stack
is exercised end to end, isolated from the module-level lab state other
tests touch.
"""
import subprocess
import sys
import unittest


class ThreadNotificationsExclusionTests(unittest.TestCase):
    def test_get_thread_has_no_notifications_but_other_endpoints_do(self):
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

    app.middleware("http")(server.inject_notifications)
    app.include_router(router)

    meta, root = messages.create_thread(
        server.REPO_DIR, from_agent="a", from_role="worker", title="t", text="hello",
    )
    # A message addressed to "a" so build_notifications has something real
    # to report — proves the exclusion is deliberate, not just an empty list.
    messages.add_message(server.REPO_DIR, from_agent="b", from_role=None,
                          to="agent:a", text="ping")

    with TestClient(app) as client:
        thread_resp = client.get(f"/api/v1/threads/{meta['id']}", headers={"X-Agent-Id": "a"})
        assert thread_resp.status_code == 200, thread_resp.text
        assert "_notifications" not in thread_resp.json(), thread_resp.json()

        # A dict-shaped endpoint that does NOT itself deliver the pending
        # "ping" message (unlike GET /api/v1/messages, which would consume
        # it into its own body and so legitimately have nothing left to
        # piggyback) still gets the general _notifications injection.
        list_resp = client.get("/api/v1/threads", headers={"X-Agent-Id": "a"})
        assert list_resp.status_code == 200, list_resp.text
        assert "_notifications" in list_resp.json(), list_resp.json()
'''
        result = subprocess.run([sys.executable, "-c", script],
                                capture_output=True, text=True, timeout=25)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
