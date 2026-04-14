"""Score T7: Analytics — can the agent answer data questions efficiently?

Tests multi-step data analysis: grouping, filtering, hierarchical aggregation,
and statistical reasoning (fluke vs reliable results).

Fixture properties:
- Ideas 5, 10, 12, 13 have 3 experiments each
- Idea 12 is the "fluke": one high score (0.85) + two low repeats (0.30, 0.28)
- Idea 13 is the "reliable": consistent scores (0.72, 0.74, 0.71)
"""
import json
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
from score_common import LabClient, api_call_count, endpoint_was_called, new_ideas_after, score_result

SEED_IDEAS = 15
SEED_BEST_SCORE = 0.75

# Pre-computed correct answers from the fixture
# Q1: hierarchical per-approach means (average within idea, then across ideas)
CORRECT_TABLE_MEAN = 0.3793    # ideas 1-5
CORRECT_POLY_MEAN = 0.3593     # ideas 6-10
CORRECT_HYBRID_MEAN = 0.5333   # ideas 11-13 (excl 14 over-budget, 15 no exp)

# Q5: Most promising idea is 13 (reliable: 0.72, 0.74, 0.71)
# NOT idea 12 (fluke: 0.85, 0.30, 0.28)
RELIABLE_IDEA = 13
FLUKE_IDEA = 12


def _notes_text(client: LabClient, ideas: list[dict]) -> str:
    """Collect all note text for keyword analysis."""
    all_text = []
    for idea in ideas:
        try:
            notes = client.get(f"/ideas/{idea['id']}/notes")
            for note in notes:
                all_text.append(note.get("text", "").lower())
        except Exception:
            pass
    return " ".join(all_text)


