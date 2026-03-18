"""The Lab — Experiment Management API."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .git_ops import (
    GitError,
    branch_diff,
    checkout_idea,
    create_branch_from,
    create_branch_from_merge,
    get_current_branch,
    get_default_branch,
)
from .store import Store
from .runner import ExperimentRunner

# --- Configuration ---
REPO_DIR = Path(os.environ.get("THE_LAB_REPO", os.getcwd())).resolve()

store = Store(REPO_DIR)
app = FastAPI(title="The Lab", version="0.1.0")
runner = ExperimentRunner(store)


@app.on_event("startup")
async def startup():
    await runner.reattach_running()

_DEV_MODE = os.environ.get("THE_LAB_DEV") == "1"
_STATIC_DIR = Path(__file__).parent / "static"

# Serve Vite build output if it exists, otherwise fall back to legacy dashboard.html
if _STATIC_DIR.exists() and (_STATIC_DIR / "index.html").exists():
    _SPA_HTML = (_STATIC_DIR / "index.html").read_text()
    _ASSETS_DIR = _STATIC_DIR / "assets"
    if _ASSETS_DIR.exists():
        app.mount("/assets", StaticFiles(directory=_ASSETS_DIR), name="assets")
else:
    _SPA_HTML = None

_DASHBOARD_HTML: str | None = None

def _load_dashboard() -> str:
    global _DASHBOARD_HTML
    if _DEV_MODE or _DASHBOARD_HTML is None:
        html_path = Path(__file__).parent / "dashboard.html"
        if html_path.exists():
            _DASHBOARD_HTML = html_path.read_text()
        else:
            _DASHBOARD_HTML = "<html><body>Dashboard not found. Run npm run build in dashboard/.</body></html>"
    return _DASHBOARD_HTML


# --- Request schemas ---

class ResourceItem(BaseModel):
    url: str
    label: str = ""


class NewIdeaRequest(BaseModel):
    parent_ids: list[int] = []
    description: str


class SuggestIdeaRequest(BaseModel):
    description: str
    parent_ids: list[int] = []
    priority: Literal["normal", "high"] = "normal"
    resources: list[ResourceItem] = []


class AdoptRequest(BaseModel):
    agent_note: str | None = None


class NewExperimentRequest(BaseModel):
    description: str
    meta: dict | None = None
    script_content: str | None = None
    tags: list[str] = []


class StartExperimentRequest(BaseModel):
    timeout: float | None = None


class NoteRequest(BaseModel):
    text: str
    level: Literal["insight", "milestone", "observation", "debug"] = "observation"
    resources: list[ResourceItem] = []


class ConcludeRequest(BaseModel):
    conclusion: str


class AbandonRequest(BaseModel):
    reason: str


class ReopenRequest(BaseModel):
    reason: str


# --- Script guard ---

SCRIPT_GUARD = """\
if [ -z "$THE_LAB_TOKEN" ]; then
  echo "ERROR: This script must be run via the-lab API (POST /experiments/<id>/start)." >&2
  exit 1
