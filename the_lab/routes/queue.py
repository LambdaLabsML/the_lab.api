"""Queue + resources management endpoints."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query

from .. import queue as queue_mod
from ..deps import REPO_DIR, runner, store
from ..schemas import PriorityRequest, QueueConfigRequest, ResourceRequest

router = APIRouter()


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------

@router.get("/api/v1/queue")
def get_queue(
    history: int = Query(
        default=20,
        ge=0,
        le=200,
        description="How many recently-finished experiments to include (newest first). "
                    "0 disables the history section.",
    ),
):
    """Inspect current queue state: queued + running experiments + recent history.

    Useful for the dashboard's queue pane and for agents that want to know
    "how busy is the lab right now". Returns:
      - ``queued``: experiments waiting for a free slot, in dispatch order.
      - ``running``: currently running experiments with their resource assignment.
      - ``recent``: most recently finished experiments (completed / failed /
        cancelled), newest first. Capped by the ``history`` query param.
      - ``resources``: per-resource utilization (capacity, in_use, free, holders).
      - ``config``: paused, dispatch_interval_s.
    """
    resources, qc = queue_mod.load_config(REPO_DIR)
    all_exps = store.list_all_experiments()

    def _qmeta(e: dict) -> dict:
        return e.get("meta") or {}

    def _slim(e: dict) -> dict:
        qm = _qmeta(e)
        return {
            "id": e.get("id"),
            "label": e.get("label"),
            "idea_id": e.get("idea_id"),
            "description": e.get("description"),
            "status": e.get("status"),
            "priority": int(qm.get("priority", 0) or 0),
            "requirements": qm.get("requirements") or {},
            "depends_on": list(qm.get("depends_on") or []),
            "created_at": e.get("created_at"),
            "started_at": e.get("started_at"),
            "queued_at": e.get("queued_at"),
            "finished_at": e.get("finished_at"),
            "tags": e.get("tags", []),
            "assigned_resource": qm.get("assigned_resource"),
            "assigned_units": qm.get("assigned_units"),
            # Surface error + a one-line metric summary so the history view
            # is useful at a glance without a per-row /experiments call.
            "error": e.get("error"),
            "metrics": e.get("metrics"),
        }

    queued = [_slim(e) for e in all_exps if e.get("status") in ("queued", "pending")]
    queued.sort(key=lambda e: (-(e["priority"]), e["created_at"] or ""))

    running = [_slim(e) for e in all_exps if e.get("status") == "running"]
    running.sort(key=lambda e: e["started_at"] or "")

    recent: list[dict] = []
    if history > 0:
        finished = [
            _slim(e) for e in all_exps
            if e.get("status") in ("completed", "failed", "cancelled")
        ]
        # Newest first; fall back to created_at when finished_at is missing
        # (e.g. legacy cancelled rows).
        finished.sort(
            key=lambda e: e.get("finished_at") or e.get("created_at") or "",
            reverse=True,
        )
        recent = finished[:history]

    return {
        "queued": queued,
        "running": running,
        "recent": recent,
        "resources": [runner._allocator.utilization(r) for r in resources],
        "config": asdict(qc),
    }


@router.get("/api/v1/queue/config")
def get_queue_config():
    """Read queue knobs: paused flag and dispatch interval."""
    _, qc = queue_mod.load_config(REPO_DIR)
    return asdict(qc)


@router.put("/api/v1/queue/config")
def put_queue_config(req: QueueConfigRequest):
    """Update queue knobs. Either field may be omitted to leave it unchanged."""
    resources, qc = queue_mod.load_config(REPO_DIR)
    if req.paused is not None:
        qc.paused = bool(req.paused)
    if req.dispatch_interval_s is not None:
        if req.dispatch_interval_s <= 0:
            raise HTTPException(400, "dispatch_interval_s must be > 0")
        qc.dispatch_interval_s = float(req.dispatch_interval_s)
    queue_mod.save_config(REPO_DIR, resources, qc)
    runner.wake_scheduler()
    return asdict(qc)


@router.post("/api/v1/queue/pause")
def pause_queue():
    """Pause new starts. Already-running experiments are unaffected."""
    resources, qc = queue_mod.load_config(REPO_DIR)
    qc.paused = True
    queue_mod.save_config(REPO_DIR, resources, qc)
    return asdict(qc)


@router.post("/api/v1/queue/resume")
def resume_queue():
    """Resume the queue and immediately attempt to dispatch."""
    resources, qc = queue_mod.load_config(REPO_DIR)
    qc.paused = False
    queue_mod.save_config(REPO_DIR, resources, qc)
    runner.wake_scheduler()
    return asdict(qc)


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------

@router.get("/api/v1/resources")
def list_resources():
    """List all configured resources with derived caps + live utilization."""
    resources, _ = queue_mod.load_config(REPO_DIR)
    return [
        {**r.to_dict(), "utilization": runner._allocator.utilization(r)}
        for r in resources
    ]


@router.get("/api/v1/resources/{name}")
def get_resource(name: str):
    """Get one resource's config + derived caps + utilization."""
    resources, _ = queue_mod.load_config(REPO_DIR)
    res = next((r for r in resources if r.name == name), None)
    if res is None:
        raise HTTPException(404, f"resource '{name}' not found")
    return {**res.to_dict(), "utilization": runner._allocator.utilization(res)}


@router.put("/api/v1/resources/{name}")
def put_resource(name: str, req: ResourceRequest):
    """Create or replace a resource. The URL ``name`` overrides the body name."""
    payload = req.model_dump()
    payload["name"] = name
    try:
        resource = queue_mod.Resource(**payload)
        queue_mod.upsert_resource(REPO_DIR, resource)
    except (ValueError, TypeError) as e:
        raise HTTPException(400, str(e))
    runner.wake_scheduler()
    return resource.to_dict()


@router.delete("/api/v1/resources/{name}")
def delete_resource(name: str):
    """Remove a resource. Won't stop already-running experiments holding it."""
    if not queue_mod.remove_resource(REPO_DIR, name):
        raise HTTPException(404, f"resource '{name}' not found")
    runner.wake_scheduler()
    return {"status": "deleted", "name": name}


# ---------------------------------------------------------------------------
# Per-experiment priority
# ---------------------------------------------------------------------------

@router.post("/api/v1/experiments/{exp_ref}/priority")
def set_priority(exp_ref: str, req: PriorityRequest):
    """Bump or demote a queued experiment's priority. Higher = runs first.

    Has no effect on already-running experiments.
    """
    exp = store.get_experiment(exp_ref)
    if not exp:
        raise HTTPException(404, "experiment not found")
    new_meta = {**(exp.get("meta") or {}), "priority": int(req.priority)}
    label = exp.get("label") or str(exp["id"])
    updated = store.update_experiment(label, meta=new_meta)
    runner.wake_scheduler()
    return updated
