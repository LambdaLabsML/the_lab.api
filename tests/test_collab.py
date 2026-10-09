"""Live collaborative editing: OT core fuzzing, UTF-16 handling, the document
manager's transform/apply/merge lifecycle, and the WebSocket wire protocol."""
from __future__ import annotations

import json
import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from the_lab import collab


# ─────────────────────────────────────────────────────────────────────────
# OT core: randomized convergence fuzzing
# ─────────────────────────────────────────────────────────────────────────

# Alphabet includes plain ASCII plus the two surrogate halves of an emoji
# (U+1F600, split the way ``to_u16`` would split it) so components land on
# and around astral characters, not just inside BMP text.
_ALPHABET = list("abcXYZ \n") + [chr(0xD83D), chr(0xDE00)]


def _random_op(text: str, rng: random.Random, max_components: int = 3) -> tuple[list[dict], str]:
    """A random, internally-consistent multi-component op starting from
    *text* (each component's position is relative to the text as modified
    by the ones before it, matching ``apply_op``'s semantics). Returns
    ``(op, resulting_text)``."""
    ops: list[dict] = []
    cur = text
    for _ in range(rng.randint(1, max_components)):
        if cur and rng.random() < 0.5:
            p = rng.randint(0, len(cur) - 1)  # leaves at least one char to delete
            maxlen = len(cur) - p
            length = rng.randint(1, min(5, maxlen))
            d = cur[p:p + length]
            ops.append({"p": p, "d": d})
            cur = cur[:p] + cur[p + length:]
        else:
            p = rng.randint(0, len(cur))
            length = rng.randint(1, 5)
            ins = "".join(rng.choice(_ALPHABET) for _ in range(length))
            ops.append({"p": p, "i": ins})
            cur = cur[:p] + ins + cur[p:]
    return ops, cur


def _random_text(rng: random.Random, max_len: int = 25) -> str:
    return "".join(rng.choice(_ALPHABET) for _ in range(rng.randint(0, max_len)))


class ConvergenceFuzzTests(unittest.TestCase):
    """TP1: for concurrent ops a, b on the same doc, applying each in turn
    and then the other (correctly transformed) must converge."""

    def test_random_concurrent_ops_converge(self):
        rng = random.Random(20261009)
        trials = 4000
        for i in range(trials):
            doc = _random_text(rng)
            a, _ = _random_op(doc, rng)
            b, _ = _random_op(doc, rng)
            left_text = collab.apply_op(collab.apply_op(doc, a), collab.transform(b, a, "right"))
            right_text = collab.apply_op(collab.apply_op(doc, b), collab.transform(a, b, "left"))
            self.assertEqual(left_text, right_text, f"trial {i}: doc={doc!r} a={a!r} b={b!r}")

    def test_single_component_ops_also_converge(self):
        # A simpler sub-case (one component each) run with a lot more trials,
        # since it's cheap and exercises insert/delete-vs-insert/delete more
        # densely than the multi-component generator does.
        rng = random.Random(777)
        for i in range(4000):
            doc = _random_text(rng, max_len=15)
            a, _ = _random_op(doc, rng, max_components=1)
            b, _ = _random_op(doc, rng, max_components=1)
            left_text = collab.apply_op(collab.apply_op(doc, a), collab.transform(b, a, "right"))
            right_text = collab.apply_op(collab.apply_op(doc, b), collab.transform(a, b, "left"))
            self.assertEqual(left_text, right_text, f"trial {i}: doc={doc!r} a={a!r} b={b!r}")

    def test_three_way_chain_also_converges(self):
        # Sanity check beyond the pairwise property: apply a, then b
        # (transformed against a), then c (transformed against both,
        # against the composition) should match applying in a different
        # order with the matching transforms — i.e. transform composes.
        rng = random.Random(4242)
        for i in range(1000):
            doc = _random_text(rng, max_len=15)
            a, _ = _random_op(doc, rng, max_components=2)
            b, _ = _random_op(doc, rng, max_components=2)
            # Server order: a then b (b transformed against a's history).
            server_text = collab.apply_op(collab.apply_op(doc, a), collab.transform(b, a, "left"))
            # The "b sender"'s own view: apply b locally, then receive a
            # (transformed against b, from the right) and apply it too.
            client_text = collab.apply_op(collab.apply_op(doc, b), collab.transform(a, b, "right"))
            self.assertEqual(server_text, client_text, f"trial {i}: doc={doc!r} a={a!r} b={b!r}")


