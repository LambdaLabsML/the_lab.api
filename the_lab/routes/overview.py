"""Overview endpoints: leaderboard, digest, wait, backlog, orient, graph, chart-data."""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Query

from ..deps import (
    store,
    runner,
    REPO_DIR,
    _idea_context,
    _branch_diff_summary,
    _read_task,
    metric_direction,
)
from ..git_ops import get_current_branch

router = APIRouter(prefix="/api/v1")


# --- Leaderboard helper (used only by leaderboard + digest) ---

def _build_leaderboard_response(
    metric: str, top: int, recent: int, tags: str | None, include_details: bool,
) -> dict:
    """Shared implementation for /leaderboard and /digest endpoints."""
    all_ideas = store.list_ideas()
    all_exps = store.list_all_experiments()

    idea_by_id: dict[int, dict] = {idea["id"]: idea for idea in all_ideas}
    exps_by_idea: dict[int, list[dict]] = {}
    for exp in all_exps:
        exps_by_idea.setdefault(exp["idea_id"], []).append(exp)

    def _idea_desc(idea_id: int) -> str | None:
        idea = idea_by_id.get(idea_id)
        return idea["description"] if idea else None

    def _public_meta(exp: dict) -> dict:
        return {
            k: v for k, v in (exp.get("meta") or {}).items()
            if k not in ("git_branch", "git_commit", "worktree")
        }

    def _last_activity(idea_exps: list[dict]) -> str | None:
        stamps = [
            e.get("finished_at") or e.get("started_at") or e.get("created_at")
            for e in idea_exps
            if e.get("finished_at") or e.get("started_at") or e.get("created_at")
        ]
        return max(stamps) if stamps else None

    open_ideas = []
    for idea in all_ideas:
        if idea.get("status") != "active":
            continue
        idea_exps = exps_by_idea.get(idea["id"], [])
        open_ideas.append({
            "id": idea["id"],
            "description": idea["description"],
            "source": idea.get("source", "agent"),
            "priority": idea.get("priority", "normal"),
            "running_experiments": sum(1 for e in idea_exps if e.get("status") == "running"),
            "pending_experiments": sum(1 for e in idea_exps if e.get("status") == "pending"),
            "last_activity": _last_activity(idea_exps),
        })

    running_experiments = []
    for exp in all_exps:
        if exp.get("status") != "running":
            continue
        running_experiments.append({
            "experiment_id": exp["id"],
            "idea_id": exp["idea_id"],
            "idea_description": _idea_desc(exp["idea_id"]),
            "description": exp.get("description"),
            "tags": exp.get("tags", []),
            "started_at": exp.get("started_at"),
            "runtime": exp.get("runtime"),
            "meta": _public_meta(exp),
        })
    running_experiments.sort(key=lambda e: e.get("started_at") or "", reverse=True)

    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
    filtered_exps = all_exps
    if tag_list:
        filtered_exps = [
            e for e in all_exps
            if all(t in (e.get("tags") or []) for t in tag_list)
        ]

    completed = [e for e in filtered_exps if e.get("status") == "completed" and e.get("metrics")]
    with_metric = [
        e for e in completed
        if metric in e["metrics"] and isinstance(e["metrics"][metric], (int, float))
    ]

    direction = metric_direction(metric)
    minimize = direction == "minimize"
    by_value = sorted(with_metric, key=lambda e: e["metrics"][metric], reverse=not minimize)
    leaderboard = []
    for exp in by_value[:top]:
        entry = {
            "experiment_id": exp["id"],
            "idea_id": exp["idea_id"],
            "idea_description": _idea_desc(exp["idea_id"]),
            "value": exp["metrics"][metric],
            "tags": exp.get("tags", []),
            "meta": _public_meta(exp),
            "finished_at": exp.get("finished_at"),
        }
        if include_details:
            entry["all_metrics"] = exp.get("metrics", {})
            entry["settings"] = exp.get("meta", {})
            entry["experiment_description"] = exp.get("description")
        leaderboard.append(entry)

    by_time_desc = sorted(with_metric, key=lambda e: e.get("finished_at") or "", reverse=True)
    recent_out = []
    for exp in by_time_desc[:recent]:
        entry = {
            "experiment_id": exp["id"],
            "idea_id": exp["idea_id"],
            "idea_description": _idea_desc(exp["idea_id"]),
            "value": exp["metrics"][metric],
            "tags": exp.get("tags", []),
            "finished_at": exp.get("finished_at"),
        }
        if include_details:
            entry["all_metrics"] = exp.get("metrics", {})
            entry["settings"] = exp.get("meta", {})
            entry["experiment_description"] = exp.get("description")
        recent_out.append(entry)

    best_idea = None
    if by_value:
        best_exp = by_value[0]
        idea = idea_by_id.get(best_exp["idea_id"])
        if idea:
            insights = [n["text"] for n in store.get_notes(idea["id"], levels={"insight"})]
            best_idea = {
                "id": idea["id"],
                "description": idea["description"],
                "parent_ids": idea.get("parent_ids", []),
                "branch": idea.get("branch"),
                "conclusion": idea.get("conclusion"),
                "best_value": best_exp["metrics"][metric],
                "key_insights": insights[:5],
                "branch_from_this": f"POST /ideas/new with parent_ids=[{idea['id']}]",
            }

    by_time_asc = sorted(with_metric, key=lambda e: e.get("finished_at") or "")
    progression = []
    running_best = None
    for exp in by_time_asc:
        v = exp["metrics"][metric]
        improved = running_best is None or (v < running_best if minimize else v > running_best)
        if improved:
            running_best = v
            progression.append({
                "experiment_id": exp["id"],
                "idea_id": exp["idea_id"],
                "idea_description": _idea_desc(exp["idea_id"]),
                "value": v,
                "finished_at": exp.get("finished_at"),
            })

    key_insights = []
    for idea in all_ideas:
        for note in store.get_notes(idea["id"], levels={"insight"}):
            key_insights.append({
                "text": note["text"],
                "idea_id": idea["id"],
                "created_at": note.get("created_at"),
            })
    key_insights.sort(key=lambda n: n.get("created_at", ""), reverse=True)

    # Extract a search keyword from the best idea's description
    search_hint = None
    if best_idea:
        desc = best_idea.get("description") or ""
        # Pick the longest non-stopword from the description as a search keyword
        stopwords = {"the", "a", "an", "and", "or", "for", "with", "from", "to", "in", "on", "by", "of", "is", "it", "as", "at", "this", "that", "all", "no", "not", "use", "try", "new", "idea"}
        words = [w.strip("()[]{}:,.'\"") for w in desc.lower().split() if len(w) > 2]
        keywords = [w for w in words if w not in stopwords and w.isalpha()]
        if keywords:
            # Prefer longer, more specific words
            kw = sorted(keywords, key=len, reverse=True)[0]
            search_hint = {
                "action": f"GET /ideas/search?q={kw}",
                "description": f"Find all ideas related to '{kw}' before branching",
            }
    if not search_hint:
        search_hint = {
            "action": "GET /ideas/search?q=keyword",
            "description": "Search for related ideas before branching",
        }

    return {
        "next_step": search_hint,
        "metric": metric,
        "direction": direction,
        "best_idea": best_idea,
        "leaderboard": leaderboard,
        "total_experiments_with_metric": len(with_metric),
        "open_ideas": open_ideas,
        "running_experiments": running_experiments,
        "recent": recent_out,
        "progression": progression,
        "key_insights": key_insights[:10],
    }


