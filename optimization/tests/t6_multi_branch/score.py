"""Score T6: Multi-Branch Workflow — did the agent work across multiple idea branches?"""
import json
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
from score_common import LabClient, api_call_count, endpoint_was_called, score_result

SEED_IDEAS = 15
SEED_BEST_SCORE = 0.75


def score(api_url: str) -> dict:
    client = LabClient(api_url)
    ideas = client.ideas()
    stats = client.stats()
    calls = api_call_count(stats)
    call_list = stats.get("calls", [])

    checks = {}

    # Did the agent check out >= 2 different ideas?
    # Look for POST /checkout or POST /ideas/new (auto-checkout)
    checkout_idea_ids = set()
    for c in call_list:
        ep = c.get("endpoint", "")
        # POST /ideas/{id}/checkout
        if "POST" in ep and "/checkout" in ep:
            # Extract idea_id from endpoint like "POST /api/v1/ideas/13/checkout"
            parts = ep.split("/")
            for i, part in enumerate(parts):
                if part == "ideas" and i + 1 < len(parts):
                    try:
                        checkout_idea_ids.add(int(parts[i + 1]))
                    except ValueError:
                        pass
        # POST /ideas/new also does auto-checkout — the response contains the new idea id
        # We can detect this via new ideas created
        if "POST" in ep and "/ideas/new" in ep:
            # This creates a new idea and auto-checkouts; we'll count it below
            pass

    # Also count new ideas (auto-checkout)
    new_idea_ids = set()
    for idea in ideas:
        if idea["id"] > SEED_IDEAS:
            new_idea_ids.add(idea["id"])

    all_checkout_ids = checkout_idea_ids | new_idea_ids
    checks["checked_out_multiple"] = min(len(all_checkout_ids) / 2, 1.0)

    # Did the agent create experiments on >= 2 different idea_ids?
    experiment_idea_ids = set()
    for idea in ideas:
        exps = client.experiments(idea["id"])
        # Count experiments beyond the seed (seeded ideas have 1 exp each, except 14=2, 15=0)
        seed_exp_count = {i: 1 for i in range(1, 16)}
        seed_exp_count[14] = 2
        seed_exp_count[15] = 0
        seed_count = seed_exp_count.get(idea["id"], 0)
        new_exps = [e for e in exps if e["id"] > 16]  # 16 total seed experiments
        if new_exps:
            experiment_idea_ids.add(idea["id"])
    checks["experiments_on_different_ideas"] = min(len(experiment_idea_ids) / 2, 1.0)

    # Correct branch context: experiments landed on the right idea
    # Analyze the call sequence: checkout X -> create experiment on X (not Y)
    current_branch_id = None
    correct_context = True
    mismatch_count = 0
    total_exp_creates = 0
    for c in call_list:
        ep = c.get("endpoint", "")
        # Track checkouts
        if "POST" in ep and "/checkout" in ep:
            parts = ep.split("/")
            for i, part in enumerate(parts):
                if part == "ideas" and i + 1 < len(parts):
                    try:
                        current_branch_id = int(parts[i + 1])
                    except ValueError:
                        pass
        # Track new idea creation (auto-checkout)
        if "POST" in ep and "/ideas/new" in ep:
            # The new idea auto-checks out; we'll infer the id from experiments
            current_branch_id = "new"
        # Track experiment creation
        if "POST" in ep and "/experiments" in ep and "/start" not in ep:
            total_exp_creates += 1
            # Extract idea_id from endpoint like "POST /api/v1/ideas/13/experiments"
            parts = ep.split("/")
            for i, part in enumerate(parts):
                if part == "ideas" and i + 1 < len(parts):
                    try:
                        exp_idea_id = int(parts[i + 1])
                        if current_branch_id is not None and current_branch_id != "new" and exp_idea_id != current_branch_id:
                            mismatch_count += 1
                    except ValueError:
                        pass

    checks["correct_branch_context"] = 1.0 if (total_exp_creates > 0 and mismatch_count == 0) else (
        max(0.0, 1.0 - mismatch_count / max(total_exp_creates, 1))
    )

    # No cross-branch confusion
    checks["no_cross_branch_confusion"] = 1.0 if mismatch_count == 0 else max(0.0, 1.0 - mismatch_count * 0.5)

    # Used orient or backlog to check current branch?
    checks["used_orient_or_backlog"] = 1.0 if (
        endpoint_was_called(stats, "/orient") or
        endpoint_was_called(stats, "/backlog")
    ) else 0.0

    # Compared results: added notes comparing the two approaches
    total_new_notes = 0
    comparison_notes = 0
    for idea in ideas:
        try:
            notes = client.get(f"/ideas/{idea['id']}/notes")
            for note in notes:
                text = note.get("text", "").lower()
                # Check if it's a new note (not seeded) by checking for comparison keywords
                if any(kw in text for kw in ["compar", "vs", "versus", "better", "worse", "promising", "prefer"]):
                    comparison_notes += 1
                total_new_notes += 1
        except Exception:
            pass
    # Seeded notes total is about 20
    seed_notes = 20
    new_notes = max(total_new_notes - seed_notes, 0)
    checks["compared_results"] = min(comparison_notes / 1, 1.0)

    # Score improved: at least one new experiment beats 0.75
    best_new_score = 0.0
    for idea in ideas:
        for exp in client.experiments(idea["id"]):
            if exp.get("status") == "completed":
                s = (exp.get("metrics") or {}).get("score", 0)
                best_new_score = max(best_new_score, s)
    checks["score_improved"] = min(best_new_score / SEED_BEST_SCORE, 1.0) if best_new_score > SEED_BEST_SCORE else (
        best_new_score / SEED_BEST_SCORE if best_new_score > 0 else 0.0
    )

    return score_result("t6_multi_branch", checks, calls, max_calls=30)


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:9000/api/v1"
    print(json.dumps(score(url), indent=2))
