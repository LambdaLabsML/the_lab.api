"""CRUD for per-agent worktrees / registry."""
from __future__ import annotations

import json
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import PlainTextResponse
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

    When no other agents are currently registered (i.e. this is the first
    agent of a new session), all accumulated messages from previous sessions
    are cleared so the new agent starts with a clean inbox.
    """
    # Clear stale messages when this is the first agent of a new session.
    if not agents_mod.list_agents(REPO_DIR):
        messages_mod.clear_all(REPO_DIR)
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


@router.get("/api/v1/agents/{agent_id}/output")
def get_agent_output(
    agent_id: str,
    tail: int = Query(default=0, description="Return only the last N lines (0 = all)"),
):
    """Return the timestamped output log for an agent session.

    Written by the-lab-agent while the agent is running. Each line is prefixed
    with a UTC timestamp so the caller can scroll back through the session.
    Returns plain text; 404 if no log exists yet.

    Example:
        GET /api/v1/agents/mkn23/output?tail=100
    """
    entry = agents_mod.lookup_agent(REPO_DIR, agent_id)
    if not entry:
        raise HTTPException(404, f"agent '{agent_id}' not registered")
    worktree = entry.get("worktree")
    if not worktree:
        raise HTTPException(404, "agent has no worktree")
    log_path = Path(worktree) / ".the_lab" / "agents" / agent_id / "output.log"
    if not log_path.exists():
        raise HTTPException(404, "output log not found — agent may not have started yet")
    text = log_path.read_text(errors="replace")
    if tail:
        lines = text.splitlines(keepends=True)
        text = "".join(lines[-tail:])
    return PlainTextResponse(text)


def _find_claude_history_dir(worktree: str, agent_id: str) -> Path | None:
    """Return the Claude Code history directory for an agent session.

    When the agent runs inside a bwrap sandbox, sandbox_guest.py writes the
    agent's $HOME path to ``<worktree>/.the_lab/agents/<id>/claude_home``.
    We use that to locate ``$HOME/.claude/projects/``.

    Fallback: if no breadcrumb exists (non-sandboxed agent), use the real
    ``~/.claude/projects/`` directory.
    """
    crumb = Path(worktree) / ".the_lab" / "agents" / agent_id / "claude_home"
    if crumb.exists():
        claude_home = Path(crumb.read_text().strip())
    else:
        claude_home = Path.home()
    projects_dir = claude_home / ".claude" / "projects"
    return projects_dir if projects_dir.exists() else None


def _parse_history(projects_dir: Path) -> dict:
    """Parse all JSONL session files under a Claude projects directory.

    Returns a summary dict with:
    - sessions: list of sessions, newest first, each with:
        - session_id, project_path, started_at, message_count,
          input_tokens, output_tokens, cache_read_tokens, cost_usd (estimated)
    - totals: aggregated token/cost across all sessions
    """
    sessions = []
    # Pricing as of Claude Sonnet 4.x (per 1M tokens, approximate)
    PRICE_INPUT = 3.0 / 1_000_000
    PRICE_OUTPUT = 15.0 / 1_000_000
    PRICE_CACHE_READ = 0.30 / 1_000_000

    for jsonl in sorted(projects_dir.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
        msgs = []
        try:
            for line in jsonl.read_text(errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    msgs.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        except Exception:
            continue

        if not msgs:
            continue

        input_tok = output_tok = cache_tok = 0
        started_at = None
        msg_count = 0

        for m in msgs:
            ts = m.get("timestamp") or m.get("created_at")
            if ts and not started_at:
                started_at = ts
            usage = m.get("usage") or (m.get("message", {}) or {}).get("usage") or {}
            input_tok += usage.get("input_tokens", 0)
            output_tok += usage.get("output_tokens", 0)
            cache_tok += usage.get("cache_read_input_tokens", 0)
            if m.get("role") in ("user", "assistant"):
                msg_count += 1

        cost = (
            input_tok * PRICE_INPUT
            + output_tok * PRICE_OUTPUT
            + cache_tok * PRICE_CACHE_READ
        )

        # Derive a human-readable project path from the hashed directory name
        project_hash = jsonl.parent.name
        sessions.append({
            "session_id": jsonl.stem,
            "project_hash": project_hash,
            "file": str(jsonl),
            "started_at": started_at,
            "message_count": msg_count,
            "input_tokens": input_tok,
            "output_tokens": output_tok,
            "cache_read_tokens": cache_tok,
            "cost_usd": round(cost, 4),
        })

    totals = {
        "sessions": len(sessions),
        "message_count": sum(s["message_count"] for s in sessions),
        "input_tokens": sum(s["input_tokens"] for s in sessions),
        "output_tokens": sum(s["output_tokens"] for s in sessions),
        "cache_read_tokens": sum(s["cache_read_tokens"] for s in sessions),
        "cost_usd": round(sum(s["cost_usd"] for s in sessions), 4),
    }
    return {"sessions": sessions, "totals": totals}


@router.get("/api/v1/agents/{agent_id}/history")
def get_agent_history(agent_id: str):
    """Return parsed Claude Code conversation history for an agent session.

    Reads JSONL files from the agent's Claude home directory (written by
    ``claude`` CLI during the session).  Returns session summaries with token
    counts and estimated cost.  404 if no history is available yet.
    """
    entry = agents_mod.lookup_agent(REPO_DIR, agent_id)
    if not entry:
        raise HTTPException(404, f"agent '{agent_id}' not registered")
    worktree = entry.get("worktree")
    if not worktree:
        raise HTTPException(404, "agent has no worktree")
    projects_dir = _find_claude_history_dir(worktree, agent_id)
    if not projects_dir:
        raise HTTPException(404, "no Claude history directory found — agent may not have started yet")
    return _parse_history(projects_dir)