# --- Endpoints ---

@router.get("/leaderboard")
def get_leaderboard(
    metric: str = Query(..., description="Metric to rank by (e.g. accuracy, accuracy_per_mtoken)"),
    top: int = Query(default=10, description="Number of top experiments to show"),
    recent: int = Query(default=10, description="Number of most recent experiments to show"),
    tags: str | None = Query(default=None, description="Comma-separated tags — experiments must have ALL of them (AND filter)"),
    include_details: bool = Query(default=False, description="Include per-problem metrics and experiment settings/meta"),
):
    """Get a compact, metric-focused research leaderboard.

    Returns three sections ranked by the given metric:
    1. **Leaderboard** -- top N experiments by metric value (descending)
    2. **Recent** -- most recent N experiments with their metric value
    3. **Progression** -- timeline of when the global best was beaten

    Plus: open ideas, running experiments, key insights, and the best idea
    for this metric. Use ``tags=`` to scope to a subset of experiments.
    Set ``include_details=true`` to get full metrics, settings/meta, and
    experiment descriptions for each leaderboard and recent entry.

    Example:
        GET /api/v1/leaderboard?metric=accuracy&top=10&recent=5
        GET /api/v1/leaderboard?metric=accuracy_per_mtoken&top=5&tags=held-out&include_details=true
    """
    return _build_leaderboard_response(metric, top, recent, tags, include_details)


