"""Experiment CRUD, start/cancel, timeseries, compare, and analyze endpoints."""
from __future__ import annotations

import json
import os

import mimetypes

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from ..deps import (
    store,
    runner,
    REPO_DIR,
    _resolve_exp,
    _idea_context,
    _branch_diff_summary,
    _wrap_script,
)
from ..git_ops import get_current_branch
from ..schemas import (
    NewExperimentRequest,
    StartExperimentRequest,
    RenameTagRequest,
    UpdateTagsRequest,
    AnalyzeRequest,
)

router = APIRouter(prefix="/api/v1")


# --- Experiments ---

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
    exp = store.create_experiment(idea_id, req.description, meta=req.meta, tags=req.tags)

    if req.script_content is not None:
        script_path = REPO_DIR / exp["script"]
        script_path.parent.mkdir(parents=True, exist_ok=True)
        script_path.write_text(_wrap_script(req.script_content))
        os.chmod(script_path, 0o644)

    # Auto-start: when script_content is provided, start by default unless auto_start=False
    should_start = req.auto_start if req.auto_start is not None else (req.script_content is not None)
    if should_start:
        result = await runner.start(exp["id"])
        if result["status"] != "error":
            exp = result.get("experiment", exp)
            exp["auto_started"] = True

    # Include branch diff summary so agent sees what code changed (or didn't)
    diff_summary = _branch_diff_summary(idea_id)
    if diff_summary:
        exp["branch_diff"] = diff_summary

    return exp


@router.get("/ideas/{idea_id}/experiments")
def list_experiments(idea_id: int):
    """List all experiments belonging to an idea.

    Returns every experiment record for the given idea, regardless of status.
    Each record includes the experiment's description, status, metrics, meta,
    tags, and timing information. Failed experiments include a ``read_log`` URL.

    Example:
        GET /api/v1/ideas/1/experiments
        -> [{"id": 4, "idea_id": 1, "status": "completed", "metrics": {"acc": 0.91}, ...}, ...]
    """
    exps = store.list_experiments(idea_id)
    results = []
    for exp in exps:
        out = {**exp}
        if out.get("status") == "failed":
            label = out.get("label", str(out["id"]))
            out["read_log"] = f"GET /api/v1/experiments/{label}/log"
        # Strip tags to encourage ?tags= filtering on /orient or /leaderboard
        out.pop("tags", None)
        results.append(out)
    return results


# --- Bulk listing (must come before parameterized) ---

@router.get("/experiments")
def list_all_experiments(
    status: str | None = Query(default=None, description="Filter by status: completed, failed, running, pending"),
    tags: str | None = Query(default=None, description="Comma-separated tags — experiments must have ALL (AND filter)"),
    metric: str | None = Query(default=None, description="Only return experiments that have this metric"),
):
    """List all experiments across all ideas.

    Returns every experiment with its metrics, tags, and parent idea description.
    Use filters to narrow results. This is more efficient than fetching
    experiments per-idea when you need data across the whole project.

    Example:
        GET /api/v1/experiments
        GET /api/v1/experiments?status=completed&metric=score
        GET /api/v1/experiments?tags=baseline
    """
    all_exps = store.list_all_experiments()
    idea_cache: dict[int, dict] = {}

    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []

    results = []
    for exp in all_exps:
        if status and exp.get("status") != status:
            continue
        if tag_list and not all(t in (exp.get("tags") or []) for t in tag_list):
            continue
        if metric and metric not in (exp.get("metrics") or {}):
            continue
        out = {**exp}
        idea_id = out["idea_id"]
        if idea_id not in idea_cache:
            idea = store.get_idea(idea_id)
            idea_cache[idea_id] = idea
        idea = idea_cache[idea_id]
        out["idea_description"] = idea["description"] if idea else None
        # Strip tags from bulk response to encourage ?tags= filtering
        if not tag_list:
            out.pop("tags", None)
        results.append(out)

    resp = {"experiments": results, "count": len(results)}
    if not tag_list:
        resp["tag_hint"] = "Tags hidden in bulk listing. Use GET /experiments/tags to list tags, then GET /experiments?tags=<tag> to filter by approach."
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

@router.get("/experiments/{exp_ref}")
def get_experiment(exp_ref: str):
    """Get full detail for a single experiment.

    Accepts global ID (``4``) or label (``1.2`` = idea 1, experiment 2).

    Example:
        GET /api/v1/experiments/1.2
        -> {"id": 4, "label": "1.2", "idea_id": 1, "seq": 2, "status": "completed", ...}
    """
    exp = _resolve_exp(exp_ref)
    # Add score comparison for completed experiments
    if exp.get("status") == "completed":
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
                    if e["id"] == exp["id"]:
                        continue
                    em = e.get("metrics") or {}
                    for key in ("score", "accuracy", "final_score"):
                        if key in em and (best_score is None or em[key] > best_score):
                            best_score = em[key]
                            best_exp_id = e["id"]
                            best_exp_label = e.get("label", str(e["id"]))
            if best_score is not None:
                exp["progress"] = {
                    "this_score": current_score,
                    "best_score": best_score,
                    "best_experiment_id": best_exp_id,
                    "best_experiment_label": best_exp_label,
                    "is_new_best": current_score > best_score,
                }
    if exp.get("status") == "failed":
        label = exp.get("label", str(exp["id"]))
        exp["read_log"] = f"GET /api/v1/experiments/{label}/log"
    return exp


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
    """Run an experiment's script.

    Auto-commits any uncommitted changes on the idea's branch, creates a git
    worktree for isolated execution, and launches the script as a background
    process. An optional ``timeout`` (in seconds) will automatically cancel
    the experiment if it exceeds the limit. Returns the experiment record with
    the current branch context.

    Example:
        POST /api/v1/experiments/4/start {"timeout": 600}
        -> {"status": "running", "experiment": {"id": 4, ...},
            "current_branch": "idea/1", "idea_id": 1, ...}
    """
    # Resolve exp ref (global ID or label like '1.2')
    exp_check = _resolve_exp(exp_ref)
    exp_id = exp_check["id"]
    # Idempotent: if already running, return current state instead of error
    if exp_check.get("status") == "running":
        return {
            "status": "already_running",
            "experiment": exp_check,
            "current_branch": get_current_branch(cwd=REPO_DIR),
            **_idea_context(exp_check.get("idea_id")),
        }
    timeout = req.timeout if req else None
    result = await runner.start(exp_id, timeout=timeout)
    if result["status"] == "error":
        raise HTTPException(400, result.get("error", "experiment failed to start"))
    # Add idea context + current branch
    exp = result.get("experiment", {})
    result["current_branch"] = get_current_branch(cwd=REPO_DIR)
    result.update(_idea_context(exp.get("idea_id")))
    return result



