"""Inter-agent messaging: persistent inbox + asyncio wake events.

Two agents collaborating on a project (e.g. a researcher and an engineer)
need a way to pass directed requests. We piggy-back on the existing
notifications channel: a message addressed to ``agent:<id>``, ``role:<r>``,
or ``all`` becomes an unread notification on every subsequent dict-shaped
API response for that recipient, and wakes any ``/wait`` long-poll the
recipient is currently parked on.

Storage layout::

    .the_lab/messages.json
        {"next_id": 7,
         "messages": [
             {"id": 1, "from_agent": "hanw0", "from_role": "researcher",
              "to": "role:engineer", "text": "please run a 2× cache run",
              "created_at": "2026-05-13T18:00:00Z",
              "read_by": ["na2po"]},
             ...
         ]}

Reads/writes are coarse-locked at the file level; volume is expected to
be low (humans + a couple of agents). The ``wake`` event is a process-
global asyncio.Event so the long-poll path can race it against the
experiment-finished queue without per-recipient subscription bookkeeping.
"""
from __future__ import annotations

import asyncio
import json
import threading
from datetime import datetime, timezone
from pathlib import Path


_FILE_NAME = "messages.json"
_lock = threading.Lock()

# Global wake event — set whenever a message lands. /wait listeners
# race this against the experiment-finished queue. Reset by the loop in
# wait_any after it reads it (so the next message re-wakes everyone).
#
# FastAPI dispatches sync route handlers in a threadpool, so notify_wake()
# typically runs off-loop; we keep a reference to the main asyncio loop and
# schedule the .set() call via call_soon_threadsafe to stay correct.
_wake: asyncio.Event | None = None
_loop: asyncio.AbstractEventLoop | None = None


def _wake_event() -> asyncio.Event:
    """Lazily allocate the wake event the first time someone listens."""
    global _wake, _loop
    if _wake is None:
        _wake = asyncio.Event()
    if _loop is None:
        try:
            _loop = asyncio.get_running_loop()
        except RuntimeError:
            _loop = None
    return _wake


def notify_wake() -> None:
    """Trigger the wake event so /wait callers re-check for messages.

    Safe to call from any thread — if a loop is available we schedule
    ``ev.set()`` on it via call_soon_threadsafe, otherwise we no-op (no
    listener has been parked yet, so the next /wait poll picks the
    message up on its own).
    """
    ev = _wake
    loop = _loop
    if ev is None:
        return
    if loop is None:
        try:
            ev.set()
        except RuntimeError:
            pass
        return
    try:
        loop.call_soon_threadsafe(ev.set)
    except RuntimeError:
        pass


def _path(repo_dir: Path) -> Path:
    p = Path(repo_dir).resolve() / ".the_lab"
    p.mkdir(parents=True, exist_ok=True)
    return p / _FILE_NAME


def _read(repo_dir: Path) -> dict:
    path = _path(repo_dir)
    if not path.exists():
        return {"next_id": 1, "messages": []}
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {"next_id": 1, "messages": []}
    data.setdefault("next_id", 1)
    data.setdefault("messages", [])
    return data


_MAX_MESSAGES = 2000  # hard cap on total messages kept for history


def _prune(data: dict) -> dict:
    """Cap total message history at _MAX_MESSAGES, dropping the oldest first."""
    msgs = data.get("messages", [])
    if len(msgs) <= _MAX_MESSAGES:
        return data
    return {**data, "messages": msgs[-_MAX_MESSAGES:]}


def _write(repo_dir: Path, data: dict) -> None:
    _path(repo_dir).write_text(json.dumps(_prune(data), indent=2) + "\n")


def _validate_to(to: str) -> None:
    """Recipient must be ``agent:<id>``, ``role:<role>``, or ``all``."""
    if to == "all":
        return
    if to.startswith("agent:") and len(to) > len("agent:"):
        return
    if to.startswith("role:") and len(to) > len("role:"):
        return
    raise ValueError(
        f"invalid recipient {to!r}; expected 'agent:<id>', 'role:<role>', or 'all'"
    )