@router.get("/leaderboard/search")
def leaderboard_with_search(
    metric: str = Query(default="score", description="Metric to rank by"),
    q: str = Query(default="", description="Comma-separated keywords to search for in idea descriptions"),
    top: int = Query(default=10, description="Number of top experiments to show"),
):
    """Combined leaderboard + search: see rankings and find related ideas in one call.

    Returns the full leaderboard plus search results for the given keywords.
    Use this endpoint to orient yourself in one call instead of calling
    /leaderboard and /ideas/search separately.

    Example:
        GET /api/v1/leaderboard/search?metric=score
    """
    leaderboard = _build_leaderboard_response(metric, top, 5, None, False)
    searched_keywords = set()
    if q.strip():
        keywords = [k.strip() for k in q.split(",") if k.strip()]
        searched_keywords = {k.lower() for k in keywords}
        search_results = store.search_ideas_by_keywords(keywords)
        leaderboard["search_results"] = search_results[:10]
        leaderboard["search_query"] = q
    # Suggest searching for a keyword found in top leaderboard entries but not yet searched
    if searched_keywords:
        stopwords = {"the", "a", "an", "and", "or", "for", "with", "from", "to", "in", "on", "by", "of", "is", "it", "as", "at", "this", "that", "all", "no", "not", "use", "try", "new", "idea"}
        alt_keywords = []
        for entry in leaderboard.get("leaderboard", [])[:5]:
            desc = (entry.get("idea_description") or "").lower()
            words = [w.strip("()[]{}:,.'\"") for w in desc.split() if len(w) > 3]
            for w in words:
                if w.isalpha() and w not in stopwords and w not in searched_keywords and w not in alt_keywords:
                    alt_keywords.append(w)
        if alt_keywords:
            next_kw = sorted(alt_keywords, key=len, reverse=True)[0]
            leaderboard["next_step"] = {
                "action": f"GET /api/v1/ideas/search?q={next_kw}",
                "description": f"Search for '{next_kw}' ideas to compare approaches",
            }
    return leaderboard


@router.get("/digest")
def get_digest_compat(
    metric: str = Query(..., description="Metric to rank by"),
    top: int = Query(default=10, description="Number of top experiments to show"),
    recent: int = Query(default=10, description="Number of most recent experiments to show"),
    tags: str | None = Query(default=None, description="Comma-separated tags"),
    include_details: bool = Query(default=False, description="Include per-problem metrics and settings"),
):
    """Deprecated alias for ``/leaderboard``. Use ``/leaderboard`` instead."""
    return _build_leaderboard_response(metric, top, recent, tags, include_details)


