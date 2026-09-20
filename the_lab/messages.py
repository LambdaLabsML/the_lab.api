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

from . import jsonio
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


# Single-slot in-memory cache: messages are consulted on every dict-shaped
# API response (unread_for in the notifications middleware) and by every
# /wait poll pass — reading messages.json from NFS each time was the single
# hottest read in the process. Memory is authoritative; every mutation
# writes through atomically via jsonio. All access happens under _lock.
_cache: dict | None = None
_cache_path: Path | None = None


def _read(repo_dir: Path) -> dict:
    """Return the live message store dict. Call while holding _lock."""
    global _cache, _cache_path
    path = _path(repo_dir)
    if _cache is not None and _cache_path == path:
        return _cache
    if not path.exists():
        data = {"next_id": 1, "messages": []}
    else:
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            data = {"next_id": 1, "messages": []}
    data.setdefault("next_id", 1)
    data.setdefault("messages", [])
    _cache, _cache_path = data, path
    return data


def _prune(data: dict) -> dict:
    return data


def _write(repo_dir: Path, data: dict) -> None:
    """Persist and adopt *data* as the cache. Call while holding _lock."""
    global _cache, _cache_path
    _cache, _cache_path = data, _path(repo_dir)
    jsonio.write_json(_cache_path, _prune(data))


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
    in_reply_to: int | None = None,
    thread_id: int | None = None,
    references: list[dict] | None = None,
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
            "delivered_by": [],
            "notified_by": [],
        }
        if in_reply_to is not None:
            msg["in_reply_to"] = in_reply_to
        if thread_id is not None:
            msg["thread_id"] = thread_id
        if references:
            msg["references"] = references
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
            "from_agent": from_agent,
            "from_role": from_role,
            "text": text[:80],
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


def get_message(repo_dir: Path, msg_id: int) -> dict | None:
    """Return a message by id without changing any acknowledgement state."""
    with _lock:
        for m in _read(repo_dir)["messages"]:
            if m.get("id") == msg_id:
                return m
    return None


def is_for(msg: dict, *, agent_id: str | None, role: str | None) -> bool:
    """True if *msg* is addressed to this agent (directly, by role, or 'all').

    'all' means every *other* agent — a broadcast never counts as addressed to
    its own sender.
    """
    to = msg.get("to") or ""
    if to == "all":
        return not (agent_id and msg.get("from_agent") == agent_id)
    if agent_id and to == f"agent:{agent_id}":
        return True
    if role and to == f"role:{role}":
        return True
    return False


def can_see(msg: dict, *, agent_id: str | None, role: str | None) -> bool:
    """Whether an identity can see a message (recipient or original sender)."""
    return bool(is_for(msg, agent_id=agent_id, role=role) or (
        agent_id and msg.get("from_agent") == agent_id
    ))


def _created_at_for(repo_dir: Path, agent_id: str) -> str | None:
    """Registration timestamp, kept here to make automatic delivery new-only."""
    try:
        from . import agents as agents_mod
        return (agents_mod.lookup_agent(repo_dir, agent_id) or {}).get("created_at")
    except Exception:  # registry is deliberately a best-effort dependency
        return None


def automatic_delivery_eligible(
    repo_dir: Path, msg: dict, agent_id: str, *, registered_at: str | None = None,
) -> bool:
    """Automatic role/all delivery is limited to messages after registration.

    Direct messages remain available regardless of age.  Explicit inbox/history
    listing is intentionally not subject to this cutoff.
    """
    if msg.get("to", "").startswith("agent:"):
        return True
    registered = registered_at if registered_at is not None else _created_at_for(repo_dir, agent_id)
    return not registered or (msg.get("created_at") or "") >= registered


def _delivered(m: dict, agent_id: str) -> bool:
    # read_by is the old on-disk delivery marker.  Treat it as delivered forever
    # so upgrading does not replay every historical message.
    return agent_id in (m.get("delivered_by") or []) or agent_id in (m.get("read_by") or [])


def unread_for(
    repo_dir: Path,
    *,
    agent_id: str | None,
    role: str | None,
    limit: int | None = None,
    automatic_only: bool = False,
) -> list[dict]:
    """Messages addressed to this agent that they haven't marked read.

    Newest first. Senders' own messages are excluded from their own inbox.
    """
    if not agent_id and not role:
        return []
    with _lock:
        all_msgs = list(_read(repo_dir)["messages"])
    registered_at = _created_at_for(repo_dir, agent_id) if automatic_only and agent_id else None
    out = []
    for m in reversed(all_msgs):
        if not is_for(m, agent_id=agent_id, role=role):
            continue
        if m.get("from_agent") and m["from_agent"] == agent_id:
            continue
        if agent_id and agent_id in (m.get("read_by") or []):
            continue
        if automatic_only and agent_id and not automatic_delivery_eligible(repo_dir, m, agent_id, registered_at=registered_at):
            continue
        out.append(m)
        if limit is not None and len(out) >= limit:
            break
    return out


