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

    def test_message_event_preview_prefers_tldr(self):
        with patch("the_lab.ws.broadcaster.broadcast_soon") as broadcast:
            messages.add_message(
                self.repo, from_agent="b", from_role=None, to="agent:a",
                text="The detailed message body", tldr="Summary",
            )
        self.assertEqual(broadcast.call_args.args[0]["text"], "Summary")


class MessageRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(); self.repo = Path(self.tmp.name); reset_cache()
        app = FastAPI()
        @app.middleware("http")
        async def identity(request: Request, call_next):
            request.state.agent_id = request.headers.get("X-Agent-Id")
            request.state.auth_tier = request.headers.get("X-Test-Auth-Tier", "experiment")
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

    def test_tldr_is_trimmed_stored_and_returned(self):
        r = self.client.post("/api/v1/messages", headers=self.h(), json={
            "to": "agent:b", "text": "A detailed status update", "tldr": "  Status: ready  ",
        })
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["tldr"], "Status: ready")
        saved = messages.get_message(self.repo, r.json()["id"])
        self.assertEqual(saved["tldr"], "Status: ready")
        listed = self.client.get("/api/v1/messages", headers={
            "X-Test-Auth-Tier": "admin",
        }).json()["messages"]
        self.assertEqual(listed[0]["tldr"], "Status: ready")

    def test_tldr_absent_or_blank_is_omitted(self):
        absent = self.client.post("/api/v1/messages", headers=self.h(), json={
            "to": "agent:b", "text": "No summary",
        })
        blank = self.client.post("/api/v1/messages", headers=self.h(), json={
            "to": "agent:b", "text": "Blank summary", "tldr": " \n\t ",
        })
        self.assertEqual(absent.status_code, 201)
        self.assertEqual(blank.status_code, 201)
        self.assertNotIn("tldr", absent.json())
        self.assertNotIn("tldr", blank.json())
        self.assertNotIn("tldr", messages.get_message(self.repo, blank.json()["id"]))

    def test_tldr_too_long_is_a_clear_bad_request(self):
        r = self.client.post("/api/v1/messages", headers=self.h(), json={
            "to": "agent:b", "text": "Update", "tldr": "x" * 501,
        })
        self.assertEqual(r.status_code, 400)
        self.assertIn("at most 500 characters", r.json()["detail"])

    def test_anonymous_admin_dashboard_is_stamped_as_reserved_user(self):
        r = self.client.post("/api/v1/messages", headers={
            "X-The-Lab-Source": "dashboard", "X-Test-Auth-Tier": "admin",
        }, json={"to": "agent:a", "text": "from the UI"})
        self.assertEqual(r.status_code, 201)
        self.assertEqual((r.json()["from_agent"], r.json()["from_role"]), ("user", "user"))

        # Ordinary anonymous API calls remain unidentified.
        r = self.client.post("/api/v1/messages", headers={"X-Test-Auth-Tier": "admin"},
                             json={"to": "agent:a", "text": "plain API"})
        self.assertEqual((r.json()["from_agent"], r.json()["from_role"]), (None, None))

    def test_dashboard_header_requires_admin_and_never_overrides_an_agent(self):
        spoof = self.client.post("/api/v1/messages", headers={
            "X-The-Lab-Source": "dashboard", "X-Test-Auth-Tier": "experiment",
        }, json={"to": "agent:a", "text": "spoof"})
        self.assertEqual(spoof.status_code, 201)
        self.assertIsNone(spoof.json()["from_agent"])

        agent = self.client.post("/api/v1/messages", headers={
            "X-Agent-Id": "a", "X-The-Lab-Source": "dashboard", "X-Test-Auth-Tier": "admin",
        }, json={"to": "agent:b", "text": "agent"})
        self.assertEqual(agent.status_code, 201)
        self.assertEqual(agent.json()["from_agent"], "a")

        hidden = messages.add_message(self.repo, from_agent="b", from_role=None,
                                      to="agent:b", text="private")
        denied = self.client.post("/api/v1/messages", headers={
            "X-The-Lab-Source": "dashboard", "X-Test-Auth-Tier": "experiment",
        }, json={"to": "agent:a", "text": "spoof reply", "in_reply_to": hidden["id"]})
        self.assertEqual(denied.status_code, 400)

        intervention = self.client.post("/api/v1/messages", headers={
            "X-The-Lab-Source": "dashboard", "X-Test-Auth-Tier": "admin",
        }, json={"to": "agent:b", "text": "operator intervention", "in_reply_to": hidden["id"]})
        self.assertEqual(intervention.status_code, 201)
        self.assertEqual(intervention.json()["from_agent"], "user")
        self.assertEqual(intervention.json()["thread_id"], hidden["id"])

    def test_user_recipient_is_persistent_reserved_and_not_an_agent_inbox(self):
        r = self.client.post("/api/v1/messages", headers=self.h(),
                             json={"to": "user", "text": "reply"})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["to"], "user")
        self.assertEqual(r.json()["recipients"], [])
        self.assertFalse(messages.is_for(r.json(), agent_id="b", role="worker"))
        self.assertEqual(messages.unread_for(self.repo, agent_id="b", role="worker"), [])
        alias = messages.add_message(self.repo, from_agent="a", from_role=None,
                                     to="agent:user", text="alias")
        self.assertEqual(alias["to"], "user")

    def test_agent_thread_with_user_is_scoped_and_identified_admin_cannot_bypass(self):
        root = messages.add_message(self.repo, from_agent="user", from_role="user",
                                    to="agent:a", text="question")
        reply = self.client.post("/api/v1/messages", headers=self.h(), json={
            "to": "user", "text": "answer", "in_reply_to": root["id"],
        })
        self.assertEqual(reply.status_code, 201)
        self.assertEqual(reply.json()["thread_id"], root["id"])
        visible = self.client.get(f"/api/v1/messages?thread_id={root['id']}", headers=self.h())
        self.assertEqual({m["id"] for m in visible.json()["messages"]}, {root["id"], reply.json()["id"]})

        hidden = messages.add_message(self.repo, from_agent="user", from_role="user",
                                      to="agent:b", text="other")
        r = self.client.post("/api/v1/messages", headers={
            "X-Agent-Id": "a", "X-Test-Auth-Tier": "admin",
        }, json={"to": "user", "text": "snoop", "in_reply_to": hidden["id"]})
        self.assertEqual(r.status_code, 400)

    def test_deep_link_message_visibility_matches_operator_and_agent_scope(self):
        visible = messages.add_message(self.repo, from_agent="b", from_role=None,
                                       to="agent:a", text="visible")
        hidden = messages.add_message(self.repo, from_agent="b", from_role=None,
                                      to="agent:b", text="hidden")
        self.assertEqual(self.client.get(f"/api/v1/messages/{visible['id']}", headers=self.h()).status_code, 200)
        self.assertEqual(self.client.get(f"/api/v1/messages/{hidden['id']}", headers=self.h()).status_code, 403)
        self.assertEqual(self.client.get(f"/api/v1/messages/{hidden['id']}", headers={
            "X-The-Lab-Source": "dashboard", "X-Test-Auth-Tier": "admin",
        }).status_code, 200)