fi
"""


PREAMBLE_SOURCE = 'source .the_lab/preamble.sh 2>/dev/null || true\n'


def _wrap_script(content: str) -> str:
    """Inject a guard + optional preamble that prevents running outside the backend."""
    lines = content.split("\n", 1)
    if lines[0].startswith("#!"):
        return lines[0] + "\n" + SCRIPT_GUARD + PREAMBLE_SOURCE + (lines[1] if len(lines) > 1 else "")
    return "#!/bin/bash\n" + SCRIPT_GUARD + PREAMBLE_SOURCE + content


def _idea_context(idea_id: int) -> dict:
    """Build a compact context dict for an idea."""
    idea = store.get_idea(idea_id)
    if not idea:
        return {"idea_id": idea_id}
    return {
        "idea_id": idea["id"],
        "idea_description": idea["description"],
        "branch": idea["branch"],
    }


# --- Ideas ---

@app.post("/api/v1/ideas/new", status_code=201)
def create_idea(req: NewIdeaRequest):
    """Create a new idea and its corresponding git branch.

    Creates an idea record and a git branch named ``idea/<id>``. If no
    ``parent_ids`` are supplied, the current branch is inspected: when you are
    already on ``idea/N``, that idea is automatically used as the parent. For
    multi-parent ideas the branches are merged; if the merge has conflicts, the
    response contains a ``conflicts`` list instead of the created idea. The
    response also includes a ``similar_ideas`` array when existing ideas have
    descriptions close to the new one, which helps avoid duplicate work.

    Example:
        POST /api/v1/ideas/new {"parent_ids": [1], "description": "test new hypothesis"}
        -> {"id": 3, "branch": "idea/3", "status": "active", "similar_ideas": [...]}
    """
    # If no parents given, infer from current branch (idea/N → parent is N)
    parent_ids = req.parent_ids
    if not parent_ids:
        current = get_current_branch(cwd=REPO_DIR)
        if current.startswith("idea/"):
            try:
                current_idea_id = int(current.split("/")[1])
                if store.get_idea(current_idea_id):
                    parent_ids = [current_idea_id]
            except (ValueError, IndexError):
                pass

    for pid in parent_ids:
        if not store.get_idea(pid):
            raise HTTPException(404, f"parent idea {pid} not found")

    idea = store.create_idea(req.description, parent_ids, branch="")
    idea_id = idea["id"]
    branch_name = f"idea/{idea_id}"
    idea["branch"] = branch_name

    try:
        if len(parent_ids) == 0:
            base = get_default_branch(cwd=REPO_DIR)
            create_branch_from(branch_name, base, cwd=REPO_DIR)
        elif len(parent_ids) == 1:
            parent = store.get_idea(parent_ids[0])
            create_branch_from(branch_name, parent["branch"], cwd=REPO_DIR)
        else:
            parent_branches = [store.get_idea(pid)["branch"] for pid in parent_ids]
            conflicts = create_branch_from_merge(branch_name, parent_branches, cwd=REPO_DIR)
            if conflicts is not None:
                return {"status": "conflict", "conflicts": conflicts}

        store.save_idea(idea)
        similar = store.find_similar_ideas(req.description)
        similar = [s for s in similar if s["id"] != idea["id"]]
        if similar:
            idea["similar_ideas"] = similar
        return idea
    except GitError as e:
        raise HTTPException(500, str(e))


@app.post("/api/v1/ideas/{idea_id}/checkout")
def checkout_idea_endpoint(idea_id: int):
    """Switch the working tree to an idea's branch.

    Auto-commits any uncommitted changes on the current branch before
    switching. If there are staged or unstaged changes that cannot be
    committed, they are stashed so the checkout can proceed cleanly. Returns
    the new branch name along with the idea context.

    Example:
        POST /api/v1/ideas/2/checkout
        -> {"branch": "idea/2", "stashed": false, "idea_id": 2, "idea_description": "..."}
    """
    idea = store.get_idea(idea_id)
    if not idea:
        raise HTTPException(404, "idea not found")
    try:
        result = checkout_idea(idea_id, cwd=REPO_DIR)
        return {
            **result,
            "idea_id": idea["id"],
            "idea_description": idea["description"],
        }
    except GitError as e:
        raise HTTPException(500, str(e))


@app.get("/api/v1/ideas")
def list_ideas(status: str | None = None, source: str | None = None):
    """List all ideas with their notes and a compact experiment summary.

    Each idea in the response includes journal notes (insight/milestone/observation
    levels) and an ``experiment_summary`` object with counts of total, completed,
    failed, and running experiments plus the latest completed metrics. Use the
    ``status`` query parameter (e.g. ``?status=active``) to filter by idea
    status, and ``source`` (e.g. ``?source=human``) to filter by origin.

    Example:
        GET /api/v1/ideas?status=active
        -> [{"id": 1, "description": "...", "status": "active",
             "notes": [...], "experiment_summary": {"total": 5, "completed": 3, ...}}, ...]
    """
    ideas = store.list_ideas(status=status, source=source)
    for idea in ideas:
        idea["notes"] = store.get_notes(idea["id"], levels=Store.LISTING_LEVELS)
        # Compact experiment summary with latest completed metrics
        exps = store.list_experiments(idea["id"])
        completed = [e for e in exps if e["status"] == "completed" and e.get("metrics")]
        latest = max(completed, key=lambda e: e.get("finished_at", "")) if completed else None
        idea["experiment_summary"] = {
            "total": len(exps),
            "completed": len(completed),
            "failed": sum(1 for e in exps if e["status"] == "failed"),
            "running": sum(1 for e in exps if e["status"] == "running"),
            "latest_metrics": latest["metrics"] if latest else None,
            "latest_experiment_id": latest["id"] if latest else None,
        }
    return ideas


@app.get("/api/v1/ideas/{idea_id}")
def get_idea(idea_id: int, notes: str | None = None):
    """Get full detail for a single idea, including its experiments and notes.

    Returns the idea record with all associated experiments and journal notes.
    By default, debug-level notes are excluded. Pass ``?notes=all`` to include
    every note level (insight, milestone, observation, and debug), which is
    useful for troubleshooting experiment scripts or understanding low-level
    decisions.

    Example:
        GET /api/v1/ideas/1?notes=all
        -> {"id": 1, "description": "...", "status": "active", "branch": "idea/1",
            "experiments": [...], "notes": [...]}
    """
    idea = store.get_idea(idea_id)
    if not idea:
        raise HTTPException(404, "idea not found")
    idea["experiments"] = store.list_experiments(idea_id)
    if notes == "all":
        idea["notes"] = store.get_notes(idea_id, levels=Store.ALL_LEVELS)
    else:
        idea["notes"] = store.get_notes(idea_id, levels=Store.DETAIL_LEVELS)
    return idea


@app.get("/api/v1/ideas/{idea_id}/tree")
def get_idea_tree(idea_id: int):
    """Get the ancestor and descendant tree for an idea, with notes.

    Walks the parent chain upward to collect all ancestors and the child chain
    downward to collect all descendants. Each node in the tree includes its
    listing-level notes (insight/milestone/observation). The root idea itself
    is returned with detail-level notes and its full experiment list.

    Example:
        GET /api/v1/ideas/3/tree
        -> {"idea": {"id": 3, ...}, "ancestors": [{"id": 1, ...}], "descendants": [{"id": 5, ...}]}
    """
    idea = store.get_idea(idea_id)
    if not idea:
        raise HTTPException(404, "idea not found")

    ancestors = []
    visited = set()
    queue = list(idea.get("parent_ids", []))
    while queue:
        pid = queue.pop(0)
        if pid in visited:
            continue
        visited.add(pid)
        parent = store.get_idea(pid)
        if parent:
            parent["notes"] = store.get_notes(pid, levels=Store.LISTING_LEVELS)
            ancestors.append(parent)
            queue.extend(parent.get("parent_ids", []))

    descendants = []
    visited = set()
    all_ideas = store.list_ideas()
    queue_ids = [i["id"] for i in all_ideas if idea_id in i.get("parent_ids", [])]
    while queue_ids:
        cid = queue_ids.pop(0)
        if cid in visited:
            continue
        visited.add(cid)
        child = store.get_idea(cid)
        if child:
            child["notes"] = store.get_notes(cid, levels=Store.LISTING_LEVELS)
            descendants.append(child)
            queue_ids.extend(i["id"] for i in all_ideas if cid in i.get("parent_ids", []))

    idea["experiments"] = store.list_experiments(idea_id)
    idea["notes"] = store.get_notes(idea_id, levels=Store.DETAIL_LEVELS)
    return {"idea": idea, "ancestors": ancestors, "descendants": descendants}


@app.get("/api/v1/ideas/{idea_id}/diff")
def get_idea_diff(
    idea_id: int,
    base: str | None = Query(default=None, description="Base branch (default: first parent's branch, or main)"),
):
    """Get the git diff between this idea's branch and a base branch.

    Shows what changed on this idea's branch relative to a base. By default
    the base is the first parent idea's branch, or ``main`` if the idea has
    no parents. You can override this with the ``?base=`` query parameter to
    diff against any branch.

    Example:
        GET /api/v1/ideas/3/diff
        -> {"diff": "diff --git a/model.py b/model.py\\n...", "base": "idea/1", "head": "idea/3"}
    """
    idea = store.get_idea(idea_id)
    if not idea:
        raise HTTPException(404, "idea not found")
    idea_branch = idea.get("branch", "")
    if not idea_branch:
        raise HTTPException(400, "idea has no branch")

    if base is None:
        parent_ids = idea.get("parent_ids", [])
        if parent_ids:
            parent = store.get_idea(parent_ids[0])
            base = parent["branch"] if parent and parent.get("branch") else "main"
        else:
            base = "main"

    return branch_diff(idea_branch, base, cwd=REPO_DIR)


@app.post("/api/v1/ideas/{idea_id}/conclude")
def conclude_idea(idea_id: int, req: ConcludeRequest):
    """Mark an active idea as concluded with a conclusion text.

    Transitions the idea's status from ``active`` to ``concluded`` and stores
    the provided conclusion. Only active ideas can be concluded; attempting to
    conclude an idea in any other status returns a 400 error.

    Example:
        POST /api/v1/ideas/1/conclude {"conclusion": "Learning rate 3e-4 is optimal"}
        -> {"id": 1, "status": "concluded", "conclusion": "Learning rate 3e-4 is optimal", ...}
    """
    idea = store.get_idea(idea_id)
    if not idea:
        raise HTTPException(404, "idea not found")
    if idea["status"] != "active":
        raise HTTPException(400, f"idea is {idea['status']}, cannot conclude")
    return store.update_idea(idea_id, status="concluded", conclusion=req.conclusion)


@app.post("/api/v1/ideas/{idea_id}/abandon")
def abandon_idea(idea_id: int, req: AbandonRequest):
    """Abandon an idea that is no longer worth pursuing.

    Transitions the idea's status to ``abandoned`` and records the reason.
    Works on both ``active`` and ``suggested`` ideas. Ideas in other states
    (e.g. already concluded or abandoned) cannot be abandoned again and will
    return a 400 error.

    Example:
        POST /api/v1/ideas/2/abandon {"reason": "Approach too slow, superseded by idea 4"}
        -> {"id": 2, "status": "abandoned", "conclusion": "Approach too slow, superseded by idea 4", ...}
    """
    idea = store.get_idea(idea_id)
    if not idea:
        raise HTTPException(404, "idea not found")
    if idea["status"] not in ("active", "suggested"):
        raise HTTPException(400, f"idea is {idea['status']}, cannot abandon")
    return store.update_idea(idea_id, status="abandoned", conclusion=req.reason)


@app.post("/api/v1/ideas/{idea_id}/reopen")
def reopen_idea(idea_id: int, req: ReopenRequest):
    """Reopen a concluded or abandoned idea, returning it to active status.

    Moves the idea back to ``active``. The previous conclusion or abandonment
    reason is preserved as an insight-level note so it remains visible in the
    idea's history. A milestone note is also added recording the reopen reason.
    Only ``concluded`` or ``abandoned`` ideas can be reopened.

    Example:
        POST /api/v1/ideas/1/reopen {"reason": "New data suggests this is worth revisiting"}
        -> {"id": 1, "status": "active", "conclusion": null, ...}
    """
    idea = store.get_idea(idea_id)
    if not idea:
        raise HTTPException(404, "idea not found")
    if idea["status"] not in ("concluded", "abandoned"):
        raise HTTPException(400, f"idea is {idea['status']}, cannot reopen")

    old_status = idea["status"]
    old_conclusion = idea.get("conclusion")
    if old_conclusion:
        store.add_note(
            idea_id,
            f"[reopened from {old_status}] previous conclusion: {old_conclusion}",
            level="insight",
        )

    store.add_note(idea_id, f"reopened: {req.reason}", level="milestone")
    return store.update_idea(idea_id, status="active", conclusion=None)


@app.post("/api/v1/ideas/{idea_id}/note", status_code=201)
def add_note(idea_id: int, req: NoteRequest):
    """Add a journal note to an idea.

    Attaches a timestamped note to the idea. The ``level`` field controls
    visibility: ``insight`` for key findings, ``milestone`` for progress
    markers, ``observation`` (default) for general notes, and ``debug`` for
    low-level details hidden from most views. You can optionally attach
    ``resources`` (URL + label pairs) to link relevant files, papers, or
    dashboards.

    Example:
        POST /api/v1/ideas/1/note {"text": "Loss plateaued at 0.35", "level": "insight",
                                    "resources": [{"url": "https://wandb.ai/run/42", "label": "W&B run"}]}
        -> {"id": 7, "idea_id": 1, "text": "Loss plateaued at 0.35", "level": "insight", ...}
    """
    idea = store.get_idea(idea_id)
    if not idea:
        raise HTTPException(404, "idea not found")
    resources = [r.model_dump() for r in req.resources] if req.resources else None
    return store.add_note(idea_id, req.text, level=req.level, resources=resources)


# --- Experiments ---

@app.post("/api/v1/ideas/{idea_id}/experiments", status_code=201)
def create_experiment(idea_id: int, req: NewExperimentRequest):
    """Create a new experiment under an idea.

    Registers an experiment record and, if ``script_content`` is provided,
    writes the script file to disk with an auto-injected guard and preamble.
    The idea must be in ``active`` status. Use ``meta`` to store arbitrary
    hyperparameters or configuration, and ``tags`` to categorize the
    experiment for filtering and comparison.

    Example:
        POST /api/v1/ideas/1/experiments {"description": "baseline run",
                                           "script_content": "#!/bin/bash\\npython train.py",
                                           "tags": ["baseline", "v1"],
                                           "meta": {"lr": 0.001, "epochs": 50}}
        -> {"id": 4, "idea_id": 1, "status": "pending", "script": ".the_lab/scripts/4.sh", ...}
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

    return exp