# ─────────────────────────────────────────────────────────────────────────
# UTF-16 handling
# ─────────────────────────────────────────────────────────────────────────

class Utf16Tests(unittest.TestCase):
    SNOWMAN = "☃"          # BMP, 1 code unit
    EMOJI = "\U0001F600"        # astral, 2 code units (surrogate pair)
    MATH = "\U0001D54A"         # astral (double-struck S)

    def test_round_trip_plain_and_astral(self):
        for s in ("hello", "a" + self.SNOWMAN + "b", "x" + self.EMOJI + "y",
                  self.EMOJI * 3, self.MATH + self.EMOJI, ""):
            self.assertEqual(collab.from_u16(collab.to_u16(s)), s)

    def test_to_u16_length_matches_js_utf16_length(self):
        self.assertEqual(len(collab.to_u16("abc")), 3)
        self.assertEqual(len(collab.to_u16(self.SNOWMAN)), 1)
        self.assertEqual(len(collab.to_u16(self.EMOJI)), 2)
        self.assertEqual(len(collab.to_u16("x" + self.EMOJI + "y")), 4)

    def test_position_after_astral_character_lands_correctly(self):
        # "x<emoji>y" in u16 form is 4 code units: x, hi, lo, y. Inserting
        # at position 3 (after the emoji, before "y") must not split the pair.
        text = collab.to_u16("x" + self.EMOJI + "y")
        out = collab.apply_op(text, [{"p": 3, "i": "Z"}])
        self.assertEqual(collab.from_u16(out), "x" + self.EMOJI + "Zy")

    def test_delete_must_match_exact_surrogate_pair(self):
        text = collab.to_u16("x" + self.EMOJI + "y")
        hi, lo = text[1], text[2]
        # Deleting exactly the pair works...
        out = collab.apply_op(text, [{"p": 1, "d": hi + lo}])
        self.assertEqual(collab.from_u16(out), "xy")
        # ...but deleting just one surrogate half (splitting the pair) is
        # still mechanically valid at the u16 level (it matches the text);
        # from_u16 on the resulting lone surrogate just can't recombine it.
        with self.assertRaises(collab.OTError):
            collab.apply_op(text, [{"p": 1, "d": "Q"}])  # doesn't match the doc

    def test_round_trip_through_disk_write_and_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "f.md"
            original = "intro " + self.EMOJI + " done " + self.MATH
            p.write_bytes(collab.from_u16(collab.to_u16(original)).encode("utf-8"))
            self.assertEqual(p.read_text(encoding="utf-8"), original)
            # And the hash is computed on the real (recombined) text.
            self.assertEqual(collab.content_hash(original),
                              collab.content_hash(collab.from_u16(collab.to_u16(original))))


# ─────────────────────────────────────────────────────────────────────────
# Document manager
# ─────────────────────────────────────────────────────────────────────────

