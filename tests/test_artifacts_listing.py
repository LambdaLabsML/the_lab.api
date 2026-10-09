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


if __name__ == "__main__":
    unittest.main()