@app.get("/api/v1/ideas/{idea_id}/experiments")
def list_experiments(idea_id: int):
    """List all experiments belonging to an idea.

    Returns every experiment record for the given idea, regardless of status.
    Each record includes the experiment's description, status, metrics, meta,
    tags, and timing information.

    Example:
        GET /api/v1/ideas/1/experiments
        -> [{"id": 4, "idea_id": 1, "status": "completed", "metrics": {"acc": 0.91}, ...}, ...]
    """
    return store.list_experiments(idea_id)


@app.get("/api/v1/experiments/tags")
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


class RenameTagRequest(BaseModel):
    old: str
    new: str


@app.post("/api/v1/experiments/tags/rename")
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


@app.get("/api/v1/experiments/compare")
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

    # Pivot metrics into a table: metric_key → [value per experiment]
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

    return {
        "experiment_ids": exp_ids,
        "experiments": experiments,
        "metric_keys": metric_keys,
        "metrics": metrics_table,
        "meta_keys": meta_keys,
        "meta": meta_table,
    }


@app.get("/api/v1/experiments/{exp_id}")
def get_experiment(exp_id: int):
    """Get full detail for a single experiment.

    Returns the complete experiment record including its status, description,
    metrics, meta, tags, script path, and timing information (started_at,
    finished_at).

    Example:
        GET /api/v1/experiments/4
        -> {"id": 4, "idea_id": 1, "status": "completed", "description": "baseline run",
            "metrics": {"acc": 0.91, "loss": 0.35}, "meta": {"lr": 0.001}, "tags": ["baseline"], ...}
    """
    exp = store.get_experiment(exp_id)
    if not exp:
        raise HTTPException(404, "experiment not found")
    return exp


