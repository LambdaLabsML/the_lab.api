"""Focused compatibility and HTTP coverage for message acknowledgement state."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from the_lab import messages
from the_lab.routes import messages as routes


def reset_cache():
    messages._cache = messages._cache_path = None


class MessageStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(); self.repo = Path(self.tmp.name); reset_cache()
        self.addCleanup(self.tmp.cleanup); self.addCleanup(reset_cache)

    def test_legacy_read_by_is_delivered(self):
        (self.repo / ".the_lab").mkdir()
        (self.repo / ".the_lab/messages.json").write_text(json.dumps({"next_id": 2, "messages": [{"id": 1, "to": "agent:a", "from_agent": "b", "text": "old", "created_at": "2020-01-01T00:00:00+00:00", "read_by": ["a"]}]}))
        self.assertEqual(messages.undelivered_for(self.repo, agent_id="a", role=None), [])
        self.assertEqual(messages.unread_for(self.repo, agent_id="a", role=None), [])

    def test_notification_delivery_and_read_are_separate(self):
        msg = messages.add_message(self.repo, from_agent="b", from_role=None, to="agent:a", text="hello")
        self.assertEqual([x["id"] for x in messages.claim_notifications_for(self.repo, agent_id="a", role=None)], [msg["id"]])
        self.assertEqual(messages.get_message(self.repo, msg["id"])["read_by"], [])
        self.assertEqual([x["id"] for x in messages.claim_unread_for(self.repo, agent_id="a", role=None)], [msg["id"]])
        saved = messages.get_message(self.repo, msg["id"])
        self.assertEqual(saved["delivered_by"], ["a"]); self.assertEqual(saved["read_by"], [])

    def test_registration_cutoff_and_explicit_history(self):
        old = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        with patch.object(messages, "_created_at_for", return_value=datetime.now(timezone.utc).isoformat()):
            with messages._lock:
                data = messages._read(self.repo); data["messages"] = [
                    {"id": 1, "to": "all", "from_agent": "b", "text": "old", "created_at": old, "read_by": []},
                    {"id": 2, "to": "agent:a", "from_agent": "b", "text": "dm", "created_at": old, "read_by": []}]
                data["next_id"] = 3; messages._write(self.repo, data)
            self.assertEqual([x["id"] for x in messages.undelivered_for(self.repo, agent_id="a", role="x")], [2])
            self.assertEqual({x["id"] for x in messages.unread_for(self.repo, agent_id="a", role="x")}, {1, 2})

    def test_bounded_mark_all(self):
        one = messages.add_message(self.repo, from_agent="b", from_role=None, to="agent:a", text="one")
        two = messages.add_message(self.repo, from_agent="b", from_role=None, to="agent:a", text="two")
        self.assertEqual(messages.mark_all_read(self.repo, "a", None, before_id=two["id"]), 1)
        self.assertEqual(messages.get_message(self.repo, one["id"])["read_by"], ["a"])
        self.assertEqual(messages.get_message(self.repo, two["id"])["read_by"], [])


class MessageRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(); self.repo = Path(self.tmp.name); reset_cache()
        app = FastAPI()
        @app.middleware("http")
        async def identity(request: Request, call_next):
            request.state.agent_id = request.headers.get("X-Agent-Id")
            request.state.auth_tier = "experiment"
            return await call_next(request)
        app.include_router(routes.router); self.client = TestClient(app)
        lookup = lambda _repo, aid: {"agent_id": aid, "role": "worker"}
        self.patches = [patch.object(routes, "REPO_DIR", self.repo), patch.object(routes.agents_mod, "lookup_agent", side_effect=lookup), patch.object(routes.agents_mod, "list_agents", return_value=[]), patch.object(routes.agents_mod, "read_listening", return_value={}), patch.object(routes.agents_mod, "note_message_poll")]
        for p in self.patches: p.start(); self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup); self.addCleanup(reset_cache)

    def h(self, aid="a"): return {"X-Agent-Id": aid}

    def test_scoped_empty_thread_never_leaks_history(self):
        private = messages.add_message(self.repo, from_agent="b", from_role=None, to="agent:b", text="private")
        r = self.client.get(f"/api/v1/messages?thread_id={private['id']}", headers=self.h())
        self.assertEqual(r.status_code, 200); self.assertEqual(r.json()["messages"], [])

    def test_reply_inherits_thread_and_hidden_parent_rejected(self):
        parent = messages.add_message(self.repo, from_agent="b", from_role=None, to="agent:a", text="parent")
        r = self.client.post("/api/v1/messages", headers=self.h(), json={"to":"agent:b", "text":"reply", "in_reply_to":parent["id"]})
        self.assertEqual(r.status_code, 201); self.assertEqual(r.json()["thread_id"], parent["id"])
        hidden = messages.add_message(self.repo, from_agent="b", from_role=None, to="agent:b", text="hidden")
        r = self.client.post("/api/v1/messages", headers=self.h(), json={"to":"agent:b", "text":"no", "in_reply_to":hidden["id"]})
        self.assertEqual(r.status_code, 400)

    def test_read_all_optional_bounded_and_get_delivers_not_reads(self):
        one = messages.add_message(self.repo, from_agent="b", from_role=None, to="agent:a", text="one")
        self.assertEqual(self.client.get("/api/v1/messages", headers=self.h()).status_code, 200)
        saved = messages.get_message(self.repo, one["id"])
        self.assertEqual(saved["delivered_by"], ["a"]); self.assertEqual(saved["read_by"], [])
        two = messages.add_message(self.repo, from_agent="b", from_role=None, to="agent:a", text="two")
        r = self.client.post(f"/api/v1/messages/read_all?before_id={two['id']}", headers=self.h())
        self.assertEqual(r.status_code, 200); self.assertEqual(r.json()["marked_read"], 1)
        self.assertEqual(messages.get_message(self.repo, two["id"])["read_by"], [])
        self.assertEqual(self.client.post("/api/v1/messages/read_all", headers=self.h()).status_code, 200)
        self.assertEqual(messages.get_message(self.repo, two["id"])["read_by"], ["a"])


if __name__ == "__main__": unittest.main()
