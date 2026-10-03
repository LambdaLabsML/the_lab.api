"""Leaderboard ranking follows the configured metric direction."""
import unittest
from unittest.mock import patch

from the_lab.routes import overview


class LeaderboardDirectionTests(unittest.TestCase):
    def test_override_minimize_sorts_ascending(self):
        with patch("the_lab.deps._read_metric_directions", return_value={"score": "minimize"}):
            self.assertFalse(overview._resolve_descending("score", None))

    def test_default_heuristic(self):
        with patch("the_lab.deps._read_metric_directions", return_value={}):
            self.assertTrue(overview._resolve_descending("score", None))
            self.assertFalse(overview._resolve_descending("val_loss", None))

    def test_explicit_sort_wins(self):
        with patch("the_lab.deps._read_metric_directions", return_value={"score": "minimize"}):
            self.assertTrue(overview._resolve_descending("score", "desc"))
            self.assertFalse(overview._resolve_descending("score", "asc"))


if __name__ == "__main__":
    unittest.main()
