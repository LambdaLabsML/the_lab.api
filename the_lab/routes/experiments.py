"""Experiment CRUD, start/cancel, timeseries, compare, and analyze endpoints."""
from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import datetime, timezone

import mimetypes

import math

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse

from .. import jsonio
from ..deps import (
    store,
    runner,
    REPO_DIR,
    _resolve_exp,
    _branch_diff_summary,
    _description_short,
    _wrap_script,
)
from ..schemas import (
    NewExperimentRequest,
    StartExperimentRequest,
    RenameTagRequest,
    UpdateTagsRequest,
    AnalyzeRequest,
    RequeueRequest,
    SlurmDoneRequest,
)

router = APIRouter(prefix="/api/v1")


def _truncate_soft(text: str | None, limit: int = 240) -> str | None:
    """Shorten *text* without cutting mid-sentence/mid-word.

    Prefers a sentence boundary (``. `` / newline) at or before *limit*, else
    falls back to the last whole word, always ending with an explicit ellipsis.
    Fixes error/summary strings that were clipped mid-sentence.
    """
    if not text:
        return text
    text = text.strip()
    if len(text) <= limit:
        return text
    window = text[:limit]
    # Prefer the last sentence end within the window.
    for sep in (". ", ".\n", "\n", "! ", "? "):
        idx = window.rfind(sep)
        if idx > 0:
            return text[: idx + 1].rstrip()
    # Otherwise back off to the last whole word.
    sp = window.rfind(" ")
    cut = window[:sp] if sp > 0 else window
    return cut.rstrip() + " …"


# --- Paging + compaction (feedback A1) ---
# Opt-in only: callers that pass `page` get a paged+compact response; without
# `page` the legacy raw shape is preserved so the dashboard keeps working.

def _exp_sort_key(exp: dict):
    """Newest-first ordering key: prefer finished_at, then created_at, then id.

    Returns a tuple sorted descending, so more-recent experiments come first.
    """
    return (
        exp.get("finished_at") or "",
        exp.get("created_at") or "",
        str(exp.get("id") or ""),
    )


def _list_row(exp: dict, idea_cache: dict[int, dict], metric: str | None,
              tag_list: list[str]) -> dict:
    """One trimmed listing row: short idea/description, metrics, soft error."""
    label = exp.get("label", str(exp["id"]))
    idea_id = exp["idea_id"]
    if idea_id not in idea_cache:
        idea_cache[idea_id] = store.get_idea(idea_id)
    idea = idea_cache[idea_id]

    # When ?metric=X is given, show only that value (the caller knows what
    # they care about). Otherwise include all metrics — we don't know which
    # metric the project optimises, so hardcoding names would be wrong.
    all_metrics = exp.get("metrics") or {}
    shown_metrics = {metric: all_metrics[metric]} if metric else all_metrics

    out = {
        "id":          exp["id"],
        "label":       label,
        "idea_id":     idea_id,
        "idea":        _description_short(idea["description"], limit=80) if idea else None,
        "description": _description_short(exp.get("description")) or None,
        "status":      exp.get("status"),
        "metrics":     shown_metrics,
        # Truncate on a sentence/word boundary (not mid-sentence) so the
        # error stays readable; full text via GET /experiments/{ref}.
        "error":       _truncate_soft(exp.get("error"), limit=240) or None,
        "runtime":     exp.get("runtime"),
        "finished_at": exp.get("finished_at"),
    }
    if tag_list:
        out["tags"] = exp.get("tags")
    if exp.get("status") == "failed":
        out["log"] = f"GET /api/v1/experiments/{label}/log?tail=50"
    return out


# --- Experiments ---

def _create_and_queue_experiment(
    idea_id: int,
    description: str,
    meta: dict,
    tags: list[str] | None,
    script_content: str | None,
) -> dict:
    """Shared create+queue path used by single-create and batch-create.

    Mirrors the exact store calls of the single-create handler so scoring and
    queueing behave identically: create the record, optionally write the wrapped
    script, flip to ``queued``, broadcast, and wake the scheduler. Returns the
    (queued) experiment record.
    """
    exp = store.create_experiment(idea_id, description, meta=meta, tags=tags)

    if script_content is not None:
        script_path = REPO_DIR / exp["script"]
        script_path.parent.mkdir(parents=True, exist_ok=True)
        script_path.write_text(_wrap_script(script_content))
        os.chmod(script_path, 0o755)

    # The queue takes it from here. New experiments default to "queued";
    # the scheduler will start them when capacity permits and dependencies
    # have settled. The legacy `auto_start` flag is ignored — queueing is
    # mandatory now.
    label = exp.get("label") or str(exp["id"])
    store.update_experiment(
        label,
        status="queued",
        queued_at=datetime.now(timezone.utc).isoformat(),
    )
    exp = store.get_experiment(label) or exp
    try:
        from .. import ws as ws_mod
        ws_mod.broadcaster.broadcast_soon({
            "type": "experiment_queued",
            "label": exp.get("label") or str(exp["id"]),
            "idea_id": exp.get("idea_id"),
        })
    except Exception:
        pass
    runner.wake_scheduler()
    return exp


def _queue_position(exp: dict) -> int | None:
    """Best-effort queue position for *exp* among queued/pending experiments."""
    queue = [
        e for e in store.list_all_experiments()
        if e.get("status") in ("queued", "pending")
    ]
    queue.sort(key=lambda e: (
        -int((e.get("meta") or {}).get("priority", 0) or 0),
        e.get("created_at") or "",
    ))
    for i, qexp in enumerate(queue):
        if qexp.get("id") == exp.get("id"):
            return i + 1
    return None


@router.post("/ideas/{idea_id}/experiments", status_code=201)
async def create_experiment(idea_id: int, req: NewExperimentRequest):
    """Create a new experiment under an idea.

    Registers an experiment record and, if ``script_content`` is provided,
    writes the script file to disk with an auto-injected guard and preamble.
    The idea must be in ``active`` status. Use ``meta`` to store arbitrary
    hyperparameters or configuration, and ``tags`` to categorize the
    experiment for filtering and comparison.

    Set ``auto_start: true`` to immediately start the experiment after creation,
    saving a separate ``POST /experiments/<id>/start`` call.

    Example:
        POST /api/v1/ideas/1/experiments {"description": "baseline run",
                                           "script_content": "#!/bin/bash\\npython train.py",
                                           "tags": ["baseline", "v1"],
                                           "meta": {"lr": 0.001, "epochs": 50},
                                           "auto_start": true}
        -> {"id": 4, "idea_id": 1, "status": "running", "script": ".the_lab/scripts/4.sh", ...}
    """
    idea = store.get_idea(idea_id)
    if not idea:
        raise HTTPException(404, "idea not found")
    if idea["status"] != "active":
        raise HTTPException(400, f"idea is {idea['status']}, cannot add experiments")

    # Build the queue-related fields into meta so they survive the existing
    # store API (no schema migration needed).
    extra_meta = {}
    if req.priority:
        extra_meta["priority"] = req.priority
    if req.requirements is not None:
        extra_meta["requirements"] = req.requirements.model_dump(exclude_none=True)
    if req.depends_on:
        extra_meta["depends_on"] = list(req.depends_on)
    if not req.depends_on_success:
        extra_meta["depends_on_success"] = False
    merged_meta = {**(req.meta or {}), **extra_meta}

    exp = _create_and_queue_experiment(
        idea_id, req.description, merged_meta, req.tags, req.script_content,
    )

    # Surface the queue position so callers know roughly when to expect it
    # to start. Best-effort — doesn't account for resource capacity.
    queue_position = _queue_position(exp)

    label = exp.get("label") or str(exp["id"])
    return {
        "id":             exp["id"],
        "label":          label,
        "idea_id":        idea_id,
        "status":         exp.get("status"),
        "queue_position": queue_position,
        "wait":           f"GET /api/v1/wait?experiment_id={label}",
        "_more":          f"GET /api/v1/experiments/{label}",
    }


