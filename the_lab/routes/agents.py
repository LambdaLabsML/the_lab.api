"""CRUD for per-agent worktrees / registry."""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from .. import agents as agents_mod
from .. import messages as messages_mod
from ..deps import REPO_DIR, store
from ..git_ops import get_current_branch

_costs_lock = threading.Lock()

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
    # Is the agent actively waiting on `the-lab messages` right now?
    try:
        lp = agents_mod.read_listening(REPO_DIR).get(entry.get("agent_id"))
        out["last_message_poll"] = lp
        out["listening"] = agents_mod.is_listening(lp)
    except Exception:
        out["listening"] = False
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
    try:
        return agents_mod.register_agent(REPO_DIR, store, role=req.role, pid=req.pid)
    except Exception as e:
        raise HTTPException(500, f"failed to register agent: {e}")


@router.get("/api/v1/agents")
def list_registered_agents():
    """List all currently registered agents, each enriched with the branch /
    idea they're currently checked out on plus an unread-message count."""
    return [_enrich(e) for e in agents_mod.list_agents(REPO_DIR)]


@router.get("/api/v1/agents/past")
def list_past_agents_endpoint():
    """Return the 200 most recently completed agents."""
    return agents_mod.list_past_agents(REPO_DIR)


@router.get("/api/v1/agents/costs")
def get_agent_costs():
    """Return per-agent cost + token data for all known agents.

    Cached in ``.the_lab/agent_costs.json``.

    Completed agents are computed once and stored as a flat record.
    Live agents accumulate a ``readings`` list — one entry appended per
    call — so callers can draw a cost-over-time chart as the agent runs.

    Response schema::

        {
          "<agent_id>": {
            // Completed agent — single record at completion time
            "cost": 1.234, "inTok": 120000, "outTok": 8000,
            "ts": "2025-05-28T12:34:56", "live": false
          },
          "<live_agent_id>": {
            // Live agent — time-series of cost snapshots
            "live": true,
            "ts": "2025-05-28T11:00:00",   // creation time (x-axis anchor)
            "readings": [
              {"ts": "2025-05-28T11:05:00", "cost": 0.50, "inTok": 40000, "outTok": 3000},
              {"ts": "2025-05-28T11:06:00", "cost": 1.10, "inTok": 80000, "outTok": 6000}
            ]
          }
        }
    """
    from datetime import datetime, timezone

    cache_path = REPO_DIR / ".the_lab" / "agent_costs.json"

    with _costs_lock:
        try:
            cache: dict = json.loads(cache_path.read_text()) if cache_path.exists() else {}
        except Exception:
            cache = {}

    live_agents = agents_mod.list_agents(REPO_DIR)
    past_agents = agents_mod.list_past_agents(REPO_DIR)
    live_ids = {a["agent_id"] for a in live_agents}

    to_compute: list[dict] = []
    for a in live_agents:
        to_compute.append({**a, "_live": True})
    for a in past_agents:
        aid = a["agent_id"]
        existing = cache.get(aid, {})
        # Re-compute if: never cached, or was previously live (now finalise it)
        if not existing or existing.get("live"):
            to_compute.append({**a, "_live": False})

    now_iso = datetime.now(timezone.utc).isoformat()
    changed = False

    for a in to_compute:
        aid = a["agent_id"]
        worktree = a.get("worktree", "")
        is_live = a["_live"]
        ts = a.get("completed_at") or a.get("created_at") or now_iso

        project_dir = _find_agent_project_dir(worktree) if worktree else None
        if not project_dir:
            continue

        exact_dir = Path.home() / ".claude" / "projects" / _worktree_project_dir(worktree)
        since: float | None = None
        if project_dir != exact_dir:
            if not is_live:
                continue  # shared fallback dir — skip past agents (ambiguous)
            created_at = a.get("created_at")
            if created_at:
                try:
                    since = datetime.fromisoformat(created_at).timestamp()
                except Exception:
                    pass

        try:
            data = _parse_jsonl_dir(project_dir, since=since)
            t = data["totals"]
            if not (t["cost_usd"] > 0 or t["input_tokens"] > 0):
                continue

            if is_live:
                # Append a new reading to the time-series
                existing = cache.get(aid, {})
                readings = existing.get("readings", [])
                readings.append({
                    "ts": now_iso,
                    "cost": t["cost_usd"],
                    "inTok": t["input_tokens"],
                    "outTok": t["output_tokens"],
                })
                readings = readings[-500:]  # cap history
                cache[aid] = {"live": True, "ts": ts, "readings": readings}
            else:
                # Completed — single flat record
                cache[aid] = {
                    "cost": t["cost_usd"],
                    "inTok": t["input_tokens"],
                    "outTok": t["output_tokens"],
                    "ts": ts,
                    "live": False,
                }
            changed = True
        except Exception:
            pass

    # Finalise any agent that was live but has now completed:
    # Convert its readings to a flat completed record using the last reading.
    for aid, entry in list(cache.items()):
        if entry.get("live") and aid not in live_ids:
            readings = entry.get("readings", [])
            last = readings[-1] if readings else {}
            cache[aid] = {
                "cost": last.get("cost", 0),
                "inTok": last.get("inTok", 0),
                "outTok": last.get("outTok", 0),
                "ts": entry.get("ts", now_iso),
                "live": False,
            }
            changed = True

    if changed:
        with _costs_lock:
            try:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(json.dumps(cache, indent=2))
            except Exception:
                pass

    return cache


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


