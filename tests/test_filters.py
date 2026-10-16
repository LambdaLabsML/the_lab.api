"""The experiment filter language and saved filters (the_lab/filters.py)."""
import tempfile
import unittest
from pathlib import Path

from the_lab import filters as F

EXPS = [
    {"id": 1, "label": "10.1", "idea_id": 10, "status": "completed", "tags": ["smoke"],
     "description": "Sampling arm A", "metrics": {"score": 12.5, "sub": {"hits": 3}},
     "meta": {"MAX_WALL_SECONDS": 1800, "SEED": 0, "MODE": "loose"}},
    {"id": 2, "label": "11.1", "idea_id": 11, "status": "completed", "tags": [],
     "description": "long run", "metrics": {"score": 25.0},
     "meta": {"MAX_WALL_SECONDS": "9000", "SEED": 1, "MODE": "strict"}},
    {"id": 3, "label": "12.1", "idea_id": 12, "status": "running", "tags": ["bench"],
     "description": "long run B", "metrics": {},
     "meta": {"MAX_WALL_SECONDS": 10800, "SEED": 5}},
    {"id": 4, "label": "1050.1", "idea_id": 13, "status": "failed", "tags": None,
     "description": None, "metrics": None, "meta": None},
]
IDEAS = [{"id": 10, "parent_ids": []}, {"id": 11, "parent_ids": [10]},
         {"id": 12, "parent_ids": [11]}, {"id": 13, "parent_ids": []}]


def sel(expr, saved=None, exclude=None):
    ctx = F.Context(children=F.idea_children(IDEAS),
                    saved=lambda name: (saved or {}).get(name))
    pred = F.compile_expr(expr, ctx, exclude=exclude)
    return [e["label"] for e in EXPS if pred(e)]


class FilterLanguageTests(unittest.TestCase):
    def test_meta_numeric_comparison_including_numeric_strings(self):
        self.assertEqual(sel("meta.MAX_WALL_SECONDS >= 9000"), ["11.1", "12.1"])
        self.assertEqual(sel("META.max_wall_seconds < 9000"), ["10.1"])   # case-insensitive

    def test_empty_expression_selects_everything(self):
        self.assertEqual(len(sel("")), 4)

    def test_boolean_logic_and_parentheses(self):
        self.assertEqual(sel("status = completed and (tag = smoke or metric.score > 20)"), ["10.1", "11.1"])
        self.assertEqual(sel("not status = completed"), ["12.1", "1050.1"])
        self.assertEqual(sel("status = running || status == failed"), ["12.1", "1050.1"])
        self.assertEqual(sel("!(status = completed) && tag = bench"), ["12.1"])

    def test_tags(self):
        self.assertEqual(sel("tag = smoke"), ["10.1"])
        self.assertEqual(sel("tag != smoke"), ["11.1", "12.1", "1050.1"])
        self.assertEqual(sel("tag in [smoke, bench]"), ["10.1", "12.1"])
        self.assertEqual(sel("tags exists"), ["10.1", "12.1"])

    def test_idea_and_descendants(self):
        self.assertEqual(sel("idea under 10"), ["10.1", "11.1", "12.1"])
        self.assertEqual(sel("idea = 13"), ["1050.1"])

    def test_labels_keep_their_text(self):
        self.assertEqual(sel("label = 1050.1"), ["1050.1"])
        self.assertEqual(sel("label not in [10.1, 11.1]"), ["12.1", "1050.1"])

    def test_missing_fields_and_exists(self):
        self.assertEqual(sel("meta.SEED missing"), ["1050.1"])
        self.assertEqual(sel("meta.MODE exists"), ["10.1", "11.1"])
        self.assertEqual(sel("meta.MODE != strict"), ["10.1"])   # absent never compares

    def test_contains_and_strings(self):
        self.assertEqual(sel('description ~ "LONG run"'), ["11.1", "12.1"])
        self.assertEqual(sel("description !~ long"), ["10.1"])
        self.assertEqual(sel("meta.MODE = 'loose'"), ["10.1"])

    def test_nested_metrics(self):
        self.assertEqual(sel("metric.sub.hits = 3"), ["10.1"])
        self.assertEqual(sel("metrics.score >= 12.5"), ["10.1", "11.1"])

    def test_saved_filter_references_and_exclusions(self):
        saved = {"long": {"id": "f1", "expr": "meta.MAX_WALL_SECONDS >= 9000", "exclude": ["12.1"]}}
        self.assertEqual(sel("@long", saved), ["11.1"])
        self.assertEqual(sel("@long or tag = smoke", saved), ["10.1", "11.1"])
        self.assertEqual(sel("", exclude=["10.1", "11.1"]), ["12.1", "1050.1"])

    def test_errors_are_reported_with_position(self):
        for bad in ("meta.X >=", "status = ", "foo = 1", "(status = running", "tag in [a b]",
                    "idea under x", "meta.X ?? 3", "status running"):
            with self.subTest(bad=bad):
                with self.assertRaises(F.FilterError):
                    sel(bad)
        with self.assertRaises(F.FilterError) as cm:
            sel("@nope")
        self.assertIn("unknown saved filter", str(cm.exception))

    def test_self_reference_is_rejected(self):
        saved = {"a": {"id": "f1", "expr": "@b"}, "b": {"id": "f2", "expr": "@a"}}
        with self.assertRaises(F.FilterError):
            sel("@a", saved)


class SavedFilterStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)

    def test_create_get_update_archive(self):
        rec = F.create_filter(self.repo, name="long", expr="meta.MAX_WALL_SECONDS >= 9000",
                              owner="user:david", shared=True)
        self.assertEqual(F.get_filter(self.repo, "LONG")["id"], rec["id"])
        self.assertEqual(F.get_filter(self.repo, "@long")["id"], rec["id"])
        spaced = F.create_filter(self.repo, name="legacy hidden", expr="", owner="user:david")
        self.assertEqual(F.get_filter(self.repo, "@legacy_hidden")["id"], spaced["id"])
        upd = F.update_filter(self.repo, rec["id"], exclude=["11.1", "11.1"], description="9000 s+")
        self.assertEqual(upd["exclude"], ["11.1"])
        F.update_filter(self.repo, rec["id"], archived=True)
        self.assertIsNone(F.get_filter(self.repo, "long"))
        self.assertEqual(len(F.list_filters(self.repo, include_archived=True)), 2)  # long (archived) + legacy hidden

    def test_names_are_unique_and_validated_and_expressions_checked(self):
        F.create_filter(self.repo, name="long", expr="", owner="user:david")
        with self.assertRaises(F.FilterError):
            F.create_filter(self.repo, name="Long", expr="", owner="user:alice")
        with self.assertRaises(F.FilterError):
            F.create_filter(self.repo, name="", expr="", owner="user:david")
        with self.assertRaises(F.FilterError):
            F.create_filter(self.repo, name="bad", expr="status =", owner="user:david")


if __name__ == "__main__":
    unittest.main()
