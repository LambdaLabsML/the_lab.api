"""Threads as shared spaces: discovery, joining, delivery, search, suggestions."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from the_lab import messages
from the_lab.routes import agents as agent_routes
from the_lab.routes import ideas as idea_routes
from the_lab.routes import messages as routes
from the_lab.store import Store


def reset_cache():
    messages._cache = messages._cache_path = None


class ThreadRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(); self.repo = Path(self.tmp.name); reset_cache()
        self.addCleanup(self.tmp.cleanup); self.addCleanup(reset_cache)
        self.store = Store(self.repo)
        idea = self.store.create_idea("baseline", [], "main"); self.store.save_idea(idea)
        self.idea_id = idea["id"]
        self.exp = self.store.create_experiment(self.idea_id, "run")["label"]
        app = FastAPI()

        @app.middleware("http")
        async def identity(request: Request, call_next):
            request.state.agent_id = request.headers.get("X-Agent-Id")
            request.state.auth_tier = request.headers.get("X-Test-Auth-Tier", "experiment")
            return await call_next(request)
        app.include_router(routes.router); app.include_router(idea_routes.router, prefix="")
        self.client = TestClient(app)
        self.registered = {"a", "b", "c"}
        lookup = lambda _repo, aid: {"agent_id": aid, "role": "worker"} if aid in self.registered else None
        for p in [
            patch.object(routes, "REPO_DIR", self.repo), patch.object(routes, "store", self.store),
            patch.object(idea_routes, "REPO_DIR", self.repo), patch.object(idea_routes, "store", self.store),
            patch.object(routes.agents_mod, "lookup_agent", side_effect=lookup),
            patch.object(routes.agents_mod, "list_agents", return_value=[]),
            patch.object(routes.agents_mod, "read_listening", return_value={}),
            patch.object(routes.agents_mod, "note_message_poll"),
            patch("the_lab.ws.broadcaster.broadcast_soon"),
        ]:
            p.start(); self.addCleanup(p.stop)

    def h(self, aid="a"): return {"X-Agent-Id": aid}

    def op(self): return {"X-Test-Auth-Tier": "admin", "X-The-Lab-Source": "dashboard"}

    def thread(self, aid="a", **body):
        body = {"title": "KV cache OOM", "text": "seeing OOMs", **body}
        r = self.client.post("/api/v1/threads", json=body, headers=self.h(aid))
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()

    def inbox(self, aid):
        return [m["id"] for m in self.client.get(
            "/api/v1/messages?undelivered=1", headers=self.h(aid)).json()["messages"]]

    # --- discovery & joining -------------------------------------------------

    def test_public_thread_is_findable_readable_and_joinable(self):
        tid = self.thread()["thread"]["id"]
        self.client.post("/api/v1/messages", json={"thread_id": tid, "text": "more detail"}, headers=self.h("a"))
        listed = self.client.get("/api/v1/threads?q=oom", headers=self.h("b")).json()
        self.assertEqual([t["id"] for t in listed["threads"]], [tid])
        self.assertFalse(listed["threads"][0]["joined"])
        # Full history is readable before joining.
        got = self.client.get(f"/api/v1/threads/{tid}", headers=self.h("b")).json()
        self.assertEqual(len(got["messages"]), 2)
        # Not a member yet: thread posts are not delivered to b.
        self.assertEqual(self.inbox("b"), [])
        self.assertTrue(self.client.post(f"/api/v1/threads/{tid}/join", headers=self.h("b")).json()["joined"])
        sent = self.client.post("/api/v1/messages", json={"thread_id": tid, "text": "new"}, headers=self.h("a")).json()
        self.assertEqual(sent["to"], f"thread:{tid}")
        self.assertEqual(self.inbox("b"), [sent["id"]])
        # Messages from before b joined are history, not deliveries.
        self.assertNotIn(tid, self.inbox("b"))

    def test_posting_joins_and_mute_stops_delivery(self):
        tid = self.thread()["thread"]["id"]
        self.client.post("/api/v1/messages", json={"thread_id": tid, "text": "me too"}, headers=self.h("c"))
        sent = self.client.post("/api/v1/messages", json={"thread_id": tid, "text": "ack"}, headers=self.h("a")).json()
        self.assertIn(sent["id"], self.inbox("c"))
        self.client.post(f"/api/v1/threads/{tid}/join?muted=1", headers=self.h("c"))
        sent2 = self.client.post("/api/v1/messages", json={"thread_id": tid, "text": "x"}, headers=self.h("a")).json()
        self.assertNotIn(sent2["id"], self.inbox("c"))
        # Muted members still read it.
        self.assertEqual(self.client.get(f"/api/v1/threads/{tid}", headers=self.h("c")).status_code, 200)
        self.client.post(f"/api/v1/threads/{tid}/leave", headers=self.h("c"))
        self.assertFalse(self.client.get(f"/api/v1/threads/{tid}", headers=self.h("c")).json()["joined"])

    def test_private_thread_is_members_only(self):
        tid = self.thread(visibility="private", members=["b"])["thread"]["id"]
        self.assertEqual(self.client.get("/api/v1/threads", headers=self.h("c")).json()["total"], 0)
        self.assertEqual(self.client.get(f"/api/v1/threads/{tid}", headers=self.h("c")).status_code, 403)
        self.assertEqual(self.client.post(f"/api/v1/threads/{tid}/join", headers=self.h("c")).status_code, 403)
        r = self.client.post("/api/v1/messages", json={"thread_id": tid, "text": "hi"}, headers=self.h("c"))
        self.assertEqual(r.status_code, 400)
        # The opening message reached invited member b.
        self.assertIn(tid, self.inbox("b"))
        # Members can add others; non-members cannot.
        self.assertEqual(self.client.post(f"/api/v1/threads/{tid}/members", json={"agent_ids": ["c"]}, headers=self.h("c")).status_code, 403)
        self.client.post(f"/api/v1/threads/{tid}/members", json={"agent_ids": ["c"]}, headers=self.h("b"))
        self.assertEqual(self.client.get(f"/api/v1/threads/{tid}", headers=self.h("c")).status_code, 200)

    def test_legacy_threads_derive_visibility_from_their_root(self):
        bcast = messages.add_message(self.repo, from_agent="a", from_role="w", to="all", text="announce")
        messages.add_message(self.repo, from_agent="b", from_role="w", to="all", text="re", in_reply_to=bcast["id"], thread_id=bcast["id"])
        dm = messages.add_message(self.repo, from_agent="a", from_role="w", to="agent:b", text="secret")
        messages.add_message(self.repo, from_agent="b", from_role="w", to="agent:a", text="re", in_reply_to=dm["id"], thread_id=dm["id"])
        ids = {t["id"]: t for t in self.client.get("/api/v1/threads", headers=self.h("c")).json()["threads"]}
        self.assertIn(bcast["id"], ids); self.assertNotIn(dm["id"], ids)
        self.assertEqual(ids[bcast["id"]]["title"], "announce")
        # c can join the public legacy thread and is then delivered thread posts.
        self.assertEqual(self.client.post(f"/api/v1/threads/{bcast['id']}/join", headers=self.h("c")).status_code, 200)
        m = self.client.post("/api/v1/messages", json={"thread_id": bcast["id"], "text": "to members"}, headers=self.h("a")).json()
        self.assertIn(m["id"], self.inbox("c"))
        self.assertEqual(self.client.post(f"/api/v1/threads/{dm['id']}/join", headers=self.h("c")).status_code, 403)

    def test_one_off_broadcasts_are_not_listed_as_threads(self):
        messages.add_message(self.repo, from_agent="a", from_role="w", to="all", text="fyi")
        self.assertEqual(self.client.get("/api/v1/threads", headers=self.h("b")).json()["total"], 0)

    def test_close_posts_conclusion_and_filters_listing(self):
        tid = self.thread()["thread"]["id"]
        self.client.post(f"/api/v1/threads/{tid}/join", headers=self.h("b"))
        self.assertEqual(self.client.patch(f"/api/v1/threads/{tid}", json={"summary": "s"}, headers=self.h("c")).status_code, 403)
        meta = self.client.patch(f"/api/v1/threads/{tid}", json={"status": "closed", "conclusion": "fixed"}, headers=self.h("a")).json()
        self.assertEqual(meta["status"], "closed")
        self.assertEqual(self.client.get("/api/v1/threads", headers=self.h("b")).json()["total"], 0)
        self.assertEqual(self.client.get("/api/v1/threads?status=closed", headers=self.h("b")).json()["total"], 1)
        last = self.client.get(f"/api/v1/threads/{tid}", headers=self.h("b")).json()["messages"][-1]
        self.assertIn("Conclusion: fixed", last["text"])
        r = self.client.post("/api/v1/messages", json={"thread_id": tid, "text": "late"}, headers=self.h("b")).json()
        self.assertTrue(any("closed" in h for h in r["hints"]))

    # --- search, references, hints --------------------------------------------

    def test_search_respects_visibility_and_references(self):
        tid = self.thread(text=f"exp {self.exp} OOMs at 200k context")["thread"]["id"]
        messages.add_message(self.repo, from_agent="a", from_role="w", to="agent:b", text="private OOM note")
        res = self.client.get("/api/v1/messages/search?q=oom", headers=self.h("c")).json()
        self.assertEqual([r["id"] for r in res["results"]], [tid])
        self.assertIn("OOMs", res["results"][0]["snippet"])
        by_ref = self.client.get(f"/api/v1/messages/search?ref=experiment:{self.exp}", headers=self.h("c")).json()
        self.assertEqual(by_ref["total"], 1)
        self.assertEqual(self.client.get("/api/v1/messages/search?q=oom", headers=self.h("b")).json()["total"], 2)
        self.assertEqual(self.client.get("/api/v1/messages/search", headers=self.h("b")).status_code, 400)

    def test_search_accepts_query_as_alias_for_q(self):
        tid = self.thread(text=f"exp {self.exp} OOMs at 200k context")["thread"]["id"]
        res = self.client.get("/api/v1/messages/search?query=oom", headers=self.h("c")).json()
        self.assertEqual([r["id"] for r in res["results"]], [tid])
        # Both given: q wins (it's checked first), but either alone works.
        res2 = self.client.get("/api/v1/messages/search?q=oom&query=unrelated-noise", headers=self.h("c")).json()
        self.assertEqual([r["id"] for r in res2["results"]], [tid])

    def test_get_thread_pages_by_recency_with_before_id_and_fields(self):
        tid = self.thread()["thread"]["id"]
        ids = [tid]
        for i in range(5):
            m = self.client.post("/api/v1/messages", json={"thread_id": tid, "text": f"m{i}"},
                                 headers=self.h("a")).json()
            ids.append(m["id"])
        # Newest-3 default page.
        page1 = self.client.get(f"/api/v1/threads/{tid}?limit=3", headers=self.h("a")).json()
        self.assertEqual([m["id"] for m in page1["messages"]], ids[-3:])
        self.assertIn("next_before_id", page1)
        self.assertEqual(page1["next_before_id"], ids[-3])
        # Page further back with before_id — stable, chronological.
        page2 = self.client.get(
            f"/api/v1/threads/{tid}?limit=3&before_id={page1['next_before_id']}", headers=self.h("a"),
        ).json()
        self.assertEqual([m["id"] for m in page2["messages"]], ids[:3])
        self.assertNotIn("next_before_id", page2)
        # Legacy offset=0 still returns the newest page (old behavior kept).
        legacy = self.client.get(f"/api/v1/threads/{tid}?limit=3&offset=0", headers=self.h("a")).json()
        self.assertEqual([m["id"] for m in legacy["messages"]], ids[-3:])
        self.assertEqual(legacy["offset"], 0)
        # fields=tldr gives compact previews, not full text.
        compact = self.client.get(f"/api/v1/threads/{tid}?limit=3&fields=tldr", headers=self.h("a")).json()
        self.assertEqual([m["id"] for m in compact["messages"]], ids[-3:])
        self.assertIn("preview", compact["messages"][0])
        self.assertNotIn("text", compact["messages"][0])
        self.assertEqual(self.client.get(f"/api/v1/threads/{tid}?fields=bogus", headers=self.h("a")).status_code, 400)

    def test_mentions_become_references_only_when_they_exist(self):
        r = self.client.post("/api/v1/messages", json={
            "to": "all", "text": f"exp {self.exp} beats idea {self.idea_id}; exp 99.9 and idea 999 don't exist"},
            headers=self.h("a")).json()
        self.assertEqual(r["references"], [{"kind": "experiment", "id": self.exp}, {"kind": "idea", "id": self.idea_id}])

    def test_send_hints(self):
        r = self.client.post("/api/v1/messages", json={"to": "all", "text": "hello"}, headers=self.h("a")).json()
        self.assertTrue(any("thread" in h for h in r["hints"]))
        r = self.client.post("/api/v1/messages", json={"to": "agent:ghost", "text": "hi"}, headers=self.h("a")).json()
        self.assertTrue(any("ghost" in h for h in r["hints"]))
        r = self.client.post("/api/v1/messages", json={"to": "agent:b", "text": "hi"}, headers=self.h("a")).json()
        self.assertNotIn("hints", r)
        self.assertEqual(self.client.post("/api/v1/messages", json={"text": "no target"}, headers=self.h("a")).status_code, 400)

    def test_digest_lists_open_threads(self):
        self.assertEqual(messages.threads_digest(self.repo, agent_id="b", role="w"), {})
        tid = self.thread()["thread"]["id"]
        digest = messages.threads_digest(self.repo, agent_id="b", role="w")
        self.assertEqual(digest["active_threads"][0]["id"], tid)
        self.assertIn("join", digest["active_threads_hint"])

    def test_announced_thread_notifies_everyone_once(self):
        tid = self.thread(announce=True)["thread"]["id"]
        self.assertIn(tid, self.inbox("b"))
        self.assertIn(tid, self.inbox("c"))
        with self.assertRaises(ValueError):
            messages.create_thread(self.repo, from_agent="a", from_role="w", title="t", text="x",
                                   visibility="private", announce=True)

    # --- suggestions as messages ---------------------------------------------

    def test_suggestion_opens_announced_thread_and_abandon_closes_it(self):
        r = self.client.post("/api/v1/ideas/suggest", json={"description": "Try cosine LR", "priority": "high"},
                             headers=self.op())
        self.assertEqual(r.status_code, 201, r.text)
        idea = r.json()
        tid = idea["thread_id"]
        meta = messages.thread_meta(self.repo, tid)
        self.assertEqual((meta["kind"], meta["idea_id"], meta["visibility"]), ("suggestion", idea["id"], "public"))
        root = messages.get_message(self.repo, tid)
        self.assertEqual((root["from_agent"], root["to"]), ("user", "all"))
        self.assertIn("Priority: HIGH", root["text"])
        self.assertIn(tid, self.inbox("b"))
        listed = self.client.get("/api/v1/threads?kind=suggestion", headers=self.h("b")).json()
        self.assertEqual(listed["threads"][0]["idea_id"], idea["id"])
        r = self.client.post(f"/api/v1/ideas/{idea['id']}/abandon", json={"reason": "superseded"}, headers=self.h("b"))
        self.assertEqual(r.status_code, 200, r.text)
        meta = messages.thread_meta(self.repo, tid)
        self.assertEqual(meta["status"], "closed")
        self.assertIn("superseded", meta["conclusion"])


if __name__ == "__main__":
    unittest.main()
