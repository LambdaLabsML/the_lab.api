"""Score T3: Error Recovery — did the agent diagnose and fix failures?"""
import json
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
from score_common import LabClient, api_call_count, endpoint_was_called, score_result

SEED_IDEAS = 3
SEED_FAILED_EXPS = 4  # exp_ids 2,3,4,5


def score(api_url: str) -> dict:
    client = LabClient(api_url)
    ideas = client.ideas()
    stats = client.stats()
    calls = api_call_count(stats)

    checks = {}

    # Did the agent read experiment logs?
    checks["read_logs"] = 1.0 if endpoint_was_called(stats, "/log") else 0.0

    # Did the agent check experiment status (GET /experiments)?
    checks["checked_status"] = 1.0 if endpoint_was_called(stats, "GET /api/v1/experiments/") else 0.0

    # Count new successful experiments
    new_successes = 0
    best_new_score = 0.0
    for idea in ideas:
        for exp in client.experiments(idea["id"]):
            if exp["id"] > 5 and exp.get("status") == "completed":  # after seed
                new_successes += 1
                s = (exp.get("metrics") or {}).get("score", 0)
                best_new_score = max(best_new_score, s)

    # Fixed at least some failures (created successful replacements)?
    checks["experiments_fixed"] = min(new_successes / 2, 1.0)

    # Score improved over baseline (0.50)?
    checks["score_improved"] = min(best_new_score / 0.50, 1.0) if best_new_score > 0.50 else (best_new_score / 0.50 if best_new_score > 0 else 0.0)

    # Did the agent create notes explaining what was wrong?
    total_notes = 0
    for idea in ideas:
        try:
            notes = client.get(f"/ideas/{idea['id']}/notes")
            total_notes += len(notes)
        except Exception:
            pass
    seed_notes = 2
    new_notes = max(total_notes - seed_notes, 0)
    checks["documented_errors"] = min(new_notes / 1, 1.0)

    return score_result("t3_error_recovery", checks, calls)


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:9000/api/v1"
    print(json.dumps(score(url), indent=2))
