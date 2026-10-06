"""Write-behind message persistence (coalesced, compact writes)."""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from the_lab import messages


class MessageWriteBehindTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        messages._cache = messages._cache_path = None
        self.addCleanup(self.tmp.cleanup)

    def tearDown(self):
        messages.flush()
        messages._cache = messages._cache_path = None

    def test_flush_persists_compact_latest_state(self):
        m = messages.add_message(self.repo, from_agent="b", from_role=None, to="agent:a", text="hi")
        messages.claim_unread_for(self.repo, agent_id="a", role=None)
        messages.flush()
        path = self.repo / ".the_lab" / "messages.json"
        raw = path.read_text()
        self.assertNotIn("\n  ", raw)  # compact, C-encoder output
        saved = json.loads(raw)["messages"][0]
        self.assertEqual(saved["id"], m["id"])
        self.assertEqual(saved["delivered_by"], ["a"])

    def test_stale_snapshot_never_overwrites_newer(self):
        messages.add_message(self.repo, from_agent="b", from_role=None, to="all", text="one")
        with messages._lock:
            old = messages._snapshot_locked()
        messages.add_message(self.repo, from_agent="b", from_role=None, to="all", text="two")
        messages.flush()
        messages._persist(old)
        saved = json.loads((self.repo / ".the_lab" / "messages.json").read_text())
        self.assertEqual(len(saved["messages"]), 2)


if __name__ == "__main__":
    unittest.main()
