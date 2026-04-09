"""Shared scoring utilities for test fixtures."""
from __future__ import annotations

import json
import urllib.request
from pathlib import Path


class LabClient:
    """Lightweight Lab API client for scoring."""

    def __init__(self, base_url: str):
        self.base = base_url.rstrip("/")

    def get(self, path: str) -> dict:
        resp = urllib.request.urlopen(f"{self.base}{path}", timeout=10)
        return json.loads(resp.read())

    def ideas(self) -> list[dict]:
        return self.get("/ideas")

    def idea(self, idea_id: int) -> dict:
        return self.get(f"/ideas/{idea_id}")

    def experiments(self, idea_id: int) -> list[dict]:
        return self.get(f"/ideas/{idea_id}/experiments")

    def experiment(self, ref: str) -> dict:
        return self.get(f"/experiments/{ref}")

    def stats(self) -> dict:
        return self.get("/stats?pattern_length=2")

    def leaderboard(self, metric: str = "score") -> dict:
        return self.get(f"/leaderboard?metric={metric}")


def api_call_count(stats: dict) -> int:
    """Total API calls from stats."""
    return stats.get("total_calls", 0)


def endpoint_was_called(stats: dict, endpoint_substring: str) -> bool:
    """Check if an endpoint was called (by substring match in call list)."""
    for c in stats.get("calls", []):
        if endpoint_substring in c.get("endpoint", ""):
            return True
    return False


def new_ideas_after(ideas: list[dict], seed_count: int) -> list[dict]:
    """Return ideas created by the agent (after the pre-seeded ones)."""
    return [i for i in ideas if i["id"] > seed_count]


def best_score_across(client: LabClient, ideas: list[dict]) -> tuple[float, dict | None]:
    """Find the best score across all experiments in the given ideas."""
    best = 0.0
    best_exp = None
    for idea in ideas:
        for exp in client.experiments(idea["id"]):
            if exp.get("status") == "completed":
                score = (exp.get("metrics") or {}).get("score", 0)
                if score > best:
                    best = score
                    best_exp = exp
    return best, best_exp


def score_result(
    name: str,
    checks: dict[str, float],
    api_calls: int,
    max_calls: int = 50,
) -> dict:
    """Compute a test score from individual checks.

    Each check is 0.0-1.0. The total is a weighted average penalized by
    excessive API calls.
    """
    if not checks:
        return {"test": name, "score": 0.0, "checks": {}, "api_calls": api_calls}

    raw = sum(checks.values()) / len(checks)
    # Efficiency penalty: more than max_calls → linear penalty
    efficiency = max(0.0, 1.0 - max(0, api_calls - max_calls) / max_calls)
    final = raw * (0.7 + 0.3 * efficiency)  # 70% correctness, 30% efficiency

    return {
        "test": name,
        "score": round(final, 4),
        "raw_score": round(raw, 4),
        "efficiency": round(efficiency, 4),
        "checks": {k: round(v, 4) for k, v in checks.items()},
        "api_calls": api_calls,
    }
