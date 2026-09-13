"""Per-agent worktrees + registry.

When ``the-lab-agent`` starts in isolated mode it calls
``register_agent()`` to get its own git worktree, agent id, and branch.
Each agent's git operations (idea creation, adopt, etc.) are routed to
that worktree by the X-Agent-Id middleware. The registry persists at
``.the_lab/agents/registry.json``.

Layout::

    .the_lab/agents/
        registry.json                 -- {agent_id: {role, branch, pid, ...}}
        archive/<agent_id>/           -- durable data kept after worktree removal
        <agent_id>/                   -- the worktree (git worktree add ...)
            .the_lab.agentid          -- 5-char id, locally untracked
            .the_lab.link -> ../../../.the_lab   -- read main-checkout logs
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
from . import jsonio

_ID_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"
AGENTID_FILE = ".the_lab.agentid"

# An agent is "listening" if it polled its own inbox (the-lab messages) within
# this many seconds. Warm sources: the ~3s HTTP poll loop, or the messages
# WebSocket, whose keep-alive re-warms only every 30s — so the window must sit
# comfortably ABOVE that ping cadence (2 pings + slack) or a WS-connected
# agent's flag oscillates 20s-on/10s-off and the dashboard row flickers.
LISTENING_WINDOW_SEC = 75
_listening_lock = threading.Lock()


def _agents_dir(repo_dir: Path) -> Path:
    p = repo_dir / ".the_lab" / "agents"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _registry_path(repo_dir: Path) -> Path:
    return _agents_dir(repo_dir) / "registry.json"


# In-memory single-slot caches (one repo per process). The notifications
# middleware consults the registry, listening map, and history on every
# dict-shaped API response — serving them from memory removes several NFS
# reads per response. Disk stays the durable form (written atomically via
# jsonio); memory is authoritative between writes.
_registry_lock = threading.Lock()
_registry_cache: dict | None = None
_registry_cache_path: Path | None = None


def _read_registry(repo_dir: Path) -> dict:
    global _registry_cache, _registry_cache_path
    path = _registry_path(repo_dir)
    with _registry_lock:
        if _registry_cache is None or _registry_cache_path != path:
            try:
                loaded = json.loads(path.read_text()) if path.exists() else {}
            except (json.JSONDecodeError, OSError):
                loaded = {}
            _registry_cache = loaded
            _registry_cache_path = path
        return _registry_cache


def _write_registry(repo_dir: Path, data: dict) -> None:
    global _registry_cache, _registry_cache_path
    with _registry_lock:
        _registry_cache = data
        _registry_cache_path = _registry_path(repo_dir)
    jsonio.write_json(_registry_path(repo_dir), data)


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


# Worktree-local scaffolding that must never participate in git tracking/merges.
_WORKTREE_LOCAL_PATHS = (AGENTID_FILE, ".the_lab.link", ".claude", ".mcp.json")


def _make_worktree_local_untracked(worktree: Path) -> None:
    """Ensure our per-worktree scaffolding is git-ignored *locally* and untracked.

    Feedback (.the_lab.agentid UU CONFLICT): AGENTID_FILE is committed/tracked in
    the shared tree, so agents sharing lineage hit a guaranteed UU conflict on
    every create_idea auto-checkout. We fix it per-worktree (no repo commit):

      1. Append the scaffolding names to this worktree's own exclude file so a
         fresh copy is never re-staged. A worktree has its *own* gitdir, so we
         resolve the real path via ``git rev-parse --git-path info/exclude``
         rather than assuming ``.git/info/exclude``.
      2. If any name is already tracked, ``git rm --cached`` it in this worktree
         so it stops causing merge conflicts (leaves the file on disk).

    Fail-soft: any git/IO error is swallowed — this is best-effort hardening.
    """
    try:
        result = git_ops._run(
            ["rev-parse", "--git-path", "info/exclude"], cwd=worktree, check=False,
        )
        exclude_rel = result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        exclude_rel = ""
    if exclude_rel:
        try:
            exclude_path = Path(exclude_rel)
            if not exclude_path.is_absolute():
                exclude_path = worktree / exclude_path
            exclude_path.parent.mkdir(parents=True, exist_ok=True)
            existing = exclude_path.read_text() if exclude_path.exists() else ""
            have = set(existing.splitlines())
            additions = [p for p in _WORKTREE_LOCAL_PATHS if p not in have]
            if additions:
                prefix = "" if (not existing or existing.endswith("\n")) else "\n"
                with exclude_path.open("a") as fh:
                    fh.write(prefix + "\n".join(additions) + "\n")
        except OSError:
            pass

    # Drop any already-tracked copies from this worktree's index so shared
    # lineage stops conflicting. --cached keeps the on-disk file intact.
    for name in _WORKTREE_LOCAL_PATHS:
        try:
            git_ops._run(
                ["rm", "--cached", "--quiet", "--ignore-unmatch", name],
                cwd=worktree, check=False,
            )
        except Exception:
            pass


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
    # Feedback (WORKTREE .the_lab LINK): a worktree is a checkout of an idea
    # branch, so it can't see experiment logs / world-dumps that live only in
    # the main checkout's .the_lab/. Link to it under a distinct name so tooling
    # run inside the worktree can read them without clobbering a worktree-local
    # .the_lab if the branch happens to commit one.
    _ensure_symlink(worktree / ".the_lab.link", Path("../../../.the_lab"))

    # The id file is gitignored (see below); useful for scripts running inside
    # the worktree that need to look up the agent's id without an env var.
    (worktree / AGENTID_FILE).write_text(agent_id + "\n")

    # Feedback (.the_lab.agentid UU CONFLICT): AGENTID_FILE is tracked in the
    # committed tree, so sharing lineage across agents guarantees a UU conflict
    # on every create_idea auto-checkout. Make our worktree-local scaffolding
    # untracked *regardless of the repo's committed state*: add it to this
    # worktree's own git exclude file, and drop it from the index if tracked.
    _make_worktree_local_untracked(worktree)

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

# Listening state is purely in-memory: it is pure recency data (an agent
# counts as listening only if it polled within LISTENING_WINDOW_SEC), so
# after a restart the map rebuilds itself within seconds from the poll/WS
# traffic. The old listening.json write-per-poll was a full NFS write every
# ~3s per listening agent for data that expires in 75s.
_listening_state: dict[str, str] = {}


def note_message_poll(repo_dir: Path, agent_id: str) -> None:
    """Record that *agent_id* just polled its own inbox (the-lab messages loop).

    Emits an ``agent_changed`` event only on the silence→listening
    transition, so WS clients can flip the listening dot without polling
    the roster; steady-state polls stay silent.
    """
    if not agent_id:
        return
    now_iso = datetime.now(timezone.utc).isoformat()
    with _listening_lock:
        prev = _listening_state.get(agent_id)
        _listening_state[agent_id] = now_iso
    if prev is None or not is_listening(prev):
        try:
            from . import ws as ws_mod
            ws_mod.broadcaster.broadcast_soon({
                "type": "agent_changed",
                "agent_id": agent_id,
                "change": "listening",
                "last_message_poll": now_iso,
            })
        except Exception:
            pass


def read_listening(repo_dir: Path) -> dict:
    """Map of agent_id -> last inbox-poll ISO timestamp."""
    with _listening_lock:
        return dict(_listening_state)


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


_history_lock = threading.Lock()
_history_cache: list | None = None


def _load_history(repo_dir: Path) -> list:
    global _history_cache
    if _history_cache is None:
        hist_path = _history_path(repo_dir)
        try:
            _history_cache = json.loads(hist_path.read_text()) if hist_path.exists() else []
        except Exception:
            _history_cache = []
    return _history_cache


def record_completed_agent(repo_dir: Path, entry: dict, completed_at: str | None = None) -> None:
    """Append a completed agent entry to the history log."""
    global _history_cache
    record = dict(entry)
    record["completed_at"] = completed_at or datetime.now(timezone.utc).isoformat()
    with _history_lock:
        history = [record] + _load_history(repo_dir)
        history = history[:200]  # keep last 200 entries
        _history_cache = history
        jsonio.write_json(_history_path(repo_dir), history)


def list_past_agents(repo_dir: Path) -> list[dict]:
    """Return the history of completed agents, newest first."""
    with _history_lock:
        return list(_load_history(repo_dir))


def _claude_projects_dir_for(worktree: Path) -> Path:
    """Path to the agent's Claude session-history dir under ~/.claude/projects.

    Claude Code names the project dir by replacing every non-alphanumeric char
    in the absolute worktree path with '-' — the same scheme agent_cli.py uses
    to move sessions on resume. Kept in sync with that logic.
    """
    import re
    name = re.sub(r"[^a-zA-Z0-9]", "-", str(Path(worktree).resolve()))
    return Path.home() / ".claude" / "projects" / name


def _archive_agent_data(repo_dir: Path, agent_id: str, entry: dict) -> None:
    """Archive an agent's durable data before its worktree is deleted.

    Feedback (PRESERVE FILES ON CLOSE): removing the worktree orphaned the
    agent's Claude session history (~/.claude/projects/<hash>/*.jsonl, keyed off
    the worktree path) and any agent-specific lab stats in the worktree's
    .the_lab/. We keep the *data* — not the whole worktree checkout — under
    ``.the_lab/agents/archive/<agent_id>/`` so it survives cleanup.

    Fail-soft throughout: archiving must never block unregistration.
    """
    import shutil
    try:
        worktree = Path(entry.get("worktree", "")).resolve()
        archive_dir = _agents_dir(repo_dir) / "archive" / agent_id
        archive_dir.mkdir(parents=True, exist_ok=True)

        # (a) Claude session history. Record its source path always, and copy the
        # jsonl session files if the dir is still present.
        proj_dir = _claude_projects_dir_for(worktree)
        meta = {
            "agent_id": agent_id,
            "worktree": str(worktree),
            "branch": entry.get("branch"),
            "claude_projects_dir": str(proj_dir),
            "archived_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            if proj_dir.is_dir():
                dst = archive_dir / "claude_projects"
                dst.mkdir(parents=True, exist_ok=True)
                copied = 0
                for f in proj_dir.iterdir():
                    if f.is_file():
                        try:
                            shutil.copy2(f, dst / f.name)
                            copied += 1
                        except OSError:
                            pass
                meta["claude_sessions_copied"] = copied
        except OSError:
            pass

        # (b) Agent-specific lab stats living in the worktree's own .the_lab/
        # (real dir, not the .the_lab.link symlink to the main checkout). Copy
        # it wholesale — it's the branch-local per-agent state.
        try:
            wt_lab = worktree / ".the_lab"
            if wt_lab.is_dir() and not wt_lab.is_symlink():
                shutil.copytree(
                    wt_lab, archive_dir / "the_lab",
                    symlinks=True, dirs_exist_ok=True,
                )
                meta["worktree_the_lab_copied"] = True
        except OSError:
            pass

        try:
            jsonio.write_json(archive_dir / "meta.json", meta)
        except OSError:
            pass
    except Exception:
        pass  # archiving is best-effort; never block cleanup


def unregister_agent(repo_dir: Path, agent_id: str, *, keep_branch: bool = True) -> bool:
    repo_dir = Path(repo_dir).resolve()
    registry = _read_registry(repo_dir)
    entry = registry.pop(agent_id, None)
    if entry is None:
        return False
    record_completed_agent(repo_dir, entry)
    # Preserve durable data (Claude history + worktree lab stats) BEFORE the
    # worktree checkout is deleted, otherwise it's orphaned/lost. See feedback.
    _archive_agent_data(repo_dir, agent_id, entry)
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
