"""CRUD for per-agent worktrees / registry."""
from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import agents as agents_mod
from .. import messages as messages_mod
from ..deps import REPO_DIR, store
from ..git_ops import get_current_branch

router = APIRouter()


_IDEA_BRANCH_RE = re.compile(r"^idea/(\d+)$")


def _enrich(entry: dict) -> dict:
    """Augment a registry entry with live worktree state.

    Adds ``current_branch`` (the branch currently checked out in the agent's
    worktree) and, when that branch is ``idea/<N>``, a ``current_idea`` block
    summarising which idea the agent is working on. Best-effort — git or
    store errors degrade silently because the registry entry itself is
    still useful.
    """
    out = dict(entry)
    worktree = entry.get("worktree")
    try:
        if worktree and Path(worktree).exists():
            branch = get_current_branch(cwd=worktree)
            out["current_branch"] = branch
            m = _IDEA_BRANCH_RE.match(branch or "")
            if m:
                idea_id = int(m.group(1))
                idea = store.get_idea(idea_id)
                if idea:
                    out["current_idea"] = {
                        "id": idea["id"],
                        "description": idea.get("description"),
                        "status": idea.get("status"),
                    }
    except Exception:
        pass
    try:
        unread = messages_mod.unread_for(
            REPO_DIR, agent_id=entry.get("agent_id"), role=entry.get("role"),
        )
        out["unread_messages"] = len(unread)
    except Exception:
        out["unread_messages"] = 0
    return out


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
    """List all currently registered agents, each enriched with the branch /
    idea they're currently checked out on plus an unread-message count."""
    return [_enrich(e) for e in agents_mod.list_agents(REPO_DIR)]


@router.get("/api/v1/agents/{agent_id}")
def get_agent(agent_id: str):
    """Return a single agent's registry entry (enriched), or 404."""
    entry = agents_mod.lookup_agent(REPO_DIR, agent_id)
    if not entry:
        raise HTTPException(404, f"agent '{agent_id}' not registered")
    return _enrich(entry)


@router.delete("/api/v1/agents/{agent_id}")
def unregister_agent(agent_id: str, keep_branch: bool = True):
    """Remove an agent's worktree (and optionally its branch)."""
    if not agents_mod.unregister_agent(REPO_DIR, agent_id, keep_branch=keep_branch):
        raise HTTPException(404, f"agent '{agent_id}' not registered")
    return {"status": "unregistered", "agent_id": agent_id}
