"""Immutable shared views keep complex dashboard URLs short across browsers."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from the_lab.routes import dashboard_views


class DashboardViewsTests(TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        self.patch = patch.object(dashboard_views, "REPO_DIR", self.repo); self.patch.start(); self.addCleanup(self.patch.stop)
        app = FastAPI()
        @app.middleware("http")
        async def identity(request: Request, call_next):
            request.state.auth_tier = request.headers.get("X-Test-Tier", "admin")
            request.state.agent_id = request.headers.get("X-Agent-Id")
            return await call_next(request)
        app.include_router(dashboard_views.router)
        self.client = TestClient(app)
        self.headers = {"X-The-Lab-Source": "dashboard"}

    def save(self, state, name="accuracy-3-graphs"):
        return self.client.post("/api/v1/dashboard/views", headers=self.headers, json={"name": name, "state": state})

    def test_deduplication_reload_and_immutable_versions(self):
        state = {"route": {"section": "review", "idea": 123}, "overview": {"metric": "accuracy"}}
        first = self.save(state); self.assertEqual(first.status_code, 200)
        ident = first.json()["id"]
        self.assertLess(len("/overview/views/" + ident), 80)
        self.assertEqual(self.save(dict(reversed(list(state.items())))).json()["id"], ident)
        self.assertEqual(self.client.get("/api/v1/dashboard/views/" + ident, headers=self.headers).json()["state"], state)
        changed = {**state, "overview": {"metric": "loss"}}
        self.assertNotEqual(self.save(changed).json()["id"], ident)
        self.assertEqual(self.client.get("/api/v1/dashboard/views/" + ident, headers=self.headers).json()["state"], state)

    def test_only_dashboard_users_can_read_and_write(self):
        ident = self.save({"route": {"section": "review"}}).json()["id"]
        for headers in ({}, {**self.headers, "X-Test-Tier": "experiment"}, {**self.headers, "X-Agent-Id": "worker"}):
            self.assertEqual(self.client.post("/api/v1/dashboard/views", headers=headers, json={"state": {}}).status_code, 403)
            self.assertEqual(self.client.get("/api/v1/dashboard/views/" + ident, headers=headers).status_code, 403)

    def test_limits_unknown_views_and_unsafe_names(self):
        response = self.save({"route": {"section": "review"}}, name="../Accuracy: test!")
        self.assertRegex(response.json()["id"], r"^accuracy-test-[0-9a-f]{16}$")
        self.assertEqual(self.save({"huge": "a" * 100_001}).status_code, 413)
        self.assertEqual(self.client.get("/api/v1/dashboard/views/missing", headers=self.headers).status_code, 404)