class DocManagerTests(unittest.TestCase):
    def make_doc(self, text="hello world"):
        return collab.CollabDoc("doc.md", collab.to_u16(text))

    def test_two_concurrent_ops_from_same_base_converge_with_correct_acks(self):
        doc = self.make_doc()
        applied_a, ops_a, hash_a = collab.submit_op(doc, "A", "Alice", "#58a6ff", 0,
                                                     [{"p": 0, "i": "ALPHA-"}])
        self.assertEqual(applied_a, 0)
        self.assertEqual(doc.version, 1)
        self.assertEqual(collab.from_u16(doc.text), "ALPHA-hello world")

        # B is still at version 0 (concurrent with A) — gets transformed
        # against A's history before applying.
        applied_b, ops_b, hash_b = collab.submit_op(doc, "B", "Bob", "#f0883e", 0,
                                                     [{"p": 11, "i": "-BETA"}])
        self.assertEqual(applied_b, 1)  # applied at the version A's op produced
        self.assertEqual(doc.version, 2)
        # B's insert position (11, end of "hello world") shifts by len("ALPHA-").
        self.assertEqual(ops_b, [{"p": 11 + len("ALPHA-"), "i": collab.to_u16("-BETA")}])
        self.assertEqual(collab.from_u16(doc.text), "ALPHA-hello world-BETA")
        self.assertEqual(hash_b, collab.content_hash(collab.from_u16(doc.text)))
        self.assertNotEqual(hash_a, hash_b)

    def test_op_based_on_old_version_is_transformed_against_full_history(self):
        doc = self.make_doc("0123456789")
        collab.submit_op(doc, "A", "Alice", "#1", 0, [{"p": 0, "i": "aa"}])     # v0 -> v1
        collab.submit_op(doc, "A", "Alice", "#1", 1, [{"p": 0, "i": "bb"}])     # v1 -> v2
        # C joined at v0 and only now submits, based on the stale v=0.
        applied_c, ops_c, _ = collab.submit_op(doc, "C", "Carl", "#2", 0, [{"p": 10, "i": "Z"}])
        self.assertEqual(applied_c, 2)
        self.assertEqual(ops_c, [{"p": 10 + 4, "i": "Z"}])  # shifted past "bb" + "aa"
        self.assertEqual(collab.from_u16(doc.text), "bbaa0123456789Z")

    def test_base_version_older_than_kept_history_is_an_error(self):
        doc = self.make_doc()
        doc.version = 50
        doc.history = []  # nothing kept — oldest retained version is 50
        with self.assertRaises(collab.OTError):
            collab.submit_op(doc, "A", "Alice", "#1", 0, [{"p": 0, "i": "x"}])
        # Document is untouched by the failed attempt.
        self.assertEqual(doc.version, 50)

    def test_bad_op_delete_mismatch_yields_error_and_leaves_doc_untouched(self):
        doc = self.make_doc("hello world")
        text_before, version_before = doc.text, doc.version
        with self.assertRaises(collab.OTError):
            collab.submit_op(doc, "A", "Alice", "#1", 0, [{"p": 0, "d": "NOPE!"}])
        self.assertEqual(doc.text, text_before)
        self.assertEqual(doc.version, version_before)

    def test_malformed_ops_payload_is_an_error(self):
        doc = self.make_doc()
        for bad in (None, [], "oops", [{"p": 0}], [{"i": "x"}], [{"p": "0", "i": "x"}]):
            with self.assertRaises(collab.OTError):
                collab.submit_op(doc, "A", "Alice", "#1", 0, bad)


class ExternalEditMergeTests(unittest.IsolatedAsyncioTestCase):
    """An agent writing the artifact file on disk while a user has
    unflushed edits in the in-memory doc: both must survive."""

    def setUp(self):
        # ``collab.docs`` is a module-level registry keyed by path; clear it
        # so an earlier test's "doc.md" entry can't leak into this one.
        collab.docs.clear()
        self.addCleanup(collab.docs.clear)

    async def test_disk_change_merges_with_unflushed_user_edit(self):
        from the_lab.routes import experiments
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp).resolve()
            root = repo / ".the_lab" / "artifacts"
            root.mkdir(parents=True)
            target = root / "doc.md"
            target.write_text("hello world")
            with patch.object(experiments, "REPO_DIR", repo), patch("the_lab.deps.REPO_DIR", repo):
                doc = await collab.get_doc("doc.md", target)
                # User edit, left unflushed (flushed_text/version stay at the
                # on-disk baseline).
                collab.submit_op(doc, "A", "Alice", "#58a6ff", 0, [{"p": 0, "i": "USER-"}])
                self.assertEqual(collab.from_u16(doc.text), "USER-hello world")
                self.assertEqual(doc.flushed_text, collab.to_u16("hello world"))
                self.assertEqual(target.read_text(), "hello world")  # not written yet

                # Agent writes the file independently (based on the old
                # "hello world", unaware of the user's pending edit).
                target.write_text("hello world AGENT")

                result = await collab.check_and_merge_external(doc)
                self.assertEqual(result["status"], "merged")

                merged = collab.from_u16(doc.text)
                self.assertIn("USER-", merged)
                self.assertIn("AGENT", merged)
                self.assertEqual(merged, "USER-hello world AGENT")
                # The merge was flushed back to disk.
                self.assertEqual(target.read_text(), merged)
                self.assertEqual(doc.flushed_text, doc.text)

    async def test_deleted_file_is_reported_and_doc_is_dropped(self):
        from the_lab.routes import experiments
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp).resolve()
            root = repo / ".the_lab" / "artifacts"
            root.mkdir(parents=True)
            target = root / "doc.md"
            target.write_text("hello")
            with patch.object(experiments, "REPO_DIR", repo), patch("the_lab.deps.REPO_DIR", repo):
                doc = await collab.get_doc("doc.md", target)
                target.unlink()
                result = await collab.check_and_merge_external(doc)
                self.assertEqual(result, {"status": "deleted"})