def _worktree_project_dir(worktree: str) -> str:
    """Return the Claude Code project-directory name for a given worktree path.

    Claude Code names project subdirectories by replacing every non-alphanumeric
    character in the absolute path with ``-``.  Example::

        /lambda/nfs/.../agents/abc12
        → -lambda-nfs----agents-abc12
    """
    return re.sub(r"[^a-zA-Z0-9]", "-", worktree)


def _find_agent_project_dir(worktree: str) -> Path | None:
    """Return the specific Claude Code JSONL project directory for this worktree.

    JSONL files always land in the REAL ``~/.claude/projects/`` on the host,
    regardless of whether the agent is sandboxed (bwrap sets a different HOME
    but the host home is still writable inside the namespace).

    Tries multiple candidate paths in order:
    1. Exact worktree path hash (correct for agents launched with fixed code).
    2. Repo root hash — worktrees live at ``<repo>/.the_lab/agents/<id>``; older
       agents were launched with cwd=repo_root so their JSONL lands there.
    3. Brute-force scan: find the project dir whose most-recent JSONL was created
       closest to the agent's worktree mtime (last-resort fallback).
    """
    projects_root = Path.home() / ".claude" / "projects"
    if not projects_root.exists():
        return None

    # Candidate 1: exact worktree hash
    target = projects_root / _worktree_project_dir(worktree)
    if target.exists():
        return target

    # Candidate 2: repo root (strip /.the_lab/agents/<id> suffix if present)
    wt_path = Path(worktree)
    try:
        # Typical layout: <repo>/.the_lab/agents/<agent_id>
        # Walk up looking for a directory that has .git or .the_lab at root
        candidate = wt_path
        for _ in range(4):
            candidate = candidate.parent
            if (candidate / ".git").exists() or (candidate / ".the_lab").exists():
                repo_target = projects_root / _worktree_project_dir(str(candidate))
                if repo_target.exists():
                    return repo_target
                break
    except Exception:
        pass

    return None


