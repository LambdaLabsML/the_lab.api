"""Per-agent worktrees + registry.

When ``the-lab-agent`` starts in isolated mode it calls
``register_agent()`` to get its own git worktree, agent id, and branch.
Each agent's git operations (idea creation, adopt, etc.) are routed to
that worktree by the X-Agent-Id middleware. The registry persists at
``.the_lab/agents/registry.json``.

Layout::

    .the_lab/agents/
        registry.json                 -- {agent_id: {role, branch, pid, ...}}
        <agent_id>/                   -- the worktree (git worktree add ...)
            .the_lab.agentid          -- 5-char hex, gitignored
            .claude -> ../../../.claude
            .mcp.json -> ../../../.mcp.json
            (project source files at the chosen branch)
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import git_ops

_ID_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"
AGENTID_FILE = ".the_lab.agentid"

# An agent is "listening" if it polled its own inbox (the-lab messages, which
# hits GET /messages?for_me=1) within this many seconds. The CLI default poll
# interval is ~3s, so this tolerates a few missed polls / a slower --poll.
LISTENING_WINDOW_SEC = 20
_listening_lock = threading.Lock()


def _agents_dir(repo_dir: Path) -> Path:
    p = repo_dir / ".the_lab" / "agents"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _registry_path(repo_dir: Path) -> Path:
    return _agents_dir(repo_dir) / "registry.json"


def _read_registry(repo_dir: Path) -> dict:
    path = _registry_path(repo_dir)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _write_registry(repo_dir: Path, data: dict) -> None:
    _registry_path(repo_dir).write_text(json.dumps(data, indent=2) + "\n")


def _generate_agent_id(existing: set[str]) -> str:
    """5-char lowercase-alphanumeric id, retry-on-collision."""
    for _ in range(64):
        candidate = "".join(secrets.choice(_ID_ALPHABET) for _ in range(5))
        if candidate not in existing:
            return candidate
    raise RuntimeError("could not allocate a unique 5-char agent id")


def _recent_active_idea_branch(store, default: str = "main") -> str:
    """Return the most-recent-by-created_at ``idea/N`` branch with status=active.

    Falls back to *default* (the repo's main branch) if no active ideas exist
    or the store hasn't loaded any ideas yet.
    """
    try:
        active = [i for i in store.list_ideas() if i.get("status") == "active"]
    except Exception:
        return default
    if not active:
        return default
    active.sort(key=lambda i: i.get("created_at") or "", reverse=True)
    branch = active[0].get("branch")
    return branch or default


def _ensure_symlink(link: Path, target: Path) -> None:
    """Create a symlink at *link* pointing at *target*. Idempotent."""
    if link.is_symlink() or link.exists():
        return
    try:
        link.symlink_to(target)
    except OSError:
        pass  # filesystem doesn't support symlinks; not fatal


def register_agent(
    repo_dir: Path,
    store,
    role: str | None = None,
    pid: int | None = None,
) -> dict:
    """Allocate an id, create the agent's worktree, return registry entry."""
    repo_dir = Path(repo_dir).resolve()
    registry = _read_registry(repo_dir)
    agent_id = _generate_agent_id(set(registry.keys()))

    parent_branch = _recent_active_idea_branch(
        store, default=git_ops.get_default_branch(cwd=repo_dir),
    )
    agent_branch = f"agent_init_{agent_id}"
    git_ops.create_branch_from(agent_branch, parent_branch, cwd=repo_dir)

    worktree = _agents_dir(repo_dir) / agent_id
    git_ops._run(
        ["worktree", "add", str(worktree), agent_branch],
        cwd=repo_dir,
    )

    # Symlinks — let the agent's tooling find configs without absolute paths.
    _ensure_symlink(worktree / ".claude", Path("../../../.claude"))
    _ensure_symlink(worktree / ".mcp.json", Path("../../../.mcp.json"))

    # The id file is gitignored (see init); useful for scripts running inside
    # the worktree that need to look up the agent's id without an env var.
    (worktree / AGENTID_FILE).write_text(agent_id + "\n")

    entry = {
        "agent_id": agent_id,
        "role": role or "default",
        "branch": agent_branch,
        "parent_branch": parent_branch,
        "worktree": str(worktree),
        "pid": pid,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    registry[agent_id] = entry
    _write_registry(repo_dir, registry)
    return entry


def lookup_agent(repo_dir: Path, agent_id: str) -> dict | None:
    return _read_registry(Path(repo_dir).resolve()).get(agent_id)


def list_agents(repo_dir: Path) -> list[dict]:
    return list(_read_registry(Path(repo_dir).resolve()).values())


# ── "Actively listening" tracking ───────────────────────────────────────────
# Recorded in a separate file from the registry so the high-frequency poll
# writes never race with register/unregister.

def _listening_path(repo_dir: Path) -> Path:
    return _agents_dir(repo_dir) / "listening.json"


def note_message_poll(repo_dir: Path, agent_id: str) -> None:
    """Record that *agent_id* just polled its own inbox (the-lab messages loop)."""
    if not agent_id:
        return
    path = _listening_path(Path(repo_dir).resolve())
    with _listening_lock:
        try:
            data = json.loads(path.read_text()) if path.exists() else {}
        except (json.JSONDecodeError, OSError):
            data = {}
        data[agent_id] = datetime.now(timezone.utc).isoformat()
        try:
            path.write_text(json.dumps(data, indent=2))
        except OSError:
            pass


def read_listening(repo_dir: Path) -> dict:
    """Map of agent_id -> last inbox-poll ISO timestamp."""
    path = _listening_path(Path(repo_dir).resolve())
    try:
        return json.loads(path.read_text()) if path.exists() else {}
    except (json.JSONDecodeError, OSError):
        return {}


def is_listening(last_poll_iso: str | None, window_sec: int = LISTENING_WINDOW_SEC) -> bool:
    """True if a recorded poll timestamp falls within the listening window."""
    if not last_poll_iso:
        return False
    try:
        t = datetime.fromisoformat(last_poll_iso)
    except (ValueError, TypeError):
        return False
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - t).total_seconds() <= window_sec


_HISTORY_FILE = "history.json"


def _history_path(repo_dir: Path) -> Path:
    return _agents_dir(repo_dir) / _HISTORY_FILE


def record_completed_agent(repo_dir: Path, entry: dict, completed_at: str | None = None) -> None:
    """Append a completed agent entry to the history log."""
    hist_path = _history_path(repo_dir)
    try:
        history = json.loads(hist_path.read_text()) if hist_path.exists() else []
    except Exception:
        history = []
    record = dict(entry)
    record["completed_at"] = completed_at or datetime.now(timezone.utc).isoformat()
    # Keep last 200 entries
    history = [record] + history
    history = history[:200]
    hist_path.write_text(json.dumps(history, indent=2))


def list_past_agents(repo_dir: Path) -> list[dict]:
    """Return the history of completed agents, newest first."""
    hist_path = _history_path(repo_dir)
    if not hist_path.exists():
        return []
    try:
        return json.loads(hist_path.read_text())
    except Exception:
        return []


def unregister_agent(repo_dir: Path, agent_id: str, *, keep_branch: bool = True) -> bool:
    repo_dir = Path(repo_dir).resolve()
    registry = _read_registry(repo_dir)
    entry = registry.pop(agent_id, None)
    if entry is None:
        return False
    record_completed_agent(repo_dir, entry)
    worktree = Path(entry["worktree"])
    git_ops.remove_worktree(worktree, cwd=repo_dir)
    git_ops.prune_worktrees(cwd=repo_dir)
    if not keep_branch:
        try:
            git_ops._run(["branch", "-D", entry["branch"]], cwd=repo_dir, check=False)
        except Exception:
            pass
    _write_registry(repo_dir, registry)
    return True


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # PID exists but we can't signal it — count it as alive.
        return True
    except OSError:
        return False
    return True


def prune_dead_agents(repo_dir: Path) -> list[str]:
    """Remove registry entries whose PIDs are gone. Returns ids removed."""
    repo_dir = Path(repo_dir).resolve()
    registry = _read_registry(repo_dir)
    removed: list[str] = []
    for agent_id, entry in list(registry.items()):
        pid = entry.get("pid")
        if pid is None:
            continue  # PID unknown — leave it alone
        if _pid_alive(int(pid)):
            continue
        try:
            git_ops.remove_worktree(Path(entry["worktree"]), cwd=repo_dir)
        except Exception:
            pass
        registry.pop(agent_id, None)
        removed.append(agent_id)
    if removed:
        git_ops.prune_worktrees(cwd=repo_dir)
        _write_registry(repo_dir, registry)
    return removed


def find_branch_holder(repo_dir: Path, branch: str) -> dict | None:
    """Return the registry entry of the agent currently holding *branch*.

    Branches in git can be checked out in only one worktree at a time. This
    helper scans ``git worktree list`` and matches the result against the
    agent registry. Returns ``{"agent_id": None, "worktree": <path>}`` if
    the holder is the main repo's working tree (not an agent), or ``None``
    when nothing has the branch checked out.
    """
    repo_dir = Path(repo_dir).resolve()
    target = f"refs/heads/{branch}"
    result = git_ops._run(
        ["worktree", "list", "--porcelain"], cwd=repo_dir, check=False,
    )
    if result.returncode != 0:
        return None
    current_wt: str | None = None
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            current_wt = line[len("worktree "):].strip()
        elif line.startswith("branch ") and line[len("branch "):].strip() == target:
            holder_path = current_wt or ""
            if not holder_path:
                continue
            for entry in list_agents(repo_dir):
                if str(Path(entry["worktree"]).resolve()) == str(Path(holder_path).resolve()):
                    return entry
            return {"agent_id": None, "worktree": holder_path, "branch": branch}
    return None


def get_cwd_for_request(repo_dir: Path, agent_id: str | None) -> Path:
    """Resolve the cwd a git-touching route should use.

    Returns the agent's worktree if registered, otherwise *repo_dir*.
    Intentionally does NOT raise — middleware handles the unknown-id case.
    """
    if not agent_id:
        return Path(repo_dir).resolve()
    entry = lookup_agent(repo_dir, agent_id)
    if not entry:
        return Path(repo_dir).resolve()
    wt = Path(entry["worktree"])
    return wt if wt.exists() else Path(repo_dir).resolve()
