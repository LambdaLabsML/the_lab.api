"""Full-history personal inbox, thread cursors, and anchored conversation reads."""
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


class DashboardInboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        messages._cache = messages._cache_path = None
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.reset)

    def reset(self):
        messages._cache = messages._cache_path = None

    def msg(self, ident, sender="agent:a", to="all", text="update", thread=None, reply=None):
        return {"id": ident, "from_agent": sender, "from_role": None, "to": to,
                "text": text, "tldr": None, "thread_id": thread or ident,
                "in_reply_to": reply, "read_by": [], "delivered_to": [],
                "created_at": (datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=ident)).isoformat()}

    def seed(self, rows, metadata=None):
        (self.repo / ".the_lab").mkdir(exist_ok=True)
        (self.repo / ".the_lab/messages.json").write_text(json.dumps({
            "next_id": max((m["id"] for m in rows), default=0) + 1,
            "messages": rows, "threads": metadata or {},
        }))
        self.reset()

    def query(self, **kwargs):
        args = dict(viewer="user:david", aliases=[], sent_ids=set(), read_ids=set(), saved_threads=set())
        args.update(kwargs)
        return messages.dashboard_inbox(self.repo, **args)

    def test_personal_replies_are_found_before_pagination_even_behind_a_busy_thread(self):
        question = self.msg(1, "user:david", text="Why did accuracy drop?")
        answer = self.msg(2, to="user:david", text="Tokenizer mismatch", thread=1, reply=1)
        busy = [self.msg(i, thread=3) for i in range(3, 703)]
        self.seed([question, answer, *busy])
        result = self.query()
        self.assertEqual([t["id"] for t in result["threads"]], [1])
        row = result["threads"][0]
        self.assertEqual(row["focus"]["id"], 2)
        self.assertEqual(row["question"]["id"], 1)
        self.assertEqual(row["participants"], ["user:david", "a"])
        self.assertEqual(row["my_message_count"], 1)
        self.assertEqual(result["counts"]["for_you"], 1)
        self.assertEqual(self.query(view="all")["total"], 2)

    def test_inbox_aggregates_ideas_and_participants_across_old_replies(self):
        root = self.msg(1, "user:david", text="Compare idea/123 and #124; thread #999 is unrelated")
        old = self.msg(2, "agent:a", to="agent:b", thread=1, text="exp/125.2 and 126.1; loss 0.25; message #888")
        old["references"] = [{"kind": "idea", "id": 127}, {"kind": "experiment", "id": "128.1"}]
        self.seed([root, old, self.msg(3, "agent:b", thread=1, text="Latest update")])
        row = self.query(view="all")["threads"][0]
        self.assertEqual(row["idea_ids"], [126, 125, 128, 127, 124, 123])
        self.assertEqual(row["participants"], ["user:david", "a", "b"])

    def test_read_and_saved_are_independent_and_do_not_change_agent_acknowledgements(self):
        self.seed([self.msg(1, "user:david", text="Question"), self.msg(2, to="thread:1", thread=1, reply=1)])
        self.assertEqual(self.query()["counts"]["for_you"], 1)
        self.assertEqual(self.query(read_ids={2}, saved_threads={1})["threads"], [])
        saved = self.query(view="saved", read_ids={2}, saved_threads={1})
        self.assertEqual(saved["threads"][0]["id"], 1)
        self.assertEqual(saved["threads"][0]["unread_for_me"], 0)
        self.assertEqual(messages.get_message(self.repo, 2)["read_by"], [])

    def test_saved_conversation_returns_to_the_saved_answer_not_the_latest_update(self):
        self.seed([self.msg(1, "user:david", text="Question"),
                   self.msg(2, to="thread:1", thread=1, reply=1, text="Actual answer"),
                   self.msg(3, to="thread:1", thread=1, text="Later chatter"), self.msg(4)])
        saved = self.query(view="saved", saved_threads={1}, saved_focus={"1": 2})
        self.assertEqual(saved["threads"][0]["focus"]["id"], 2)
        self.assertEqual(saved["threads"][0]["question"]["preview"], "Question")
        invalid = self.query(view="saved", saved_threads={1}, saved_focus={"1": 4})
        self.assertEqual(invalid["threads"][0]["focus"]["id"], 3)

    def test_mine_includes_single_questions_and_anonymous_identity_is_browser_scoped(self):
        self.seed([self.msg(1, "user", text="Mine"), self.msg(2, "user", text="Other browser"),
                   self.msg(3, "user:david", text="Named question"), self.msg(4, "user:someone", text="Other human")])
        mine = self.query(view="mine", sent_ids={1})
        self.assertEqual({t["id"] for t in mine["threads"]}, {1, 3})
        self.assertTrue(all(t["you_posted_last"] for t in mine["threads"]))

    def test_full_text_search_reaches_old_replies_and_aliases_match_whole_names(self):
        self.seed([self.msg(1, "user:david"), self.msg(2, text="checkpoint stored in reports/rare-result.md", thread=1),
                   self.msg(3, text="@Dave please review"), self.msg(4, text="Davenport finished")])
        search = self.query(view="all", q="rare-result")
        self.assertEqual(search["threads"][0]["focus"]["id"], 2)
        self.assertEqual([t["id"] for t in self.query(aliases=["Dave"])["threads"]], [3])

    def test_thread_cursor_does_not_repeat_a_busy_thread_or_shift_when_new_threads_arrive(self):
        rows = [self.msg(i, "user:david") for i in range(1, 8)]
        self.seed(rows)
        first = self.query(view="all", limit=3)
        self.assertEqual([t["id"] for t in first["threads"]], [7, 6, 5])
        self.seed([*rows, self.msg(8)])
        second = self.query(view="all", before_id=first["next_before_id"], limit=3)
        self.assertEqual([t["id"] for t in second["threads"]], [4, 3, 2])

    def test_anchored_window_reads_forward_and_backward_without_fetching_the_archive(self):
        self.seed([self.msg(i, thread=1) for i in range(1, 502)])
        args = dict(agent_id=None, role=None, operator=True, limit=20)
        page = messages.get_thread(self.repo, 1, around_id=120, **args)
        self.assertEqual([m["id"] for m in page["messages"]], list(range(110, 130)))
        self.assertEqual(page["next_before_id"], 110)
        self.assertEqual(page["next_after_id"], 129)
        newer = messages.get_thread(self.repo, 1, after_id=129, **args)
        older = messages.get_thread(self.repo, 1, before_id=110, **args)
        self.assertEqual([m["id"] for m in newer["messages"]], list(range(130, 150)))
        self.assertEqual([m["id"] for m in older["messages"]], list(range(90, 110)))
        with self.assertRaises(ValueError):
            messages.get_thread(self.repo, 1, around_id=120, before_id=110, **args)
        with self.assertRaises(ValueError):
            messages.get_thread(self.repo, 1, around_id=999, **args)

    def test_conversation_filters_apply_before_paging_and_keep_human_replies(self):
        self.seed([self.msg(1, "user:david"), self.msg(2, "agent:a", thread=1, reply=1),
                   self.msg(3, "user:alex", thread=1), self.msg(4, "agent:b", thread=1, reply=3),
                   *[self.msg(i, thread=1) for i in range(5, 505)],
                   self.msg(505, to="user:david", thread=1), self.msg(506, "user", thread=1),
                   self.msg(507, thread=1, reply=506)])
        args = dict(agent_id=None, role=None, operator=True, viewer="user:david", limit=2)
        mine = messages.get_thread(self.repo, 1, audience="me", **args)
        self.assertEqual([m["id"] for m in mine["messages"]], [2, 505])
        self.assertEqual(mine["total"], 3)
        older = messages.get_thread(self.repo, 1, audience="me", before_id=2, **args)
        self.assertEqual([m["id"] for m in older["messages"]], [1])
        users = messages.get_thread(self.repo, 1, audience="users", limit=20,
                                    **{k: v for k, v in args.items() if k != "limit"})
        self.assertEqual([m["id"] for m in users["messages"]], [1, 2, 3, 4, 505, 506, 507])
        anon = messages.get_thread(self.repo, 1, audience="me", sent_ids={506}, **args)
        self.assertEqual([m["id"] for m in anon["messages"]], [506, 507])
        anchored = messages.get_thread(self.repo, 1, audience="me", around_id=400, **args)
        self.assertTrue(anchored["messages"])
        self.assertTrue(all(m["id"] in {1, 2, 505} for m in anchored["messages"]))

    def test_hidden_gaps_count_only_this_thread_and_context_is_bounded(self):
        rows = [self.msg(1, "user:david"), self.msg(2, thread=1, reply=1),
                *[self.msg(i, thread=99 if i == 6 else 1) for i in range(3, 11)],
                self.msg(11, "user:david", thread=1), self.msg(12, thread=1, reply=11),
                self.msg(13, thread=1), self.msg(14, thread=1)]
        self.seed(rows)
        args = dict(agent_id=None, role=None, operator=True)
        filtered = messages.get_thread(self.repo, 1, audience="me", viewer="user:david", limit=20, **args)
        self.assertEqual(filtered["gaps"], [{"after_id": 2, "before_id": 11, "count": 7},
                                           {"after_id": 12, "before_id": None, "count": 2}])
        first = messages.get_thread(self.repo, 1, context=True, after_id=2, before_id=11, limit=3, **args)
        self.assertEqual([m["id"] for m in first["messages"]], [3, 4, 5])
        self.assertEqual(first["total"], 7)
        second = messages.get_thread(self.repo, 1, context=True, after_id=5, before_id=11, limit=3, **args)
        self.assertEqual([m["id"] for m in second["messages"]], [7, 8, 9])
        final = messages.get_thread(self.repo, 1, context=True, after_id=9, before_id=11, limit=3, **args)
        self.assertEqual([m["id"] for m in final["messages"]], [10])
        self.assertEqual(final["total"], 1)
        with self.assertRaises(ValueError):
            messages.get_thread(self.repo, 1, context=True, after_id=6, before_id=11, **args)
        with self.assertRaises(ValueError):
            messages.get_thread(self.repo, 1, context=True, after_id=11, before_id=2, **args)
        self.assertEqual(messages.get_message(self.repo, 3)["read_by"], [])

    def test_filter_with_no_matches_still_exposes_context_and_page_boundary_gaps(self):
        self.seed([self.msg(1), self.msg(2, thread=1), self.msg(3, "user:david", thread=1),
                   self.msg(4, thread=1), self.msg(5, "user:david", thread=1)])
        args = dict(agent_id=None, role=None, operator=True)
        page = messages.get_thread(self.repo, 1, audience="me", viewer="user:david", limit=1, **args)
        self.assertEqual(page["gaps"], [{"after_id": 3, "before_id": 5, "count": 1}])
        empty = messages.get_thread(self.repo, 1, audience="me", viewer="user:alex", **args)
        self.assertEqual(empty["gaps"], [{"after_id": None, "before_id": None, "count": 5}])
        context = messages.get_thread(self.repo, 1, context=True, limit=3, **args)
        self.assertEqual([m["id"] for m in context["messages"]], [1, 2, 3])

    def test_http_query_requires_dashboard_admin_and_validates_personal_fields(self):
        self.seed([self.msg(1, "user:david"), self.msg(2, to="user:david", thread=1, reply=1)])
        app = FastAPI()
        @app.middleware("http")
        async def identity(request: Request, call_next):
            request.state.agent_id = request.headers.get("X-Agent-Id")
            request.state.auth_tier = request.headers.get("X-Test-Tier", "admin")
            return await call_next(request)
        app.include_router(routes.router)
        with patch.object(routes, "REPO_DIR", self.repo), TestClient(app) as client:
            source = {"X-The-Lab-Source": "dashboard"}
            self.assertEqual(client.post("/api/v1/dashboard/inbox", json={}).status_code, 403)
            self.assertEqual(client.post("/api/v1/dashboard/inbox", headers={**source, "X-Test-Tier": "experiment"}, json={}).status_code, 403)
            with patch.object(routes.agents_mod, "lookup_agent", return_value={}):
                self.assertEqual(client.post("/api/v1/dashboard/inbox", headers={**source, "X-Agent-Id": "a"}, json={}).status_code, 403)
            response = client.post("/api/v1/dashboard/inbox", headers=source, json={"as_user": "david"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["counts"]["for_you"], 1)
            self.assertEqual(client.post("/api/v1/dashboard/inbox", headers=source, json={"view": "invalid"}).status_code, 422)
            self.assertEqual(client.get("/api/v1/threads/1?around_id=2&limit=1", headers=source).json()["messages"][0]["id"], 2)
            self.assertEqual(client.post("/api/v1/dashboard/threads/1", json={"context": True}).status_code, 403)
            conversation = client.post("/api/v1/dashboard/threads/1", headers=source, json={"audience": "me", "as_user": "david"})
            self.assertEqual(conversation.status_code, 200)
            self.assertEqual([m["id"] for m in conversation.json()["messages"]], [1, 2])
            self.assertEqual(client.post("/api/v1/dashboard/threads/1", headers=source, json={"audience": "invalid"}).status_code, 422)


if __name__ == "__main__":
    unittest.main()