@app.post("/api/v1/experiments/{exp_id}/start")
async def start_experiment(exp_id: int, req: StartExperimentRequest | None = None):
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
    timeout = req.timeout if req else None
    result = await runner.start(exp_id, timeout=timeout)
    if result["status"] == "error":
        raise HTTPException(400, result)
    # Add idea context + current branch
    exp = result.get("experiment", {})
    result["current_branch"] = get_current_branch(cwd=REPO_DIR)
    result.update(_idea_context(exp.get("idea_id")))
    return result


@app.post("/api/v1/experiments/{exp_id}/restart")
async def restart_experiment(exp_id: int):
    """Re-run a failed or cancelled experiment.

    Restarts the experiment using the same script and configuration. Only
    experiments in ``failed`` or ``cancelled`` status can be restarted;
    attempting to restart an experiment in any other state returns a 400 error.
    The experiment transitions back to ``running`` and a new worktree/process
    is created.

    Example:
        POST /api/v1/experiments/4/restart
        -> {"status": "running", "experiment": {"id": 4, ...}, "current_branch": "idea/1", ...}
    """
    exp = store.get_experiment(exp_id)
    if not exp:
        raise HTTPException(404, "experiment not found")
    if exp["status"] not in ("failed", "cancelled"):
        raise HTTPException(400, f"experiment is {exp['status']}, can only restart failed or cancelled experiments")
    result = await runner.start(exp_id)
    if result["status"] == "error":
        raise HTTPException(400, result)
    result["current_branch"] = get_current_branch(cwd=REPO_DIR)
    result.update(_idea_context(exp.get("idea_id")))
    return result


