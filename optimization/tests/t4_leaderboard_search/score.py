"""Score T4: Leaderboard & Search — did the agent navigate efficiently?"""
import json
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
from score_common import LabClient, api_call_count, endpoint_was_called, new_ideas_after, score_result

SEED_IDEAS = 15
BEST_DIRECTION_IDS = {11, 12, 13}  # hybrid — best score 0.75
BEST_IDEA_ID = 13  # score 0.75
SEED_BEST_SCORE = 0.75


def score(api_url: str) -> dict:
    client = LabClient(api_url)
    ideas = client.ideas()
    stats = client.stats()
    calls = api_call_count(stats)

    new_ideas = new_ideas_after(ideas, SEED_IDEAS)
    checks = {}

    # Used leaderboard?
    checks["used_leaderboard"] = 1.0 if endpoint_was_called(stats, "/leaderboard") else 0.0

    # Used search?
    checks["used_search"] = 1.0 if endpoint_was_called(stats, "/search") else 0.0

    # Used orient?
    checks["used_orient"] = 1.0 if endpoint_was_called(stats, "/orient") else 0.0

    # Did the agent choose the best direction (hybrid)?
    chose_best = 0.0
    if new_ideas:
        for idea in new_ideas:
            parents = set(idea.get("parent_ids", []))
            if parents & BEST_DIRECTION_IDS:
                chose_best = 1.0
                break
            # Polynomial direction (second best) gets partial credit
            elif parents & {6, 7, 8, 9, 10}:
                chose_best = max(chose_best, 0.6)
            # Table-heavy gets less credit
            elif parents & {1, 2, 3, 4, 5}:
                chose_best = max(chose_best, 0.3)
    checks["chose_best_direction"] = chose_best

    # Did the agent branch from the actual best idea?
    branched_from_best = 0.0
    if new_ideas:
        for idea in new_ideas:
            if BEST_IDEA_ID in idea.get("parent_ids", []):
                branched_from_best = 1.0
                break
    checks["branched_from_best"] = branched_from_best

    # Did the agent improve the score?
    best_new_score = 0.0
    for idea in new_ideas:
        for exp in client.experiments(idea["id"]):
            if exp.get("status") == "completed":
                s = (exp.get("metrics") or {}).get("score", 0)
                best_new_score = max(best_new_score, s)
    checks["score_improved"] = min(best_new_score / SEED_BEST_SCORE, 1.0) if best_new_score > 0 else 0.0

    # Did the agent look for related/similar ideas before creating new ones?
    # Check if /search was called with keywords related to the best direction
    search_calls = [
        c for c in stats.get("calls", [])
        if "/search" in c.get("endpoint", "") and "GET" in c.get("endpoint", "")
    ]
    checks["searched_related"] = min(len(search_calls) / 2, 1.0)

    # Efficiency: didn't read every single idea individually
    # (should use leaderboard/search instead)
    individual_idea_gets = sum(
        1 for c in stats.get("calls", [])
        if "GET /api/v1/ideas/" in c.get("endpoint", "") and "/search" not in c.get("endpoint", "")
    )
    checks["navigation_efficiency"] = max(0.0, 1.0 - individual_idea_gets / 15)

    return score_result("t4_leaderboard_search", checks, calls, max_calls=40)


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:9000/api/v1"
    print(json.dumps(score(url), indent=2))