@router.get("/wait")
async def wait_for_experiment(
    timeout: float = Query(default=3600, le=86400),
    experiment_id: str | None = Query(default=None, description="Global ID or label (e.g. '4' or '1.2')"),
    idea_id: int | None = Query(default=None),
):
    """Long-poll until an experiment finishes.

    Blocks until a matching experiment completes, fails, or is cancelled, then
    returns the result. Use ``?experiment_id=4`` to wait for a specific
    experiment, ``?idea_id=1`` to wait for any experiment under that idea, or
    neither to wait for any experiment globally. The ``?timeout`` parameter
    (default 3600s, max 86400s) controls how long to wait before returning a
    timeout response.

    Example:
        GET /api/v1/wait?experiment_id=4&timeout=300
        -> {"status": "completed", "experiment": {"id": 4, "metrics": {...}, ...},
            "current_branch": "idea/1", "idea_id": 1, ...}
    """
    # Resolve label to global ID if needed
    resolved_exp_id = None
    if experiment_id is not None:
        try:
            exp = store.resolve_experiment(str(experiment_id))
        except ValueError as e:
            raise HTTPException(400, str(e))
        if exp:
            resolved_exp_id = exp["id"]
        else:
            raise HTTPException(404, f"experiment '{experiment_id}' not found")
    result = await runner.wait_any(
        timeout=timeout,
        experiment_id=resolved_exp_id,
        idea_id=idea_id,
    )
    result["current_branch"] = get_current_branch(cwd=REPO_DIR)
    exp = result.get("experiment")
    if exp:
        result.update(_idea_context(exp.get("idea_id")))
        # Include branch diff summary so agent sees what was tested
        diff_summary = _branch_diff_summary(exp.get("idea_id"))
        if diff_summary:
            result["branch_diff"] = diff_summary
        # Add score comparison to help agent track progress
        metrics = exp.get("metrics") or {}
        current_score = None
        for key in ("score", "accuracy", "final_score"):
            if key in metrics:
                current_score = metrics[key]
                break
        if current_score is not None:
            best_score = None
            best_exp_id = None
            best_exp_label = None
            for idea in store.list_ideas():
                for e in store.list_experiments(idea["id"]):
                    if e["id"] == exp.get("id"):
                        continue
                    em = e.get("metrics") or {}
                    for key in ("score", "accuracy", "final_score"):
                        if key in em and (best_score is None or em[key] > best_score):
                            best_score = em[key]
                            best_exp_id = e["id"]
                            best_exp_label = e.get("label", str(e["id"]))
            if best_score is not None:
                result["progress"] = {
                    "this_score": current_score,
                    "best_score": best_score,
                    "best_experiment_id": best_exp_id,
                    "best_experiment_label": best_exp_label,
                    "is_new_best": current_score > best_score,
                    "delta": round(current_score - best_score, 6),
                }
        # If experiment failed or scored poorly, point to /log for diagnosis
        exp_label = exp.get("label", str(exp["id"]))
        if exp.get("status") == "failed":
            result["diagnosis"] = (
                f"Experiment {exp_label} failed. "
                f"Read the log for error details: GET /api/v1/experiments/{exp_label}/log"
            )
        elif current_score is not None and best_score is not None and current_score < best_score:
            result["diagnosis"] = (
                f"Score {current_score} is below best {best_score}. "
                f"Check the log for clues: GET /api/v1/experiments/{exp_label}/log"
            )
        # Auto-log experiment result as observation notes
        idea_id_val = exp.get("idea_id")
        if idea_id_val and exp.get("status") in ("completed", "failed"):
            note_text = f"Experiment {exp_label}: "
            if exp.get("status") == "failed":
                note_text += f"FAILED — {exp.get('error', 'unknown error')}"
            elif current_score is not None:
                note_text += f"score={current_score}"
            else:
                note_text += "completed (no score metric)"
            store.add_note(idea_id_val, note_text, level="observation")
            # Second note: comparison analysis
            if current_score is not None and best_score is not None:
                if current_score > best_score:
                    comparison = f"Experiment {exp_label} achieved NEW BEST score (+{current_score - best_score:.4f} over previous best {best_score})"
                else:
                    comparison = f"Experiment {exp_label} scored {current_score} vs best {best_score} (delta={current_score - best_score:.4f}) — consider a different approach"
                store.add_note(idea_id_val, comparison, level="observation")
            elif exp.get("status") == "failed":
                store.add_note(idea_id_val, f"Experiment {exp_label} needs debugging — check logs with GET /experiments/log", level="debug")
    return result


