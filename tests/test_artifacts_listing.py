"""GET /api/v1/artifacts: the dashboard's read-only artifacts explorer."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient


class ArtifactsListingTests(unittest.TestCase):
    def setUp(self):
        from the_lab.routes import experiments
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name).resolve()
        self.root = self.repo / ".the_lab" / "artifacts"
        (self.root / "reports").mkdir(parents=True)
        (self.root / "reports" / "idea_1.md").write_text("# report\n")
        (self.root / "notes.txt").write_text("hello")
        (self.root / "Zeta").mkdir()
        self.outside = Path(tempfile.mkdtemp()); self.addCleanup(lambda: os.rmdir(self.outside))
        os.symlink(self.outside, self.root / "elsewhere")
        for p in (patch.object(experiments, "REPO_DIR", self.repo), patch("the_lab.deps.REPO_DIR", self.repo)):
            p.start(); self.addCleanup(p.stop)
        app = FastAPI(); app.include_router(experiments.router)
        self.client = TestClient(app)

    def test_root_lists_folders_first_and_marks_external_links(self):
        r = self.client.get("/api/v1/artifacts").json()
        self.assertEqual(r["path"], "")
        self.assertEqual([(e["name"], e["kind"]) for e in r["entries"]],
                         [("elsewhere", "dir"), ("reports", "dir"), ("Zeta", "dir"), ("notes.txt", "file")])
        external = {e["name"]: e.get("external", False) for e in r["entries"]}
        self.assertTrue(external["elsewhere"]); self.assertFalse(external["reports"])
        self.assertEqual(r["entries"][-1]["size"], 5)

    def test_subfolder_and_files_are_served_by_the_files_route(self):
        r = self.client.get("/api/v1/artifacts", params={"path": "reports"}).json()
        self.assertEqual(r["path"], "reports")
        self.assertEqual([e["name"] for e in r["entries"]], ["idea_1.md"])
        self.assertEqual(self.client.get(f"/api/v1/files/{r['base']}/reports/idea_1.md").text, "# report\n")

    def test_cannot_escape_the_artifacts_root(self):
        for bad in ("..", "../..", "reports/../../..", "elsewhere"):
            self.assertEqual(self.client.get("/api/v1/artifacts", params={"path": bad}).status_code, 403, bad)
        # A leading slash is stripped: "/etc" means artifacts/etc, which doesn't exist.
        for missing in ("missing", "/etc"):
            self.assertEqual(self.client.get("/api/v1/artifacts", params={"path": missing}).status_code, 404, missing)



class ArtifactEditingTests(unittest.TestCase):
    H = {"X-The-Lab-Source": "dashboard"}

    def setUp(self):
        from the_lab.routes import experiments
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name).resolve()
        (self.repo / ".the_lab" / "artifacts").mkdir(parents=True)
        for p in (patch.object(experiments, "REPO_DIR", self.repo), patch("the_lab.deps.REPO_DIR", self.repo)):
            p.start(); self.addCleanup(p.stop)
        app = FastAPI(); app.include_router(experiments.router)
        self.client = TestClient(app)

    def put(self, path, body, headers=None):
        return self.client.put("/api/v1/artifacts/file", params={"path": path}, json=body,
                               headers=self.H if headers is None else headers)

    def test_create_edit_and_conflict(self):
        r = self.put("notes/plan.md", {"content": "# Plan\n", "create": True})
        self.assertEqual(r.status_code, 200, r.text); v1 = r.json()["version"]
        self.assertTrue(r.json()["created"])
        self.assertEqual((self.repo / ".the_lab/artifacts/notes/plan.md").read_text(), "# Plan\n")
        self.assertEqual(self.put("notes/plan.md", {"content": "x", "create": True}).status_code, 409)
        got = self.client.get("/api/v1/artifacts/file", params={"path": "notes/plan.md"}).json()
        self.assertEqual((got["content"], got["version"], got["editable"]), ("# Plan\n", v1, True))
        r = self.put("notes/plan.md", {"content": "# Plan\n- a\n", "base_version": v1})
        self.assertEqual(r.status_code, 200); v2 = r.json()["version"]
        # An edit based on the old version is a conflict, not an overwrite.
        r = self.put("notes/plan.md", {"content": "stale", "base_version": v1})
        self.assertEqual((r.status_code, r.json()["version"]), (409, v2))
        self.assertEqual((self.repo / ".the_lab/artifacts/notes/plan.md").read_text(), "# Plan\n- a\n")
        log = (self.repo / ".the_lab/artifact_edits.jsonl").read_text().strip().splitlines()
        self.assertEqual(len(log), 2)

    def test_only_the_dashboard_user_writes_and_only_inside_artifacts(self):
        self.assertEqual(self.put("a.md", {"content": "x", "create": True}, headers={}).status_code, 403)
        self.assertEqual(self.put("a.md", {"content": "x", "create": True},
                                  headers={**self.H, "X-Agent-Id": "zdqvo"}).status_code, 403)
        for bad in ("../x.md", "/etc/x.md", "a/../../x.md", ".hidden.md", ""):
            self.assertEqual(self.put(bad, {"content": "x", "create": True}).status_code, 403, bad)
        self.assertEqual(self.put("run.sh", {"content": "x", "create": True}).status_code, 415)


if __name__ == "__main__":
    unittest.main()
