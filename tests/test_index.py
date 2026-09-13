"""Contract tests for the disposable filesystem-authoritative SQLite index."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from the_lab.cli import cmd_index
from the_lab.index import FilesystemIndex, IndexUnavailable, scan_filesystem
from the_lab.store import Store


class FilesystemIndexTests(unittest.TestCase):
    def setUp(self):
        self.sources = tempfile.TemporaryDirectory()
        self.caches = tempfile.TemporaryDirectory()
        self.repo = Path(self.sources.name)
        (self.repo / ".the_lab" / "instance.id").parent.mkdir(parents=True)
        (self.repo / ".the_lab" / "instance.id").write_text("testinst\n")
        self.idea_dir = self.repo / ".the_lab" / "experiments" / "1"
        (self.idea_dir / "1").mkdir(parents=True)
        (self.idea_dir / "idea.json").write_text(json.dumps({"id": 1, "description": "idea", "status": "active", "created_at": "2024-01-01"}))
        self.exp_path = self.idea_dir / "1" / "experiment.json"
        self._write_exp(status="completed", score=0.5)
        self.index = FilesystemIndex(self.repo, "testinst", index_dir=self.caches.name)

    def tearDown(self):
        self.caches.cleanup()
        self.sources.cleanup()

    def _write_exp(self, *, status: str, score: float):
        self.exp_path.write_text(json.dumps({"status": status, "description": "run", "created_at": "2024-01-02",
                                               "finished_at": "2024-01-03", "metrics": {"score": score, "nested": {"loss": 2}, "flag": True}}))

    def test_rebuild_parity_and_numeric_catalog(self):
        self.index.rebuild()
        source = scan_filesystem(self.repo)
        got = self.index.list_records("experiments")["records"]
        self.assertEqual(got, source["experiments"])
        chart = self.index.completed_history(limit=1, metrics=["score"], compact=True)
        self.assertEqual(chart["total"], 1)
        self.assertEqual(chart["experiments"][0]["metrics"], {"score": 0.5})
        self.assertEqual(chart["metric_keys"], ["nested.loss", "score"])

    def test_delete_database_never_loses_files(self):
        self.index.rebuild()
        self.index.path.unlink()
        self.assertTrue(self.exp_path.exists())
        self.index.rebuild()
        self.assertEqual(self.index.list_records("experiments")["total"], 1)

    def test_incremental_updates_and_deletes(self):
        self.index.rebuild()
        self._write_exp(status="failed", score=0.25)
        changed = scan_filesystem(self.repo)["experiments"][0]
        self.index.upsert("experiments", changed)
        self.assertEqual(self.index.list_records("experiments", status="failed")["total"], 1)
        self.index.delete("experiments", "1.1")
        self.assertEqual(self.index.list_records("experiments")["total"], 0)

    def test_external_edit_and_deletion_are_found_by_snapshot_reconcile(self):
        self.index.rebuild()
        self._write_exp(status="failed", score=0.25)  # no Store/index callback
        result = self.index.reconcile()
        self.assertEqual(result["changed"], 1)
        self.assertEqual(self.index.list_records("experiments", status="failed")["total"], 1)
        self.exp_path.unlink()
        result = self.index.reconcile()
        self.assertEqual(result["deleted"], 1)
        self.assertEqual(self.index.list_records("experiments")["total"], 0)

    def test_failed_incremental_update_after_fs_commit_repairs_by_reconcile(self):
        self.index.rebuild()
        self._write_exp(status="failed", score=0.25)  # successful authoritative commit
        self.index.path.unlink()  # emulate an index failure after that commit
        with self.assertRaises(IndexUnavailable):
            self.index.upsert("experiments", scan_filesystem(self.repo)["experiments"][0])
        self.index.reconcile()  # startup/periodic repair reads durable JSON
        self.assertEqual(self.index.list_records("experiments", status="failed")["total"], 1)

    def test_corrupt_and_schema_mismatch_are_rebuildable(self):
        self.index.rebuild()
        self.index.path.write_bytes(b"not sqlite")
        self.index.rebuild()
        conn = sqlite3.connect(self.index.path)
        conn.execute("UPDATE metadata SET value='999' WHERE key='schema_version'")
        conn.commit(); conn.close()
        with self.assertRaises(IndexUnavailable):
            self.index.list_records("ideas")
        self.index.reconcile()
        self.assertTrue(self.index.status()["ready"])

    def test_concurrent_readers_and_rebuild(self):
        self.index.rebuild()
        failures = []
        start = threading.Barrier(3)
        def reader():
            try:
                start.wait()
                # Separate objects model separate worker processes: no shared
                # Python mutex is involved in this reader/rebuild race.
                separate = FilesystemIndex(self.repo, "testinst", index_dir=self.caches.name)
                for _ in range(20):
                    self.assertEqual(separate.list_records("experiments")["total"], 1)
            except BaseException as exc:  # retain thread assertion
                failures.append(exc)
        def writer():
            try:
                start.wait()
                for _ in range(4): self.index.rebuild()
            except BaseException as exc:
                failures.append(exc)
        threads = [threading.Thread(target=reader), threading.Thread(target=writer), threading.Thread(target=reader)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(failures, [])

    def test_metric_edge_cases_and_chart_parity(self):
        self.exp_path.write_text(json.dumps({"status": "completed", "created_at": "2024-01-02",
            "metrics": {"a.b": 9, "a": {"b": 2, "deep": {"x": 3}}, "flag": True, "nan": float("nan")}}))
        # Old flat layout must also be rebuilt, and records without metrics do
        # not appear in chart history (matching _chart_rows).
        (self.idea_dir / "2.json").write_text(json.dumps({"status": "completed", "created_at": "2024-01-04", "metrics": None}))
        self.index.rebuild()
        chart = self.index.completed_history()
        self.assertEqual(chart["total"], 1)
        self.assertEqual(chart["experiments"][0]["metrics"], {"a.b": 9.0, "a.deep.x": 3.0})
        self.assertIn("a.deep.x", chart["metric_keys"])
        self.assertEqual(self.index.completed_history(metrics=["absent"])["experiments"][0]["metrics"], {})

    def test_revision_fence_rejects_reordered_callbacks_and_stale_queries(self):
        self.index.rebuild({**scan_filesystem(self.repo), "version": 2})
        newer = scan_filesystem(self.repo)["experiments"][0] | {"status": "failed"}
        older = scan_filesystem(self.repo)["experiments"][0] | {"status": "completed"}
        self.index.upsert("experiments", newer, revision=3)
        with self.assertRaises(IndexUnavailable):
            self.index.upsert("experiments", older, revision=2)  # delayed A after B
        with self.assertRaises(IndexUnavailable):
            self.index.list_records("experiments", required_revision=3)

    def test_revision_gap_dirties_until_authoritative_reconcile(self):
        first = scan_filesystem(self.repo)["experiments"][0]
        second = first | {"id": "1.2", "label": "1.2", "seq": 2, "description": "second"}
        self.index.rebuild({"ideas": scan_filesystem(self.repo)["ideas"], "experiments": [first], "version": 1})
        # B's callback reaches SQLite before A's. It cannot silently claim
        # revision 3 while record A's revision-2 content is missing.
        with self.assertRaises(IndexUnavailable):
            self.index.upsert("experiments", second, revision=3)
        with self.assertRaises(IndexUnavailable):
            self.index.list_records("experiments", required_revision=3)
        self.index.reconcile({"ideas": scan_filesystem(self.repo)["ideas"], "experiments": [first, second], "version": 3})
        self.assertEqual(self.index.list_records("experiments", required_revision=3)["total"], 2)

    def test_startup_rebuild_replaces_high_revision_prior_process_index(self):
        old = scan_filesystem(self.repo)
        self.index.rebuild({**old, "version": 99})
        # A new process has an authoritative snapshot without the old record.
        fresh = FilesystemIndex(self.repo, "testinst", index_dir=self.caches.name)
        fresh.rebuild({"ideas": old["ideas"], "experiments": [], "version": 1})
        self.assertEqual(fresh.list_records("experiments", required_revision=1)["total"], 0)

    def test_store_end_to_end_db_loss_rebuild_preserves_authoritative_bytes(self):
        with patch.dict(os.environ, {"THE_LAB_INDEX_DIR": self.caches.name}):
            store = Store(self.repo)
            exp = store.create_experiment(1, "run")
            store.update_experiment(exp["id"], status="completed", metrics={"score": 7})
            source_hashes = {path.relative_to(self.repo): path.read_bytes()
                             for path in (self.repo / ".the_lab").rglob("*") if path.is_file()}
            store.index.path.unlink()
            store.reconcile()  # unchanged-FS fast path must still repair index
            chart = store.index.completed_history(required_revision=store.index_version)
            self.assertIn({"score": 7.0}, [row["metrics"] for row in chart["experiments"]])
            after_hashes = {path.relative_to(self.repo): path.read_bytes()
                            for path in (self.repo / ".the_lab").rglob("*") if path.is_file()}
            self.assertEqual(after_hashes, source_hashes)

    def test_store_restart_rebuilds_old_high_revision_from_changed_files(self):
        with patch.dict(os.environ, {"THE_LAB_INDEX_DIR": self.caches.name}):
            first = Store(self.repo)
            exp = first.create_experiment(1, "temporary")
            first.index.rebuild({**first.index_snapshot(), "version": 99})
            # Simulate changes while the old process is gone: change an idea
            # and remove an experiment source file, then construct Store anew.
            first._exp_json_path(exp["idea_id"], exp["seq"]).unlink()
            idea_path = self.idea_dir / "idea.json"
            idea = json.loads(idea_path.read_text()) | {"description": "changed"}
            idea_path.write_text(json.dumps(idea))
            second = Store(self.repo)
            self.assertEqual(second.get_idea(1)["description"], "changed")
            self.assertIsNone(second.get_experiment(exp["id"]))
            self.assertEqual(second.index.list_records("experiments", required_revision=second.index_version)["total"], 1)

    def test_same_instance_id_repos_are_isolated(self):
        with tempfile.TemporaryDirectory() as second_source:
            other = Path(second_source)
            (other / ".the_lab" / "instance.id").parent.mkdir(parents=True)
            (other / ".the_lab" / "instance.id").write_text("testinst\n")
            other_index = FilesystemIndex(other, "testinst", index_dir=self.caches.name)
            self.assertNotEqual(self.index.path, other_index.path)

    def test_cli_rebuild_scans_without_mutating_authoritative_files(self):
        before = {path.relative_to(self.repo): path.read_bytes()
                  for path in self.repo.rglob("*") if path.is_file()}
        with patch("sys.argv", ["the-lab"]):
            cmd_index(["rebuild", str(self.repo), "--index-dir", self.caches.name])
        after = {path.relative_to(self.repo): path.read_bytes()
                 for path in self.repo.rglob("*") if path.is_file()}
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