@router.post("/ideas/{idea_id}/experiments/batch", status_code=201)
async def batch_create_experiments(idea_id: int, request: Request):
    """Batch-create queued experiments that share one script (feedback M1).

    Creates one queued experiment per entry in ``metas``, each reusing the same
    ``shared_script_content`` and the exact single-create store path (so
    scoring/queueing behave identically). Body:

        {"shared_script_content": "#!/bin/bash\\npython train.py",
         "metas": [
             {"label"?: str, "meta"?: {"lr": 0.01}, "tags"?: ["sweep"], "description"?: str},
             {"meta": {"lr": 0.03}},
             ...
         ]}

    ``label`` is advisory only (recorded on meta as ``requested_label``); the
    store assigns the canonical ``<idea>.<seq>`` label. Returns a compact
    summary. Fails 400 on empty/invalid ``metas``.

    Example:
        POST /api/v1/ideas/1/experiments/batch
        -> {"created": [{"id": "1.4", "label": "1.4"}, {"id": "1.5", "label": "1.5"}], "count": 2}
    """
    idea = store.get_idea(idea_id)
    if not idea:
        raise HTTPException(404, "idea not found")
    if idea["status"] != "active":
        raise HTTPException(400, f"idea is {idea['status']}, cannot add experiments")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "body must be valid JSON")
    if not isinstance(body, dict):
        raise HTTPException(400, "body must be a JSON object")

    shared_script_content = body.get("shared_script_content")
    if shared_script_content is not None and not isinstance(shared_script_content, str):
        raise HTTPException(400, "shared_script_content must be a string")

    metas = body.get("metas")
    if not isinstance(metas, list) or not metas:
        raise HTTPException(400, "metas must be a non-empty list")

    # Validate every entry up front so we don't half-create the batch.
    for i, m in enumerate(metas):
        if not isinstance(m, dict):
            raise HTTPException(400, f"metas[{i}] must be an object")
        if "meta" in m and m["meta"] is not None and not isinstance(m["meta"], dict):
            raise HTTPException(400, f"metas[{i}].meta must be an object")
        if "tags" in m and m["tags"] is not None and not isinstance(m["tags"], list):
            raise HTTPException(400, f"metas[{i}].tags must be a list")

    created = []
    for m in metas:
        # Preserve an advisory label on meta (store assigns the real one).
        merged_meta = dict(m.get("meta") or {})
        if m.get("label"):
            merged_meta.setdefault("requested_label", m["label"])
        description = m.get("description") or "batch experiment"
        exp = _create_and_queue_experiment(
            idea_id,
            description,
            merged_meta,
            m.get("tags"),
            shared_script_content,
        )
        created.append({"id": exp["id"], "label": exp.get("label") or str(exp["id"])})

    return {"created": created, "count": len(created)}


@router.get("/ideas/{idea_id}/experiments")
def list_experiments(
    idea_id: int,
    page: int | None = Query(default=None, description="1-based page number. OPT-IN: when set, returns a paged+compact shape; omit for the full raw list."),
    page_size: int = Query(default=10, description="Page size when paging is enabled."),
):
    """List all experiments belonging to an idea.

    Returns every experiment record for the given idea, regardless of status.
    Each record includes the experiment's description, status, metrics, meta,
    tags, and timing information. Failed experiments include a ``read_log`` URL.

    Feedback A1 — opt-in paging: pass ``?page=N`` to receive a newest-first,
    compacted page ``{"experiments": [...], "page", "page_size", "total",
    "total_pages"}``. When ``page`` is omitted the legacy raw list is returned
    UNCHANGED so the dashboard (which consumes this as ``Experiment[]``) keeps
    working.

    Example:
        GET /api/v1/ideas/1/experiments
        -> [{"id": 4, "idea_id": 1, "status": "completed", "metrics": {"acc": 0.91}, ...}, ...]
        GET /api/v1/ideas/1/experiments?page=1&page_size=10
        -> {"experiments": [{"id": 4, ..., "metrics": {"_collapsed": true, ...}}], "page": 1, ...}
    """
    # A1: paging is OPT-IN. Only switch to the paged+compact shape when `page`
    # is provided; compact the full store records so placeholders point at real
    # nested data reachable via GET /experiments/<ref>.
    if page is not None:
        return _paginate(store.list_experiments(idea_id), page, page_size)

    exps = store.list_experiments(idea_id)
    results = []
    for exp in exps:
        label = exp.get("label", str(exp["id"]))
        out = {
            "id":          exp["id"],
            "label":       label,
            "idea_id":     exp["idea_id"],
            "description": exp.get("description"),
            "status":      exp.get("status"),
            "metrics":     exp.get("metrics"),
            "error":       exp.get("error"),
            "runtime":     exp.get("runtime"),
            "started_at":  exp.get("started_at"),
            "finished_at": exp.get("finished_at"),
        }
        if exp.get("status") == "failed":
            out["read_log"] = f"GET /api/v1/experiments/{label}/log"
        results.append(out)
    return results


# --- Bulk listing (must come before parameterized) ---

@router.get("/experiments")
def list_all_experiments(
    status: str | None = Query(default=None, description="Filter by status: completed, failed, running, pending"),
    tags: str | None = Query(default=None, description="Comma-separated tags — experiments must have ALL (AND filter)"),
    metric: str | None = Query(default=None, description="Only return experiments that have this metric"),
    page: int = Query(default=1, ge=1, description="1-based page number (newest first). Paging is ALWAYS on; page 1 is returned when omitted."),
    page_size: int = Query(default=10, ge=1, le=100, description="Rows per page (max 100)."),
    show_all: bool = Query(default=False, alias="all", description="Escape hatch: return EVERY row unpaged. Expensive on large projects — prefer filters + paging."),
):
    """List experiments across all ideas — PAGED, newest first.

    Returns trimmed rows (metrics, status, short idea/description) with pager
    meta ``{"experiments": [...], "page", "page_size", "total", "total_pages"}``.
    Page 1 with 10 rows is the default; use ``?page=N`` / ``?page_size=M`` to
    walk further. Filters (status/tags/metric) compose with paging.

    Pass ``?metric=X`` to filter to experiments that have metric X **and** show
    only that metric value in the response — useful for focused comparison when
    you know which metric you're optimising. Without ``?metric``, all metrics
    are returned for each experiment.

    ``?all=true`` returns the full unpaged list (legacy shape with ``count``).
    On large projects this is hundreds of kB — use it only when you truly need
    every row; filters + paging cover almost every workflow.

    Example:
        GET /api/v1/experiments                     (page 1, newest 10)
        GET /api/v1/experiments?page=3&page_size=25
        GET /api/v1/experiments?status=completed&metric=score
        GET /api/v1/experiments?tags=baseline&all=true
    """
    all_exps = store.list_all_experiments()
    idea_cache: dict[int, dict] = {}

    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []

    filtered = []
    for exp in all_exps:
        if status and exp.get("status") != status:
            continue
        if tag_list and not all(t in (exp.get("tags") or []) for t in tag_list):
            continue
        if metric and metric not in (exp.get("metrics") or {}):
            continue
        filtered.append(exp)

    tag_hint = (
        None if tag_list else
        "Tags hidden in bulk listing. Use GET /experiments/tags to list tags, then GET /experiments?tags=<tag> to filter by approach."
    )

    if show_all:
        results = [_list_row(e, idea_cache, metric, tag_list) for e in filtered]
        resp = {"experiments": results, "count": len(results)}
        if tag_hint:
            resp["tag_hint"] = tag_hint
        return resp

    ordered = sorted(filtered, key=_exp_sort_key, reverse=True)
    total = len(ordered)
    total_pages = math.ceil(total / page_size) if total else 0
    window = ordered[(page - 1) * page_size:(page - 1) * page_size + page_size]
    resp = {
        "experiments": [_list_row(e, idea_cache, metric, tag_list) for e in window],
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": total_pages,
    }
    if tag_hint:
        resp["tag_hint"] = tag_hint
    return resp