@app.post("/api/v1/experiments/{exp_id}/cancel")
async def cancel_experiment(exp_id: int):
    """Kill a running experiment.

    Sends SIGTERM to the experiment process, giving it a chance to clean up.
    If the process does not exit promptly, SIGKILL is sent to force
    termination. The experiment status is set to ``cancelled``.

    Example:
        POST /api/v1/experiments/4/cancel
        -> {"id": 4, "status": "cancelled", ...}
    """
    result = await runner.cancel(exp_id)
    if result is None:
        raise HTTPException(404, "experiment not found")
    return result


@app.get("/api/v1/experiments/{exp_id}/log")
def get_experiment_log(exp_id: int, tail: int | None = None):
    """Read the stdout/stderr log for an experiment.

    Returns the combined output log captured during experiment execution. Use
    ``?tail=50`` to retrieve only the last 50 lines, which is useful for
    checking recent progress on long-running experiments without downloading
    the entire log.

    Example:
        GET /api/v1/experiments/4/log?tail=50
        -> {"log": "Epoch 49/50 - loss: 0.31 - acc: 0.92\\nEpoch 50/50 - loss: 0.30 ..."}
    """
    log = runner.get_log(exp_id, tail=tail)
    if log is None:
        raise HTTPException(404, "experiment not found")
    return {"log": log}