def undelivered_for(repo_dir: Path, *, agent_id: str | None, role: str | None,
                    limit: int | None = None) -> list[dict]:
    """Inbox messages awaiting automatic delivery (read status is irrelevant)."""
    if not agent_id:
        return []
    with _lock:
        all_msgs = list(_read(repo_dir)["messages"])
    registered_at = _created_at_for(repo_dir, agent_id)
    out = []
    for m in reversed(all_msgs):
        if not is_for(m, agent_id=agent_id, role=role) or m.get("from_agent") == agent_id:
            continue
        if _delivered(m, agent_id) or not automatic_delivery_eligible(repo_dir, m, agent_id, registered_at=registered_at):
            continue
        out.append(m)
        if limit is not None and len(out) >= limit:
            break
    return out


def claim_unread_for(
    repo_dir: Path,
    *,
    agent_id: str | None,
    role: str | None,
    limit: int | None = None,
) -> list[dict]:
    """Atomically select AND claim delivery without marking messages read.

    N1: cross-channel dedup rests on claim-on-delivery. Selecting the unread
    set and marking it read happen inside ONE ``_lock`` acquisition, so two
    channels (the WS listener vs. a piggybacked/polling reader) can never both
    deliver the same message — whichever claims it first wins, the other sees
    it already in ``read_by`` and skips it.

    Selection excludes already-delivered (including legacy ``read_by``) and
    applies the new-registration cutoff for role/all broadcasts. ``agent_id``
    is required because delivery acknowledgement is per agent.
    """
    if not agent_id:
        return []
    out: list[dict] = []
    with _lock:
        data = _read(repo_dir)
        changed = False
        registered_at = _created_at_for(repo_dir, agent_id)
        for m in reversed(data["messages"]):
            # Same predicate as unread_for (kept in lock-step to avoid drift).
            if not is_for(m, agent_id=agent_id, role=role):
                continue
            if m.get("from_agent") and m["from_agent"] == agent_id:
                continue
            if _delivered(m, agent_id):
                continue
            if not automatic_delivery_eligible(repo_dir, m, agent_id, registered_at=registered_at):
                continue
            out.append(m)
            m.setdefault("delivered_by", []).append(agent_id)
            changed = True
            if limit is not None and len(out) >= limit:
                break
        if changed:
            _write(repo_dir, data)
    return out


def mark_delivered_many(repo_dir: Path, ids, agent_id: str) -> int:
    """Acknowledge rendered/delivered messages without treating them as read."""
    want = {i for i in ids}
    if not want or not agent_id:
        return 0
    n = 0
    with _lock:
        data = _read(repo_dir)
        for m in data["messages"]:
            if m.get("id") in want and not _delivered(m, agent_id):
                m.setdefault("delivered_by", []).append(agent_id)
                n += 1
        if n:
            _write(repo_dir, data)
    return n


def mark_delivered(repo_dir: Path, msg_id: int, agent_id: str) -> dict | None:
    """Acknowledge one delivered message; idempotent."""
    with _lock:
        data = _read(repo_dir)
        for m in data["messages"]:
            if m.get("id") == msg_id:
                if not _delivered(m, agent_id):
                    m.setdefault("delivered_by", []).append(agent_id)
                    _write(repo_dir, data)
                return m
    return None


def claim_notifications_for(
    repo_dir: Path, *, agent_id: str | None, role: str | None, limit: int = 20,
) -> list[dict]:
    """Claim preview notifications only; this neither delivers nor reads text."""
    if not agent_id:
        return []
    out: list[dict] = []
    with _lock:
        data = _read(repo_dir)
        changed = False
        registered_at = _created_at_for(repo_dir, agent_id)
        for m in reversed(data["messages"]):
            if not is_for(m, agent_id=agent_id, role=role) or m.get("from_agent") == agent_id:
                continue
            if _delivered(m, agent_id) or agent_id in (m.get("notified_by") or []):
                continue
            if not automatic_delivery_eligible(repo_dir, m, agent_id, registered_at=registered_at):
                continue
            out.append(m)
            m.setdefault("notified_by", []).append(agent_id)
            changed = True
            if len(out) >= limit:
                break
        if changed:
            _write(repo_dir, data)
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


def mark_read_many(repo_dir: Path, ids, agent_id: str) -> int:
    """Mark several messages read by *agent_id* in one read/write. Returns count."""
    want = {i for i in ids}
    if not want or not agent_id:
        return 0
    n = 0
    with _lock:
        data = _read(repo_dir)
        for m in data["messages"]:
            if m["id"] in want:
                rb = m.setdefault("read_by", [])
                if agent_id not in rb:
                    rb.append(agent_id)
                    n += 1
        if n:
            _write(repo_dir, data)
    return n


def mark_all_read(
    repo_dir: Path, agent_id: str, role: str | None,
    *, before_id: int | None = None, older_than: datetime | None = None,
) -> int:
    """Mark bounded currently-unread inbox messages read. Returns count."""
    n = 0
    with _lock:
        data = _read(repo_dir)
        for m in data["messages"]:
            if not is_for(m, agent_id=agent_id, role=role):
                continue
            if before_id is not None and m.get("id", 0) >= before_id:
                continue
            if older_than is not None:
                try:
                    created = datetime.fromisoformat(m.get("created_at", ""))
                    if created.tzinfo is None:
                        created = created.replace(tzinfo=timezone.utc)
                    if created >= older_than:
                        continue
                except (TypeError, ValueError):
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