# ─────────────────────────────────────────────────────────────────────────
# WebSocket wire protocol (end to end, two clients on one artifact)
# ─────────────────────────────────────────────────────────────────────────

class CollabWebSocketTests(unittest.TestCase):
    def setUp(self):
        from the_lab import app as app_mod
        from the_lab.routes import experiments
        self.app_mod = app_mod
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name).resolve()
        (self.repo / ".the_lab" / "artifacts").mkdir(parents=True)
        self.file = self.repo / ".the_lab" / "artifacts" / "doc.md"
        self.file.write_text("hello world")

        for p in (patch.object(experiments, "REPO_DIR", self.repo),
                  patch("the_lab.deps.REPO_DIR", self.repo),
                  patch.object(app_mod, "_AUTH_ENABLED", False)):
            p.start(); self.addCleanup(p.stop)

        collab.docs.clear()
        self.addCleanup(collab.docs.clear)
        self._orig_flush = collab.FLUSH_DEBOUNCE_SECONDS
        self._orig_poll = collab.POLL_INTERVAL_SECONDS
        collab.FLUSH_DEBOUNCE_SECONDS = 0.05
        collab.POLL_INTERVAL_SECONDS = 0.1
        self.addCleanup(lambda: setattr(collab, "FLUSH_DEBOUNCE_SECONDS", self._orig_flush))
        self.addCleanup(lambda: setattr(collab, "POLL_INTERVAL_SECONDS", self._orig_poll))

        # A fresh, minimal app carrying only the collab endpoint — avoids
        # triggering the real app's startup event (journal attach etc.)
        # against the real repo.
        fastapi_app = FastAPI()
        fastapi_app.websocket("/api/v1/artifacts/collab")(app_mod.artifacts_collab_endpoint)
        self.client = TestClient(fastapi_app)

    def test_joined_op_cursor_presence_and_saved(self):
        with self.client.websocket_connect(
                "/api/v1/artifacts/collab?path=doc.md&name=Alice&client=A") as ws_a:
            frame_a = ws_a.receive_json()
            self.assertEqual(frame_a["type"], "joined")
            self.assertEqual(frame_a["client"], "A")
            self.assertEqual(frame_a["text"], "hello world")
            self.assertEqual(frame_a["hash"], collab.content_hash("hello world"))
            v_a = frame_a["v"]

            with self.client.websocket_connect(
                    "/api/v1/artifacts/collab?path=doc.md&name=Bob&client=B") as ws_b:
                presence_a = ws_a.receive_json()
                self.assertEqual(presence_a["type"], "presence")
                self.assertEqual({c["id"] for c in presence_a["clients"]}, {"A", "B"})

                frame_b = ws_b.receive_json()
                self.assertEqual(frame_b["type"], "joined")
                self.assertEqual({c["id"] for c in frame_b["clients"]}, {"A", "B"})
                self.assertNotEqual(frame_a["color"], frame_b["color"])

                # A submits an op; A gets an ack, B gets the (untransformed,
                # since nothing concurrent happened) op.
                ws_a.send_json({"type": "op", "v": v_a, "ops": [{"p": 0, "i": "XX"}]})
                ack = ws_a.receive_json()
                self.assertEqual(ack["type"], "ack")
                self.assertEqual(ack["v"], v_a)
                self.assertEqual(ack["hash"], collab.content_hash("XXhello world"))

                op_frame = ws_b.receive_json()
                self.assertEqual(op_frame["type"], "op")
                self.assertEqual(op_frame["ops"], [{"p": 0, "i": "XX"}])
                self.assertEqual(op_frame["client"], "A")
                self.assertEqual(op_frame["name"], "Alice")

                # Cursor relay: B's cursor reaches A, not B itself.
                ws_b.send_json({"type": "cursor", "anchor": 1, "head": 2})
                cursor = ws_a.receive_json()
                self.assertEqual(cursor, {"type": "cursor", "client": "B", "anchor": 1, "head": 2})

                # Debounced flush: both get "saved", and the file now
                # reflects A's edit.
                saved_a = ws_a.receive_json()
                self.assertEqual(saved_a["type"], "saved")
                saved_b = ws_b.receive_json()
                self.assertEqual(saved_b["type"], "saved")
                self.assertEqual(saved_a["v"], saved_b["v"])
                self.assertEqual(self.file.read_text(), "XXhello world")

            # B disconnects: A sees updated presence (just itself).
            presence_left = ws_a.receive_json()
            self.assertEqual(presence_left["type"], "presence")
            self.assertEqual([c["id"] for c in presence_left["clients"]], ["A"])

    def test_bad_path_is_rejected_with_error_and_close(self):
        with self.client.websocket_connect(
                "/api/v1/artifacts/collab?path=missing.md&name=Alice&client=A") as ws:
            frame = ws.receive_json()
            self.assertEqual(frame["type"], "error")

    def test_non_editable_extension_is_rejected(self):
        script = self.repo / ".the_lab" / "artifacts" / "run.sh"
        script.write_text("echo hi")
        with self.client.websocket_connect(
                "/api/v1/artifacts/collab?path=run.sh&name=Alice&client=A") as ws:
            frame = ws.receive_json()
            self.assertEqual(frame["type"], "error")


