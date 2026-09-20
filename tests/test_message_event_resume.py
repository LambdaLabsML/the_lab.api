"""Exercise HTTP cursor contracts used by reconnecting MCP listeners."""
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from the_lab.routes.operational import router
from the_lab.ws import Broadcaster


class EventResumeTests(unittest.TestCase):
    def setUp(self):
        self.events = Broadcaster()
        self.events.broadcast({"type": "message_received", "id": 1})
        self.events.broadcast({"type": "experiment_finished", "label": "1.1"})
        self.events.broadcast({"type": "message_received", "id": 2})
        self.patch = patch("the_lab.ws.broadcaster", self.events)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)

    def test_explicit_resume_and_header(self):
        for url, headers in [
            ("?after_seq=1&timeout=0", {}),
            ("?timeout=0", {"Last-Event-ID": "1"}),
        ]:
            result = self.client.get("/api/v1/events" + url, headers=headers).json()
            self.assertEqual([e["seq"] for e in result["events"]], [2, 3])
            self.assertEqual(result["seq"], 3)

    def test_tail_does_not_replay_old_messages(self):
        result = self.client.get("/api/v1/events?tail=true&timeout=0").json()
        self.assertEqual(result["events"], [])
        self.assertEqual(result["seq"], 3)

    def test_filtered_empty_response_advances_cursor(self):
        result = self.client.get(
            "/api/v1/events?since=0&types=never&timeout=0.01").json()
        self.assertEqual(result["events"], [])
        self.assertEqual(result["seq"], 3)

    def test_explicit_cursor_wins_over_header(self):
        result = self.client.get("/api/v1/events?after_seq=3&timeout=0",
                                 headers={"Last-Event-ID": "0"}).json()
        self.assertEqual(result["events"], [])

    def test_bad_header_is_rejected(self):
        result = self.client.get("/api/v1/events?timeout=0",
                                 headers={"Last-Event-ID": "broken"})
        self.assertEqual(result.status_code, 422)
