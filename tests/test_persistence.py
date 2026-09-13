import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from the_lab.instance_lock import InstanceLock, InstanceLockError
from the_lab.store import Store, StoreDegradedError
from the_lab.stats import ApiStats


class PersistenceTests(unittest.TestCase):
    def test_corrupt_sibling_is_reported_without_losing_valid_records(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            good = root / ".the_lab/experiments/1"
            good.mkdir(parents=True)
            (good / "idea.json").write_text(json.dumps({"id": 1, "description": "good", "parent_ids": []}))
            (good / "notes.json").write_text("[]")
            bad = root / ".the_lab/experiments/2"
            bad.mkdir()
            (bad / "idea.json").write_text("{not json")
            store = Store(root)
            self.assertEqual(store.get_idea(1)["description"], "good")
            self.assertFalse(store.is_ready)
            self.assertIn(str(bad / "idea.json"), store.load_errors)
            with self.assertRaises(StoreDegradedError):
                store.create_idea("blocked", [], "main")

    def test_failed_note_write_does_not_publish_cache(self):
        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td))
            idea = store.create_idea("x", [], "main")
            store.save_idea(idea)
            with patch("the_lab.store._write_json", side_effect=OSError("disk offline")):
                with self.assertRaises(OSError):
                    store.add_note(idea["id"], "must not appear")
            self.assertEqual(store.get_notes(idea["id"]), [])

    def test_writer_lock_contention_then_release(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lock = InstanceLock(root)
            lock.acquire()
            code = (
                "from pathlib import Path; from the_lab.instance_lock import InstanceLock; "
                "import sys; "
                "\ntry:\n InstanceLock(Path(sys.argv[1])).acquire()\nexcept Exception: sys.exit(7)\n"
            )
            env = {**__import__("os").environ, "PYTHONPATH": str(Path.cwd())}
            self.assertEqual(subprocess.run([sys.executable, "-c", code, str(root)], env=env).returncode, 7)
            lock.release()
            child = subprocess.run([sys.executable, "-c", code, str(root)], env=env)
            self.assertEqual(child.returncode, 0)

    def test_stats_concurrent_flush_keeps_newer_snapshot_dirty_then_publishes_it(self):
        with tempfile.TemporaryDirectory() as td:
            stats = ApiStats(Path(td) / "stats.json")
            stats.record("GET", "/old")
            entered, release = threading.Event(), threading.Event()
            from the_lab import stats as stats_mod
            original = stats_mod.jsonio.write_json
            calls = 0

            def delayed(path, data, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    entered.set()
                    release.wait(2)
                return original(path, data, **kwargs)

            with patch.object(stats_mod.jsonio, "write_json", delayed):
                first = threading.Thread(target=stats.flush)
                first.start()
                self.assertTrue(entered.wait(1))
                stats.record("GET", "/new")
                second = threading.Thread(target=stats.flush)
                second.start()
                release.set()
                first.join(2)
                second.join(2)
            saved = json.loads((Path(td) / "stats.json").read_text())
            self.assertEqual(saved["calls"].get("GET /new"), 1)
