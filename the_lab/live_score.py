"""Live ARC-AGI-3 score of a running experiment, computed lab-side (lab 6588 / 6596; fak33).

The agent writes, per game, ``n_levels_completed`` and ``level_actions`` (real actions per level as
the scorecard counts them) into its progress file. The lab owns the per-level human baselines (every
closed scorecard carries ``level_baseline_actions``; they are constant per game) and applies the
official formula here. The agent never sees the baselines, so competition mode is unchanged.

Formula (arc3 scorecard, mirrored by agent/state.py GameState.score):
  per game: s_i = min((baseline_i / actions_i)^2 * 100, 115) for each completed level i,
            score = sum_i (i + 1) * s_i / sum_all_levels (i + 1),
            capped at the completed levels' weight share * 100;
  run: sum of game scores / number of environments on the scorecard (unstarted games count 0).
"""
from __future__ import annotations

from typing import Any


def baselines_from_scorecard(scorecard: dict[str, Any] | None) -> dict[str, list[int]]:
    """{environment id: level_baseline_actions} from a closed scorecard."""
    out: dict[str, list[int]] = {}
    for env in (scorecard or {}).get("environments") or []:
        for run in env.get("runs") or []:
            baseline = run.get("level_baseline_actions")
            if baseline:
                out[str(env.get("id"))] = [int(b) for b in baseline]
                break
    return out


def game_score(baseline: list[int], level_actions: list[int], levels_completed: int) -> float:
    if not baseline:
        return 0.0
    weights = [i + 1 for i in range(len(baseline))]
    done = max(0, min(int(levels_completed), len(baseline)))
    total = 0.0
    scored_weight = 0
    for i in range(done):
        actions = int(level_actions[i]) if i < len(level_actions) else 0
        level = 100.0 if baseline[i] == 0 else min((baseline[i] / max(1, actions)) ** 2 * 100, 115.0)
        total += level * weights[i]
        if level > 0:
            scored_weight += weights[i]
    return min(total / sum(weights), scored_weight / sum(weights) * 100)


def live_score(progress: dict[str, Any] | None, baselines: dict[str, list[int]],
               n_environments: int = 25) -> float | None:
    """Run score from a progress payload's ``per_game`` block, or None when the progress has no
    per-level action counts (agent builds before the field existed) or a game has no baseline."""
    per_game = (progress or {}).get("per_game") or {}
    if not per_game or not baselines:
        return None
    total = 0.0
    for game_id, entry in per_game.items():
        if not isinstance(entry, dict) or "level_actions" not in entry:
            return None
        baseline = baselines.get(str(game_id))
        if baseline is None:
            return None
        total += game_score(baseline, list(entry.get("level_actions") or []),
                            int(entry.get("n_levels_completed") or 0))
    return total / max(1, int(n_environments))


def with_live_score(progress: dict[str, Any], baselines: dict[str, list[int]]) -> dict[str, Any]:
    """``progress`` plus ``live_score`` (exact score so far of the current attempt, a lower bound
    on the final) when the agent wrote per-level action counts; ``score`` then shows it and the
    agent's own value moves to ``progress_score``. Returns a copy; never modifies the input."""
    if not isinstance(progress, dict):
        return progress
    live = live_score(progress, baselines)
    if live is None:
        return progress
    return {**progress, "progress_score": progress.get("score"), "score": live, "live_score": live,
            "score_source": "live (current attempt; closed scorecard is final)"}