@app.get("/api/v1/experiments/{exp_id}/progress")
def get_experiment_progress(exp_id: int):
    """Read script-reported progress for an experiment.

    Returns the experiment's current status and, if the script has written a
    progress file (``<script_name>.progress``), includes the parsed JSON
    progress data. Scripts report progress by writing JSON to this file during
    execution.

    Example:
        GET /api/v1/experiments/4/progress
        -> {"status": "running", "progress": {"epoch": 25, "total_epochs": 50, "loss": 0.34}}
    """
    exp = store.get_experiment(exp_id)
    if not exp:
        raise HTTPException(404, "experiment not found")
    progress_path = REPO_DIR / exp["script"].replace(".sh", ".progress")
    result = {"status": exp["status"]}
    if progress_path.exists():
        try:
            result["progress"] = json.loads(progress_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return result


# --- Suggest & Adopt ---

@app.post("/api/v1/ideas/suggest", status_code=201)
def suggest_idea(req: SuggestIdeaRequest):
    """Submit an idea suggestion from a human for the agent to consider.

    Creates an idea in ``suggested`` status with ``source=human``. The idea
    does not get a git branch until the agent adopts it. Use ``priority`` to
    flag urgent suggestions as ``high``, and attach ``resources`` (URL + label
    pairs) to provide context such as papers, datasets, or related issues.

    Example:
        POST /api/v1/ideas/suggest {"description": "Try cosine annealing schedule",
                                     "parent_ids": [1], "priority": "high",
                                     "resources": [{"url": "https://arxiv.org/abs/...", "label": "paper"}]}
        -> {"id": 5, "status": "suggested", "source": "human", "priority": "high", ...}
    """
    for pid in req.parent_ids:
        if not store.get_idea(pid):
            raise HTTPException(404, f"parent idea {pid} not found")
    resources = [r.model_dump() for r in req.resources] if req.resources else []
    idea = store.create_idea(
        req.description,
        parent_ids=req.parent_ids,
        branch="",
        source="human",
        status="suggested",
        priority=req.priority,
        resources=resources,
    )
    store.save_idea(idea)
    return idea


@app.post("/api/v1/ideas/{idea_id}/adopt")
def adopt_idea(idea_id: int, req: AdoptRequest | None = None):
    """Adopt a suggested idea, creating its git branch and activating it.

    Transitions a ``suggested`` idea to ``active`` status and creates its
    ``idea/<id>`` branch (from the parent branch or main). Only ideas in
    ``suggested`` status can be adopted. An optional ``agent_note`` is saved
    as an observation note on the idea. For multi-parent ideas, branches are
    merged; if conflicts arise, the response contains a ``conflicts`` list.

    Example:
        POST /api/v1/ideas/5/adopt {"agent_note": "Looks promising, starting now"}
        -> {"id": 5, "status": "active", "branch": "idea/5", ...}
    """
    idea = store.get_idea(idea_id)
    if not idea:
        raise HTTPException(404, "idea not found")
    if idea["status"] != "suggested":
        raise HTTPException(400, f"idea is {idea['status']}, can only adopt suggested ideas")

    branch_name = f"idea/{idea_id}"
    parent_ids = idea.get("parent_ids", [])

    try:
        if len(parent_ids) == 0:
            base = get_default_branch(cwd=REPO_DIR)
            create_branch_from(branch_name, base, cwd=REPO_DIR)
        elif len(parent_ids) == 1:
            parent = store.get_idea(parent_ids[0])
            create_branch_from(branch_name, parent["branch"], cwd=REPO_DIR)
        else:
            parent_branches = [store.get_idea(pid)["branch"] for pid in parent_ids]
            conflicts = create_branch_from_merge(branch_name, parent_branches, cwd=REPO_DIR)
            if conflicts is not None:
                return {"status": "conflict", "conflicts": conflicts}

        store.update_idea(idea_id, status="active", branch=branch_name)
        if req and req.agent_note:
            store.add_note(idea_id, req.agent_note, level="observation")
        return store.get_idea(idea_id)
    except GitError as e:
        raise HTTPException(500, str(e))


# --- Timeseries ---

@app.get("/api/v1/experiments/{exp_id}/timeseries")
def get_experiment_timeseries(
    exp_id: int,
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
    points = store.get_timeseries(exp_id)
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


@app.get("/api/v1/experiments/compare-curves")
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


# --- Digest ---

@app.get("/api/v1/digest")
def get_digest(
    metric: str | None = Query(default=None, description="Focus on this metric — ranks ideas/experiments by it and returns a leaderboard"),
    top: int | None = Query(default=None, description="Limit leaderboard to top N experiments (default: all). Only used with metric="),
    tags: str | None = Query(default=None, description="Comma-separated tags — experiments must have ALL of them (AND filter)"),
):
    """Get a compact summary of all research activity.

    Without parameters, returns the full digest: global best metrics,
    concluded ideas with conclusions and insights, abandoned ideas, and
    recent key insights. Designed for a quick overview of what has been
    tried, what worked, and what was learned.

    With ``metric=accuracy``, focuses the digest on that metric: returns a
    ranked leaderboard of the top experiments, the best idea for that metric,
    and a progression timeline showing how the global best improved over time.

    With ``tags=ablation``, scopes everything to experiments with that tag.
    Multiple tags are AND-filtered: ``tags=ablation,training`` requires both.

    Combine all three: ``?metric=accuracy&top=10&tags=ablation`` gives the
    top 10 ablation experiments by accuracy.

    Example:
        GET /api/v1/digest
        -> {"total_ideas": 8, "total_experiments": 15, "best_metrics": {...}, ...}

        GET /api/v1/digest?metric=accuracy&top=5&tags=ablation,training
        -> {"metric": "accuracy", "tags": ["ablation", "training"],
            "leaderboard": [...], "best_idea": {...}, "progression": [...], ...}
    """
    all_ideas = store.list_ideas()
    all_exps = store.list_all_experiments()

    # Optional tag filter (AND: experiment must have ALL specified tags)
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
    if tag_list:
        all_exps = [e for e in all_exps if all(t in (e.get("tags") or []) for t in tag_list)]

    completed = [e for e in all_exps if e.get("status") == "completed" and e.get("metrics")]

    # --- Metric-focused digest ---
    if metric:
        # Leaderboard: experiments ranked by this metric (descending)
        with_metric = [e for e in completed if metric in e["metrics"] and isinstance(e["metrics"][metric], (int, float))]
        with_metric.sort(key=lambda e: e["metrics"][metric], reverse=True)

        if top is not None and top > 0:
            leaderboard = with_metric[:top]
        else:
            leaderboard = with_metric

        leaderboard_out = []
        for exp in leaderboard:
            idea = store.get_idea(exp["idea_id"])
            leaderboard_out.append({
                "experiment_id": exp["id"],
                "idea_id": exp["idea_id"],
                "idea_description": idea["description"] if idea else None,
                "value": exp["metrics"][metric],
                "tags": exp.get("tags", []),
                "meta": {k: v for k, v in (exp.get("meta") or {}).items() if k not in ("git_branch", "git_commit", "worktree")},
                "finished_at": exp.get("finished_at"),
            })

        # Best idea: which idea achieved the highest value for this metric?
        best_idea = None
        if leaderboard_out:
            best_exp = leaderboard_out[0]
            idea = store.get_idea(best_exp["idea_id"])
            if idea:
                insights = [n["text"] for n in store.get_notes(idea["id"], levels={"insight"})]
                best_idea = {
                    "id": idea["id"],
                    "description": idea["description"],
                    "conclusion": idea.get("conclusion"),
                    "best_value": best_exp["value"],
                    "key_insights": insights[:5],
                }

        # Progression: how the global best improved over time
        by_time = sorted(with_metric, key=lambda e: e.get("finished_at") or "")
        progression = []
        running_best = None
        for exp in by_time:
            v = exp["metrics"][metric]
            if running_best is None or v > running_best:
                running_best = v
                idea = store.get_idea(exp["idea_id"])
                progression.append({
                    "experiment_id": exp["id"],
                    "idea_id": exp["idea_id"],
                    "idea_description": idea["description"] if idea else None,
                    "value": v,
                    "finished_at": exp.get("finished_at"),
                })

        return {
            "metric": metric,
            "tags": tag_list or None,
            "total_experiments_with_metric": len(with_metric),
            "leaderboard": leaderboard_out,
            "best_idea": best_idea,
            "progression": progression,
        }

    # --- Full digest (no metric focus) ---
    # Global best metrics
    best_metrics: dict[str, dict] = {}
    for exp in completed:
        for key, value in exp["metrics"].items():
            if not isinstance(value, (int, float)):
                continue
            if key not in best_metrics or value > best_metrics[key]["value"]:
                best_metrics[key] = {
                    "value": value,
                    "idea_id": exp.get("idea_id"),
                    "experiment_id": exp.get("id"),
                }

    # Concluded ideas with conclusions and best metrics
    concluded = []
    for idea in all_ideas:
        if idea.get("status") != "concluded":
            continue
        idea_exps = [e for e in completed if e.get("idea_id") == idea["id"]]
        idea_best = {}
        for exp in idea_exps:
            for k, v in exp["metrics"].items():
                if isinstance(v, (int, float)) and (k not in idea_best or v > idea_best[k]):
                    idea_best[k] = v
        insights = [
            n["text"] for n in store.get_notes(idea["id"], levels={"insight"})
        ]
        concluded.append({
            "id": idea["id"],
            "description": idea["description"],
            "conclusion": idea.get("conclusion"),
            "key_insights": insights[:3],
            "best_metrics": idea_best or None,
            "experiment_count": len(idea_exps),
        })

    # Abandoned ideas (compact)
    abandoned = [
        {"id": i["id"], "description": i["description"], "reason": i.get("conclusion")}
        for i in all_ideas if i.get("status") == "abandoned"
    ]

    # Recent insight-level notes
    key_insights = []
    for idea in all_ideas:
        for note in store.get_notes(idea["id"], levels={"insight"}):
            key_insights.append({
                "text": note["text"],
                "idea_id": idea["id"],
                "created_at": note.get("created_at"),
            })
    key_insights.sort(key=lambda n: n.get("created_at", ""), reverse=True)

    status_counts = {}
    for idea in all_ideas:
        s = idea.get("status", "unknown")
        status_counts[s] = status_counts.get(s, 0) + 1

    return {
        "tags": tag_list or None,
        "total_ideas": len(all_ideas),
        "total_experiments": len(all_exps),
        "active_ideas": status_counts.get("active", 0),
        "suggested_ideas": status_counts.get("suggested", 0),
        "concluded_count": status_counts.get("concluded", 0),
        "abandoned_count": status_counts.get("abandoned", 0),
        "best_metrics": best_metrics,
        "concluded_ideas": concluded,
        "abandoned_ideas": abandoned,
        "key_insights": key_insights[:20],
    }


# --- Wait ---

@app.get("/api/v1/wait")
async def wait_for_experiment(
    timeout: float = Query(default=3600, le=86400),
    experiment_id: int | None = Query(default=None),
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
    result = await runner.wait_any(
        timeout=timeout,
        experiment_id=experiment_id,
        idea_id=idea_id,
    )
    result["current_branch"] = get_current_branch(cwd=REPO_DIR)
    exp = result.get("experiment")
    if exp:
        result.update(_idea_context(exp.get("idea_id")))
    return result


# --- Overview ---

@app.get("/api/v1/backlog")
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
    return {
        "current_branch": current,
        "active_ideas": result,
        "suggested_ideas": suggested_items,
        "total_running": total_running,
        "total_pending": total_pending,
    }


@app.get("/api/v1/graph")
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
        # Time range: earliest start → latest finish (for timeline view)
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


@app.get("/api/v1/chart-data")
def get_chart_data():
    """Get all data for the dashboard metrics chart in one request.

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
    ideas = store.list_ideas()
    completed_exps = []
    running_progress = []
    for idea in ideas:
        exps = store.list_experiments(idea["id"])
        for exp in exps:
            if exp.get("status") == "completed" and exp.get("metrics"):
                completed_exps.append({
                    **exp,
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
                        "metrics": progress,
                        "_running": True,
                        "idea_description": idea["description"],
                        "idea_status": idea["status"],
                    })
    return {"experiments": completed_exps, "running": running_progress}


# --- SPA Fallback (must be last) ---
# Serves the Preact app for any non-API path (enables client-side routing).
# Falls back to legacy dashboard.html if Vite build output doesn't exist.

@app.get("/{path:path}", response_class=HTMLResponse, include_in_schema=False)
def spa_fallback(path: str):
    if path.startswith("api/"):
        raise HTTPException(404)
    if _SPA_HTML:
        return _SPA_HTML
    return _load_dashboard()
