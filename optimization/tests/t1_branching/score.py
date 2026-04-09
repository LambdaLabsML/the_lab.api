"""Score T1: Branching — did the agent find the best idea and branch from it?"""
import json
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
from score_common import LabClient, api_call_count, endpoint_was_called, new_ideas_after, score_result

SEED_IDEAS = 15
BEST_IDEA_ID = 10  # score 0.95
GOOD_PARENT_IDS = {10, 7, 4}  # acceptable parents (the best branch)


def score(api_url: str) -> dict:
    client = LabClient(api_url)
    ideas = client.ideas()
    stats = client.stats()

    new_ideas = new_ideas_after(ideas, SEED_IDEAS)
    calls = api_call_count(stats)

    checks = {}

    # Did the agent create any new ideas?
    checks["created_idea"] = min(len(new_ideas) / 1, 1.0)

    # Did the new idea branch from the best idea (or its lineage)?
    if new_ideas:
        best_parent = 0.0
        for idea in new_ideas:
            parents = set(idea.get("parent_ids", []))
            if BEST_IDEA_ID in parents:
                best_parent = 1.0
            elif parents & GOOD_PARENT_IDS:
                best_parent = max(best_parent, 0.7)
            elif parents:
                best_parent = max(best_parent, 0.2)
        checks["branched_from_best"] = best_parent
    else:
        checks["branched_from_best"] = 0.0

    # Did the agent run an experiment on the new idea?
    new_exps_completed = 0
    best_new_score = 0.0
    for idea in new_ideas:
        for exp in client.experiments(idea["id"]):
            if exp.get("status") == "completed":
                new_exps_completed += 1
                s = (exp.get("metrics") or {}).get("score", 0)
                best_new_score = max(best_new_score, s)
    checks["ran_experiment"] = min(new_exps_completed / 1, 1.0)

    # Did the score improve?
    checks["score_improved"] = min(best_new_score / 0.95, 1.0) if best_new_score > 0 else 0.0

    # Did the agent use navigation endpoints?
    checks["used_orient_or_leaderboard"] = 1.0 if (
        endpoint_was_called(stats, "/orient") or
        endpoint_was_called(stats, "/leaderboard")
    ) else 0.0

    return score_result("t1_branching", checks, calls)


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:9000/api/v1"
    print(json.dumps(score(url), indent=2))
