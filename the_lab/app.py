"""The Lab — Experiment Management API."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from .git_ops import (
    GitError,
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

_DASHBOARD_HTML: str | None = None
_DEV_MODE = os.environ.get("THE_LAB_DEV") == "1"

def _load_dashboard() -> str:
    global _DASHBOARD_HTML
    if _DEV_MODE or _DASHBOARD_HTML is None:
        html_path = Path(__file__).parent / "dashboard.html"
        _DASHBOARD_HTML = html_path.read_text()
    return _DASHBOARD_HTML


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return _load_dashboard()


# --- Request schemas ---

class NewIdeaRequest(BaseModel):
    parent_ids: list[int] = []
    description: str


class NewExperimentRequest(BaseModel):
    description: str
    meta: dict | None = None
    script_content: str | None = None


class NoteRequest(BaseModel):
    text: str
    level: Literal["insight", "milestone", "observation", "debug"] = "observation"


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


def _wrap_script(content: str) -> str:
    """Inject a guard that prevents running outside the backend."""
    lines = content.split("\n", 1)
    if lines[0].startswith("#!"):
        return lines[0] + "\n" + SCRIPT_GUARD + (lines[1] if len(lines) > 1 else "")
    return "#!/bin/bash\n" + SCRIPT_GUARD + content


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
        return idea
    except GitError as e:
        raise HTTPException(500, str(e))


@app.post("/api/v1/ideas/{idea_id}/checkout")
def checkout_idea_endpoint(idea_id: int):
    """Auto-commit any uncommitted changes and checkout this idea's branch."""
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
def list_ideas(status: str | None = None):
    ideas = store.list_ideas(status=status)
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


@app.post("/api/v1/ideas/{idea_id}/conclude")
def conclude_idea(idea_id: int, req: ConcludeRequest):
    idea = store.get_idea(idea_id)
    if not idea:
        raise HTTPException(404, "idea not found")
    if idea["status"] != "active":
        raise HTTPException(400, f"idea is {idea['status']}, cannot conclude")
    return store.update_idea(idea_id, status="concluded", conclusion=req.conclusion)


@app.post("/api/v1/ideas/{idea_id}/abandon")
def abandon_idea(idea_id: int, req: AbandonRequest):
    idea = store.get_idea(idea_id)
    if not idea:
        raise HTTPException(404, "idea not found")
    if idea["status"] != "active":
        raise HTTPException(400, f"idea is {idea['status']}, cannot abandon")
    return store.update_idea(idea_id, status="abandoned", conclusion=req.reason)


@app.post("/api/v1/ideas/{idea_id}/reopen")
def reopen_idea(idea_id: int, req: ReopenRequest):
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
    idea = store.get_idea(idea_id)
    if not idea:
        raise HTTPException(404, "idea not found")
    return store.add_note(idea_id, req.text, level=req.level)


# --- Experiments ---

@app.post("/api/v1/ideas/{idea_id}/experiments", status_code=201)
def create_experiment(idea_id: int, req: NewExperimentRequest):
    idea = store.get_idea(idea_id)
    if not idea:
        raise HTTPException(404, "idea not found")
    if idea["status"] != "active":
        raise HTTPException(400, f"idea is {idea['status']}, cannot add experiments")
    exp = store.create_experiment(idea_id, req.description, meta=req.meta)

    if req.script_content is not None:
        script_path = REPO_DIR / exp["script"]
        script_path.parent.mkdir(parents=True, exist_ok=True)
        script_path.write_text(_wrap_script(req.script_content))
        os.chmod(script_path, 0o644)

    return exp


@app.get("/api/v1/ideas/{idea_id}/experiments")
def list_experiments(idea_id: int):
    return store.list_experiments(idea_id)


@app.get("/api/v1/experiments/compare")
def compare_experiments(
    ids: str = Query(..., description="Comma-separated experiment IDs"),
    metrics: str | None = Query(default=None, description="Comma-separated metric keys to include (default: all)"),
):
    """Side-by-side comparison of experiments: metrics, meta, and descriptions."""
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
    exp = store.get_experiment(exp_id)
    if not exp:
        raise HTTPException(404, "experiment not found")
    return exp


@app.post("/api/v1/experiments/{exp_id}/start")
async def start_experiment(exp_id: int):
    result = await runner.start(exp_id)
    if result["status"] == "error":
        raise HTTPException(400, result)
    # Add idea context + current branch
    exp = result.get("experiment", {})
    result["current_branch"] = get_current_branch(cwd=REPO_DIR)
    result.update(_idea_context(exp.get("idea_id")))
    return result


@app.post("/api/v1/experiments/{exp_id}/restart")
async def restart_experiment(exp_id: int):
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
    result = await runner.cancel(exp_id)
    if result is None:
        raise HTTPException(404, "experiment not found")
    return result


@app.get("/api/v1/experiments/{exp_id}/log")
def get_experiment_log(exp_id: int, tail: int | None = None):
    log = runner.get_log(exp_id, tail=tail)
    if log is None:
        raise HTTPException(404, "experiment not found")
    return {"log": log}


# --- Wait ---

@app.get("/api/v1/wait")
async def wait_for_experiment(
    timeout: float = Query(default=3600, le=86400),
    experiment_id: int | None = Query(default=None),
    idea_id: int | None = Query(default=None),
):
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
            "pending_experiments": pending,
            "running_experiments": running,
            "completed_experiments": completed,
            "failed_experiments": failed,
        })
    return {
        "current_branch": current,
        "active_ideas": result,
        "total_running": total_running,
        "total_pending": total_pending,
    }


@app.get("/api/v1/graph")
def get_graph():
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
