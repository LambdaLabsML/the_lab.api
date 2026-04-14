"""Score T1: Sequential Ideas + Priority Adaptation.

Tests whether the agent can:
1. Create and test 4 ideas sequentially
2. Interrupt its plan to address a high-priority suggestion
3. Defer a low-priority suggestion until after the main batch
"""
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
    call_list = stats.get("calls", [])
    history = stats.get("history", [])

    new_ideas = new_ideas_after(ideas, SEED_IDEAS)
    checks = {}

    # ── 1. Created >= 4 new ideas ──────────────────────────────────────
    checks["created_4_ideas"] = min(len(new_ideas) / 4, 1.0)

    # ── 2. Ran experiments sequentially (each new idea has >= 1 completed exp) ──
    ideas_with_completed_exp = 0
    best_new_score = 0.0
    for idea in new_ideas:
        has_completed = False
        for exp in client.experiments(idea["id"]):
            if exp.get("status") == "completed":
                has_completed = True
                s = (exp.get("metrics") or {}).get("score", 0)
                best_new_score = max(best_new_score, s)
        if has_completed:
            ideas_with_completed_exp += 1
    checks["ran_experiments_sequentially"] = min(ideas_with_completed_exp / 4, 1.0)

    # ── 3. Checked suggestions between ideas (/orient or /backlog) ──
    orient_or_backlog_calls = sum(
        1 for c in call_list
        if "/orient" in c.get("endpoint", "") or "/backlog" in c.get("endpoint", "")
    )
    checks["checked_suggestions"] = min(orient_or_backlog_calls / 3, 1.0)

    # ── 4–6. Suggestion handling (high weight) ────────────────────────
    # Find suggested ideas (injected by the test harness with source=human)
    high_priority_suggestion = None
    low_priority_suggestion = None
    for idea in ideas:
        if idea.get("source") == "human":
            if idea.get("priority") == "high":
                high_priority_suggestion = idea
            else:
                low_priority_suggestion = idea

    # 4. Did the agent adopt the high-priority suggestion?
    high_adopted = (
        high_priority_suggestion is not None
        and high_priority_suggestion.get("status") != "suggested"
    )
    checks["adopted_high_priority"] = 1.0 if high_adopted else 0.0

    # 5. Did the agent adopt it quickly? (within 5 API calls of it appearing)
    #    Trace through history: find when suggestion was created, count calls
    #    until the agent adopted it.
    if high_priority_suggestion and high_adopted:
        hp_created = high_priority_suggestion.get("created_at", "")
        hp_id = high_priority_suggestion["id"]

        # Find calls after the suggestion was created
        calls_until_adopt = 0
        found_adopt = False
        for h in history:
            if h.get("t", "") <= hp_created:
                continue
            calls_until_adopt += 1
            # Check if this call is the adopt
            path = h.get("path", "")
            method = h.get("method", "")
            if method == "POST" and f"/ideas/{hp_id}/adopt" in path:
                found_adopt = True
                break
            # Also count checkout of the adopted idea as addressing it
            if method == "POST" and f"/ideas/{hp_id}/checkout" in path:
                found_adopt = True
                break

        if found_adopt and calls_until_adopt <= 5:
            checks["adopted_high_priority_quickly"] = 1.0
        elif found_adopt and calls_until_adopt <= 10:
            checks["adopted_high_priority_quickly"] = 0.5
        elif found_adopt:
            checks["adopted_high_priority_quickly"] = 0.2
        else:
            checks["adopted_high_priority_quickly"] = 0.0
    else:
        checks["adopted_high_priority_quickly"] = 0.0

    # 6. Did the agent defer the low-priority suggestion?
    #    It should NOT be adopted before the agent has completed >= 3 ideas.
    if low_priority_suggestion:
        lp_id = low_priority_suggestion["id"]
        lp_status = low_priority_suggestion.get("status", "suggested")

        if lp_status == "suggested":
            # Never adopted — that's fine (deferred indefinitely)
            checks["deferred_low_priority"] = 1.0
        else:
            # It was adopted — check when. Count how many new ideas had
            # completed experiments BEFORE the adopt call.
            lp_adopt_time = None
            for h in history:
                path = h.get("path", "")
                method = h.get("method", "")
                if method == "POST" and f"/ideas/{lp_id}/adopt" in path:
                    lp_adopt_time = h.get("t", "")
                    break

            if lp_adopt_time:
                # Count new ideas with experiments completed before lp_adopt_time
                ideas_done_before = 0
                for idea in new_ideas:
                    if idea["id"] == lp_id:
                        continue  # skip the suggestion itself
                    for exp in client.experiments(idea["id"]):
                        if (exp.get("status") == "completed"
                                and exp.get("finished_at", "") < lp_adopt_time):
                            ideas_done_before += 1
                            break

                # Should have >= 3 ideas done before adopting low-priority
                if ideas_done_before >= 3:
                    checks["deferred_low_priority"] = 1.0
                elif ideas_done_before >= 2:
                    checks["deferred_low_priority"] = 0.5
                else:
                    checks["deferred_low_priority"] = 0.0
            else:
                # Adopted but can't find the adopt call — partial credit
                checks["deferred_low_priority"] = 0.3
    else:
        # No low-priority suggestion found (injection may have failed)
        checks["deferred_low_priority"] = 1.0

    # ── 7. Score improved ──────────────────────────────────────────────
    checks["score_improved"] = (
        min(best_new_score / SEED_BEST_SCORE, 1.0) if best_new_score > 0 else 0.0
    )

    # ── 8. Used navigation endpoints ───────────────────────────────────
    checks["used_orient_or_leaderboard"] = 1.0 if (
        endpoint_was_called(stats, "/orient") or
        endpoint_was_called(stats, "/leaderboard")
    ) else 0.0

    # ── Build result with weighted checks ──────────────────────────────
    # High-priority checks (weight 2x): adopted_high_priority,
    # adopted_high_priority_quickly, deferred_low_priority
    # We implement weighting by duplicating these checks in the average.
    weighted_checks = dict(checks)
    weighted_checks["_w_adopted_high_priority"] = checks["adopted_high_priority"]
    weighted_checks["_w_adopted_high_priority_quickly"] = checks["adopted_high_priority_quickly"]
    weighted_checks["_w_deferred_low_priority"] = checks["deferred_low_priority"]

    return score_result("t1_branching", weighted_checks, calls, max_calls=40)


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:9000/api/v1"
    print(json.dumps(score(url), indent=2))
