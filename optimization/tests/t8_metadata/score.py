"""Score T8: Metadata Comprehension — does the agent understand tags and metrics?

Tests whether the agent:
1. Normalizes messy tags
2. Understands what each tag represents (documents purpose)
3. Understands metric semantics (direction, meaning)
4. Documents new tags when creating them
"""
import json
import re
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
from score_common import LabClient, api_call_count, endpoint_was_called, endpoint_query_matched, new_ideas_after, score_result

SEED_IDEAS = 15
SEED_BEST_SCORE = 0.75
SEED_NOTES = 20


def score(api_url: str) -> dict:
    client = LabClient(api_url)
    ideas = client.ideas()
    stats = client.stats()
    calls = api_call_count(stats)
    call_list = stats.get("calls", [])
    history = stats.get("history", [])

    new_ideas = new_ideas_after(ideas, SEED_IDEAS)
    checks = {}

    # ── 1. Listed tags ─────────────────────────────────────────────────
    checks["listed_tags"] = 1.0 if endpoint_was_called(stats, "/experiments/tags") else 0.0

    # ── 2. Renamed tags ───────────────────────────────────────────────
    rename_calls = sum(1 for c in call_list if "/tags/rename" in c.get("endpoint", ""))
    checks["renamed_tags"] = min(rename_calls / 1, 1.0)

    # ── 3. Tags normalized ────────────────────────────────────────────
    try:
        tags_data = client.get("/experiments/tags")
        if isinstance(tags_data, list):
            all_tags = set(t.lower() if isinstance(t, str) else str(t).lower() for t in tags_data)
        elif isinstance(tags_data, dict):
            all_tags = set(k.lower() for k in tags_data.keys()) if tags_data else set()
        else:
            all_tags = set()

        messy_variants = {"table", "table-v1", "polynomial", "poly", "hybrid", "hybrid-approach"}
        remaining_variants = messy_variants & all_tags
        if len(remaining_variants) <= 3:
            checks["tags_normalized"] = 1.0
        elif len(remaining_variants) <= 4:
            checks["tags_normalized"] = 0.7
        else:
            checks["tags_normalized"] = max(0.0, 1.0 - (len(remaining_variants) - 3) / 3)
    except Exception:
        checks["tags_normalized"] = 0.0

    # ── 4. Understood metric direction ──────────────────────────────
    # Check via leaderboard usage: did the agent use the correct sort order?
    # sort=asc for convergence_gap (lower is better) proves understanding.
    used_correct_sort = endpoint_query_matched(stats, "/leaderboard", ["convergence_gap", "sort=asc"])
    # Also accept sort=desc for score (higher is better) — but since desc is
    # the default, only count it if they explicitly passed sort=desc
    explicit_desc_score = endpoint_query_matched(stats, "/leaderboard", ["metric=score", "sort=desc"])
    checks["understood_metric_direction"] = 1.0 if (used_correct_sort or explicit_desc_score) else 0.0

    # ── 5. Described tag purposes (notes explaining what tags mean) ───
    # Collect all notes text
    all_notes_text = []
    for idea in ideas:
        try:
            notes = client.get(f"/ideas/{idea['id']}/notes")
            for note in notes:
                all_notes_text.append(note.get("text", "").lower())
        except Exception:
            pass
    notes_text = " ".join(all_notes_text)

    # Check if notes explain what the main tags represent
    tag_explanations = 0
    # Table-heavy: agent explained it's about lookup tables
    if any(kw in notes_text for kw in ["table-heavy", "table heavy"]):
        if any(kw in notes_text for kw in ["lookup", "interpolat", "precomputed",
                                            "stores values", "table-based"]):
            tag_explanations += 1
    # Polynomial: agent explained it's about mathematical approximations
    if "polynomial" in notes_text:
        if any(kw in notes_text for kw in ["chebyshev", "padé", "pade", "approximat",
                                            "no table", "no memory", "zero memory"]):
            tag_explanations += 1
    # Hybrid: agent explained it combines both approaches
    if "hybrid" in notes_text:
        if any(kw in notes_text for kw in ["combin", "table + poly", "table and poly",
                                            "small table", "both", "correction"]):
            tag_explanations += 1
    checks["described_tag_purposes"] = min(tag_explanations / 2, 1.0)

    # ── 6. Mapped tags to experiments ─────────────────────────────────
    # Agent identified which experiments belong to which tags
    mapped = any(
        kw in notes_text
        for kw in ["ideas 1-5", "ideas 1 to 5", "ideas 1 through 5",
                    "ideas 6-10", "ideas 6 to 10",
                    "ideas 11-15", "ideas 11 to 15",
                    "5 experiment", "5 idea"]
    )
    # Also accept if they used tag filtering to explore
    used_tag_filter = (
        endpoint_query_matched(stats, "/leaderboard", ["tags="])
        or endpoint_query_matched(stats, "/orient", ["tags="])
    )
    checks["mapped_tags_to_experiments"] = 1.0 if (mapped or used_tag_filter) else 0.0

    # ── 7. Described metric semantics ─────────────────────────────────
    # Notes explain what metrics mean and their optimization direction
    metric_understanding = 0
    if any(kw in notes_text for kw in ["score", "composite"]):
        if any(kw in notes_text for kw in ["maximize", "higher is better", "higher = better",
                                            "geometric mean", "maximiz"]):
            metric_understanding += 1
    if any(kw in notes_text for kw in ["memory", "bytes"]):
        if any(kw in notes_text for kw in ["budget", "4096", "minimize", "within",
                                            "constraint", "limit"]):
            metric_understanding += 1
    checks["described_metric_semantics"] = min(metric_understanding / 1, 1.0)

    # ── 8. Documented new tag on new experiment ───────────────────────
    # When creating a new experiment with a new/existing tag, the agent
    # should also write a note explaining what the tag means.
    new_exp_has_tag = False
    new_tag_documented = False
    for idea in new_ideas:
        exps = client.experiments(idea["id"])
        for exp in exps:
            if exp.get("tags"):
                new_exp_has_tag = True
                # Check if any note on this idea mentions the tag and its purpose
                try:
                    idea_notes = client.get(f"/ideas/{idea['id']}/notes")
                    idea_notes_text = " ".join(n.get("text", "").lower() for n in idea_notes)
                    for tag in exp["tags"]:
                        tag_lower = tag.lower()
                        if tag_lower in idea_notes_text:
                            # Note mentions the tag — check if it explains its purpose
                            if any(kw in idea_notes_text for kw in [
                                "tag", "label", "approach", "strategy", "method",
                                "represent", "denote", "indicate", "mean",
                            ]):
                                new_tag_documented = True
                except Exception:
                    pass

    if new_exp_has_tag and new_tag_documented:
        checks["documented_new_tag"] = 1.0
    elif new_exp_has_tag:
        checks["documented_new_tag"] = 0.0  # tagged but didn't explain
    else:
        checks["documented_new_tag"] = 0.0  # no tags at all

    # ── 9. Score improved ─────────────────────────────────────────────
    best_new_score = 0.0
    for idea in new_ideas:
        for exp in client.experiments(idea["id"]):
            if exp.get("status") == "completed":
                s = (exp.get("metrics") or {}).get("score", 0)
                best_new_score = max(best_new_score, s)
    checks["score_improved"] = (
        min(best_new_score / SEED_BEST_SCORE, 1.0) if best_new_score > 0 else 0.0
    )

    return score_result("t8_metadata", checks, calls, max_calls=35)


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:9000/api/v1"
    print(json.dumps(score(url), indent=2))
