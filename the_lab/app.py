"""The Lab — Experiment Management API."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

from .git_ops import (
    GitError,
    create_branch_from,
    create_branch_from_merge,
    get_default_branch,
    get_worktree_path,
)
from .store import Store
from .runner import ExperimentRunner

# --- Configuration ---
REPO_DIR = Path(os.environ.get("THE_LAB_REPO", os.getcwd())).resolve()

store = Store(REPO_DIR)
app = FastAPI(title="The Lab", version="0.1.0")
runner = ExperimentRunner(store)


# --- Request schemas ---

class NewIdeaRequest(BaseModel):
    parent_ids: list[int] = []
    description: str


class NewExperimentRequest(BaseModel):
    description: str
    meta: dict | None = None


class NoteRequest(BaseModel):
    text: str
    level: Literal["insight", "milestone", "observation", "debug"] = "observation"


class ConcludeRequest(BaseModel):
    conclusion: str


class AbandonRequest(BaseModel):
    reason: str


class ReopenRequest(BaseModel):
    reason: str


# --- Ideas ---

@app.post("/api/v1/ideas/new", status_code=201)
def create_idea(req: NewIdeaRequest):
    for pid in req.parent_ids:
        if not store.get_idea(pid):
            raise HTTPException(404, f"parent idea {pid} not found")

    idea = store.create_idea(req.description, req.parent_ids, branch="")
    idea_id = idea["id"]
    branch_name = f"idea/{idea_id}"
    idea["branch"] = branch_name

    try:
        if len(req.parent_ids) == 0:
            base = get_default_branch(cwd=REPO_DIR)
            create_branch_from(branch_name, base, cwd=REPO_DIR)
        elif len(req.parent_ids) == 1:
            parent = store.get_idea(req.parent_ids[0])
            create_branch_from(branch_name, parent["branch"], cwd=REPO_DIR)
        else:
            parent_branches = [store.get_idea(pid)["branch"] for pid in req.parent_ids]
            conflicts = create_branch_from_merge(branch_name, parent_branches, cwd=REPO_DIR)
            if conflicts is not None:
                return {"status": "conflict", "conflicts": conflicts}

        get_worktree_path(idea_id, cwd=REPO_DIR)
        store.save_idea(idea)
        return idea
    except GitError as e:
        raise HTTPException(500, str(e))


@app.get("/api/v1/ideas")
def list_ideas(status: str | None = None):
    ideas = store.list_ideas(status=status)
    # Attach insight + milestone notes to each idea
    for idea in ideas:
        idea["notes"] = store.get_notes(idea["id"], levels=Store.LISTING_LEVELS)
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

    # Walk ancestors
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

    # Walk descendants
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
    idea = store.update_idea(idea_id, status="concluded", conclusion=req.conclusion)
    return idea


@app.post("/api/v1/ideas/{idea_id}/abandon")
def abandon_idea(idea_id: int, req: AbandonRequest):
    idea = store.get_idea(idea_id)
    if not idea:
        raise HTTPException(404, "idea not found")
    if idea["status"] != "active":
        raise HTTPException(400, f"idea is {idea['status']}, cannot abandon")
    idea = store.update_idea(idea_id, status="abandoned", conclusion=req.reason)
    return idea


@app.post("/api/v1/ideas/{idea_id}/reopen")
def reopen_idea(idea_id: int, req: ReopenRequest):
    idea = store.get_idea(idea_id)
    if not idea:
        raise HTTPException(404, "idea not found")
    if idea["status"] not in ("concluded", "abandoned"):
        raise HTTPException(400, f"idea is {idea['status']}, cannot reopen")

    # Preserve old conclusion as an insight note
    old_status = idea["status"]
    old_conclusion = idea.get("conclusion")
    if old_conclusion:
        store.add_note(
            idea_id,
            f"[reopened from {old_status}] previous conclusion: {old_conclusion}",
            level="insight",
        )

    store.add_note(idea_id, f"reopened: {req.reason}", level="milestone")
    idea = store.update_idea(idea_id, status="active", conclusion=None)
    return idea


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
    return store.create_experiment(idea_id, req.description, meta=req.meta)


@app.get("/api/v1/ideas/{idea_id}/experiments")
def list_experiments(idea_id: int):
    return store.list_experiments(idea_id)


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
    return result


@app.post("/api/v1/experiments/{exp_id}/cancel")
async def cancel_experiment(exp_id: int):
    result = await runner.cancel(exp_id)
    if result is None:
        raise HTTPException(404, "experiment not found")
    return result


# --- Wait ---

@app.get("/api/v1/wait")
async def wait_for_experiment(timeout: float = Query(default=3600, le=86400)):
    return await runner.wait_any(timeout=timeout)


# --- Overview ---

@app.get("/api/v1/backlog")
def get_backlog():
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
    return {"active_ideas": result, "total_running": total_running, "total_pending": total_pending}


@app.get("/api/v1/graph")
def get_graph():
    ideas = store.list_ideas()
    nodes = [{"id": i["id"], "description": i["description"], "status": i["status"]} for i in ideas]
    edges = []
    for idea in ideas:
        for pid in idea.get("parent_ids", []):
            edges.append({"from": pid, "to": idea["id"]})
    return {"nodes": nodes, "edges": edges}
