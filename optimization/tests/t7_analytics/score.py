"""Score T7: Analytics & Tag Management — did the agent organize tags and analyze?"""
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

    new_ideas = new_ideas_after(ideas, SEED_IDEAS)
    checks = {}

    # Did the agent list tags?
    checks["listed_tags"] = 1.0 if endpoint_was_called(stats, "/experiments/tags") else 0.0

    # Did the agent rename tags?
    rename_calls = sum(1 for c in call_list if "/tags/rename" in c.get("endpoint", ""))
    checks["renamed_tags"] = min(rename_calls / 1, 1.0)

    # Are tags normalized after the agent's work?
    # Check for duplicate variants being reduced
    try:
        tags_data = client.get("/experiments/tags")
        # tags_data should be a list of tag strings or a dict with tag info
        if isinstance(tags_data, list):
            all_tags = set(t.lower() if isinstance(t, str) else str(t).lower() for t in tags_data)
        elif isinstance(tags_data, dict):
            all_tags = set(k.lower() for k in tags_data.keys()) if tags_data else set()
        else:
            all_tags = set()

        # The messy fixture has variants like: table, Table, table-v1, polynomial, poly, Polynomial,
        # hybrid, Hybrid, hybrid-approach, over-budget, failed, merge
        # After normalization, we'd expect fewer distinct tag groups
        # Count how many "similar" tag groups exist
        messy_variants = {"table", "table-v1", "polynomial", "poly", "hybrid", "hybrid-approach"}
        remaining_variants = messy_variants & all_tags
        # Good normalization: 3 or fewer variant tags remain (table, polynomial, hybrid)
        # Bad: all 6+ variants still exist
        if len(remaining_variants) <= 3:
            checks["tags_normalized"] = 1.0
        elif len(remaining_variants) <= 4:
            checks["tags_normalized"] = 0.7
        else:
            checks["tags_normalized"] = max(0.0, 1.0 - (len(remaining_variants) - 3) / 3)
    except Exception:
        checks["tags_normalized"] = 0.0

    # Did the agent use tag filtering?
    checks["used_tag_filter"] = 1.0 if (
        any("tags=" in c.get("endpoint", "") for c in call_list if
            "/leaderboard" in c.get("endpoint", "") or "/orient" in c.get("endpoint", ""))
    ) else 0.0

    # Did the agent understand metric direction?
    checks["understood_metric_direction"] = 1.0 if endpoint_was_called(stats, "/metric-direction") else 0.0

    # Did the agent document analysis (mean scores, comparisons)?
    analysis_notes = 0
    total_new_notes = 0
    for idea in ideas:
        try:
            notes = client.get(f"/ideas/{idea['id']}/notes")
            for note in notes:
                text = note.get("text", "").lower()
                if any(kw in text for kw in ["mean", "average", "avg", "analysis", "best approach",
                                              "highest", "lowest", "performance", "comparison"]):
                    analysis_notes += 1
                total_new_notes += 1
        except Exception:
            pass
    seed_notes = 20
    new_notes = max(total_new_notes - seed_notes, 0)
    checks["documented_analysis"] = min(analysis_notes / 1, 1.0)

    # Score improved: created new experiment beating 0.75
    best_new_score = 0.0
    for idea in new_ideas:
        for exp in client.experiments(idea["id"]):
            if exp.get("status") == "completed":
                s = (exp.get("metrics") or {}).get("score", 0)
                best_new_score = max(best_new_score, s)
    checks["score_improved"] = min(best_new_score / SEED_BEST_SCORE, 1.0) if best_new_score > 0 else 0.0

    # Wait efficiency: no repeated /wait calls for the same experiment
    wait_calls = [c for c in call_list if "/wait" in c.get("endpoint", "")]
    wait_exp_ids = []
    for c in wait_calls:
        ep = c.get("endpoint", "")
        # Extract experiment_id from query like "/wait?experiment_id=5"
        if "experiment_id=" in ep:
            try:
                eid = ep.split("experiment_id=")[1].split("&")[0]
                wait_exp_ids.append(eid)
            except (IndexError, ValueError):
                pass
    # Check for duplicate wait calls on the same experiment
    if wait_exp_ids:
        unique_waits = len(set(wait_exp_ids))
        total_waits = len(wait_exp_ids)
        checks["wait_efficiency"] = unique_waits / total_waits if total_waits > 0 else 1.0
    else:
        checks["wait_efficiency"] = 1.0  # No wait calls is fine (maybe they didn't need to)

    return score_result("t7_analytics", checks, calls, max_calls=25)


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:9000/api/v1"
    print(json.dumps(score(url), indent=2))