@router.get("/backlog")
def get_backlog():
    """Get an overview of active work and the suggestion backlog.

    Returns the current git branch, all active ideas with per-idea experiment
    counts (running, pending, completed, failed), and all suggested ideas
    waiting for adoption. Use this endpoint to understand what is in flight,
    what needs attention, and what the human has suggested.

    Example:
        GET /api/v1/backlog
        -> {"current_branch": "idea/3", "total_running": 1, "total_pending": 2,
            "active_ideas": [{"id": 3, "description": "...", "running_experiments": 1, ...}],
            "suggested_ideas": [{"id": 5, "description": "...", "priority": "high", ...}]}
    """
    current = get_current_branch(cwd=REPO_DIR)
    ideas = store.list_ideas(status="active")
    result = []
    total_running = 0
    total_pending = 0
    for idea in ideas:
        exps = store.list_experiments(idea["id"])
        pending = sum(1 for e in exps if e["status"] == "pending")
        running = sum(1 for e in exps if e["status"] == "running")
        completed = sum(1 for e in exps if e["status"] == "completed")
        failed = sum(1 for e in exps if e["status"] == "failed")
        total_running += running
        total_pending += pending
        result.append({
            "id": idea["id"],
            "description": idea["description"],
            "source": idea.get("source", "agent"),
            "pending_experiments": pending,
            "running_experiments": running,
            "completed_experiments": completed,
            "failed_experiments": failed,
        })
    suggested = store.list_ideas(status="suggested")
    suggested_items = [
        {"id": s["id"], "description": s["description"],
         "priority": s.get("priority", "normal"), "resources": s.get("resources", [])}
        for s in suggested
    ]
    resp: dict = {
        "current_branch": current,
        "active_ideas": result,
        "suggested_ideas": suggested_items,
        "total_running": total_running,
        "total_pending": total_pending,
    }
    task = _read_task()
    if task:
        resp["current_task"] = task
    return resp