# --- Aggregate log endpoint (must come before parameterized) ---

@router.get("/experiments/log")
def get_all_failed_logs(tail: int = Query(default=30, description="Number of log lines per experiment")):
    """Get logs for all failed experiments in one call.

    Returns the last N lines of stdout/stderr for every failed experiment
    across all ideas. Use this to quickly diagnose failures without
    fetching logs one-by-one.

    Example:
        GET /api/v1/experiments/log
        -> {"failed_experiments": [{"label": "14.1", "idea_id": 14, "log": "..."}]}
    """
    failed = []
    for exp in store.list_all_experiments():
        if exp.get("status") == "failed":
            log = runner.get_log(exp["id"], tail=tail)
            failed.append({
                "label": exp.get("label", str(exp["id"])),
                "idea_id": exp["idea_id"],
                "error": exp.get("error"),
                "log": log or "(no log available)",
            })
    return {"failed_experiments": failed, "count": len(failed)}


# --- Tags (literal paths before parameterized) ---

@router.get("/experiments/tags")
def list_tags():
    """List all unique experiment tags with their usage counts.

    Scans every experiment and returns a sorted list of distinct tags, each
    paired with the number of experiments that carry it. Useful for populating
    tag filter UIs and understanding how experiments are categorized.

    Example:
        GET /api/v1/experiments/tags
        -> {"tags": [{"tag": "baseline", "count": 3}, {"tag": "v2", "count": 1}]}
    """
    tag_counts: dict[str, int] = {}
    for exp in store.list_all_experiments():
        for tag in exp.get("tags") or []:
            tag_counts[tag] = tag_counts.get(tag, 0) + 1
    return {"tags": [{"tag": t, "count": c} for t, c in sorted(tag_counts.items())]}


@router.post("/experiments/tags/rename")
def rename_tag(req: RenameTagRequest):
    """Rename a tag across all experiments.

    Replaces every occurrence of ``old`` with ``new`` in every experiment's tag
    list. If an experiment already has the ``new`` tag, duplicates are removed
    automatically so each tag appears at most once per experiment. Returns the
    count of experiments that were updated.

    Example:
        POST /api/v1/experiments/tags/rename {"old": "basline", "new": "baseline"}
        -> {"old": "basline", "new": "baseline", "updated": 2}
    """
    updated = 0
    for exp in store.list_all_experiments():
        tags = exp.get("tags") or []
        if req.old in tags:
            new_tags = [req.new if t == req.old else t for t in tags]
            # Deduplicate in case new already existed
            seen = set()
            deduped = []
            for t in new_tags:
                if t not in seen:
                    seen.add(t)
                    deduped.append(t)
            store.update_experiment(exp["id"], tags=deduped)
            updated += 1
    return {"old": req.old, "new": req.new, "updated": updated}


@router.patch("/experiments/{exp_ref}/tags")
def update_experiment_tags(exp_ref: str, req: UpdateTagsRequest):
    """Add or remove tags on a single experiment.

    Provide ``add`` and/or ``remove`` lists. Tags are deduplicated
    automatically. Returns the experiment's updated tag list.

    Examples:
        PATCH /api/v1/experiments/5.3/tags {"add": ["baseline", "v2"]}
        PATCH /api/v1/experiments/5.3/tags {"remove": ["draft"]}
        PATCH /api/v1/experiments/5.3/tags {"add": ["final"], "remove": ["draft"]}
    """
    exp = _resolve_exp(exp_ref)
    tags = list(exp.get("tags") or [])
    # Remove first, then add (so add wins if same tag appears in both)
    for t in req.remove:
        if t in tags:
            tags.remove(t)
    for t in req.add:
        if t not in tags:
            tags.append(t)
    store.update_experiment(exp["id"], tags=tags)
    return {"label": exp.get("label", exp["id"]), "tags": tags}


@router.post("/experiments/tags/batch")
def batch_update_tags(req: UpdateTagsRequest, experiments: str = Query(..., description="Comma-separated experiment labels (e.g. '5.3,5.4,6.1')")):
    """Add or remove tags on multiple experiments at once.

    Example:
        POST /api/v1/experiments/tags/batch?experiments=5.3,5.4 {"add": ["reviewed"]}
    """
    labels = [l.strip() for l in experiments.split(",") if l.strip()]
    updated = []
    for label in labels:
        exp = store.resolve_experiment(label)
        if not exp:
            continue
        tags = list(exp.get("tags") or [])
        for t in req.remove:
            if t in tags:
                tags.remove(t)
        for t in req.add:
            if t not in tags:
                tags.append(t)
        store.update_experiment(exp["id"], tags=tags)
        updated.append({"label": exp.get("label", exp["id"]), "tags": tags})
    return {"updated": len(updated), "experiments": updated}


