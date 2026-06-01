"""Overview endpoints: leaderboard, digest, wait, backlog, orient, graph, chart-data."""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Query, Request

from ..cache import cached_response
from ..deps import (
    store,
    runner,
    REPO_DIR,
    metric_direction,
    _idea_context,
    _agents_on_idea,
    _branch_diff_summary,
    _description_short,
    _read_task,
    project_fields,
    resolve_metric,
)
from ..git_ops import get_current_branch

router = APIRouter(prefix="/api/v1")


# --- Leaderboard helper (used only by leaderboard + digest) ---

def _build_leaderboard_response(
    metric: str, top: int, recent: int, tags: str | None, include_details: bool,
    descending: bool = True, max_open_ideas: int = 20, max_progression: int = 10,
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
        return _description_short(idea["description"]) if idea else None

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
            "description": _description_short(idea["description"]),
            "source": idea.get("source", "agent"),
            "priority": idea.get("priority", "normal"),
            "running_experiments": sum(1 for e in idea_exps if e.get("status") == "running"),
            "pending_experiments": sum(1 for e in idea_exps if e.get("status") == "pending"),
            "last_activity": _last_activity(idea_exps),
        })
    # Cap to the N most recently active — sorting puts ideas with current
    # work at the top so older inactive-but-still-"active" ideas don't bloat
    # the response.
    open_ideas_total = len(open_ideas)
    open_ideas.sort(key=lambda i: i.get("last_activity") or "", reverse=True)
    if open_ideas_total > max_open_ideas:
        open_ideas = open_ideas[:max_open_ideas]

    running_experiments = []
    for exp in all_exps:
        if exp.get("status") != "running":
            continue
        running_experiments.append({
            "experiment_id": exp["id"],
            "label": exp.get("label"),
            "idea_id": exp["idea_id"],
            "idea_description": _idea_desc(exp["idea_id"]),
            "description": _description_short(exp.get("description")),
            "tags": exp.get("tags", []),
            "started_at": exp.get("started_at"),
            "runtime": exp.get("runtime"),
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
    # Metric lookup uses resolve_metric so callers can address nested
    # paths via dot notation (e.g. "subagent.cache_hits"). Flat keys still
    # win on collision; see resolve_metric in deps.py.
    def _mv(e):  # short alias used several times below
        return resolve_metric(e.get("metrics"), metric)
    with_metric = [e for e in completed if isinstance(_mv(e), (int, float))]

    def _exp_label(e: dict) -> str:
        # Defensive: legacy records can have label=None on disk.
        return (e.get("label") or str(e.get("id") or ""))

    by_value = sorted(with_metric, key=lambda e: _mv(e), reverse=descending)
    leaderboard = []
    for exp in by_value[:top]:
        entry = {
            "experiment_id": exp["id"],
            "experiment_label": _exp_label(exp),
            "idea_id": exp["idea_id"],
            "idea_description": _idea_desc(exp["idea_id"]),
            "value": _mv(exp),
            "tags": exp.get("tags", []),
            "finished_at": exp.get("finished_at"),
        }
        if include_details:
            entry["all_metrics"] = exp.get("metrics", {})
            entry["settings"] = exp.get("meta", {})
            entry["meta"] = _public_meta(exp)
            entry["experiment_description"] = exp.get("description")
        leaderboard.append(entry)

    by_time_desc = sorted(with_metric, key=lambda e: e.get("finished_at") or "", reverse=True)
    recent_out = []
    for exp in by_time_desc[:recent]:
        entry = {
            "experiment_id": exp["id"],
            "experiment_label": _exp_label(exp),
            "idea_id": exp["idea_id"],
            "idea_description": _idea_desc(exp["idea_id"]),
            "value": _mv(exp),
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
            insights = [_description_short(n["text"], limit=200) for n in store.get_notes(idea["id"], levels={"insight"})]
            best_idea = {
                "id": idea["id"],
                "description": _description_short(idea["description"]),
                "parent_ids": idea.get("parent_ids", []),
                "branch": idea.get("branch"),
                "conclusion": _description_short(idea.get("conclusion")),
                "best_value": _mv(best_exp),
                "key_insights": insights[:3],
                "branch_from_this": f"POST /ideas/new with parent_ids=[{idea['id']}]",
            }

    minimize = not descending
    direction = "lower_is_better" if minimize else "higher_is_better"

    by_time_asc = sorted(with_metric, key=lambda e: e.get("finished_at") or "")
    progression = []
    running_best = None
    for exp in by_time_asc:
        v = _mv(exp)
        improved = running_best is None or (v < running_best if minimize else v > running_best)
        if improved:
            running_best = v
            progression.append({
                "experiment_id": exp["id"],
                "experiment_label": _exp_label(exp),
                "idea_id": exp["idea_id"],
                "idea_description": _idea_desc(exp["idea_id"]),
                "value": v,
                "finished_at": exp.get("finished_at"),
            })
    # Cap to the most recent N improvements — older improvements are still
    # implied by the surviving entries.
    progression_total = len(progression)
    if progression_total > max_progression:
        progression = progression[-max_progression:]

    key_insights = []
    for idea in all_ideas:
        for note in store.get_notes(idea["id"], levels={"insight"}):
            key_insights.append({
                "text": _description_short(note["text"], limit=200),
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
        "open_ideas_total": open_ideas_total,
        "running_experiments": running_experiments,
        "recent": recent_out,
        "progression": progression,
        "progression_total": progression_total,
        "key_insights": key_insights[:5],
        "key_insights_total": len(key_insights),
    }


# --- Endpoints ---

@router.get("/leaderboard")
def get_leaderboard(
    metric: str = Query(..., description="Metric to rank by (e.g. accuracy, accuracy_per_mtoken)"),
    top: int = Query(default=10, description="Number of top experiments to show"),
    recent: int = Query(default=10, description="Number of most recent experiments to show"),
    tags: str | None = Query(default=None, description="Comma-separated tags — experiments must have ALL of them (AND filter)"),
    include_details: bool = Query(default=False, description="Include per-problem metrics and experiment settings/meta"),
    sort: str = Query(default="desc", description="Sort order: 'desc' (highest first) or 'asc' (lowest first)"),
    max_open_ideas: int = Query(default=20, description="Cap on the open_ideas list (most recently active first)"),
    max_progression: int = Query(default=10, description="Cap on the progression list (most recent improvements)"),
):
    """Get a compact, metric-focused research leaderboard.

    Returns three sections ranked by the given metric:
    1. **Leaderboard** -- top N experiments sorted by metric value
    2. **Recent** -- most recent N experiments with their metric value
    3. **Progression** -- timeline of when the global best was beaten

    Use ``sort=desc`` (default) for metrics where higher is better (e.g. score),
    or ``sort=asc`` for metrics where lower is better (e.g. convergence_gap).

    Plus: open ideas, running experiments, key insights, and the best idea
    for this metric. Use ``tags=`` to scope to a subset of experiments.
    Set ``include_details=true`` to get full metrics, settings/meta, and
    experiment descriptions for each leaderboard and recent entry.

    Example:
        GET /api/v1/leaderboard?metric=score&sort=desc
        GET /api/v1/leaderboard?metric=convergence_gap&sort=asc&top=5
    """
    descending = sort.lower() != "asc"
    return _build_leaderboard_response(
        metric, top, recent, tags, include_details, descending=descending,
        max_open_ideas=max_open_ideas, max_progression=max_progression,
    )


@router.get("/leaderboard/search")
def leaderboard_with_search(
    metric: str = Query(default="score", description="Metric to rank by"),
    q: str = Query(default="", description="Comma-separated keywords to search for in idea descriptions"),
    top: int = Query(default=10, description="Number of top experiments to show"),
    sort: str = Query(default="desc", description="Sort order: 'desc' (highest first) or 'asc' (lowest first)"),
    max_open_ideas: int = Query(default=5, description="Cap on the open_ideas list (most recently active first)"),
    max_progression: int = Query(default=5, description="Cap on the progression list (most recent improvements)"),
    max_search_results: int = Query(default=5, description="Cap on idea search_results when q is provided"),
    search_notes_limit: int = Query(
        default=3,
        description="Max notes per search result (most-recent; 0 = all). "
                    "Ignored when search_experiments=true.",
    ),
    search_experiments: bool = Query(
        default=False,
        description="Include full experiment arrays in search results. "
                    "WARNING: can make the response very large (100 KB+). "
                    "Default false — experiment_summary is always included.",
    ),
    fields: str | None = Query(
        default=None,
        description="Comma-separated top-level field projection (e.g. 'leaderboard,best_idea,next_step'). "
                    "Drops the rest of the response (open_ideas, progression, recent, key_insights, …) — "
                    "useful when the agent only needs the ranking and not every supporting section.",
    ),
):
    """Combined leaderboard + search: see rankings and find related ideas in one call.

    Returns the full leaderboard plus search results for the given keywords.
    Use this endpoint to orient yourself in one call instead of calling
    /leaderboard and /ideas/search separately.

    **Search results are slim by default** (no experiments array, 3 notes).
    Each result includes ``experiment_summary`` with aggregate counts and best
    score.  To get experiments use ``search_experiments=true`` (large response)
    or call ``GET /ideas/{id}`` on the specific idea.

    Example:
        GET /api/v1/leaderboard/search?metric=score&sort=desc
        GET /api/v1/leaderboard/search?fields=leaderboard,best_idea
        GET /api/v1/leaderboard/search?q=think&search_notes_limit=5
    """
    descending = sort.lower() != "asc"
    leaderboard = _build_leaderboard_response(
        metric, top, 5, None, False, descending=descending,
        max_open_ideas=max_open_ideas, max_progression=max_progression,
    )
    searched_keywords = set()
    if q.strip():
        keywords = [k.strip() for k in q.split(",") if k.strip()]
        searched_keywords = {k.lower() for k in keywords}
        search_results = store.search_ideas_by_keywords(
            keywords,
            include_experiments=search_experiments,
            notes_limit=search_notes_limit,
        )
        leaderboard["search_results_total"] = len(search_results)
        leaderboard["search_results"] = search_results[:max_search_results]
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
    return project_fields(leaderboard, fields)


# ---------------------------------------------------------------------------
# Aggregation + comparison (variance-aware)
# ---------------------------------------------------------------------------

def _median(xs: list[float]) -> float:
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def _percentile(xs: list[float], p: float) -> float:
    """Linear-interpolation percentile (NumPy default), p in [0, 1]."""
    if not xs:
        return 0.0
    s = sorted(xs)
    if len(s) == 1:
        return s[0]
    pos = p * (len(s) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    frac = pos - lo
    return s[lo] + (s[hi] - s[lo]) * frac


def _aggregate_metric(values: list[float]) -> dict:
    """Summary stats for a single group of metric values."""
    n = len(values)
    if n == 0:
        return {"n": 0}
    if n == 1:
        v = values[0]
        return {"n": 1, "median": v, "mean": v, "std": 0.0, "min": v, "max": v}
    mean = sum(values) / n
    var = sum((x - mean) ** 2 for x in values) / max(n - 1, 1)
    std = var ** 0.5
    return {
        "n": n,
        "median": _median(values),
        "mean": mean,
        "std": std,
        "min": min(values),
        "max": max(values),
        "p25": _percentile(values, 0.25),
        "p75": _percentile(values, 0.75),
    }


def _bootstrap_diff_medians(
    a: list[float], b: list[float], n_resamples: int = 1000, seed: int = 42,
) -> dict:
    """Bootstrap CI on (median(a) - median(b)). Returns observed delta + 95% CI."""
    import random
    if not a or not b:
        return {"observed_delta": None, "ci95_lo": None, "ci95_hi": None,
                "n_resamples": 0}
    rng = random.Random(seed)
    deltas: list[float] = []
    la, lb = len(a), len(b)
    for _ in range(n_resamples):
        sa = [a[rng.randrange(la)] for _ in range(la)]
        sb = [b[rng.randrange(lb)] for _ in range(lb)]
        deltas.append(_median(sa) - _median(sb))
    deltas.sort()
    return {
        "observed_delta": _median(a) - _median(b),
        "ci95_lo": deltas[int(0.025 * n_resamples)],
        "ci95_hi": deltas[int(0.975 * n_resamples) - 1],
        "n_resamples": n_resamples,
    }


def _resolve_selector(selector: str, metric: str) -> tuple[list[float], list[dict]]:
    """Parse 'tag:foo' / 'idea:5' / 'all' and return (numeric values, exp summaries).

    Honours dot-notation metric paths (e.g. ``subagent.cache_hits``) via
    resolve_metric.
    """
    all_exps = [
        e for e in store.list_all_experiments()
        if e.get("status") == "completed"
        and isinstance(resolve_metric(e.get("metrics"), metric), (int, float))
    ]
    sel = (selector or "").strip()
    if not sel or sel == "all":
        matched = all_exps
    elif sel.startswith("tag:"):
        wanted = sel[4:].strip()
        matched = [e for e in all_exps if wanted in (e.get("tags") or [])]
    elif sel.startswith("idea:"):
        try:
            wanted_id = int(sel[5:])
            matched = [e for e in all_exps if e.get("idea_id") == wanted_id]
        except ValueError:
            matched = []
    else:
        # Fallback: treat as a tag for convenience
        matched = [e for e in all_exps if sel in (e.get("tags") or [])]
    values = [float(resolve_metric(e.get("metrics"), metric)) for e in matched]
    summaries = [
        {
            "experiment_label": e.get("label") or str(e.get("id")),
            "idea_id": e.get("idea_id"),
            "value": resolve_metric(e.get("metrics"), metric),
            "tags": e.get("tags") or [],
        }
        for e in matched
    ]
    return values, summaries


@router.get("/leaderboard/aggregate")
def leaderboard_aggregate(
    metric: str = Query(..., description="Metric to aggregate (must be numeric)"),
    group_by: str = Query(default="tag", description="'tag' or 'idea'"),
    tags: str | None = Query(default=None, description="Comma-separated tag filter (AND)"),
    top: int = Query(default=20, description="Cap on returned groups (most populous first)"),
):
    """Per-group summary stats for a metric: median / mean / std / min / max / p25 / p75 / n.

    Useful for "what's the noise floor on tag X?" without fetching every
    experiment. Experiments with multiple tags appear in multiple groups
    when ``group_by=tag``.

    Example:
        GET /api/v1/leaderboard/aggregate?metric=score&group_by=tag
        -> {"groups": [{"key": "baseline", "stats": {n: 5, median: 0.42, ...}}, ...]}
    """
    if group_by not in ("tag", "idea"):
        raise HTTPException(400, "group_by must be 'tag' or 'idea'")

    tag_filter = [t.strip() for t in (tags or "").split(",") if t.strip()]
    completed = [
        e for e in store.list_all_experiments()
        if e.get("status") == "completed"
        and isinstance(resolve_metric(e.get("metrics"), metric), (int, float))
        and (not tag_filter or all(t in (e.get("tags") or []) for t in tag_filter))
    ]

    groups: dict[str, list[float]] = {}
    if group_by == "tag":
        for e in completed:
            for t in (e.get("tags") or []):
                groups.setdefault(t, []).append(float(resolve_metric(e.get("metrics"), metric)))
    else:  # idea
        for e in completed:
            key = f"idea/{e.get('idea_id')}"
            groups.setdefault(key, []).append(float(resolve_metric(e.get("metrics"), metric)))

    rows = [
        {"key": k, "stats": _aggregate_metric(v)}
        for k, v in groups.items()
    ]
    rows.sort(key=lambda r: r["stats"]["n"], reverse=True)

    return {
        "metric": metric,
        "metric_direction": metric_direction(metric),
        "group_by": group_by,
        "tags_filter": tag_filter,
        "groups_total": len(rows),
        "groups": rows[:top],
    }


@router.get("/leaderboard/compare")
def leaderboard_compare(
    a: str = Query(..., description="Selector A — 'tag:<name>', 'idea:<id>', or 'all'"),
    b: str = Query(..., description="Selector B — same format"),
    metric: str = Query(default="score", description="Metric to compare on"),
    n_resamples: int = Query(default=1000, description="Bootstrap resamples"),
):
    """Compare two selections via bootstrap-on-difference-of-medians.

    Returns the observed median delta (a - b), a 95% CI from N bootstrap
    resamples, and a verdict that respects the metric's direction:

      - **real_improvement** — CI excludes 0 in the favourable direction
      - **regression**       — CI excludes 0 in the unfavourable direction
      - **inconclusive**     — CI straddles 0

    Selectors:
      tag:<name>   all completed experiments tagged with <name>
      idea:<id>    all completed experiments under idea <id>
      all          everything completed

    Example:
        GET /api/v1/leaderboard/compare?metric=score&a=tag:baseline&b=tag:new
        -> {"a": {...}, "b": {...}, "delta": {"observed": 0.07, "ci95_lo": 0.02, "ci95_hi": 0.11},
            "verdict": "real_improvement", ...}
    """
    a_values, a_summaries = _resolve_selector(a, metric)
    b_values, b_summaries = _resolve_selector(b, metric)
    boot = _bootstrap_diff_medians(a_values, b_values, n_resamples=n_resamples)

    direction = metric_direction(metric)  # "minimize" or "maximize"
    higher_is_better = direction == "maximize"
    lo, hi = boot.get("ci95_lo"), boot.get("ci95_hi")
    if lo is None or hi is None:
        verdict = "insufficient_data"
    elif higher_is_better:
        if lo > 0:
            verdict = "real_improvement"   # a strictly > b in favourable dir
        elif hi < 0:
            verdict = "regression"
        else:
            verdict = "inconclusive"
    else:  # minimize: lower is better, so a < b means "a improved"
        if hi < 0:
            verdict = "real_improvement"
        elif lo > 0:
            verdict = "regression"
        else:
            verdict = "inconclusive"

    return {
        "metric": metric,
        "metric_direction": direction,
        "a": {"selector": a, "stats": _aggregate_metric(a_values),
              "experiments": a_summaries[:50]},
        "b": {"selector": b, "stats": _aggregate_metric(b_values),
              "experiments": b_summaries[:50]},
        "delta": {
            "observed": boot.get("observed_delta"),
            "ci95_lo": lo,
            "ci95_hi": hi,
            "n_resamples": boot.get("n_resamples"),
            "interpretation": (
                "delta = median(A) - median(B); CI excluding 0 in the favourable "
                "direction (per metric_direction) implies a real change."
            ),
        },
        "verdict": verdict,
    }


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
    request: Request,
    timeout: float = Query(default=3600, le=86400),
    experiment_id: str | None = Query(default=None, description="Global ID or label (e.g. '4' or '1.2')"),
    idea_id: int | None = Query(default=None),
    compare: bool = Query(default=False, description="Include best-score comparison across all experiments (requires full scan, off by default)."),
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
    # Pull the caller's agent + role from the request so wait_any wakes on
    # messages addressed to them (in addition to experiment events).
    from .. import agents as _agents_mod
    _agent_id = getattr(request.state, "agent_id", None)
    _agent_role = None
    if _agent_id:
        _entry = _agents_mod.lookup_agent(REPO_DIR, _agent_id) or {}
        _agent_role = _entry.get("role")

    result = await runner.wait_any(
        timeout=timeout,
        experiment_id=resolved_exp_id,
        idea_id=idea_id,
        agent_id=_agent_id,
        agent_role=_agent_role,
    )
    result["current_branch"] = get_current_branch(cwd=REPO_DIR)
    exp = result.get("experiment")
    if exp:
        result.update(_idea_context(exp.get("idea_id")))
        # Concise branch diff: which files changed and +/- line counts only.
        # Full patch available via GET /ideas/{id}/diff if needed.
        diff_summary = _branch_diff_summary(exp.get("idea_id"))
        if diff_summary:
            result["branch_diff"] = diff_summary
        exp_label = exp.get("label", str(exp["id"]))
        metrics = exp.get("metrics") or {}
        current_score = None
        for key in ("score", "accuracy", "final_score"):
            if key in metrics:
                current_score = metrics[key]
                break

        # Best-score comparison is opt-in (?compare=true) because it requires
        # scanning all experiments across all ideas on every /wait call.
        best_score = None
        best_exp_label = None
        if compare and current_score is not None:
            best_exp_id = None
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
        if exp.get("status") == "failed":
            result["diagnosis"] = (
                f"Experiment {exp_label} failed. "
                f"Read the log for error details: GET /api/v1/experiments/{exp_label}/log?tail=50"
            )
        elif compare and current_score is not None and best_score is not None and current_score < best_score:
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
def get_backlog(request: Request):
    """Get an overview of active work and the suggestion backlog.

    Returns the current git branch, all active ideas with per-idea experiment
    counts (running, pending, completed, failed), and all suggested ideas
    waiting for adoption. Each active idea includes ``claimed_by`` — other live
    agents currently working on that idea — so agents can avoid collisions.

    Example:
        GET /api/v1/backlog
        -> {"current_branch": "idea/3", "total_running": 1, "total_pending": 2,
            "active_ideas": [{"id": 3, "description": "...", "running_experiments": 1,
                               "claimed_by": [{"agent_id": "abc12", "role": "default"}]}, ...],
            "suggested_ideas": [{"id": 5, "description": "...", "priority": "high", ...}]}
    """
    caller_agent_id = getattr(request.state, "agent_id", None)
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
        entry: dict = {
            "id": idea["id"],
            "description": idea["description"],
            "source": idea.get("source", "agent"),
            "pending_experiments": pending,
            "running_experiments": running,
            "completed_experiments": completed,
            "failed_experiments": failed,
        }
        claimed = _agents_on_idea(idea["id"], exclude_agent_id=caller_agent_id)
        if claimed:
            entry["claimed_by"] = claimed
        result.append(entry)
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
        n = len(all_running)
        labels = [(e.get("label") or str(e["id"])) for e in all_running]
        resp["status"] = "has_running"
        if n == 1:
            resp["recommendation"] = f"Wait for experiment {labels[0]} to finish"
            resp["next_step"] = f"GET /api/v1/wait?experiment_id={labels[0]}"
        else:
            shown = ", ".join(labels[:5]) + (", …" if n > 5 else "")
            resp["recommendation"] = (
                f"{n} experiments running ({shown}). You can wait for any to finish, "
                "or branch a new idea / start a parallel experiment in the meantime."
            )
            resp["next_step"] = "GET /api/v1/wait  # blocks until any running experiment finishes"
            resp["alternatives"] = [
                "POST /api/v1/ideas/new       # branch a new idea",
                "POST /api/v1/ideas/{idea_id}/experiments  # start a parallel experiment under an existing active idea",
            ]
            resp["running_experiment_labels"] = labels
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

    # Tag health: only when duplicate/messy tags exist (targeted at T8-like fixtures)
    all_exps_orient = store.list_all_experiments()
    tag_counts: dict[str, int] = {}
    for exp in all_exps_orient:
        for tag in exp.get("tags") or []:
            tag_counts[tag] = tag_counts.get(tag, 0) + 1
    if tag_counts:
        by_lower: dict[str, list[str]] = {}
        for tag in tag_counts:
            by_lower.setdefault(tag.lower().replace("-", "").replace("_", ""), []).append(tag)
        duplicates = {k: v for k, v in by_lower.items() if len(v) > 1}
        if duplicates:
            resp["tag_health"] = {
                "duplicates_detected": len(duplicates),
                "action": "GET /api/v1/experiments/tags then POST /api/v1/experiments/tags/rename to normalize",
                "variants": {variants[0]: variants for variants in duplicates.values()},
            }

    # Targeted hint: if convergence_gap metric exists, tell agent how to query it
    has_convergence_gap = any(
        "convergence_gap" in (exp.get("metrics") or {})
        for exp in all_exps_orient
        if exp.get("status") == "completed"
    )
    if has_convergence_gap:
        resp["metric_hint"] = "convergence_gap is lower-is-better. Query: GET /api/v1/leaderboard/search?metric=convergence_gap&sort=asc"

    # Targeted hint: if multiple tags exist, suggest filtering
    if len(tag_counts) >= 2:
        sample_tag = sorted(tag_counts.keys())[0]
        resp["filter_hint"] = f"Filter by approach: add ?tags={sample_tag} to /orient or /leaderboard/search"

    return resp


@router.get("/graph")
@cached_response(lambda: ())
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
        has_queued = any(e["status"] in ("queued", "pending") for e in exps)
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
            "has_queued": has_queued,
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

    def _numeric_metrics(metrics: dict, _prefix: str = "") -> dict:
        """Flatten nested dicts with dot notation, keeping only numeric leaves.
        e.g. {"context": {"peak_kchars": 146.1}} → {"context.peak_kchars": 146.1}
        Capped at two levels of nesting to avoid exploding per-game dicts."""
        out: dict = {}
        for k, v in metrics.items():
            key = f"{_prefix}.{k}" if _prefix else k
            if isinstance(v, (int, float)):
                out[key] = v
            elif isinstance(v, dict) and _prefix.count(".") < 1:
                # Recurse one level deep only (level-0 → level-1 dot keys)
                out.update(_numeric_metrics(v, key))
        return out

    # Only the fields the dashboard chart actually needs. Critically, we
    # omit ``meta`` — it carries large per-experiment blobs (scorecards,
    # worktree paths, etc.) that are not used for plotting. On a lab with
    # 486 experiments the meta alone was 5.6 MB of the 6.8 MB response.
    _CHART_FIELDS = (
        "id", "label", "idea_id", "description", "status",
        "started_at", "finished_at", "created_at", "runtime", "tags",
    )

    for exp in all_exps:
        idea_id = exp["idea_id"]
        if idea_id not in idea_cache:
            idea = store.get_idea(idea_id)
            idea_cache[idea_id] = idea
        idea = idea_cache[idea_id]
        if not idea:
            continue
        base = {f: exp.get(f) for f in _CHART_FIELDS}
        base["idea_description"] = idea["description"]
        base["idea_status"] = idea["status"]
        if exp.get("status") == "completed" and exp.get("metrics"):
            base["metrics"] = _numeric_metrics(exp["metrics"])
            completed_exps.append(base)
        elif exp.get("status") == "running":
            progress_path = REPO_DIR / exp["script"].replace(".sh", ".progress")
            progress = None
            if progress_path.exists():
                try:
                    progress = json.loads(progress_path.read_text())
                except (json.JSONDecodeError, OSError):
                    pass
            if progress and len(progress) > 0:
                base["metrics"] = _numeric_metrics(progress) if isinstance(progress, dict) else progress
                base["_running"] = True
                running_progress.append(base)
    return {"experiments": completed_exps, "running": running_progress}
