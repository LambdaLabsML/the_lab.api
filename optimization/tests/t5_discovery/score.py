"""Score T5: Discovery & Adaptation — did the agent find undocumented endpoints?"""
import json
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
from score_common import LabClient, api_call_count, endpoint_was_called, new_ideas_after, score_result

SEED_IDEAS = 15
SEED_BEST_SCORE = 0.75


def score(api_url: str) -> dict:
    client = LabClient(api_url)
    ideas = client.ideas()
    stats = client.stats()
    calls = api_call_count(stats)

    new_ideas = new_ideas_after(ideas, SEED_IDEAS)
    checks = {}

    # Did the agent explore /openapi.json or /docs?
    checks["explored_openapi"] = 1.0 if (
        endpoint_was_called(stats, "/openapi.json") or
        endpoint_was_called(stats, "/docs")
    ) else 0.0

    # Did the agent discover GET /experiments/tags (not in stripped docs)?
    checks["discovered_tags"] = 1.0 if endpoint_was_called(stats, "/experiments/tags") else 0.0

    # Did the agent discover GET /leaderboard/search (not in stripped docs)?
    checks["discovered_leaderboard_search"] = 1.0 if endpoint_was_called(stats, "/leaderboard/search") else 0.0

    # Did the agent discover GET /experiments/log (aggregate endpoint, not in stripped docs)?
    checks["discovered_failed_logs"] = 1.0 if endpoint_was_called(stats, "/experiments/log") else 0.0

    # Did the agent actually USE a discovered endpoint meaningfully?
    # (created an idea after using leaderboard/search, or used correct metric direction)
    used_meaningfully = 0.0
    call_list = stats.get("calls", [])

    # Check if leaderboard/search was called BEFORE creating a new idea
    saw_leaderboard_search = False
    for c in call_list:
        ep = c.get("endpoint", "")
        if "/leaderboard/search" in ep:
            saw_leaderboard_search = True
        if saw_leaderboard_search and "POST" in ep and "/ideas" in ep:
            used_meaningfully = 1.0
            break

    # Also count it if they used /experiments/tags and then ran experiments
    if used_meaningfully < 1.0:
        saw_tags = False
        for c in call_list:
            ep = c.get("endpoint", "")
            if "/experiments/tags" in ep:
                saw_tags = True
            if saw_tags and "POST" in ep and "/experiments" in ep:
                used_meaningfully = 1.0
                break

    checks["used_discovered_feature"] = used_meaningfully

    # Did the score improve beyond the pre-seeded best (0.75)?
    best_new_score = 0.0
    for idea in new_ideas:
        for exp in client.experiments(idea["id"]):
            if exp.get("status") == "completed":
                s = (exp.get("metrics") or {}).get("score", 0)
                best_new_score = max(best_new_score, s)
    checks["score_improved"] = min(best_new_score / SEED_BEST_SCORE, 1.0) if best_new_score > 0 else 0.0

    return score_result("t5_discovery", checks, calls, max_calls=20)


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:9000/api/v1"
    print(json.dumps(score(url), indent=2))