@router.get("/experiments/compare")
def compare_experiments(
    ids: str = Query(..., description="Comma-separated experiment IDs"),
    metrics: str | None = Query(default=None, description="Comma-separated metric keys to include (default: all)"),
):
    """Side-by-side comparison of experiments by metrics and metadata.

    Fetches the requested experiments and pivots their metrics and meta into
    aligned tables so values are easy to compare column-by-column. Pass
    ``?ids=1,2,3`` to select experiments and optionally ``?metrics=acc,loss``
    to restrict which metric keys appear. All metric keys are included by
    default.

    Example:
        GET /api/v1/experiments/compare?ids=4,5,6&metrics=acc,loss
        -> {"experiment_ids": [4,5,6], "metric_keys": ["acc","loss"],
            "metrics": {"acc": [0.91, 0.93, 0.89], "loss": [0.35, 0.30, 0.40]},
            "meta_keys": ["lr"], "meta": {"lr": [0.001, 0.003, 0.001]}, ...}
    """
    try:
        exp_ids = [int(x.strip()) for x in ids.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(400, "ids must be comma-separated integers")
    if not exp_ids:
        raise HTTPException(400, "no experiment IDs provided")

    filter_metrics = None
    if metrics is not None:
        filter_metrics = [k.strip() for k in metrics.split(",") if k.strip()]

    experiments = []
    for eid in exp_ids:
        exp = store.get_experiment(eid)
        if not exp:
            raise HTTPException(404, f"experiment {eid} not found")
        experiments.append(exp)

    # Pivot metrics into a table: metric_key -> [value per experiment]
    all_metric_keys = sorted({k for e in experiments for k in (e.get("metrics") or {})})
    metric_keys = [k for k in filter_metrics if k in all_metric_keys] if filter_metrics else all_metric_keys
    metrics_table = {
        key: [(e.get("metrics") or {}).get(key) for e in experiments]
        for key in metric_keys
    }

    # Same for meta
    meta_keys = sorted({k for e in experiments for k in (e.get("meta") or {})})
    meta_table = {
        key: [(e.get("meta") or {}).get(key) for e in experiments]
        for key in meta_keys
    }

    # --- Config diff: highlight what changed between experiments ---
    meta_diff = {k: v for k, v in meta_table.items() if len(set(str(x) for x in v)) > 1}
    metric_diff = {k: v for k, v in metrics_table.items() if len(set(str(x) for x in v)) > 1}

    tag_sets = [set(e.get("tags") or []) for e in experiments]
    all_tags = sorted(set().union(*tag_sets)) if tag_sets else []
    tag_diff = {t: [t in ts for ts in tag_sets] for t in all_tags if len(set(t in ts for ts in tag_sets)) > 1}

    idea_descs = []
    for e in experiments:
        idea = store.get_idea(e["idea_id"])
        idea_descs.append({"idea_id": e["idea_id"], "idea_description": idea["description"] if idea else None})
    idea_desc_diff = idea_descs if len(set(d["idea_description"] for d in idea_descs)) > 1 else None

    return {
        "experiment_ids": exp_ids,
        "experiments": experiments,
        "metric_keys": metric_keys,
        "metrics": metrics_table,
        "meta_keys": meta_keys,
        "meta": meta_table,
        "config_diff": {
            "meta": meta_diff,
            "metrics": metric_diff,
            "tags": tag_diff,
            "idea_descriptions": idea_desc_diff,
        },
    }


@router.post("/experiments/analyze")
async def analyze_experiments(req: AnalyzeRequest):
    """Run an analysis script against one or more experiments.

    Executes a script from ``.the_lab/artifacts/trace_tools/`` with a JSON
    manifest describing the target experiments (IDs, paths, metadata, metrics).
    The script receives ``--manifest <path>`` plus any extra ``args``. It must
    print a JSON object to stdout with ``columns`` (ordered key list) and
    ``rows`` (array of objects) for easy table formatting.

    Example:
        POST /api/v1/experiments/analyze
        {"experiment_ids": [315, 320], "script": "analyze_collab_uptake",
         "args": ["--max-rollouts", "5"]}
        -> {"experiment_ids": [315, 320], "script": "analyze_collab_uptake",
            "columns": ["experiment_id", "problem", "exposures", ...],
            "rows": [{"experiment_id": 315, "problem": "066", ...}, ...]}
    """
    import asyncio
    import tempfile
    from pathlib import Path

    if not req.experiment_ids:
        raise HTTPException(400, "no experiment IDs provided")

    # Validate script name (prevent path traversal)
    script_name = req.script.replace("/", "").replace("\\", "").replace("..", "")
    tools_dir = REPO_DIR / ".the_lab" / "artifacts" / "trace_tools"
    script_path = None
    for ext in ("", ".py", ".sh"):
        candidate = tools_dir / (script_name + ext)
        if candidate.exists():
            script_path = candidate
            break
    if not script_path:
        available = [f.stem for f in tools_dir.glob("*") if f.is_file()] if tools_dir.exists() else []
        raise HTTPException(404, f"script '{script_name}' not found in .the_lab/artifacts/trace_tools/. Available: {available}")

    # Build manifest with experiment metadata and paths
    experiments = []
    for eid in req.experiment_ids:
        exp = store.get_experiment(eid)
        if not exp:
            raise HTTPException(404, f"experiment {eid} not found")
        idea = store.get_idea(exp["idea_id"])
        exp_dir = str(store.lab_dir / str(exp["idea_id"]))
        # Find rollout output dir from meta if available
        rollout_dir = None
        meta = exp.get("meta") or {}
        for key in ("outdir", "rollout_outdir"):
            if key in meta:
                candidate = REPO_DIR / meta[key]
                if candidate.exists():
                    rollout_dir = str(candidate)
                    break
        # Also check standard location: {exp_id}_rollouts/
        if not rollout_dir:
            standard = store.lab_dir / str(exp["idea_id"]) / f"{eid}_rollouts"
            if standard.exists():
                rollout_dir = str(standard)
        experiments.append({
            "id": eid,
            "idea_id": exp["idea_id"],
            "idea_description": idea["description"] if idea else None,
            "description": exp.get("description"),
            "status": exp.get("status"),
            "dir": exp_dir,
            "rollout_dir": rollout_dir,
            "script_path": str(REPO_DIR / exp["script"]) if exp.get("script") else None,
            "log_path": str((REPO_DIR / exp["script"]).with_suffix(".log")) if exp.get("script") else None,
            "meta": meta,
            "metrics": exp.get("metrics"),
            "tags": exp.get("tags", []),
        })

    # Write manifest to temp file
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, dir="/tmp") as f:
        json.dump({"experiments": experiments}, f)
        manifest_path = f.name

    try:
        # Determine how to run the script
        if script_path.suffix == ".py":
            cmd = ["python3", str(script_path), "--manifest", manifest_path] + req.args
        else:
            cmd = ["bash", str(script_path), "--manifest", manifest_path] + req.args

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(REPO_DIR),
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)

        if proc.returncode != 0:
            raise HTTPException(500, {
                "error": f"script exited with code {proc.returncode}",
                "stderr": stderr.decode(errors="replace")[-2000:],
            })

        # Parse JSON from stdout (last non-empty line or full output)
        output = stdout.decode(errors="replace").strip()
        result = None
        if output:
            # Try full output first, then last line
            for candidate in [output, output.split("\n")[-1]]:
                try:
                    result = json.loads(candidate)
                    break
                except json.JSONDecodeError:
                    continue

        if result is None:
            raise HTTPException(500, {
                "error": "script produced no valid JSON output",
                "stdout": output[-2000:],
                "stderr": stderr.decode(errors="replace")[-2000:],
            })

        return {
            "experiment_ids": req.experiment_ids,
            "script": req.script,
            "columns": result.get("columns", list(result["rows"][0].keys()) if result.get("rows") else []),
            "rows": result.get("rows", []),
        }
    finally:
        Path(manifest_path).unlink(missing_ok=True)