def score(api_url: str) -> dict:
    client = LabClient(api_url)
    ideas = client.ideas()
    stats = client.stats()
    calls = api_call_count(stats)
    call_list = stats.get("calls", [])

    new_ideas = new_ideas_after(ideas, SEED_IDEAS)
    notes_text = _notes_text(client, ideas)
    checks = {}

    # ── Q1: Per-approach means documented ──────────────────────────────
    # Check if the agent wrote notes with per-approach averages
    has_approach_analysis = (
        any(kw in notes_text for kw in ["table-heavy", "table heavy", "table approach"])
        and any(kw in notes_text for kw in ["polynomial", "poly approach"])
        and any(kw in notes_text for kw in ["hybrid"])
        and any(kw in notes_text for kw in ["mean", "average", "avg"])
    )
    checks["approach_means_documented"] = 1.0 if has_approach_analysis else 0.0

    # ── Q1b: Hierarchical mean correct (not flat) ─────────────────────
    # Check if the reported hybrid mean is closer to 0.53 (hierarchical)
    # than to 0.57 (flat). We look for numbers in notes near the correct values.
    import re
    numbers_in_notes = [float(m) for m in re.findall(r"0\.\d{2,4}", notes_text)]
    # Check if any reported number is close to the hierarchical hybrid mean
    hierarchical_close = any(abs(n - CORRECT_HYBRID_MEAN) < 0.03 for n in numbers_in_notes)
    flat_close = any(abs(n - 0.5714) < 0.03 for n in numbers_in_notes)
    if hierarchical_close and not flat_close:
        checks["hierarchical_mean_correct"] = 1.0
    elif hierarchical_close:
        checks["hierarchical_mean_correct"] = 0.7  # got it right but maybe also reported flat
    elif flat_close:
        checks["hierarchical_mean_correct"] = 0.3  # used flat mean (wrong methodology)
    else:
        checks["hierarchical_mean_correct"] = 0.0

    # ── Q4: Identified over-budget experiments ─────────────────────────
    over_budget_noted = any(
        kw in notes_text
        for kw in ["4608", "over budget", "over-budget", "exceeds budget",
                    "above budget", "memory exceeded"]
    )
    checks["identified_over_budget"] = 1.0 if over_budget_noted else 0.0

    # ── Q5: Chose reliable idea over fluke ─────────────────────────────
    # Check if agent identified idea 13 as most promising (not idea 12)
    chose_reliable = False
    chose_fluke = False
    # Check notes for recommendation
    if any(kw in notes_text for kw in ["idea 13", "idea/13", "#13"]):
        if any(kw in notes_text for kw in ["promising", "continue", "best", "reliable",
                                            "consistent", "recommend", "most stable"]):
            chose_reliable = True
    if any(kw in notes_text for kw in ["idea 12", "idea/12", "#12"]):
        if any(kw in notes_text for kw in ["promising", "continue", "best"]):
            chose_fluke = True

    # Also check if agent branched from idea 13 (action speaks louder)
    for idea in new_ideas:
        if RELIABLE_IDEA in idea.get("parent_ids", []):
            chose_reliable = True
        if FLUKE_IDEA in idea.get("parent_ids", []) and RELIABLE_IDEA not in idea.get("parent_ids", []):
            chose_fluke = True

    if chose_reliable and not chose_fluke:
        checks["chose_reliable_over_fluke"] = 1.0
    elif chose_reliable:
        checks["chose_reliable_over_fluke"] = 0.7  # mentioned both but favored reliable
    elif chose_fluke:
        checks["chose_reliable_over_fluke"] = 0.0  # fell for the fluke
    else:
        checks["chose_reliable_over_fluke"] = 0.0

    # ── Q5b: Recognized variance/reproducibility issue ────────────────
    variance_noted = any(
        kw in notes_text
        for kw in ["variance", "reproducib", "inconsisten", "outlier", "fluke",
                    "cannot reproduce", "not reproducible", "one-time", "unreliable",
                    "spread", "standard deviation", "std"]
    )
    checks["recognized_variance"] = 1.0 if variance_noted else 0.0

    # ── Q6: Understood convergence_gap direction ───────────────────────
    # convergence_gap = 1.0 - score, so it should be MINIMIZED (sort=asc).
    # Primary signal: did the agent use sort=asc when querying the leaderboard
    # for convergence_gap? That proves they understood the direction.
    used_asc = any(
        "convergence_gap" in c.get("endpoint", "") and "sort=asc" in c.get("endpoint", "")
        for c in call_list
        if "/leaderboard" in c.get("endpoint", "")
    )
    queried_gap = any(
        "convergence_gap" in c.get("endpoint", "")
        for c in call_list
        if "/leaderboard" in c.get("endpoint", "")
    )
    if used_asc:
        checks["understood_convergence_gap"] = 1.0
    elif queried_gap:
        checks["understood_convergence_gap"] = 0.3  # queried but wrong sort direction
    else:
        checks["understood_convergence_gap"] = 0.0

    # ── Query efficiency ──────────────────────────────────────────────
    # Penalize reading ideas one-by-one when bulk endpoints exist
    individual_idea_gets = sum(
        1 for c in call_list
        if "GET /api/v1/ideas/" in c.get("endpoint", "")
        and "/search" not in c.get("endpoint", "")
        and "/experiments" not in c.get("endpoint", "")
        and "/notes" not in c.get("endpoint", "")
    )
    checks["query_efficiency"] = max(0.0, 1.0 - individual_idea_gets / 15)

    # ── Used tag filtering ────────────────────────────────────────────
    checks["used_tag_filter"] = 1.0 if (
        any("tags=" in c.get("endpoint", "") for c in call_list
            if "/leaderboard" in c.get("endpoint", "") or "/orient" in c.get("endpoint", ""))
    ) else 0.0

    # ── Score improved ────────────────────────────────────────────────
    best_new_score = 0.0
    for idea in new_ideas:
        for exp in client.experiments(idea["id"]):
            if exp.get("status") == "completed":
                s = (exp.get("metrics") or {}).get("score", 0)
                best_new_score = max(best_new_score, s)
    checks["score_improved"] = (
        min(best_new_score / SEED_BEST_SCORE, 1.0) if best_new_score > 0 else 0.0
    )

    # ── Documented analysis (wrote notes with answers) ────────────────
    seed_notes = 20
    total_notes = 0
    for idea in ideas:
        try:
            total_notes += len(client.get(f"/ideas/{idea['id']}/notes"))
        except Exception:
            pass
    new_notes = max(total_notes - seed_notes, 0)
    checks["documented_analysis"] = min(new_notes / 3, 1.0)

    return score_result("t7_analytics", checks, calls, max_calls=25)


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:9000/api/v1"
    print(json.dumps(score(url), indent=2))