@router.post("/experiments/{exp_ref}/rerun", status_code=201)
async def rerun_experiment(exp_ref: str):
    """Create and start a new experiment cloned from an existing one's script.

    Reads the script file of the referenced experiment and creates a brand-new
    experiment on the same idea with the same script content, description
    (prefixed with "rerun: "), tags, and meta. The new experiment is auto-started
    immediately. Works on experiments in any status — useful for re-running a
    completed experiment to check reproducibility.

    Example:
        POST /api/v1/experiments/1.2/rerun
        -> {"id": "1.3", "label": "1.3", "idea_id": 1, "status": "running", ...}
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

    # Create the new experiment
    desc = f"rerun: {source['description']}"
    new_exp = store.create_experiment(
        idea_id, desc, meta=source.get("meta"), tags=source.get("tags"),
    )

    # Write the script (reuse content as-is — it's already wrapped)
    new_script_path = REPO_DIR / new_exp["script"]
    new_script_path.parent.mkdir(parents=True, exist_ok=True)
    new_script_path.write_text(script_content)
    os.chmod(new_script_path, 0o644)

    # Auto-start
    result = await runner.start(new_exp["id"])
    if result["status"] == "error":
        raise HTTPException(400, result.get("error", "experiment failed to start"))

    exp_out = result.get("experiment", new_exp)
    exp_out["rerun_of"] = source.get("label", str(source["id"]))
    return exp_out


@router.post("/experiments/{exp_ref}/cancel")
async def cancel_experiment(exp_ref: str):
    """Cancel a pending or running experiment.

    For running experiments, sends SIGTERM to the experiment process, giving it
    a chance to clean up. If the process does not exit promptly, SIGKILL is
    sent to force termination. Pending experiments are marked ``cancelled``
    immediately.

    Example:
        POST /api/v1/experiments/4/cancel
        -> {"id": 4, "status": "cancelled", ...}
    """
    exp = _resolve_exp(exp_ref)
    result = await runner.cancel(exp["id"])
    if result is None:
        raise HTTPException(404, "experiment not found")
    return result


@router.get("/experiments/{exp_ref}/log")
def get_experiment_log(exp_ref: str, tail: int | None = None):
    """Read the stdout/stderr log for an experiment.

    Returns the combined output log captured during experiment execution. Use
    ``?tail=50`` to retrieve only the last 50 lines, which is useful for
    checking recent progress on long-running experiments without downloading
    the entire log.

    Example:
        GET /api/v1/experiments/4/log?tail=50
        -> {"log": "Epoch 49/50 - loss: 0.31 - acc: 0.92\\nEpoch 50/50 - loss: 0.30 ..."}
    """
    exp = _resolve_exp(exp_ref)
    log = runner.get_log(exp["id"], tail=tail)
    if log is None:
        raise HTTPException(404, "experiment log not found")
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
    progress_path = REPO_DIR / exp["script"].replace(".sh", ".progress")
    result = {"status": exp["status"]}
    if progress_path.exists():
        try:
            result["progress"] = json.loads(progress_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return result


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
    exp = _resolve_exp(exp_ref)
    exp["_hint"] = f"Tip: use GET /api/v1/experiments/{exp_ref} directly next time"
    return exp


@router.post("/ideas/{idea_id}/experiments/{exp_ref}/start")
async def start_experiment_via_idea(idea_id: int, exp_ref: str):
    """Convenience alias — redirects to POST /experiments/{exp_ref}/start."""
    return await start_experiment(exp_ref)


@router.get("/ideas/{idea_id}/experiments/{exp_ref}/log")
def get_experiment_log_via_idea(idea_id: int, exp_ref: str, tail: int | None = None):
    """Convenience alias — redirects to GET /experiments/{exp_ref}/log."""
    return get_experiment_log(exp_ref, tail=tail)


@router.post("/ideas/{idea_id}/experiments/{exp_ref}/cancel")
async def cancel_experiment_via_idea(idea_id: int, exp_ref: str):
    """Convenience alias — redirects to POST /experiments/{exp_ref}/cancel."""
    return await cancel_experiment(exp_ref)



@router.post("/ideas/{idea_id}/experiments/{exp_ref}/rerun", status_code=201)
async def rerun_experiment_via_idea(idea_id: int, exp_ref: str):
    """Convenience alias — redirects to POST /experiments/{exp_ref}/rerun."""
    return await rerun_experiment(exp_ref)


@router.get("/ideas/{idea_id}/experiments/{exp_ref}/progress")
def get_experiment_progress_via_idea(idea_id: int, exp_ref: str):
    """Convenience alias — redirects to GET /experiments/{exp_ref}/progress."""
    return get_experiment_progress(exp_ref)