def add_message(
    repo_dir: Path,
    *,
    from_agent: str | None,
    from_role: str | None,
    to: str,
    text: str,
) -> dict:
    """Persist a new message and wake any /wait listeners."""
    _validate_to(to)
    text = (text or "").strip()
    if not text:
        raise ValueError("message text is empty")
    with _lock:
        data = _read(repo_dir)
        msg = {
            "id": data["next_id"],
            "from_agent": from_agent,
            "from_role": from_role,
            "to": to,
            "text": text,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "read_by": [],
        }
        data["next_id"] = msg["id"] + 1
        data["messages"].append(msg)
        _write(repo_dir, data)
    notify_wake()
    try:
        from . import ws as ws_mod
        ws_mod.broadcaster.broadcast_soon({
            "type": "message_received",
            "id": msg["id"],
            "to": to,
            "from_role": from_role,
        })
    except Exception:  # noqa: BLE001
        pass
    return msg


def list_messages(
    repo_dir: Path,
    *,
    limit: int | None = None,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """All messages, newest first. Returns (page, total_count)."""
    with _lock:
        msgs = list(_read(repo_dir)["messages"])
    msgs.reverse()
    total = len(msgs)
    if offset:
        msgs = msgs[offset:]
    if limit is not None:
        msgs = msgs[:limit]
    return msgs, total


def is_for(msg: dict, *, agent_id: str | None, role: str | None) -> bool:
    """True if *msg* is addressed to this agent (directly, by role, or 'all')."""
    to = msg.get("to") or ""
    if to == "all":
        return True
    if agent_id and to == f"agent:{agent_id}":
        return True
    if role and to == f"role:{role}":
        return True
    return False


def unread_for(
    repo_dir: Path,
    *,
    agent_id: str | None,
    role: str | None,
    limit: int | None = None,
) -> list[dict]:
    """Messages addressed to this agent that they haven't marked read.

    Newest first. Senders' own messages are excluded from their own inbox.
    """
    if not agent_id and not role:
        return []
    with _lock:
        all_msgs = list(_read(repo_dir)["messages"])
    out = []
    for m in reversed(all_msgs):
        if not is_for(m, agent_id=agent_id, role=role):
            continue
        if m.get("from_agent") and m["from_agent"] == agent_id:
            continue
        if agent_id and agent_id in (m.get("read_by") or []):
            continue
        out.append(m)
        if limit is not None and len(out) >= limit:
            break
    return out


def mark_read(repo_dir: Path, msg_id: int, agent_id: str) -> dict | None:
    """Record that *agent_id* has read *msg_id*. Idempotent; returns the message."""
    with _lock:
        data = _read(repo_dir)
        for m in data["messages"]:
            if m["id"] == msg_id:
                rb = m.setdefault("read_by", [])
                if agent_id not in rb:
                    rb.append(agent_id)
                    _write(repo_dir, data)
                return m
    return None


def mark_all_read(repo_dir: Path, agent_id: str, role: str | None) -> int:
    """Mark every currently-unread message for this recipient as read. Returns count."""
    n = 0
    with _lock:
        data = _read(repo_dir)
        for m in data["messages"]:
            if not is_for(m, agent_id=agent_id, role=role):
                continue
            rb = m.setdefault("read_by", [])
            if agent_id not in rb:
                rb.append(agent_id)
                n += 1
        if n:
            _write(repo_dir, data)
    return n


def delete_message(repo_dir: Path, msg_id: int) -> bool:
    """Remove a message entirely. Returns True if it existed."""
    with _lock:
        data = _read(repo_dir)
        before = len(data["messages"])
        data["messages"] = [m for m in data["messages"] if m["id"] != msg_id]
        if len(data["messages"]) == before:
            return False
        _write(repo_dir, data)
    return True


def clear_all(repo_dir: Path) -> int:
    """Delete all messages and reset the id counter. Returns the count removed."""
    with _lock:
        data = _read(repo_dir)
        n = len(data["messages"])
        _write(repo_dir, {"next_id": 1, "messages": []})
    return n
