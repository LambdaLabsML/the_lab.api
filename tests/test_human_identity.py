"""Named human identities: user:<name> addressing, as_user posting, humans listing."""
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


class HumanHelperTests(unittest.TestCase):
    def test_is_human(self):
        self.assertTrue(messages.is_human("user"))
        self.assertTrue(messages.is_human("user:david"))
        self.assertFalse(messages.is_human("agent:david"))
        self.assertFalse(messages.is_human("userx"))
        self.assertFalse(messages.is_human(None))

    def test_human_id_validates_slug(self):
        self.assertEqual(messages.human_id("David"), "user:david")
        self.assertEqual(messages.human_id(" dave_2 "), "user:dave_2")
        for bad in ("", "-dave", "D@ve", "a" * 33, "Da ve"):
            with self.assertRaises(ValueError):
                messages.human_id(bad)


class HumanRouteTests(unittest.TestCase):
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
        self.patches = [
            patch.object(routes, "REPO_DIR", self.repo),
            patch.object(routes.agents_mod, "lookup_agent", side_effect=lookup),
            patch.object(routes.agents_mod, "list_agents", return_value=[]),
            patch.object(routes.agents_mod, "read_listening", return_value={}),
            patch.object(routes.agents_mod, "note_message_poll"),
        ]
        for p in self.patches: p.start(); self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup); self.addCleanup(reset_cache)

    def h(self, aid="a"): return {"X-Agent-Id": aid}
    def dash(self): return {"X-The-Lab-Source": "dashboard", "X-Test-Auth-Tier": "admin"}

    # -- addressing -----------------------------------------------------

    def test_agent_can_address_a_named_human(self):
        r = self.client.post("/api/v1/messages", headers=self.h(),
                             json={"to": "user:david", "text": "hi David"})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["to"], "user:david")
        self.assertEqual(r.json()["recipients"], [])

    def test_invalid_human_name_rejected(self):
        r = self.client.post("/api/v1/messages", headers=self.h(),
                             json={"to": "user:Not Valid!", "text": "hi"})
        self.assertEqual(r.status_code, 400)

    def test_catch_all_user_reaches_every_human_including_named(self):
        root = messages.add_message(self.repo, from_agent="a", from_role=None,
                                    to="user", text="broadcast to humans")
        self.assertTrue(messages.is_for(root, agent_id="user", role=None))
        self.assertTrue(messages.is_for(root, agent_id="user:david", role=None))
        self.assertFalse(messages.is_for(root, agent_id="a", role=None))

    def test_named_human_only_reaches_that_name(self):
        root = messages.add_message(self.repo, from_agent="a", from_role=None,
                                    to="user:david", text="hi David")
        self.assertTrue(messages.is_for(root, agent_id="user:david", role=None))
        self.assertFalse(messages.is_for(root, agent_id="user:erin", role=None))
        self.assertFalse(messages.is_for(root, agent_id="user", role=None))

    # -- sending as a named human ----------------------------------------

    def test_dashboard_can_send_as_named_human(self):
        r = self.client.post("/api/v1/messages", headers=self.dash(),
                             json={"to": "agent:a", "text": "hi", "as_user": "David"})
        self.assertEqual(r.status_code, 201)
        self.assertEqual((r.json()["from_agent"], r.json()["from_role"]), ("user:david", "user:david"))

    def test_dashboard_without_as_user_still_gets_catch_all(self):
        r = self.client.post("/api/v1/messages", headers=self.dash(),
                             json={"to": "agent:a", "text": "hi"})
        self.assertEqual((r.json()["from_agent"], r.json()["from_role"]), ("user", "user"))

    def test_agent_cannot_send_as_a_human(self):
        r = self.client.post("/api/v1/messages", headers=self.h(aid="a"),
                             json={"to": "agent:b", "text": "spoof", "as_user": "david"})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["from_agent"], "a")  # as_user silently ignored, not honored

    def test_experiment_tier_with_dashboard_header_cannot_claim_as_user(self):
        r = self.client.post("/api/v1/messages", headers={
            "X-The-Lab-Source": "dashboard", "X-Test-Auth-Tier": "experiment",
        }, json={"to": "agent:a", "text": "spoof", "as_user": "david"})
        self.assertEqual(r.status_code, 201)
        self.assertIsNone(r.json()["from_agent"])

    def test_bad_as_user_name_rejected(self):
        r = self.client.post("/api/v1/messages", headers=self.dash(),
                             json={"to": "agent:a", "text": "hi", "as_user": "Not Valid!"})
        self.assertEqual(r.status_code, 400)

    # -- threads: creation, join/leave/members/update as a named human --

    def test_create_thread_as_named_human(self):
        r = self.client.post("/api/v1/threads", headers=self.dash(),
                             json={"title": "t", "text": "hi", "as_user": "david"})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["message"]["from_agent"], "user:david")
        self.assertEqual(r.json()["thread"]["created_by"], "user:david")

    def test_thread_members_accepts_named_humans_without_unknown_hint(self):
        r = self.client.post("/api/v1/threads", headers=self.h(aid="a"),
                             json={"title": "t", "text": "hi", "members": ["user:david"]})
        self.assertEqual(r.status_code, 201)
        self.assertNotIn("hints", r.json())

    def test_join_leave_as_named_human(self):
        created = self.client.post("/api/v1/threads", headers=self.h(aid="a"),
                                   json={"title": "t", "text": "hi", "visibility": "public"})
        tid = created.json()["thread"]["id"]
        joined = self.client.post(f"/api/v1/threads/{tid}/join?as_user=david", headers=self.dash())
        self.assertEqual(joined.status_code, 200)
        self.assertTrue(joined.json()["joined"])
        meta = messages.thread_meta(self.repo, tid)
        self.assertIn("user:david", meta["members"])
        left = self.client.post(f"/api/v1/threads/{tid}/leave?as_user=david", headers=self.dash())
        self.assertEqual(left.status_code, 200)
        self.assertNotIn("user:david", messages.thread_meta(self.repo, tid)["members"])

    def test_update_thread_as_named_human_attributes_closing_message(self):
        created = self.client.post("/api/v1/threads", headers=self.dash(),
                                   json={"title": "t", "text": "hi", "as_user": "david"})
        tid = created.json()["thread"]["id"]
        r = self.client.patch(f"/api/v1/threads/{tid}", headers=self.dash(),
                              json={"status": "closed", "conclusion": "done", "as_user": "david"})
        self.assertEqual(r.status_code, 200)
        thread = self.client.get(f"/api/v1/threads/{tid}", headers=self.dash())
        closing = [m for m in thread.json()["messages"] if "Thread closed" in m["text"]][0]
        self.assertEqual(closing["from_agent"], "user:david")

    # -- humans listing ---------------------------------------------------

    def test_list_humans_collects_senders_recipients_and_members(self):
        messages.add_message(self.repo, from_agent="user:david", from_role=None,
                             to="agent:a", text="hi")
        messages.add_message(self.repo, from_agent="a", from_role=None,
                             to="user:erin", text="hi erin")
        messages.create_thread(self.repo, from_agent="a", from_role=None,
                               title="t", text="hi", members=["user:fay"])
        r = self.client.get("/api/v1/humans", headers=self.h(aid="a"))
        self.assertEqual(r.status_code, 200)
        ids = {h["id"] for h in r.json()["humans"]}
        self.assertEqual(ids, {"user:david", "user:erin", "user:fay"})
        by_id = {h["id"]: h for h in r.json()["humans"]}
        self.assertEqual(by_id["user:david"]["name"], "david")

    def test_list_humans_includes_catch_all_when_seen(self):
        messages.add_message(self.repo, from_agent="user", from_role=None, to="agent:a", text="hi")
        r = self.client.get("/api/v1/humans", headers=self.h(aid="a"))
        by_id = {h["id"]: h for h in r.json()["humans"]}
        self.assertIsNone(by_id["user"]["name"])

    def test_list_humans_requires_identification(self):
        r = self.client.get("/api/v1/humans")
        self.assertEqual(r.status_code, 403)


if __name__ == "__main__": unittest.main()