@router.get("/experiments/compare-curves")
def compare_curves(
    ids: str = Query(..., description="Comma-separated experiment IDs"),
    key: str = Query(..., description="Metric key to compare"),
):
    """Overlay training curves from multiple experiments for a single metric.

    Extracts the timeseries for the given metric key from each experiment and
    returns them as separate curves, ready for plotting on the same chart.
    Pass ``?ids=1,2&key=train_loss`` to compare the ``train_loss`` curves of
    experiments 1 and 2.

    Example:
        GET /api/v1/experiments/compare-curves?ids=4,5&key=train_loss
        -> {"key": "train_loss", "experiments": [
                {"id": 4, "points": [{"step": 0, "train_loss": 2.3}, ...]},
                {"id": 5, "points": [{"step": 0, "train_loss": 2.1}, ...]}]}
    """
    try:
        exp_ids = [int(x.strip()) for x in ids.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(400, "ids must be comma-separated integers")
    result = []
    for eid in exp_ids:
        points = store.get_timeseries(eid)
        if points is None:
            raise HTTPException(404, f"experiment {eid} not found")
        curve = [{"step": p.get("step"), key: p.get(key)} for p in points if key in p]
        result.append({"id": eid, "points": curve})
    return {"key": key, "experiments": result}


# --- Static file serving for output.md image references ---

@router.get("/files/{file_path:path}", include_in_schema=False)
def serve_repo_file(file_path: str):
    """Serve a file from the repository root directory.

    Used by the dashboard to load images embedded in output.md files.
    Path traversal outside the repo root is rejected with 403.
    """
    try:
        full_path = (REPO_DIR / file_path).resolve()
    except Exception:
        raise HTTPException(400, "invalid path")
    repo_root = REPO_DIR.resolve()
    if not str(full_path).startswith(str(repo_root) + "/") and full_path != repo_root:
        raise HTTPException(403, "access denied")
    if not full_path.exists() or not full_path.is_file():
        raise HTTPException(404, "file not found")
    content_type, _ = mimetypes.guess_type(str(full_path))
    return FileResponse(str(full_path), media_type=content_type or "application/octet-stream")


# --- Parameterized experiment routes (MUST come after literal paths) ---

def _queue_position(exp_id) -> int | None:
    """1-based position of *exp_id* in the scheduler's queued/pending order."""
    queue = [
        e for e in store.list_all_experiments()
        if e.get("status") in ("queued", "pending")
    ]
    queue.sort(key=lambda e: (
        -int((e.get("meta") or {}).get("priority", 0) or 0),
        e.get("created_at") or "",
    ))
    for i, qexp in enumerate(queue):
        if qexp.get("id") == exp_id:
            return i + 1
    return None


_META_STRIP = frozenset({
    "slurm_run_token", "slurm_job_id", "slurm_attempts",
    "worktree", "git_commit", "git_branch", "assigned_units",
})


# Meta values bigger than this (as JSON) are result blobs accumulated during
# the run (scorecards, per-environment results, …), not startup config — they
# are collapsed in GET /experiments/{ref} and served whole by …/{ref}/meta.
_META_INLINE_MAX_BYTES = 1500


def _visible_meta(exp: dict) -> dict:
    """Experiment meta minus internal infrastructure keys."""
    return {k: v for k, v in (exp.get("meta") or {}).items() if k not in _META_STRIP}


@router.get("/experiments/{exp_ref}")
def get_experiment(
    exp_ref: str,
    metric: str | None = Query(default=None, description="Surface this metric as `selected_score` at the top level."),
):
    """Get detail for a single experiment.

    Accepts global ID (``4``) or label (``1.2`` = idea 1, experiment 2).
    Internal infrastructure fields (slurm tokens, worktree paths, git hashes)
    are stripped. Use ``GET /api/v1/wait?compare=true`` for best-score
    comparison (requires a full experiment scan).

    ``meta`` shows startup keys only: values that grew large during the run
    (accumulated results such as scorecards) are collapsed to a placeholder.
    The complete dict is at ``GET /api/v1/experiments/{ref}/meta``.

    Example:
        GET /api/v1/experiments/1.2?metric=score
        -> {"id": 4, "label": "1.2", "status": "completed", "metrics": {...},
            "selected_score": {"metric": "score", "value": 0.68}, ...}
    """
    # Shallow-copy the store record (and meta) — this handler trims fields,
    # and the store hands out its live dict.
    exp = dict(_resolve_exp(exp_ref))
    label = exp.get("label", str(exp["id"]))
    if exp.get("status") == "failed":
        exp["read_log"] = f"GET /api/v1/experiments/{label}/log?tail=50"

    # Startup meta only: strip internal keys, collapse accumulated result blobs.
    meta: dict = {}
    collapsed = []
    for k, v in _visible_meta(exp).items():
        try:
            size = len(json.dumps(v, default=str))
        except (TypeError, ValueError):
            size = _META_INLINE_MAX_BYTES + 1
        if isinstance(v, (dict, list)) and size > _META_INLINE_MAX_BYTES:
            meta[k] = {"_collapsed": True, "_bytes": size,
                       "_via": f"GET /api/v1/experiments/{label}/meta"}
            collapsed.append(k)
        else:
            meta[k] = v
    exp["meta"] = meta
    if collapsed:
        exp["_note"] = (
            f"meta keys {collapsed} hold accumulated results and are collapsed "
            f"here; the full meta dict is at GET /api/v1/experiments/{label}/meta"
        )

    if metric:
        exp["selected_score"] = {
            "metric": metric,
            "value": (exp.get("metrics") or {}).get(metric),
        }
    return exp


@router.get("/experiments/{exp_ref}/meta")
def get_experiment_meta(exp_ref: str):
    """Full meta dict for one experiment — accumulated results included.

    The companion of ``GET /experiments/{ref}``, which collapses large meta
    values (scorecards and other result blobs written during the run). This
    endpoint returns every meta key uncollapsed; only internal infrastructure
    fields (slurm tokens, worktree paths, git hashes) are stripped.

    Example:
        GET /api/v1/experiments/1.2/meta
        -> {"id": 4, "label": "1.2", "meta": {"scorecard": {...}, ...}}
    """
    exp = _resolve_exp(exp_ref)
    return {
        "id": exp["id"],
        "label": exp.get("label", str(exp["id"])),
        "meta": _visible_meta(exp),
    }


@router.delete("/experiments/{exp_ref}")
def delete_experiment(exp_ref: str):
    """Delete a non-running experiment and its stored artifacts.

    Removes the experiment record from the file-backed store and deletes the
    associated script/log/progress/metrics files plus any recorded rollout or
    worktree directories. Running experiments must be cancelled first.
    """
    exp = _resolve_exp(exp_ref)
    exp_id = exp["id"]
    if exp.get("status") == "running":
        raise HTTPException(400, "running experiment must be cancelled before deletion")
    deleted = store.delete_experiment(exp_id)
    if deleted is None:
        raise HTTPException(404, "experiment not found")
    return {
        "deleted": True,
        "experiment_id": exp_id,
        "experiment_label": exp.get("label", str(exp_id)),
        "idea_id": deleted.get("idea_id"),
        "status": deleted.get("status"),
    }


@router.post("/experiments/{exp_ref}/start")
async def start_experiment(exp_ref: str, req: StartExperimentRequest | None = None):
    """Queue an experiment for the scheduler to dispatch.

    Puts (or re-puts) the experiment in ``queued`` state and wakes the
    scheduler — it will start the script as soon as a resource slot frees
    up, honouring capacity, max_parallel_jobs, priority, and dependencies.

    Idempotent: already-running and already-queued states are returned
    unchanged. Completed experiments cannot be re-started this way; use
    ``POST /api/v1/experiments/{ref}/rerun`` to create a fresh queued
    copy. Failed and cancelled experiments are re-queued.

    The ``timeout`` field on the request is recorded on the experiment's
    meta and applied by the scheduler when it eventually dispatches.

    Returns a SHORT object (id/label/status/queue_position/message) — the
    full record stays one ``GET /api/v1/experiments/{ref}`` away.

    Example:
        POST /api/v1/experiments/4/start {"timeout": 600}
        -> {"status": "queued", "id": 4, "label": "1.2", "idea_id": 1,
            "queue_position": 2, "message": "..."}
    """
    # Resolve exp ref (global ID or label like '1.2')
    exp_check = _resolve_exp(exp_ref)
    label = exp_check.get("label") or str(exp_check["id"])

    if exp_check.get("status") == "running":
        return {
            "status": "already_running",
            "id": exp_check["id"],
            "label": label,
            "idea_id": exp_check.get("idea_id"),
            "message": (
                f"already running — watch GET /api/v1/experiments/{label}/progress "
                f"or GET /api/v1/wait"
            ),
        }
    if exp_check.get("status") == "completed":
        raise HTTPException(
            400,
            "experiment is completed; use POST /experiments/{ref}/rerun "
            "to create a queued copy.",
        )

    # Stash the requested timeout on the experiment's meta so the scheduler
    # can apply it when it dispatches.
    meta_update = dict(exp_check.get("meta") or {})
    if req and req.timeout is not None:
        meta_update["timeout"] = float(req.timeout)
    # Clear any leftover error / finished_at from a previous failed run so
    # the row reads cleanly as queued.
    store.update_experiment(
        label,
        status="queued",
        meta=meta_update,
        error=None,
        finished_at=None,
        queued_at=datetime.now(timezone.utc).isoformat(),
    )
    exp_queued = store.get_experiment(label) or exp_check
    try:
        from .. import ws as ws_mod
        ws_mod.broadcaster.broadcast_soon({
            "type": "experiment_queued",
            "label": label,
            "idea_id": exp_queued.get("idea_id"),
        })
    except Exception:
        pass
    runner.wake_scheduler()

    # SHORT response (same slimming as /cancel): id/label/queue_position,
    # not the full record — that stays behind GET /experiments/{ref}.
    exp = store.get_experiment(label) or exp_check
    return {
        "status": "queued",
        "id": exp["id"],
        "label": label,
        "idea_id": exp.get("idea_id"),
        "queue_position": _queue_position(exp.get("id")),
        "message": (
            f"queued — the scheduler dispatches when capacity frees. "
            f"Watch GET /api/v1/experiments/{label}/progress or GET /api/v1/wait."
        ),
    }



@router.post("/experiments/{exp_ref}/rerun", status_code=201)
async def rerun_experiment(exp_ref: str):
    """Create a queued copy of an existing experiment from its script.

    Reads the script file of the referenced experiment and creates a brand-new
    experiment on the same idea with the same script content, description
    (prefixed with "rerun: "), tags, and meta. The new experiment is **queued**
    — the scheduler dispatches it when capacity permits, the same as any
    create_experiment call. Works on experiments in any status; useful for
    re-running a completed experiment to check reproducibility.

    Returns a SHORT object (id/label/status/queue_position/rerun_of/message)
    — fetch the full new record via ``GET /api/v1/experiments/{label}``.

    Example:
        POST /api/v1/experiments/1.2/rerun
        -> {"status": "queued", "id": 7, "label": "1.3", "idea_id": 1,
            "queue_position": 2, "rerun_of": "1.2", "message": "..."}
    """
    source = _resolve_exp(exp_ref)
    idea_id = source["idea_id"]

    idea = store.get_idea(idea_id)
    if not idea:
        raise HTTPException(404, "parent idea not found")
    if idea["status"] != "active":
        raise HTTPException(400, f"idea is {idea['status']}, reopen it before rerunning experiments")

    # Read the source experiment's script
    script_path = REPO_DIR / source["script"]
    if not script_path.exists():
        raise HTTPException(400, f"script file not found: {source['script']}")
    script_content = script_path.read_text()

    # Create the new experiment — strip stale "rerun: " prefixes so they don't accumulate
    base_desc = re.sub(r'^(rerun: )+', '', source['description'])
    desc = f"rerun: {base_desc}"
    # Strip stale executor fields from meta so the new run gets a fresh git
    # worktree (old git_commit caused worktrees with pre-fix code to be used).
    stale_keys = {"slurm_job_id", "worktree", "pid", "git_commit", "git_branch"}
    clean_meta = {k: v for k, v in (source.get("meta") or {}).items()
                  if k not in stale_keys}
    new_exp = store.create_experiment(
        idea_id, desc, meta=clean_meta, tags=source.get("tags"),
    )

    # Write the script (reuse content as-is — it's already wrapped)
    new_script_path = REPO_DIR / new_exp["script"]
    new_script_path.parent.mkdir(parents=True, exist_ok=True)
    new_script_path.write_text(script_content)
    os.chmod(new_script_path, 0o755)

    # Queue it — same pattern as create_experiment so reruns respect
    # resource capacity, priorities, and parallel-job caps instead of
    # racing past the scheduler.
    label = new_exp.get("label") or str(new_exp["id"])
    store.update_experiment(
        label,
        status="queued",
        queued_at=datetime.now(timezone.utc).isoformat(),
    )
    new_exp = store.get_experiment(label) or new_exp
    try:
        from .. import ws as ws_mod
        ws_mod.broadcaster.broadcast_soon({
            "type": "experiment_queued",
            "label": label,
            "idea_id": new_exp.get("idea_id"),
        })
    except Exception:
        pass
    runner.wake_scheduler()

    # SHORT response (same slimming as /start and /cancel).
    src_label = source.get("label", str(source["id"]))
    return {
        "status": "queued",
        "id": new_exp["id"],
        "label": label,
        "idea_id": idea_id,
        "queue_position": _queue_position(new_exp.get("id")),
        "rerun_of": src_label,
        "message": (
            f"queued copy of {src_label} — full record: "
            f"GET /api/v1/experiments/{label}"
        ),
    }


@router.post("/experiments/{exp_ref}/cancel")
async def cancel_experiment(exp_ref: str):
    """Cancel a pending or running experiment.

    For running experiments, sends SIGTERM to the experiment process, giving it
    a chance to clean up. If the process does not exit promptly, SIGKILL is
    sent to force termination. Pending experiments are marked ``cancelled``
    immediately.

    Feedback A2: returns a SHORT object only (id/label/status/cancelled_at/
    message), not the full experiment record.

    Example:
        POST /api/v1/experiments/4/cancel
        -> {"id": 4, "label": "1.2", "status": "cancelled",
            "cancelled_at": "2026-06-26T...Z", "message": "..."}
    """
    exp = _resolve_exp(exp_ref)
    try:
        result = await runner.cancel(exp["id"])
    except Exception:
        result = None
    # If runner.cancel returned None or raised, re-fetch the experiment.
    # The cancel side effect may still have succeeded (process killed,
    # status updated by the watch task) — only return 404 if the
    # experiment really isn't in the store.
    latest = result if result is not None else store.get_experiment(exp["id"])
    if latest is None:
        raise HTTPException(404, "experiment not found")

    # A2: return a SHORT object reflecting the CURRENT status only. Do not fold
    # in the full record. NOTE: there is a known separate bug where a cancelled
    # run can later report status "completed"; that end-state correctness fix is
    # deferred to a later batch — we simply report whatever status is set now.
    status = latest.get("status")
    label = latest.get("label") or exp.get("label") or str(exp["id"])
    if status not in ("cancelled", "cancelling"):
        # Flag the known race: cancel was requested but status isn't cancelled.
        message = f"cancel requested; current status is '{status}' (may settle to cancelled shortly)"
    else:
        message = "experiment cancelled"
    return {
        "id": latest.get("id", exp["id"]),
        "label": label,
        "status": status,
        "cancelled_at": datetime.now(timezone.utc).isoformat(),
        "message": message,
    }


@router.get("/experiments/{exp_ref}/log")
def get_experiment_log(
    exp_ref: str,
    tail: int | None = Query(default=25, description="Return last N lines (default 25). Pass 0 to disable."),
    head: int | None = Query(default=None, description="Return first N lines instead of tail."),
    full: bool = Query(default=False, description="Return the complete log (overrides tail/head/grep)."),
    grep: str | None = Query(default=None, description="Return only lines matching this substring (case-insensitive)."),
    above: int = Query(default=0, description="Lines of context before each grep match."),
    below: int = Query(default=0, description="Lines of context after each grep match."),
):
    """Read the stdout/stderr log for an experiment.

    Defaults to the last 25 lines. Use ``?grep=text`` to filter to matching
    lines with optional context (``?above=N&below=N`` like grep -B/-A).

    Example:
        GET /api/v1/experiments/4/log                        # last 25 lines
        GET /api/v1/experiments/4/log?tail=100               # last 100 lines
        GET /api/v1/experiments/4/log?head=50                # first 50 lines
        GET /api/v1/experiments/4/log?full=true              # entire log
        GET /api/v1/experiments/4/log?grep=error             # matching lines
        GET /api/v1/experiments/4/log?grep=error&above=2&below=5  # with context
    """
    exp = _resolve_exp(exp_ref)
    # Fast path: a plain tail read never loads the whole log — grep/head/full
    # still need the complete file.
    want_full = bool(grep) or full or head is not None or not tail
    raw = runner.get_log(exp["id"], tail=None if want_full else tail)
    if raw is None:
        raise HTTPException(404, "experiment log not found")

    if grep:
        lines = raw.splitlines()
        needle = grep.lower()
        included: set[int] = set()
        for i, line in enumerate(lines):
            if needle in line.lower():
                for j in range(max(0, i - above), min(len(lines), i + below + 1)):
                    included.add(j)
        log = "\n".join(lines[i] for i in sorted(included))
        return {"log": log, "matched_lines": len([i for i, l in enumerate(lines) if needle in l.lower()])}

    if full:
        log = raw
    elif head is not None:
        log = "\n".join(raw.splitlines()[:head])
    else:
        # Tail was already applied by runner.get_log's seek-based fast path
        # (raw IS the tail); tail=0 means the full log.
        log = raw
    return {"log": log}


@router.get("/experiments/{exp_ref}/script")
def get_experiment_script(exp_ref: str):
    """Read the launch script for an experiment.

    Returns the shell script content that was used (or will be used) to run
    the experiment.

    Example:
        GET /api/v1/experiments/1.2/script
        -> {"script": "#!/bin/bash\\nset -euo pipefail\\npython train.py"}
    """
    exp = _resolve_exp(exp_ref)
    script_path = REPO_DIR / exp["script"]
    if not script_path.exists():
        raise HTTPException(404, "script file not found")
    return {"script": script_path.read_text()}


def _resolve_output_path(script_relpath: str | None):
    """Locate the output file for a script.

    Prefers ``<script>.output.html`` over ``<script>.output.md`` if both
    exist — an agent that emits HTML deliberately is bypassing the markdown
    pipeline. Returns ``(path, format)`` where format is "html" or "md",
    or ``(None, None)`` when neither exists.
    """
    if not script_relpath:
        return None, None
    script_path = REPO_DIR / script_relpath
    html_path = script_path.parent / (script_path.stem + ".output.html")
    if html_path.exists():
        return html_path, "html"
    md_path = script_path.parent / (script_path.stem + ".output.md")
    if md_path.exists():
        return md_path, "md"
    return None, None


@router.get("/experiments/{exp_ref}/output")
def get_experiment_output(exp_ref: str):
    """Read the experiment's output file (HTML preferred, else markdown).

    Returns the file content plus ``base_path`` (directory of the file
    relative to the repo root) so the caller can resolve relative image URLs
    via ``GET /api/v1/files/<base_path>/<relative_path>``, plus ``format``
    (``"html"`` | ``"md"``) so the dashboard knows whether to run the
    markdown renderer or display the content as raw HTML.

    Example:
        GET /api/v1/experiments/1.2/output
        -> {"output": "# Results\\n", "base_path": ".the_lab/experiments/1",
            "format": "md"}
    """
    from fastapi.responses import JSONResponse
    exp = _resolve_exp(exp_ref)
    output_path, fmt = _resolve_output_path(exp.get("script"))
    if output_path is None:
        raise HTTPException(404, "output file not found")
    base_path = str(output_path.parent.relative_to(REPO_DIR))
    # Disable browser HTTP caching — agents append to output files after the
    # experiment completes (e.g. summary/post-mortem), so a stale cached
    # response would hide updates from the dashboard.
    return JSONResponse(
        {"output": output_path.read_text(), "base_path": base_path, "format": fmt},
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


# --- Progress heartbeat tracking ---
# The `.progress` file only holds the *latest* JSON body (it is overwritten on
# every POST). To report heartbeat timing (last_at / avg interval / age) we
# append one ISO-8601 timestamp per POST to a sibling `.heartbeats` file and
# summarise it on GET. Fail-soft everywhere so heartbeat bookkeeping never
# breaks the actual progress read/write.

_HEARTBEAT_KEEP = 500  # bound the sidecar so it can't grow without limit


def _heartbeat_path(script: str):
    """Sidecar file (next to `.progress`) holding one ISO timestamp per beat."""
    return REPO_DIR / script.replace(".sh", ".heartbeats")


def _record_heartbeat(script: str) -> None:
    """Append the current UTC timestamp to the experiment's heartbeat log.

    True O(1) append — the old read-all/rewrite-all per beat was two full
    NFS passes per POST /progress. The file is compacted back down to
    _HEARTBEAT_KEEP lines only when it grows past twice that.
    """
    try:
        path = _heartbeat_path(script)
        jsonio.append_line(path, datetime.now(timezone.utc).isoformat())
        # Occasional compaction, amortized: at most one rewrite per
        # _HEARTBEAT_KEEP appends.
        try:
            if path.stat().st_size > _HEARTBEAT_KEEP * 2 * 30:  # ~33 B/line
                lines = path.read_text().splitlines()
                if len(lines) > _HEARTBEAT_KEEP * 2:
                    path.write_text("\n".join(lines[-_HEARTBEAT_KEEP:]) + "\n")
        except OSError:
            pass
    except OSError:
        pass  # heartbeat tracking is best-effort


def _heartbeat_summary(script: str) -> dict:
    """Summarise recorded heartbeats: last_at, avg interval, age, count.

    avg_interval_sec is null when there are fewer than 2 heartbeats.
    """
    summary = {"last_at": None, "avg_interval_sec": None, "since_last_sec": None, "count": 0}
    try:
        path = _heartbeat_path(script)
        if not path.exists():
            return summary
        stamps = []
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                stamps.append(datetime.fromisoformat(line))
            except ValueError:
                continue
        if not stamps:
            return summary
        summary["count"] = len(stamps)
        last = stamps[-1]
        summary["last_at"] = last.isoformat()
        now = datetime.now(timezone.utc)
        summary["since_last_sec"] = round((now - last).total_seconds(), 3)
        if len(stamps) >= 2:
            gaps = [(stamps[i] - stamps[i - 1]).total_seconds() for i in range(1, len(stamps))]
            summary["avg_interval_sec"] = round(sum(gaps) / len(gaps), 3)
    except (OSError, ValueError):
        pass
    return summary


@router.get("/experiments/{exp_ref}/progress")
def get_experiment_progress(exp_ref: str):
    """Read script-reported progress for an experiment.

    Returns the experiment's current status and, if the script has written a
    progress file (``<script_name>.progress``), includes the parsed JSON
    progress data. Scripts report progress by writing JSON to this file during
    execution.

    Example:
        GET /api/v1/experiments/4/progress
        -> {"status": "running", "progress": {"epoch": 25, "total_epochs": 50, "loss": 0.34}}
    """
    exp = _resolve_exp(exp_ref)
    status = exp["status"]
    result: dict = {"status": status}

    # Heartbeat timing: last beat, avg interval between beats, age since last.
    result["heartbeat"] = _heartbeat_summary(exp["script"])

    # For terminal states return metrics as the final progress snapshot so the
    # UI never shows stale "starting" data from an earlier write.
    if status in ("completed", "failed", "cancelled"):
        metrics = exp.get("metrics")
        if metrics:
            result["progress"] = {"_final": True, "pct_complete": 100, **metrics}
        return result

    # Running / queued — return whatever the script last wrote.
    progress_path = REPO_DIR / exp["script"].replace(".sh", ".progress")
    if progress_path.exists():
        try:
            result["progress"] = json.loads(progress_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return result


@router.post("/experiments/{exp_ref}/progress")
async def post_experiment_progress(exp_ref: str, request: Request):
    """Push a live progress update from within a running experiment.

    Called by the experiment wrapper script using THE_LAB_TOKEN auth.
    The body is any JSON object. It is written to the local progress file
    and immediately broadcast via WebSocket so the dashboard updates in
    real-time without waiting for the next rsync poll.

    Example (from bash):
        curl -s -X POST "$THE_LAB_API_URL/experiments/$LABEL/progress" \\
             -H "Authorization: Bearer $THE_LAB_TOKEN" \\
             -H "Content-Type: application/json" \\
             -d '{"step": 42, "loss": 0.34}'
    """
    from .. import ws as ws_mod

    exp = _resolve_exp(exp_ref)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "body must be valid JSON")

    # Write to the local progress file so GET /progress still works, and
    # record a heartbeat for beat timing — both on the threadpool: this
    # handler is async (for request.json()) and progress posts arrive every
    # few seconds per running experiment, so NFS writes must not ride the
    # event loop.
    progress_path = REPO_DIR / exp["script"].replace(".sh", ".progress")
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(
            None, lambda: jsonio.write_json(progress_path, body, indent=0))
    except OSError as exc:
        raise HTTPException(500, f"could not write progress file: {exc}")
    await loop.run_in_executor(None, _record_heartbeat, exp["script"])

    # Broadcast immediately — no rsync lag.
    ws_mod.broadcaster.broadcast_soon({
        "type": "experiment_progress_updated",
        "label": exp.get("label", exp_ref),
        "idea_id": exp.get("idea_id"),
        "progress": body,
    })

    return {"ok": True}


@router.post("/experiments/{exp_ref}/pull")
async def pull_experiment_results(exp_ref: str):
    """Trigger immediate rsync pull from the remote slurm job dir.

    Called by the wrapper script on the compute node after the experiment
    finishes and artifacts are stable — before the wrapper cleans up the
    worktree and venv. Blocks until rsync completes so the caller knows
    the data is safe before deleting local files.
    """
    exp = _resolve_exp(exp_ref)
    label = exp.get("label") or str(exp["id"])
    ok = await runner.pull_results_for_label(label)
    return {"ok": ok, "label": label}


# --- Timeseries ---

@router.get("/experiments/{exp_ref}/timeseries")
def get_experiment_timeseries(
    exp_ref: str,
    keys: str | None = Query(default=None, description="Comma-separated metric keys to include"),
    last: int | None = Query(default=None, description="Return only the last N data points"),
):
    """Get per-step metrics logged by the experiment script.

    Returns the timeseries data that the script wrote to the
    ``$THE_LAB_METRICS`` file during execution. Each data point contains a
    ``step``, ``wall_time``, and one or more metric values. Use ``?keys=loss,lr``
    to include only specific metric keys (``step`` and ``wall_time`` are always
    included). Use ``?last=100`` to return only the most recent 100 data points.

    Example:
        GET /api/v1/experiments/4/timeseries?keys=loss,lr&last=100
        -> {"points": [{"step": 900, "wall_time": 1234.5, "loss": 0.31, "lr": 0.0003}, ...],
            "count": 100}
    """
    exp = _resolve_exp(exp_ref)
    points = store.get_timeseries(exp["id"])
    if points is None:
        raise HTTPException(404, "experiment not found")
    if keys:
        filter_keys = {k.strip() for k in keys.split(",")}
        filter_keys.add("step")
        filter_keys.add("wall_time")
        points = [{k: v for k, v in p.items() if k in filter_keys} for p in points]
    if last is not None and last > 0:
        points = points[-last:]
    return {"points": points, "count": len(points)}


# --- URL confusion redirects ---
# Agents often construct wrong URLs like /ideas/{id}/experiments/{exp_ref}
# instead of /experiments/{exp_ref}. These catch routes serve the correct
# response and include a hint about the canonical URL.

@router.get("/ideas/{idea_id}/experiments/{exp_ref}")
def get_experiment_via_idea(idea_id: int, exp_ref: str):
    """Convenience alias — redirects to GET /experiments/{exp_ref}.

    Agents sometimes construct this URL pattern. This route serves the
    experiment data directly and includes a hint about the canonical URL.
    """
    exp = dict(_resolve_exp(exp_ref))  # copy — never annotate the live record
    exp["_hint"] = f"Tip: use GET /api/v1/experiments/{exp_ref} directly next time"
    return exp


@router.post("/ideas/{idea_id}/experiments/{exp_ref}/start")
async def start_experiment_via_idea(idea_id: int, exp_ref: str):
    """Convenience alias — redirects to POST /experiments/{exp_ref}/start."""
    return await start_experiment(exp_ref)


@router.get("/ideas/{idea_id}/experiments/{exp_ref}/log")
def get_experiment_log_via_idea(idea_id: int, exp_ref: str, tail: int | None = Query(default=25), head: int | None = None, full: bool = False, grep: str | None = None, above: int = 0, below: int = 0):
    """Convenience alias — redirects to GET /experiments/{exp_ref}/log."""
    return get_experiment_log(exp_ref, tail=tail, head=head, full=full, grep=grep, above=above, below=below)


@router.post("/ideas/{idea_id}/experiments/{exp_ref}/cancel")
async def cancel_experiment_via_idea(idea_id: int, exp_ref: str):
    """Convenience alias — redirects to POST /experiments/{exp_ref}/cancel."""
    return await cancel_experiment(exp_ref)



@router.post("/ideas/{idea_id}/experiments/{exp_ref}/rerun", status_code=201)
async def rerun_experiment_via_idea(idea_id: int, exp_ref: str):
    """Convenience alias — redirects to POST /experiments/{exp_ref}/rerun."""
    return await rerun_experiment(exp_ref)


@router.post("/experiments/{exp_ref}/requeue")
async def requeue_experiment(exp_ref: str, req: RequeueRequest | None = None):
    """Mark a running or failed experiment as queued for re-dispatch.

    Called by the Slurm wrapper when the job is preempted (SIGTERM received).
    Records the event in meta.slurm_attempts, resets status to 'queued',
    and wakes the scheduler.
    """
    exp = _resolve_exp(exp_ref)
    label = exp.get("label") or str(exp["id"])
    now = datetime.now(timezone.utc).isoformat()
    reason = (req.reason if req else None) or "preempted"

    meta = dict(exp.get("meta") or {})
    slurm_attempts = list(meta.get("slurm_attempts") or [])
    slurm_attempts.append({
        "job_id": meta.get("slurm_job_id"),
        "reason": reason,
        "at": now,
    })
    meta["slurm_attempts"] = slurm_attempts
    meta.pop("slurm_job_id", None)

    updated = store.update_experiment(
        label,
        status="queued",
        queued_at=now,
        meta=meta,
        error=None,
    )
    runner.wake_scheduler()

    try:
        from .. import ws as ws_mod
        ws_mod.broadcaster.broadcast_soon({
            "type": "experiment_queued",
            "label": label,
            "idea_id": exp.get("idea_id"),
        })
    except Exception:
        pass

    return updated or store.get_experiment(label) or exp


@router.post("/experiments/{exp_ref}/slurm_done")
async def slurm_done(exp_ref: str, req: SlurmDoneRequest | None = None):
    """Hint from Slurm wrapper that the job completed normally.

    Called by the wrapper script on the compute node when the experiment
    script exits without preemption. The monitor task is the authoritative
    completion path (it rsyncs results and parses metrics); this endpoint
    is a fast-path hint so the lab marks failures quickly.

    - exit_code == 0: leave as-is (monitor will complete it after rsync)
    - exit_code != 0: mark as failed with the exit code
    """
    exp = _resolve_exp(exp_ref)
    label = exp.get("label") or str(exp["id"])
    exit_code = (req.exit_code if req else None) or 0
    now = datetime.now(timezone.utc).isoformat()

    if exit_code != 0:
        updated = store.update_experiment(
            label,
            status="failed",
            error=f"slurm job exited with code {exit_code}",
            pid=None,
            finished_at=now,
        )
        runner.wake_scheduler()
        try:
            from .. import ws as ws_mod
            ws_mod.broadcaster.broadcast_soon({
                "type": "experiment_finished",
                "label": label,
                "idea_id": exp.get("idea_id"),
                "status": "failed",
                "metrics": None,
            })
        except Exception:
            pass
        return updated or store.get_experiment(label) or exp
    # exit_code == 0: monitor handles real completion; just acknowledge
    return store.get_experiment(label) or exp


@router.get("/ideas/{idea_id}/experiments/{exp_ref}/progress")
def get_experiment_progress_via_idea(idea_id: int, exp_ref: str):
    """Convenience alias — redirects to GET /experiments/{exp_ref}/progress."""
    return get_experiment_progress(exp_ref)
