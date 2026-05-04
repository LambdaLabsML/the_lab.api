"""CRUD for per-agent worktrees / registry."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import agents as agents_mod
from ..deps import REPO_DIR, store

router = APIRouter()


class RegisterAgentRequest(BaseModel):
    role: str | None = None
    pid: int | None = None


@router.post("/api/v1/agents/register")
def register_agent(req: RegisterAgentRequest):
    """Allocate a 5-char agent id, create a per-agent git worktree.

    Branches a new ``agent_init_<id>`` from the most recent active idea
    branch (or main if none), and registers a worktree at
    ``.the_lab/agents/<id>/``. Returns ``{agent_id, worktree, branch,
    parent_branch, role}`` so the caller can ``cd`` into the worktree
    and pass the id back via the ``X-Agent-Id`` header.

    The agent is expected to call ``DELETE /api/v1/agents/{id}`` on
    exit. If it crashes, ``prune_dead_agents`` removes the entry on
    the next API server restart (when a PID is recorded).
    """
    try:
        return agents_mod.register_agent(REPO_DIR, store, role=req.role, pid=req.pid)
    except Exception as e:
        raise HTTPException(500, f"failed to register agent: {e}")


@router.get("/api/v1/agents")
def list_registered_agents():
    """List all currently registered agents."""
    return agents_mod.list_agents(REPO_DIR)


@router.get("/api/v1/agents/{agent_id}")
def get_agent(agent_id: str):
    """Return a single agent's registry entry, or 404."""
    entry = agents_mod.lookup_agent(REPO_DIR, agent_id)
    if not entry:
        raise HTTPException(404, f"agent '{agent_id}' not registered")
    return entry


@router.delete("/api/v1/agents/{agent_id}")
def unregister_agent(agent_id: str, keep_branch: bool = True):
    """Remove an agent's worktree (and optionally its branch)."""
    if not agents_mod.unregister_agent(REPO_DIR, agent_id, keep_branch=keep_branch):
        raise HTTPException(404, f"agent '{agent_id}' not registered")
    return {"status": "unregistered", "agent_id": agent_id}