if __name__ == "__main__":
    unittest.main()


class FlushRaceTests(unittest.IsolatedAsyncioTestCase):
    """Ops applied while a flush is writing must not be recorded as flushed."""

    async def asyncSetUp(self):
        from the_lab import collab
        from the_lab.routes import experiments
        self.collab = collab
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name).resolve()
        (self.repo / ".the_lab" / "artifacts").mkdir(parents=True)
        self.file = self.repo / ".the_lab" / "artifacts" / "race.md"
        self.file.write_text("abc")
        for p in (patch.object(experiments, "REPO_DIR", self.repo), patch("the_lab.deps.REPO_DIR", self.repo)):
            p.start(); self.addCleanup(p.stop)

    async def test_op_during_write_is_flushed_later(self):
        collab = self.collab
        doc = await collab.get_doc("race.md", self.file)
        self.addCleanup(lambda: collab.docs.pop("race.md", None))
        collab.submit_op(doc, "a", "A", "#58a6ff", 0, [{"p": 3, "i": "1"}])
        real_to_thread = collab.asyncio.to_thread
        calls = {"n": 0}

        async def slow_to_thread(fn, *a, **kw):
            calls["n"] += 1
            if calls["n"] == 1:   # the write: let another op land meanwhile
                collab.submit_op(doc, "b", "B", "#f0883e", doc.version, [{"p": 0, "i": "X"}])
            return await real_to_thread(fn, *a, **kw)

        with patch.object(collab.asyncio, "to_thread", slow_to_thread):
            await collab.flush_doc(doc)
        self.assertEqual(self.file.read_text(), "abc1")
        self.assertEqual(doc.flushed_text, "abc1")
        await collab.flush_doc(doc)
        self.assertEqual(self.file.read_text(), "Xabc1")