@router.get("/orient")
def orient(
    tags: str | None = Query(default=None, description="Comma-separated tags to filter best score and active ideas (running experiments always shown)"),
):
    """Get a compact orientation summary with recommended next action.

    Returns current state and a clear recommendation for what to do next.
    Use ``?tags=swarm-5,held-out`` to filter best score and active ideas
    to experiments matching ALL specified tags. Running experiments and
    suggested ideas are always shown regardless of tag filter.

    Example:
        GET /api/v1/orient?tags=held-out
        -> {"current_branch": "idea/3", "status": "has_running",
            "recommendation": "Wait for experiment 1.3 to finish",
            "next_steps": [...], "best_score": 0.91, "ideas_active": 2,
            "tags_filter": ["held-out"]}
    """
    tag_filter = {t.strip() for t in tags.split(",") if t.strip()} if tags else set()

    def _exp_matches_tags(exp: dict) -> bool:
        if not tag_filter:
            return True
        exp_tags = set(exp.get("tags") or [])
        return tag_filter.issubset(exp_tags)

    current = get_current_branch(cwd=REPO_DIR)
    all_ideas = store.list_ideas(status="active")
    all_ideas_any = store.list_ideas()  # all statuses for leaderboard

    # Always collect ALL running/pending (unfiltered)
    all_running = []
    all_pending = []
    all_failed = []
    # Filtered by tags: completed experiments and active ideas with matching experiments
    filtered_completed = []
    filtered_idea_ids = set()
    best_score = None
    best_exp_id = None
    best_exp_label = None
    best_idea_id = None

    # Collect experiments across ALL ideas (not just active) for leaderboard
    all_completed_with_score = []

    for idea in all_ideas_any:
        exps = store.list_experiments(idea["id"])
        idea_has_matching = False
        for e in exps:
            # Running/pending: collected from active ideas only
            if idea["status"] == "active":
                if e["status"] == "running":
                    all_running.append(e)
                elif e["status"] == "pending":
                    all_pending.append(e)
            # Failed: collected from ALL ideas (so orient always flags failures)
            if e["status"] == "failed":
                all_failed.append({**e, "idea_description": idea["description"]})
            # Completed: filter by tags for best score
            if e["status"] == "completed" and _exp_matches_tags(e):
                filtered_completed.append(e)
                if idea["status"] == "active":
                    idea_has_matching = True
                metrics = e.get("metrics") or {}
                for key in ("score", "accuracy", "final_score"):
                    if key in metrics and isinstance(metrics[key], (int, float)):
                        all_completed_with_score.append({
                            "experiment_label": e.get("label", str(e["id"])),
                            "idea_id": idea["id"],
                            "idea_description": idea["description"],
                            "score": metrics[key],
                            "score_key": key,
                        })
                        if best_score is None or metrics[key] > best_score:
                            best_score = metrics[key]
                            best_exp_id = e["id"]
                            best_exp_label = e.get("label", str(e["id"]))
                            best_idea_id = e["idea_id"]
        if idea["status"] == "active" and (idea_has_matching or not tag_filter):
            filtered_idea_ids.add(idea["id"])

    filtered_ideas = [i for i in all_ideas if i["id"] in filtered_idea_ids]

    resp = {
        "current_branch": current,
        "ideas_active": len(filtered_ideas),
        "experiments_completed": len(filtered_completed),
        "experiments_running": len(all_running),
        "experiments_failed": len(all_failed),
    }
    if tag_filter:
        resp["tags_filter"] = sorted(tag_filter)

    # Determine recommendation -- single focused action per state
    if all_running:
        exp = all_running[0]
        resp["status"] = "has_running"
        resp["recommendation"] = f"Wait for experiment {exp.get('label', exp['id'])} to finish"
        resp["next_step"] = f"GET /api/v1/wait?experiment_id={exp.get('label', exp['id'])}"
    elif all_failed and not filtered_completed:
        resp["status"] = "has_failures"
        resp["recommendation"] = (
            f"{len(all_failed)} experiment(s) failed. "
            f"Read the logs to diagnose, then check the leaderboard."
        )
        resp["next_step"] = "GET /api/v1/experiments/log"
        resp["then"] = "GET /api/v1/leaderboard/search?metric=score"
    elif all_pending:
        exp = all_pending[0]
        resp["status"] = "has_pending"
        resp["recommendation"] = f"Start pending experiment {exp.get('label', exp['id'])}"
        resp["next_step"] = f"POST /api/v1/experiments/{exp.get('label', exp['id'])}/start"
    elif all_failed:
        # Has both completed and failed — main path is leaderboard, but alert about failures
        resp["status"] = "has_failures_and_completions"
        resp["recommendation"] = "Check the leaderboard and search for related ideas."
        resp["next_step"] = "GET /api/v1/leaderboard/search?metric=score"
        resp["failures"] = {
            "count": len(all_failed),
            "action": "GET /api/v1/experiments/log",
            "message": f"{len(all_failed)} experiment(s) failed — read logs to diagnose: GET /api/v1/experiments/log",
        }
    else:
        resp["status"] = "ready" if filtered_completed else "no_experiments"
        resp["recommendation"] = "Check the leaderboard and search for related ideas."
        resp["next_step"] = "GET /api/v1/leaderboard/search?metric=score"

    # Suggested ideas: always shown (unfiltered)
    suggested = store.list_ideas(status="suggested")
    if suggested:
        resp["suggested_ideas"] = [
            {"id": s["id"], "description": s["description"], "priority": s.get("priority", "normal")}
            for s in suggested
        ]

    return resp