if __name__ == "__main__": unittest.main()


class ExperimentMentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(); self.repo = Path(self.tmp.name); reset_cache()
        self.addCleanup(self.tmp.cleanup); self.addCleanup(reset_cache)

    def test_text_shorthand_and_references_group_by_experiment_and_thread(self):
        root = messages.add_message(self.repo, from_agent="a", from_role=None, to="all",
                                    text="launched 1035.1/.2 (C) on 1026's head")
        messages.add_message(self.repo, from_agent="b", from_role=None, to="all",
                             text="exp/1035.2 final 28.19", thread_id=root["id"], in_reply_to=root["id"])
        other = messages.add_message(self.repo, from_agent="b", from_role=None, to="all",
                                     text="unrelated: 11035.1, 10351.1 and 1035.12 tokens",
                                     references=[{"kind": "experiment", "id": "1035.3"}])
        got = messages.experiment_mentions(self.repo, 1035, agent_id=None, role=None, operator=True)
        self.assertEqual(sorted(got), ["1035.1", "1035.12", "1035.2", "1035.3"])
        [t2] = got["1035.2"]
        self.assertEqual((t2["thread_id"], t2["count"], t2["first_id"]), (root["id"], 2, root["id"]))
        self.assertEqual(got["1035.3"][0]["thread_id"], other["id"])

    def test_agents_only_see_mentions_in_messages_they_can_see(self):
        messages.add_message(self.repo, from_agent="b", from_role=None, to="agent:c", text="private 7.1")
        self.assertEqual(messages.experiment_mentions(self.repo, 7, agent_id="a", role=None), {})
        self.assertIn("7.1", messages.experiment_mentions(self.repo, 7, agent_id="c", role=None))