def _parse_jsonl_dir(project_dir: Path, since: float | None = None) -> dict:
    """Parse all JSONL session files inside ONE Claude project directory.

    Returns a summary dict:
    - sessions: list, newest-first, each with session_id, started_at,
      message_count, input_tokens, output_tokens, cache_read_tokens,
      cache_creation_tokens, cost_usd
    - totals: aggregated across all sessions

    ``since`` — optional Unix timestamp; JSONL files modified before this
    time are skipped.  Used when multiple agents share the same project dir
    (repo-root fallback) to filter to only the current agent's sessions.
    """
    sessions = []

    # Claude Sonnet 4.x pricing per 1 M tokens (approximate)
    PRICE = {
        "input":            3.00 / 1_000_000,
        "output":          15.00 / 1_000_000,
        "cache_read":       0.30 / 1_000_000,
        "cache_creation":   3.75 / 1_000_000,
    }

    for jsonl in sorted(
        project_dir.glob("*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    ):
        # Skip files that predate this agent's registration (repo-root fallback)
        if since is not None and jsonl.stat().st_mtime < since:
            continue

        in_tok = out_tok = cache_read = cache_create = 0
        started_at = None
        msg_count = 0

        try:
            for line in jsonl.read_text(errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # Timestamp — use the first one we see
                ts = entry.get("timestamp") or entry.get("created_at")
                if ts and not started_at:
                    started_at = ts

                # Usage lives inside entry["message"]["usage"] for assistant turns
                msg = entry.get("message")
                usage = (
                    (msg.get("usage") if isinstance(msg, dict) else None)
                    or entry.get("usage")
                    or {}
                )
                in_tok      += usage.get("input_tokens", 0)
                out_tok     += usage.get("output_tokens", 0)
                cache_read  += usage.get("cache_read_input_tokens", 0)
                cache_create+= usage.get("cache_creation_input_tokens", 0)

                if entry.get("type") in ("user", "assistant"):
                    msg_count += 1
        except Exception:
            continue

        if not (in_tok or out_tok):
            continue  # empty / unstarted session

        cost = (
            in_tok       * PRICE["input"]
            + out_tok    * PRICE["output"]
            + cache_read * PRICE["cache_read"]
            + cache_create * PRICE["cache_creation"]
        )
        sessions.append({
            "session_id":          jsonl.stem,
            "started_at":          started_at,
            "message_count":       msg_count,
            "input_tokens":        in_tok,
            "output_tokens":       out_tok,
            "cache_read_tokens":   cache_read,
            "cache_creation_tokens": cache_create,
            "cost_usd":            round(cost, 4),
        })

    totals = {
        "sessions":              len(sessions),
        "message_count":         sum(s["message_count"]           for s in sessions),
        "input_tokens":          sum(s["input_tokens"]            for s in sessions),
        "output_tokens":         sum(s["output_tokens"]           for s in sessions),
        "cache_read_tokens":     sum(s["cache_read_tokens"]       for s in sessions),
        "cache_creation_tokens": sum(s["cache_creation_tokens"]   for s in sessions),
        "cost_usd":              round(sum(s["cost_usd"]          for s in sessions), 4),
    }
    return {"sessions": sessions, "totals": totals}


@router.get("/api/v1/agents/{agent_id}/history")
def get_agent_history(agent_id: str):
    """Return parsed Claude Code conversation history for an agent session.

    Works for both live agents (in the registry) and past agents (in
    history.json).  Locates JSONL files via the worktree path recorded at
    registration time.
    """
    # Try live registry first, then fall back to completed-agent history
    entry = agents_mod.lookup_agent(REPO_DIR, agent_id)
    if not entry:
        past = agents_mod.list_past_agents(REPO_DIR)
        entry = next((a for a in past if a.get("agent_id") == agent_id), None)
    if not entry:
        raise HTTPException(404, f"agent '{agent_id}' not found in registry or history")

    worktree = entry.get("worktree")
    if not worktree:
        raise HTTPException(404, "agent has no worktree recorded")

    # Determine whether the project dir is the exact worktree hash (ideal) or a
    # fallback repo-root dir shared by many agents.
    #
    # Exact match  → always usable; reliable per-agent attribution.
    # Fallback dir → shared by many agents (old agents run with cwd=repo_root).
    #   • Live agent  : best-effort with a ``since`` cutoff (single session active,
    #                   so the latest JSONL is almost certainly ours).
    #   • Past agent  : unreliable — multiple past agents share the same dir and
    #                   were often launched in parallel, so ``since`` can't
    #                   separate them.  Return 404 rather than wrong shared data.
    exact_dir = Path.home() / ".claude" / "projects" / _worktree_project_dir(worktree)
    project_dir = _find_agent_project_dir(worktree)
    if not project_dir:
        raise HTTPException(
            404,
            f"no Claude history found for this agent — "
            f"expected ~/.claude/projects/{_worktree_project_dir(worktree)}/",
        )

    is_live = agents_mod.lookup_agent(REPO_DIR, agent_id) is not None
    is_fallback = project_dir != exact_dir

    if is_fallback and not is_live:
        # Past agent using a shared fallback dir — attribution is ambiguous.
        raise HTTPException(
            404,
            f"no per-agent Claude history found for past agent '{agent_id}' "
            f"(shared project dir cannot be attributed reliably)",
        )

    since: float | None = None
    if is_fallback:
        # Live agent in fallback dir — filter by agent creation time (best-effort)
        created_at = entry.get("created_at")
        if created_at:
            try:
                from datetime import datetime
                dt = datetime.fromisoformat(created_at)
                since = dt.timestamp()
            except Exception:
                pass

    return _parse_jsonl_dir(project_dir, since=since)