@router.get("/graph")
def get_graph():
    """Get the full idea DAG for the dashboard visualization.

    Returns all ideas as nodes and their parent-child relationships as edges,
    forming a directed acyclic graph. Each node includes the idea's status,
    source, priority, whether it has running experiments, and time range
    information (first experiment start to last experiment finish). This
    powers the dashboard's graph and timeline views.

    Example:
        GET /api/v1/graph
        -> {"nodes": [{"id": 1, "description": "...", "status": "active",
                        "parent_ids": [], "has_running": true, ...}],
            "edges": [{"from": 1, "to": 3}]}
    """
    ideas = store.list_ideas()
    nodes = []
    for i in ideas:
        exps = store.list_experiments(i["id"])
        has_running = any(e["status"] == "running" for e in exps)
        # Time range: earliest start -> latest finish (for timeline view)
        starts = [e["started_at"] for e in exps if e.get("started_at")]
        finishes = [e["finished_at"] for e in exps if e.get("finished_at")]
        nodes.append({
            "id": i["id"],
            "description": i["description"],
            "status": i["status"],
            "source": i.get("source", "agent"),
            "priority": i.get("priority", "normal"),
            "has_running": has_running,
            "created_at": i.get("created_at"),
            "first_start": min(starts) if starts else i.get("created_at"),
            "last_finish": max(finishes) if finishes else None,
            "parent_ids": i.get("parent_ids", []),
        })
    edges = []
    for idea in ideas:
        for pid in idea.get("parent_ids", []):
            edges.append({"from": pid, "to": idea["id"]})
    return {"nodes": nodes, "edges": edges}


@router.get("/chart-data")
def get_chart_data():
    """Get all data for the dashboard metrics chart in one request.

    .. note::
       Non-numeric metric values (dicts, strings) are stripped from the
       ``metrics`` object so the dashboard chart receives only plottable data.

    Returns completed experiments (with their final metrics) and running
    experiments (with their latest progress data) in a single response,
    avoiding the N+1 pattern of fetching ideas then experiments per idea.
    Each experiment is enriched with its parent idea's description and status.
    Running experiments include a ``_running: true`` flag and their most
    recent progress metrics.

    Example:
        GET /api/v1/chart-data
        -> {"experiments": [{"id": 4, "metrics": {"acc": 0.91}, "idea_description": "...", ...}],
            "running": [{"id": 6, "metrics": {...}, "_running": true, "idea_description": "...", ...}]}
    """
    all_exps = store.list_all_experiments()
    completed_exps = []
    running_progress = []
    # Build idea lookup once
    idea_cache: dict[int, dict] = {}

    def _numeric_metrics(metrics: dict) -> dict:
        """Strip non-numeric values so the dashboard chart never gets objects/strings."""
        return {k: v for k, v in metrics.items() if isinstance(v, (int, float))}

    for exp in all_exps:
        idea_id = exp["idea_id"]
        if idea_id not in idea_cache:
            idea = store.get_idea(idea_id)
            idea_cache[idea_id] = idea
        idea = idea_cache[idea_id]
        if not idea:
            continue
        if exp.get("status") == "completed" and exp.get("metrics"):
            completed_exps.append({
                **exp,
                "metrics": _numeric_metrics(exp["metrics"]),
                "idea_description": idea["description"],
                "idea_status": idea["status"],
            })
        elif exp.get("status") == "running":
            progress_path = REPO_DIR / exp["script"].replace(".sh", ".progress")
            progress = None
            if progress_path.exists():
                try:
                    progress = json.loads(progress_path.read_text())
                except (json.JSONDecodeError, OSError):
                    pass
            if progress and len(progress) > 0:
                running_progress.append({
                    **exp,
                    "metrics": _numeric_metrics(progress) if isinstance(progress, dict) else progress,
                    "_running": True,
                    "idea_description": idea["description"],
                    "idea_status": idea["status"],
                })
    return {"experiments": completed_exps, "running": running_progress}
