"""Score T2: Experiment Management — did the agent iterate and improve?"""
import json
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
from score_common import LabClient, api_call_count, endpoint_was_called, new_ideas_after, best_score_across, score_result

SEED_IDEAS = 15
SEED_BEST_SCORE = 0.75


def score(api_url: str) -> dict:
    client = LabClient(api_url)
    ideas = client.ideas()
    stats = client.stats()
    calls = api_call_count(stats)

    checks = {}

    # Count new experiments (across all ideas, including seeded ones)
    # Each seeded idea has 1 experiment, except idea 14 (2 exps) and idea 15 (0 exps)
    seed_exp_counts = {i: 1 for i in range(1, 16)}
    seed_exp_counts[14] = 2
    seed_exp_counts[15] = 0
    total_new_exps = 0
    for idea in ideas:
        exps = client.experiments(idea["id"])
        seed_count = seed_exp_counts.get(idea["id"], 0)
        new_exps = len(exps) - seed_count
        total_new_exps += max(new_exps, 0)

    # Created enough experiments? (at least 3 iterations)
    checks["experiments_created"] = min(total_new_exps / 3, 1.0)

    # Used /wait instead of polling?
    checks["used_wait"] = 1.0 if endpoint_was_called(stats, "/wait") else 0.0

    # Used auto_start (script_content in experiment creation)?
    start_calls = sum(1 for c in stats.get("calls", []) if "/start" in c.get("endpoint", "") and "POST" in c.get("endpoint", ""))
    exp_create_calls = sum(1 for c in stats.get("calls", []) if "/experiments" in c.get("endpoint", "") and "POST" in c.get("endpoint", "") and "/start" not in c.get("endpoint", ""))
    checks["efficient_start"] = 1.0 if (start_calls == 0 and exp_create_calls > 0) else (0.5 if exp_create_calls > start_calls else 0.0)

    # Did the score improve?
    best, _ = best_score_across(client, ideas)
    improvement = (best - SEED_BEST_SCORE) / (1.0 - SEED_BEST_SCORE) if best > SEED_BEST_SCORE else 0.0
    checks["score_improved"] = min(improvement, 1.0)

    # Did the agent add notes documenting findings?
    total_notes = 0
    for idea in ideas:
        try:
            notes = client.get(f"/ideas/{idea['id']}/notes")
            total_notes += len(notes)
        except Exception:
            pass
    # Pre-seeded notes: count from SEED_IDEAS data
    seed_notes = 20  # total notes across all 15 seeded ideas
    new_notes = max(total_notes - seed_notes, 0)
    checks["documented_findings"] = min(new_notes / 2, 1.0)

    return score_result("t2_experiment_mgmt", checks, calls)


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:9000/api/v1"
    print(json.dumps(score(url), indent=2))
