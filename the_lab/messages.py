"""Inter-agent messaging: persistent inbox + asyncio wake events.

Two agents collaborating on a project (e.g. a researcher and an engineer)
need a way to pass directed requests. We piggy-back on the existing
notifications channel: a message addressed to ``agent:<id>``, ``role:<r>``,
or ``all`` becomes an unread notification on every subsequent dict-shaped
API response for that recipient, and wakes any ``/wait`` long-poll the
recipient is currently parked on.

Threads: every message belongs to a thread (``thread_id``, or its own id for
a root). Thread metadata lives beside the messages and makes a thread a shared
space rather than a reply chain:

  - ``visibility`` — ``public`` threads are readable and joinable by every
    agent; ``private`` threads only by their members.
  - ``members`` — agents that receive messages addressed ``to:
    "thread:<id>"``. Posting, replying or joining makes you a member; muting
    keeps membership without delivery.
  - ``title`` / ``summary`` / ``status`` (open|closed) / ``conclusion`` /
    ``kind`` (discussion|suggestion) / ``idea_id`` — for discovery and catch-up.

Older threads have no stored metadata; it is derived from the root message
(public iff the root went to ``all``) and persisted on first join or reply.

Storage layout::

    .the_lab/messages.json
        {"next_id": 7,
         "messages": [
             {"id": 1, "from_agent": "hanw0", "from_role": "researcher",
              "to": "role:engineer", "text": "please run a 2× cache run",
              "created_at": "2026-05-13T18:00:00Z",
              "read_by": ["na2po"]},
             ...
         ],
         "threads": {"5": {"id": 5, "title": "...", "visibility": "public",
                           "status": "open", "members": {"hanw0": {"joined_at": "..."}},
                           ...}}}

Reads/writes are coarse-locked at the file level; volume is expected to
be low (humans + a couple of agents). The ``wake`` event is a process-
global asyncio.Event so the long-poll path can race it against the
experiment-finished queue without per-recipient subscription bookkeeping.
"""
from __future__ import annotations

import asyncio
import atexit
import json
import os
import re
import threading

from . import jsonio
from datetime import datetime, timezone
from pathlib import Path


_FILE_NAME = "messages.json"
UI_USER = "user"
TLDR_MAX_LENGTH = 500
TITLE_MAX_LENGTH = 200
THREAD_VISIBILITIES = ("public", "private")
THREAD_STATUSES = ("open", "closed")
THREAD_KINDS = ("discussion", "suggestion")
_lock = threading.Lock()

# Human identities: before real accounts exist, every person on the
# dashboard is addressable as the catch-all "user", or — once they've named
# themselves — personally as "user:<name>". Both are "human" for every
# predicate below (visibility, membership, delivery); only the dashboard
# (never an agent) may ever send or act as one, enforced in routes/messages.py.
_HUMAN_PREFIX = f"{UI_USER}:"
HUMAN_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def is_human(identity: str | None) -> bool:
    """True for the catch-all ``user`` or a named ``user:<name>``."""
    return identity == UI_USER or bool(identity) and identity.startswith(_HUMAN_PREFIX)


def human_id(name: str) -> str:
    """Validate a human name slug and return its ``user:<name>`` identity."""
    name = (name or "").strip().lower()
    if not HUMAN_NAME_RE.match(name):
        raise ValueError(
            f"invalid user name {name!r}; expected lowercase, "
            "[a-z0-9][a-z0-9_-]{0,31}"
        )
    return f"{_HUMAN_PREFIX}{name}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def clamp_tldr(tldr: str, limit: int = TLDR_MAX_LENGTH) -> tuple[str, bool]:
    """Clamp *tldr* to *limit* characters at a word boundary, adding "…".

    Returns ``(clamped, was_truncated)``. A tldr is meant to be short, so an
    over-long one is shortened rather than rejected — callers that want to
    tell the sender it happened can compare the input length to *limit*
    (or check the returned flag) and surface a warning.
    """
    tldr = tldr or ""
    if len(tldr) <= limit:
        return tldr, False
    cut = tldr[: limit - 1]
    sp = cut.rfind(" ")
    if sp > limit * 0.6:  # don't over-shorten on a stray early space
        cut = cut[:sp]
    return cut.rstrip() + "…", True

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
    data.setdefault("threads", {})
    _cache, _cache_path = data, path
    return data


def _prune(data: dict) -> dict:
    return data


# Write-behind persistence. Every delivery/notification/read marker used to
# re-serialize the whole store (indent=2 → CPython's pure-Python encoder, ~75 ms
# for a 7.6 MB file) and rewrite it on NFS while holding _lock — and the /wait
# long-poll takes _lock on the event loop. Memory is authoritative, so writes
# are now coalesced: _write() marks the store dirty and a single timer flushes
# it at most once per _WRITE_DELAY seconds, serialized compactly (C encoder).
# A crash loses at most _WRITE_DELAY seconds of marker/message state.
# THE_LAB_MESSAGES_WRITE_DELAY=0 restores synchronous write-through.
try:
    _WRITE_DELAY = float(os.environ.get("THE_LAB_MESSAGES_WRITE_DELAY", "1.0") or 0)
except ValueError:
    _WRITE_DELAY = 1.0
_dirty_gen = 0          # bumped by every _write (under _lock)
_flushed_gen = 0        # last generation that reached disk (under _flush_io_lock)
_flush_timer: threading.Timer | None = None
_flush_io_lock = threading.Lock()


def _snapshot_locked() -> tuple[Path, str, int] | None:
    """Serialize the live store. Call while holding _lock."""
    if _cache is None or _cache_path is None:
        return None
    text = json.dumps(_prune(_cache), separators=(",", ":")) + "\n"
    return _cache_path, text, _dirty_gen


def _persist(snapshot: tuple[Path, str, int] | None) -> None:
    global _flushed_gen
    if snapshot is None:
        return
    path, text, gen = snapshot
    with _flush_io_lock:
        if gen <= _flushed_gen:
            return  # a newer snapshot already reached disk
        jsonio.write_text_atomic(path, text)
        _flushed_gen = gen


def flush() -> None:
    """Write pending message state to disk now (timer, shutdown, tests)."""
    global _flush_timer
    with _lock:
        _flush_timer = None
        snap = _snapshot_locked() if _dirty_gen != _flushed_gen else None
    _persist(snap)


atexit.register(flush)


def _write(repo_dir: Path, data: dict) -> None:
    """Adopt *data* as the cache and schedule persistence. Call while holding _lock."""
    global _cache, _cache_path, _dirty_gen, _flush_timer
    _cache, _cache_path = data, _path(repo_dir)
    _dirty_gen += 1
    if _WRITE_DELAY <= 0:
        _persist(_snapshot_locked())
        return
    if _flush_timer is None:
        _flush_timer = threading.Timer(_WRITE_DELAY, flush)
        _flush_timer.daemon = True
        _flush_timer.name = "messages-flush"
        _flush_timer.start()


def _validate_to(to: str) -> None:
    """Recipient must be an agent, role, all agents, a thread, or a human."""
    if to in ("all", UI_USER):
        return
    if to.startswith(_HUMAN_PREFIX) and HUMAN_NAME_RE.match(to[len(_HUMAN_PREFIX):]):
        return
    if to.startswith("agent:") and len(to) > len("agent:"):
        return
    if to.startswith("role:") and len(to) > len("role:"):
        return
    if to.startswith("thread:") and to[len("thread:"):].isdigit():
        return
    raise ValueError(
        f"invalid recipient {to!r}; expected 'user', 'user:<name>', 'agent:<id>', "
        "'role:<role>', 'thread:<id>', or 'all'"
    )


def _canonical_to(to: str) -> str:
    """Keep the historical ``agent:user`` spelling equivalent to ``user``."""
    return UI_USER if to == f"agent:{UI_USER}" else to


def _append_locked(
    data: dict,
    *,
    from_agent: str | None,
    from_role: str | None,
    to: str,
    text: str,
    tldr: str | None = None,
    in_reply_to: int | None = None,
    thread_id: int | None = None,
    references: list[dict] | None = None,
    created_at: str | None = None,
) -> dict:
    """Validate and append one message to *data*. Call while holding _lock."""
    if to == "thread":
        if thread_id is None:
            raise ValueError("to='thread' needs a thread_id (or in_reply_to)")
        to = f"thread:{thread_id}"
    _validate_to(to)
    to = _canonical_to(to)
    text = (text or "").strip()
    tldr = (tldr or "").strip()
    tldr, _truncated = clamp_tldr(tldr)
    if not text:
        # A tldr-only post (no separate body) is still a real message — let
        # the summary stand in for the text rather than bouncing it.
        text = tldr
    if not text:
        raise ValueError("message text is empty")
    msg = {
        "id": data["next_id"],
        "from_agent": from_agent,
        "from_role": from_role,
        "to": to,
        "text": text,
        "created_at": created_at or _now(),
        "read_by": [],
        "delivered_by": [],
        "notified_by": [],
    }
    # Do not add an empty key: existing clients and stored messages remain
    # indistinguishable from messages that never supplied a summary.
    if tldr:
        msg["tldr"] = tldr
    if in_reply_to is not None:
        msg["in_reply_to"] = in_reply_to
    if thread_id is not None:
        msg["thread_id"] = thread_id
    if references:
        msg["references"] = references
    data["next_id"] = msg["id"] + 1
    data["messages"].append(msg)
    if thread_id is not None and from_agent:
        # Posting into a thread makes you a member (persisting derived
        # metadata for older threads on first activity).
        meta = _thread_meta_locked(data, thread_id, persist=True)
        if meta is not None and from_agent not in meta["members"]:
            meta["members"][from_agent] = {"joined_at": msg["created_at"]}
    return msg


def _after_add(msg: dict) -> None:
    """Side effects of a new message: wake listeners, push a dashboard event."""
    notify_wake()
    try:
        from . import ws as ws_mod
        ws_mod.broadcaster.broadcast_soon({
            "type": "message_received",
            "id": msg["id"],
            "to": msg["to"],
            "thread_id": msg.get("thread_id") or msg["id"],
            "from_agent": msg.get("from_agent"),
            "from_role": msg.get("from_role"),
            "text": (msg.get("tldr") or msg["text"])[:80],
        })
    except Exception:  # noqa: BLE001
        pass


def _thread_changed(thread_id: int) -> None:
    try:
        from . import ws as ws_mod
        ws_mod.broadcaster.broadcast_soon({"type": "thread_changed", "thread_id": thread_id})
    except Exception:  # noqa: BLE001
        pass


def add_message(
    repo_dir: Path,
    *,
    from_agent: str | None,
    from_role: str | None,
    to: str,
    text: str,
    tldr: str | None = None,
    in_reply_to: int | None = None,
    thread_id: int | None = None,
    references: list[dict] | None = None,
) -> dict:
    """Persist a new message and wake any /wait listeners.

    ``to="thread"`` (with ``thread_id``) addresses the thread's members.
    """
    with _lock:
        data = _read(repo_dir)
        msg = _append_locked(
            data, from_agent=from_agent, from_role=from_role, to=to, text=text,
            tldr=tldr, in_reply_to=in_reply_to, thread_id=thread_id,
            references=references,
        )
        _write(repo_dir, data)
    _after_add(msg)
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


def _threads_view(threads: dict | None = None) -> dict:
    """Thread metadata for pure predicates: explicit, else the live cache."""
    if threads is not None:
        return threads
    return (_cache or {}).get("threads") or {}


def is_for(msg: dict, *, agent_id: str | None, role: str | None,
           threads: dict | None = None) -> bool:
    """True if *msg* is addressed to this agent (directly, by role, 'all', or
    as an unmuted member of the thread it was posted to).

    'all' and thread messages mean every *other* recipient — a message never
    counts as addressed to its own sender.
    """
    to = msg.get("to") or ""
    if to.startswith("thread:"):
        if not agent_id or msg.get("from_agent") == agent_id:
            return False
        meta = _threads_view(threads).get(to[len("thread:"):])
        member = ((meta or {}).get("members") or {}).get(agent_id)
        return bool(member) and not member.get("muted")
    # ``user`` is the catch-all human identity, not a registered agent — it
    # reaches every human, named or not. The explicit agent:user spelling is
    # retained as an input-compatible alias. A named ``user:<name>`` only
    # reaches that one human.
    if to in (UI_USER, f"agent:{UI_USER}"):
        return is_human(agent_id)
    if to.startswith(_HUMAN_PREFIX):
        return agent_id == to
    if to == "all":
        return not (agent_id and msg.get("from_agent") == agent_id)
    if agent_id and to == f"agent:{agent_id}":
        return True
    if role and to == f"role:{role}":
        return True
    return False


def can_see(msg: dict, *, agent_id: str | None, role: str | None,
            threads: dict | None = None) -> bool:
    """Whether an identity can see a message: recipient, original sender, or —
    for messages addressed to a thread — any agent when the thread is public,
    and any member (muted included) when it is private."""
    if is_for(msg, agent_id=agent_id, role=role, threads=threads) or (
        agent_id and msg.get("from_agent") == agent_id
    ):
        return True
    to = msg.get("to") or ""
    if to.startswith("thread:") and (agent_id or role):
        meta = _threads_view(threads).get(to[len("thread:"):])
        if meta and (meta.get("visibility") == "public" or agent_id in (meta.get("members") or {})):
            return True
    return False


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
    if msg.get("to", "").startswith("agent:") or is_human(msg.get("to", "")):
        return True
    if msg.get("to", "").startswith("thread:"):
        # Thread delivery starts when you joined; history is read explicitly.
        meta = _threads_view().get(msg["to"][len("thread:"):]) or {}
        joined = ((meta.get("members") or {}).get(agent_id) or {}).get("joined_at")
        return not joined or (msg.get("created_at") or "") >= joined
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
    since: int | None = None,
) -> list[dict]:
    """Messages addressed to this agent that they haven't marked read.

    Newest first. Senders' own messages are excluded from their own inbox.
    ``since``, when given, excludes messages with id <= since — a cursor a
    long-running listener can use to resume without re-scanning (or
    re-delivering) everything it already saw.
    """
    if not agent_id and not role:
        return []
    with _lock:
        all_msgs = list(_read(repo_dir)["messages"])
    registered_at = _created_at_for(repo_dir, agent_id) if automatic_only and agent_id else None
    out = []
    for m in reversed(all_msgs):
        if since is not None and m.get("id", 0) <= since:
            continue
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
                    limit: int | None = None, since: int | None = None) -> list[dict]:
    """Inbox messages awaiting automatic delivery (read status is irrelevant).

    ``since`` excludes messages with id <= since (see ``unread_for``).
    """
    if not agent_id:
        return []
    with _lock:
        all_msgs = list(_read(repo_dir)["messages"])
    registered_at = _created_at_for(repo_dir, agent_id)
    out = []
    for m in reversed(all_msgs):
        if since is not None and m.get("id", 0) <= since:
            continue
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
    since: int | None = None,
) -> list[dict]:
    """Atomically select AND claim delivery without marking messages read.

    N1: cross-channel dedup rests on claim-on-delivery. Selecting the unread
    set and marking it read happen inside ONE ``_lock`` acquisition, so two
    channels (the WS listener vs. a piggybacked/polling reader) can never both
    deliver the same message — whichever claims it first wins, the other sees
    it already in ``read_by`` and skips it.

    Selection excludes already-delivered (including legacy ``read_by``) and
    applies the new-registration cutoff for role/all broadcasts. ``agent_id``
    is required because delivery acknowledgement is per agent. ``since``
    excludes messages with id <= since (see ``unread_for``) — a connected
    listener uses this to keep claiming only newly-arrived messages instead
    of re-draining its whole historical backlog on every retry.
    """
    if not agent_id:
        return []
    out: list[dict] = []
    with _lock:
        data = _read(repo_dir)
        changed = False
        registered_at = _created_at_for(repo_dir, agent_id)
        for m in reversed(data["messages"]):
            if since is not None and m.get("id", 0) <= since:
                continue
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


_RECENT_BACKLOG_LIMIT = 20


def connect_snapshot_for(
    repo_dir: Path, *, agent_id: str | None, role: str | None,
    recent_limit: int = _RECENT_BACKLOG_LIMIT, claim: bool = True,
) -> tuple[list[dict], int, int | None]:
    """Bounded snapshot for a fresh (cursorless) connect.

    Returns ``(recent, older_count, cursor)``:

      - ``recent`` — the newest *recent_limit* currently-undelivered messages
        (newest first).
      - ``older_count`` — how many more undelivered messages exist beyond
        that slice. They are deliberately left alone (never auto-claimed or
        pushed) so a disconnected/slow listener can't be flooded with its
        whole historical backlog on reconnect — the caller surfaces
        ``older_count`` as a one-line hint and the agent fetches them
        explicitly (``GET /api/v1/messages?unread=1``) if it wants them.
      - ``cursor`` — the highest message id considered, newest-first; pass
        it back as ``since=`` on the next connect (or keep tracking locally)
        so this backlog is never re-scanned.

    When *claim* is true (the normal, non-peek case) the returned ``recent``
    slice is marked delivered; ``peek`` callers pass ``claim=False`` so
    nothing is mutated.
    """
    if not agent_id:
        return [], 0, None
    with _lock:
        data = _read(repo_dir)
        registered_at = _created_at_for(repo_dir, agent_id)
        candidates = []
        for m in reversed(data["messages"]):
            if not is_for(m, agent_id=agent_id, role=role):
                continue
            if m.get("from_agent") and m["from_agent"] == agent_id:
                continue
            if _delivered(m, agent_id):
                continue
            if not automatic_delivery_eligible(repo_dir, m, agent_id, registered_at=registered_at):
                continue
            candidates.append(m)
        total = len(candidates)
        recent = candidates[:recent_limit]
        if claim and recent:
            for m in recent:
                m.setdefault("delivered_by", []).append(agent_id)
            _write(repo_dir, data)
    older = max(total - len(recent), 0)
    cursor = candidates[0]["id"] if candidates else None
    return recent, older, cursor


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
        _write(repo_dir, {"next_id": 1, "messages": [], "threads": {}})
    return n


# ---------------------------------------------------------------------------
# Threads
# ---------------------------------------------------------------------------

def thread_key(msg: dict) -> int:
    return msg.get("thread_id") or msg["id"]


def _find_locked(data: dict, msg_id: int) -> dict | None:
    for m in data["messages"]:
        if m.get("id") == msg_id:
            return m
    return None


def _preview(msg: dict, n: int = 160) -> str:
    text = (msg.get("tldr") or msg.get("text") or "").strip()
    first = text.splitlines()[0] if text else ""
    return first if len(first) <= n else first[: n - 1].rstrip() + "…"


def _derive_thread(root: dict, group: list[dict] | None = None) -> dict:
    """Metadata for a thread that predates stored metadata.

    Public when anything in it was broadcast to ``all`` (it was conducted in
    the open); members are the root's addressee plus everyone who posted.
    """
    group = group or [root]
    to = root.get("to") or ""
    members: dict[str, dict] = {}
    if to.startswith("agent:") and to != f"agent:{UI_USER}":
        members[to[len("agent:"):]] = {"joined_at": root.get("created_at")}
    elif is_human(to):
        members[to] = {"joined_at": root.get("created_at")}
    elif to == f"agent:{UI_USER}":
        members[UI_USER] = {"joined_at": root.get("created_at")}
    for m in group:
        aid = m.get("from_agent")
        if aid and aid not in members:
            members[aid] = {"joined_at": m.get("created_at")}
    public = any((m.get("to") or "") == "all" for m in group)
    return {
        "id": root["id"],
        "title": _preview(root, TITLE_MAX_LENGTH),
        "visibility": "public" if public else "private",
        "status": "open",
        "kind": "discussion",
        "summary": "",
        "created_by": root.get("from_agent"),
        "created_at": root.get("created_at"),
        "members": members,
    }


def _thread_meta_locked(data: dict, thread_id: int, *, persist: bool = False) -> dict | None:
    """Stored metadata, else derived from the root (optionally persisted)."""
    meta = data["threads"].get(str(thread_id))
    if meta is not None:
        return meta
    root = _find_locked(data, thread_id)
    if root is None:
        return None
    meta = _derive_thread(root, [m for m in data["messages"] if thread_key(m) == thread_id])
    if persist:
        data["threads"][str(thread_id)] = meta
    return meta


def _thread_visible(meta: dict, root: dict | None, *, agent_id: str | None,
                    role: str | None, operator: bool, threads: dict) -> bool:
    if operator or meta.get("visibility") == "public":
        return True
    if agent_id and agent_id in (meta.get("members") or {}):
        return True
    return bool(root and can_see(root, agent_id=agent_id, role=role, threads=threads))


def thread_visible(repo_dir: Path, thread_id: int, *, agent_id: str | None,
                   role: str | None, operator: bool = False) -> bool:
    """Whether the caller may read, join and post to *thread_id*."""
    with _lock:
        data = _read(repo_dir)
        meta = _thread_meta_locked(data, thread_id)
        if meta is None:
            raise KeyError(thread_id)
        return _thread_visible(meta, _find_locked(data, thread_id), agent_id=agent_id,
                               role=role, operator=operator, threads=data["threads"])


def _public_meta(meta: dict, *, agent_id: str | None = None) -> dict:
    members = meta.get("members") or {}
    me = members.get(agent_id) if agent_id else None
    out = {k: v for k, v in meta.items() if k != "members"}
    out["member_count"] = len(members)
    out["joined"] = me is not None
    out["muted"] = bool(me and me.get("muted"))
    return out


def create_thread(
    repo_dir: Path,
    *,
    from_agent: str | None,
    from_role: str | None,
    title: str,
    text: str,
    tldr: str | None = None,
    visibility: str = "public",
    members: list[str] | tuple = (),
    announce: bool = False,
    kind: str = "discussion",
    idea_id: int | None = None,
    references: list[dict] | None = None,
) -> tuple[dict, dict]:
    """Create a thread and its root message atomically. Returns (meta, root).

    The creator and *members* join at creation. ``announce`` sends the root to
    ``all`` (everyone is notified once and can join); otherwise it goes to the
    thread's members only and others find it via listing/search.
    """
    title = (title or "").strip()
    if not title:
        raise ValueError("thread title is empty")
    if len(title) > TITLE_MAX_LENGTH:
        raise ValueError(f"thread title must be at most {TITLE_MAX_LENGTH} characters")
    if visibility not in THREAD_VISIBILITIES:
        raise ValueError(f"visibility must be one of {THREAD_VISIBILITIES}")
    if kind not in THREAD_KINDS:
        raise ValueError(f"kind must be one of {THREAD_KINDS}")
    if announce and visibility != "public":
        raise ValueError("only public threads can be announced to all")
    with _lock:
        data = _read(repo_dir)
        tid = data["next_id"]
        now = _now()
        meta = {
            "id": tid, "title": title, "visibility": visibility, "status": "open",
            "kind": kind, "summary": "", "created_by": from_agent, "created_at": now,
            "members": {a: {"joined_at": now} for a in [from_agent, *members] if a},
        }
        if idea_id is not None:
            meta["idea_id"] = idea_id
        data["threads"][str(tid)] = meta
        try:
            root = _append_locked(
                data, from_agent=from_agent, from_role=from_role,
                to="all" if announce else f"thread:{tid}", text=text, tldr=tldr,
                thread_id=tid, references=references, created_at=now,
            )
        except ValueError:
            del data["threads"][str(tid)]
            raise
        _write(repo_dir, data)
        out = _public_meta(meta, agent_id=from_agent)
    _after_add(root)
    _thread_changed(tid)
    return out, root


def join_thread(repo_dir: Path, thread_id: int, *, agent_id: str, role: str | None,
                operator: bool = False, muted: bool | None = None) -> dict:
    """Join (or re-mute/unmute) a thread you can see. Idempotent.

    Raises KeyError (no such thread) or PermissionError (private, not a member).
    """
    with _lock:
        data = _read(repo_dir)
        meta = _thread_meta_locked(data, thread_id)
        if meta is None:
            raise KeyError(thread_id)
        if not _thread_visible(meta, _find_locked(data, thread_id), agent_id=agent_id,
                               role=role, operator=operator, threads=data["threads"]):
            raise PermissionError("this thread is private; ask a member to add you")
        data["threads"][str(thread_id)] = meta
        member = meta["members"].setdefault(agent_id, {"joined_at": _now()})
        if muted is not None:
            if muted:
                member["muted"] = True
            else:
                member.pop("muted", None)
        _write(repo_dir, data)
        out = _public_meta(meta, agent_id=agent_id)
    _thread_changed(thread_id)
    return out


def leave_thread(repo_dir: Path, thread_id: int, *, agent_id: str) -> dict:
    with _lock:
        data = _read(repo_dir)
        meta = _thread_meta_locked(data, thread_id)
        if meta is None:
            raise KeyError(thread_id)
        if agent_id in meta["members"]:
            data["threads"][str(thread_id)] = meta
            del meta["members"][agent_id]
            _write(repo_dir, data)
        out = _public_meta(meta, agent_id=agent_id)
    _thread_changed(thread_id)
    return out


def add_thread_members(repo_dir: Path, thread_id: int, member_ids: list[str], *,
                       requester: str | None, operator: bool = False) -> dict:
    """Add agents to a thread. Only members (or the operator) can add others."""
    with _lock:
        data = _read(repo_dir)
        meta = _thread_meta_locked(data, thread_id)
        if meta is None:
            raise KeyError(thread_id)
        if not operator and requester not in meta["members"]:
            raise PermissionError("only thread members can add other members")
        data["threads"][str(thread_id)] = meta
        now = _now()
        for aid in member_ids:
            if aid:
                meta["members"].setdefault(aid, {"joined_at": now})
        _write(repo_dir, data)
        out = _public_meta(meta, agent_id=requester)
    _thread_changed(thread_id)
    return out


def update_thread(
    repo_dir: Path, thread_id: int, *, requester: str | None, requester_role: str | None = None,
    operator: bool = False, title: str | None = None, summary: str | None = None,
    status: str | None = None, conclusion: str | None = None, visibility: str | None = None,
) -> tuple[dict, dict | None]:
    """Edit thread metadata (members or operator). Closing posts a closing
    message carrying the conclusion to the thread. Returns (meta, closing_msg)."""
    if status is not None and status not in THREAD_STATUSES:
        raise ValueError(f"status must be one of {THREAD_STATUSES}")
    if visibility is not None and visibility not in THREAD_VISIBILITIES:
        raise ValueError(f"visibility must be one of {THREAD_VISIBILITIES}")
    if title is not None and not (0 < len(title.strip()) <= TITLE_MAX_LENGTH):
        raise ValueError(f"thread title must be 1..{TITLE_MAX_LENGTH} characters")
    closing = None
    with _lock:
        data = _read(repo_dir)
        meta = _thread_meta_locked(data, thread_id)
        if meta is None:
            raise KeyError(thread_id)
        if not operator and requester not in meta["members"]:
            raise PermissionError("only thread members can edit a thread (join it first)")
        data["threads"][str(thread_id)] = meta
        if title is not None:
            meta["title"] = title.strip()
        if summary is not None:
            meta["summary"] = summary.strip()
        if visibility is not None:
            meta["visibility"] = visibility
        if conclusion is not None:
            meta["conclusion"] = conclusion.strip()
        if status is not None and status != meta.get("status"):
            meta["status"] = status
            meta["closed_at" if status == "closed" else "reopened_at"] = _now()
            if status == "closed":
                text = "Thread closed." + (f"\n\nConclusion: {meta['conclusion']}" if meta.get("conclusion") else "")
                closing = _append_locked(
                    data, from_agent=requester, from_role=requester_role, to=f"thread:{thread_id}",
                    text=text, tldr=_preview({"text": text}, TLDR_MAX_LENGTH), thread_id=thread_id,
                )
        meta["updated_at"] = _now()
        _write(repo_dir, data)
        out = _public_meta(meta, agent_id=requester)
    if closing:
        _after_add(closing)
    _thread_changed(thread_id)
    return out, closing


def list_threads(
    repo_dir: Path, *, agent_id: str | None, role: str | None, operator: bool = False,
    q: str | None = None, status: str = "open", mine: bool = False,
    kind: str | None = None, limit: int = 20, offset: int = 0,
) -> tuple[list[dict], int]:
    """Threads the caller can see, most recently active first.

    A thread is listed once it has stored metadata or at least one reply, so
    one-off broadcasts don't drown out conversations.
    """
    with _lock:
        data = _read(repo_dir)
        msgs = list(data["messages"])
        threads = {k: v for k, v in data["threads"].items()}
    groups: dict[int, list[dict]] = {}
    for m in msgs:
        groups.setdefault(thread_key(m), []).append(m)
    terms = [t for t in (q or "").lower().split() if t]
    out = []
    for tid, group in groups.items():
        meta = threads.get(str(tid))
        root = group[0] if group[0]["id"] == tid else None
        if meta is None:
            if len(group) < 2 or root is None:
                continue
            meta = _derive_thread(root, group)
        if not _thread_visible(meta, root, agent_id=agent_id, role=role,
                               operator=operator, threads=threads):
            continue
        if status != "all" and meta.get("status", "open") != status:
            continue
        members = meta.get("members") or {}
        if mine and agent_id not in members:
            continue
        if kind and meta.get("kind", "discussion") != kind:
            continue
        if terms:
            hay = " ".join([meta.get("title") or "", meta.get("summary") or "",
                            root.get("text", "") if root else ""]).lower()
            if not all(t in hay for t in terms):
                continue
        seen = [m for m in group if operator or can_see(m, agent_id=agent_id, role=role, threads=threads)]
        last = seen[-1] if seen else group[-1]
        item = _public_meta(meta, agent_id=agent_id)
        item.update({
            "message_count": len(seen),
            "last_activity": last.get("created_at"),
            "last_message": {"id": last["id"], "from_agent": last.get("from_agent"),
                             "from_role": last.get("from_role"), "preview": _preview(last)},
        })
        out.append(item)
    out.sort(key=lambda t: t.get("last_activity") or "", reverse=True)
    return out[offset: offset + limit], len(out)


def dashboard_inbox(
    repo_dir: Path, *, viewer: str | None, aliases: list[str], sent_ids: set[int],
    read_ids: set[int], saved_threads: set[int], view: str = "for_you", q: str = "",
    status: str = "all", sender: str = "", before_id: int | None = None, limit: int = 30,
    saved_focus: dict[str, int] | None = None,
) -> dict:
    """Distinct dashboard threads, indexed over the entire archive in one pass.

    This is an operator view; callers must enforce dashboard/admin access.
    Anonymous human messages belong to this browser only when in sent_ids.
    Reading here never changes agent delivery or read state.
    """
    with _lock:
        data = _read(repo_dir)
        messages = list(data["messages"])
        metadata = dict(data["threads"])
    by_id = {m["id"]: m for m in messages}
    mine = {m["id"] for m in messages if m["id"] in sent_ids
            or (viewer and m.get("from_agent") == viewer)}
    names = [name.strip() for name in aliases if len(name.strip()) >= 2]
    mention = re.compile(r"(?<![\w@])@?(?:" + "|".join(re.escape(n) for n in names)
                         + r")(?!\w)", re.I) if names else None

    def addressed(m: dict) -> bool:
        if m["id"] in mine:
            return False
        if m.get("to") in {UI_USER, f"agent:{UI_USER}", viewer} - {None}:
            return True
        if m.get("in_reply_to") in mine:
            return True
        return bool(mention and mention.search(f"{m.get('tldr') or ''} {m.get('text') or ''}"))

    def preview(m: dict | None) -> dict | None:
        if m is None:
            return None
        body = " ".join((m.get("text") or m.get("tldr") or "").split())
        excerpt = body if len(body) <= 200 else body[:197].rsplit(" ", 1)[0] + "…"
        return {**_compact_entry(m), "preview": excerpt, "mine": m["id"] in mine,
                "thread_id": thread_key(m), "in_reply_to": m.get("in_reply_to")}

    groups: dict[int, list[dict]] = {}
    for m in messages:
        groups.setdefault(thread_key(m), []).append(m)
    counts = {"for_you": 0, "mine": 0, "all": 0, "saved": 0}
    terms = q.lower().split()
    rows = []
    titles: dict[int, str] = {}
    for tid, group in groups.items():
        group = sorted(group, key=lambda m: m["id"])
        # Include one-off direct questions too: they must not disappear until
        # somebody replies or explicitly creates thread metadata.
        root = by_id.get(tid) or group[0]
        meta = metadata.get(str(tid)) or _derive_thread(root, group)
        own = [m for m in group if m["id"] in mine]
        personal = [m for m in group if addressed(m)]
        unread = [m for m in personal if m["id"] not in read_ids]
        is_mine = bool(own or (viewer and viewer in (meta.get("members") or {})))
        is_saved = tid in saved_threads
        counts["all"] += 1
        counts["mine"] += int(is_mine)
        counts["saved"] += int(is_saved)
        counts["for_you"] += len(unread)
        if view == "replies":
            # Flat list built below; only the thread titles are needed here.
            titles[tid] = meta.get("title") or ""
            continue
        if view == "for_you" and not unread or view == "mine" and not is_mine or view == "saved" and not is_saved:
            continue
        if status != "all" and meta.get("status", "open") != status:
            continue
        if sender and not any(m.get("from_agent") == sender or m.get("to") == sender
                              or m.get("to") == f"agent:{sender}" for m in group):
            continue
        matching = group
        if terms:
            # Search replies and artifacts/references as well as titles and
            # summaries, before paginating threads (never just the newest page).
            title = f"{meta.get('title') or ''} {meta.get('summary') or ''}".lower()
            matching = [m for m in group if all(term in
                f"{title} {m.get('tldr') or ''} {m.get('text') or ''} {m.get('references') or ''}".lower()
                for term in terms)]
            if not matching:
                continue
        focus = unread[0] if view == "for_you" else matching[-1]
        saved_message = by_id.get((saved_focus or {}).get(str(tid)))
        if view == "saved" and not terms and saved_message and thread_key(saved_message) == tid:
            focus = saved_message
        parent = by_id.get(focus.get("in_reply_to"))
        question = parent if parent and parent["id"] in mine else next(
            (m for m in reversed(own) if m["id"] < focus["id"]), None)
        last = group[-1]
        idea_ids = {}
        def add_idea(ident):
            idea_ids.pop(ident, None)
            idea_ids[ident] = None
        if meta.get("idea_id") is not None:
            add_idea(int(meta["idea_id"]))
        for message in group:
            for reference in message.get("references") or []:
                rid = str(reference.get("id", ""))
                if reference.get("kind") == "idea" and rid.isdigit():
                    add_idea(int(rid))
                elif reference.get("kind") == "experiment" and re.fullmatch(r"\d+\.\d+", rid):
                    add_idea(int(rid.split(".")[0]))
            body = f"{message.get('tldr') or ''} {message.get('text') or ''}"
            body = re.sub(r"\b(?:msgs?|messages?|thread)\s*#?\d+(?:\s*(?:/|,|&|and)\s*#?\d+)*", "", body, flags=re.I)
            for match in re.finditer(r"(?<![\w/])(?:ideas?\s*[/#:]?\s*|#)(\d+)\b|(?<![\w.])exp/(\d+)\.\d+\b|(?<![\w.])(\d{3,})\.\d+\b", body, re.I):
                add_idea(int(next(value for value in match.groups() if value)))
        participants = set()
        def add_participant(identity):
            if identity:
                participants.add(identity if is_human(identity) else identity.removeprefix("agent:"))
        for identity in meta.get("members") or {}:
            add_participant(identity)
        for message in group:
            add_participant(message.get("from_agent"))
            recipient = message.get("to") or ""
            if is_human(recipient) or recipient.startswith("agent:"):
                add_participant(recipient)
        item = _public_meta(meta, agent_id=viewer)
        item.update({
            "idea_ids": list(reversed(idea_ids)),
            "participants": sorted(participants, key=lambda identity: (not is_human(identity), identity)),
            "message_count": len(group), "my_message_count": len(own), "last_activity": last.get("created_at"),
            "cursor_id": last["id"], "latest": preview(last), "focus": preview(focus),
            "question": preview(question), "latest_mine": preview(own[-1] if own else None),
            "unread_for_me": len(unread), "unread_ids": [m["id"] for m in unread], "saved": is_saved,
            "you_posted_last": last["id"] in mine,
        })
        rows.append(item)
    if view == "replies":
        # Slack-style "replies": each message addressed to the viewer (a reply
        # to one of their messages, sent to them, or naming them), newest
        # first, read ones included and flagged so they can be re-marked.
        personal = [m for m in messages if addressed(m)]
        if terms:
            personal = [m for m in personal if all(term in
                f"{titles.get(thread_key(m), '')} {m.get('tldr') or ''} {m.get('text') or ''}".lower()
                for term in terms)]
        personal.sort(key=lambda m: m["id"], reverse=True)
        total = len(personal)
        counts["replies"] = counts["for_you"]
        if before_id is not None:
            personal = [m for m in personal if m["id"] < before_id]
        page = personal[:limit]
        items = []
        for m in page:
            parent = by_id.get(m.get("in_reply_to"))
            items.append({**preview(m), "read": m["id"] in read_ids,
                          "thread_title": titles.get(thread_key(m), ""),
                          "question": preview(parent) if parent and parent["id"] in mine else None})
        return {"threads": [], "replies": items, "total": total, "counts": counts,
                "next_before_id": page[-1]["id"] if len(personal) > limit and page else None}
    rows.sort(key=lambda row: row["cursor_id"], reverse=True)
    total = len(rows)
    if before_id is not None:
        rows = [row for row in rows if row["cursor_id"] < before_id]
    page = rows[:limit]
    return {"threads": page, "total": total, "counts": counts,
            "next_before_id": page[-1]["cursor_id"] if len(rows) > limit and page else None}


def _compact_entry(m: dict) -> dict:
    """Skim-friendly message shape: no full text, just enough to triage.

    Fetch one in full via ``GET /api/v1/messages/{id}``.
    """
    return {
        "id": m["id"], "from_agent": m.get("from_agent"), "from_role": m.get("from_role"),
        "to": m.get("to"), "created_at": m.get("created_at"), "preview": _preview(m),
    }


def get_thread(
    repo_dir: Path, thread_id: int, *, agent_id: str | None, role: str | None,
    operator: bool = False, limit: int = _RECENT_BACKLOG_LIMIT, offset: int | None = None,
    before_id: int | None = None, fields: str | None = None,
    around_id: int | None = None, after_id: int | None = None,
    audience: str = "all", viewer: str | None = None, sent_ids: set[int] | None = None,
    context: bool = False,
) -> dict:
    """Thread metadata, members and the messages the caller can see.

    Pages by recency: the default and ``before_id`` modes return the newest
    ``limit`` messages (or the ``limit`` messages immediately before
    ``before_id``), in chronological order — stable even while the thread
    keeps growing between calls. ``offset`` is the legacy position-counted
    mode kept for existing callers: ``offset=0`` is the newest ``limit``,
    but a growing thread can shift what a later ``offset`` lands on.

    ``fields="tldr"`` returns compact entries (id, from_agent, to,
    created_at, preview) instead of full messages, for a cheap skim before
    fetching one message in full via ``GET /api/v1/messages/{id}``.

    Raises KeyError / PermissionError.
    """
    with _lock:
        data = _read(repo_dir)
        meta = _thread_meta_locked(data, thread_id)
        if meta is None:
            raise KeyError(thread_id)
        threads = {k: v for k, v in data["threads"].items()}
        root = _find_locked(data, thread_id)
        if not _thread_visible(meta, root, agent_id=agent_id, role=role,
                               operator=operator, threads=threads):
            raise PermissionError("this thread is private; ask a member to add you")
        group = [m for m in data["messages"] if thread_key(m) == thread_id
                 and (operator or can_see(m, agent_id=agent_id, role=role, threads=threads))]
        members = [{"agent_id": aid, **info} for aid, info in (meta.get("members") or {}).items()]
        out = _public_meta(meta, agent_id=agent_id)
    if audience not in {"all", "me", "users"}:
        raise ValueError("audience must be all, me or users")
    original_ids = {m["id"] for m in group}
    full_group = group
    if context:
        if around_id is not None or offset is not None:
            raise ValueError("context uses before_id and after_id only")
        if any(bound is not None and bound not in original_ids for bound in (before_id, after_id)):
            raise ValueError("context anchor is not in this thread")
        if before_id is not None and after_id is not None and before_id <= after_id:
            raise ValueError("context bounds must be chronological")
        group = [m for m in group if (after_id is None or m["id"] > after_id)
                 and (before_id is None or m["id"] < before_id)]
        audience = "all"
        before_id, after_id = None, 0
    if audience != "all":
        def person(identity):
            return is_human((identity or "").removeprefix("agent:"))
        human_posts = {m["id"] for m in group if
                       (person(m.get("from_agent")) or m.get("from_role") == "user")
                       if audience == "users" or m["id"] in (sent_ids or set()) or
                       (viewer and m.get("from_agent") == viewer)}
        group = [m for m in group if m["id"] in human_posts or m.get("in_reply_to") in human_posts
                 or (person(m.get("to")) and (audience == "users" or
                     (m.get("to") or "").removeprefix("agent:") in {viewer, UI_USER, f"agent:{UI_USER}"} - {None}))]
    total = len(group)
    if sum(value is not None for value in (before_id, around_id, after_id, offset)) > 1:
        raise ValueError("choose one of before_id, around_id, after_id or offset")
    start_override = None
    if around_id is not None:
        target = next((i for i, m in enumerate(group) if m["id"] == around_id), None)
        if target is None:
            if audience == "all" or around_id not in original_ids:
                raise ValueError("anchor message is not in this thread")
            target = next((i for i, m in enumerate(group) if m["id"] >= around_id), max(0, total - 1))
        start_override = max(0, target - limit // 2)
        end = min(total, start_override + limit)
    elif after_id is not None:
        start_override = next((i for i, m in enumerate(group) if m["id"] > after_id), total)
        end = min(total, start_override + limit)
    elif before_id is not None:
        end = next((i for i, m in enumerate(group) if m["id"] >= before_id), total)
    elif offset is not None:
        end = max(total - offset, 0)
    else:
        end = total
    start = start_override if start_override is not None else max(end - limit, 0)
    window = group[start:end]
    next_before_id = window[0]["id"] if start > 0 and window else None
    offset_out = offset if offset is not None else max(total - end, 0)
    if audience != "all":
        positions = {m["id"]: i for i, m in enumerate(full_group)}
        gaps = []
        for index in range(start, min(end + 1, len(group) + 1)):
            previous = group[index - 1]["id"] if index > 0 else None
            following = group[index]["id"] if index < len(group) else None
            left = positions[previous] + 1 if previous is not None else 0
            right = positions[following] if following is not None else len(full_group)
            if right > left:
                gaps.append({"after_id": previous, "before_id": following, "count": right - left})
        out["gaps"] = gaps
    out.update({
        "members": members,
        "messages": [_compact_entry(m) for m in window] if fields == "tldr" else window,
        "total": total, "limit": limit, "offset": offset_out,
    })
    if next_before_id is not None:
        out["next_before_id"] = next_before_id
    if end < total and window:
        out["next_after_id"] = window[-1]["id"]
    return out


def find_thread_for_idea(repo_dir: Path, idea_id: int, kind: str = "suggestion") -> dict | None:
    with _lock:
        for meta in _read(repo_dir)["threads"].values():
            if meta.get("idea_id") == idea_id and meta.get("kind") == kind:
                return dict(meta)
    return None


def search_messages(
    repo_dir: Path, *, agent_id: str | None, role: str | None, operator: bool = False,
    q: str | None = None, from_agent: str | None = None, thread_id: int | None = None,
    ref: str | None = None, limit: int = 20, offset: int = 0,
) -> tuple[list[dict], int]:
    """Full-text search (all terms, case-insensitive) over visible messages,
    newest first. ``ref`` filters by reference handle, e.g. ``experiment:12.3``,
    ``idea:42`` or ``resource:reports/x.md``."""
    terms = [t for t in (q or "").lower().split() if t]
    ref_kind, _, ref_id = (ref or "").partition(":")
    with _lock:
        data = _read(repo_dir)
        msgs = list(data["messages"])
        threads = {k: v for k, v in data["threads"].items()}
    hits = []
    for m in reversed(msgs):
        if from_agent and m.get("from_agent") != from_agent:
            continue
        if thread_id is not None and thread_key(m) != thread_id:
            continue
        if ref and not any(
            r.get("kind") == ref_kind and str(r.get("id", r.get("path"))) == ref_id
            for r in m.get("references") or []
        ):
            continue
        body = f"{m.get('tldr') or ''}\n{m.get('text') or ''}"
        low = body.lower()
        if terms and not all(t in low for t in terms):
            continue
        if not (operator or can_see(m, agent_id=agent_id, role=role, threads=threads)):
            continue
        hits.append((m, body, low))
    page = []
    for m, body, low in hits[offset: offset + limit]:
        at = low.find(terms[0]) if terms else 0
        start = max(at - 60, 0)
        snippet = body[start: start + 200].replace("\n", " ").strip()
        page.append({
            "id": m["id"], "thread_id": thread_key(m), "from_agent": m.get("from_agent"),
            "from_role": m.get("from_role"), "to": m.get("to"), "created_at": m.get("created_at"),
            "tldr": m.get("tldr"), "references": m.get("references") or [],
            "snippet": ("…" if start else "") + snippet,
        })
    return page, len(hits)


def _experiment_mention_re(idea_id: int) -> re.Pattern:
    """``N.M`` / ``exp/N.M`` for idea N, plus shorthand sequences such as
    ``N.1/.2`` or ``N.1 / .2 / .3``; not inside longer numbers (``1N.1``,
    ``N.12`` for ``N.1``)."""
    return re.compile(rf"(?<![\w.]){idea_id}\.(\d+)((?:\s*/\s*\.\d+)*)(?![\d])")


def experiment_mentions(
    repo_dir: Path, idea_id: int, *, agent_id: str | None, role: str | None,
    operator: bool = False,
) -> dict[str, list[dict]]:
    """Where each experiment of *idea_id* is discussed: per experiment label
    (``"12.3"``), the visible threads whose messages mention it in text
    (``12.3``, ``exp/12.3``, ``12.1/.2``) or as a structured reference,
    most recently active first, with the first / last mentioning message."""
    pattern = _experiment_mention_re(idea_id)
    with _lock:
        data = _read(repo_dir)
        msgs = list(data["messages"])
        threads = dict(data["threads"])
        found: dict[str, dict[int, dict]] = {}
        for m in msgs:
            labels: set[str] = set()
            body = f"{m.get('tldr') or ''}\n{m.get('text') or ''}"
            for match in pattern.finditer(body):
                labels.add(f"{idea_id}.{match.group(1)}")
                for seq in re.findall(r"\.(\d+)", match.group(2) or ""):
                    labels.add(f"{idea_id}.{seq}")
            for r in m.get("references") or []:
                rid = str(r.get("id", ""))
                if r.get("kind") == "experiment" and rid.startswith(f"{idea_id}."):
                    labels.add(rid)
            if not labels:
                continue
            if not (operator or can_see(m, agent_id=agent_id, role=role, threads=threads)):
                continue
            tid = thread_key(m)
            for label in labels:
                per_thread = found.setdefault(label, {})
                entry = per_thread.get(tid)
                if entry is None:
                    meta = _thread_meta_locked(data, tid) or {}
                    entry = per_thread[tid] = {
                        "thread_id": tid, "title": meta.get("title") or f"Thread #{tid}",
                        "count": 0, "first_id": m["id"], "last_id": m["id"],
                        "last_at": m.get("created_at"),
                    }
                entry["count"] += 1
                entry["last_id"] = m["id"]
                entry["last_at"] = m.get("created_at")
    return {
        label: sorted(per_thread.values(), key=lambda e: e["last_id"], reverse=True)
        for label, per_thread in found.items()
    }


def thread_meta(repo_dir: Path, thread_id: int) -> dict | None:
    """Copy of a thread's metadata (stored or derived) plus its message count."""
    with _lock:
        data = _read(repo_dir)
        meta = _thread_meta_locked(data, thread_id)
        if meta is None:
            return None
        count = sum(1 for m in data["messages"] if thread_key(m) == thread_id)
        return {**meta, "members": dict(meta.get("members") or {}), "message_count": count}


def list_humans(repo_dir: Path, *, agent_id: str | None = None, role: str | None = None,
                operator: bool = True) -> list[dict]:
    """Distinct human identities seen as a sender, recipient, or thread member.

    Each entry is ``{"id": "user:david", "name": "david" | None,
    "last_active": "<iso>"}`` (``name`` is ``None`` for the catch-all
    ``"user"``), newest-active first. Lets the dashboard build name pills /
    filters and agents see whom they can address personally. A non-operator
    caller only learns from the messages and threads it can see (same rule
    as message reads), so private conversations don't leak who was active.
    """
    with _lock:
        data = _read(repo_dir)
        threads = dict(data["threads"])
        msgs = [m for m in data["messages"]
                if operator or can_see(m, agent_id=agent_id, role=role, threads=threads)]
        if not operator:
            by_id = {m.get("id"): m for m in data["messages"]}
            threads = {
                tid: meta for tid, meta in threads.items()
                if _thread_visible(meta, by_id.get(int(tid)), agent_id=agent_id,
                                   role=role, operator=False, threads=data["threads"])
            }
    last_active: dict[str, str] = {}

    def bump(identity: str | None, ts: str | None) -> None:
        if not identity or not is_human(identity) or not ts:
            return
        if ts > last_active.get(identity, ""):
            last_active[identity] = ts

    for m in msgs:
        ts = m.get("created_at")
        bump(m.get("from_agent"), ts)
        to = m.get("to") or ""
        bump(UI_USER if to == f"agent:{UI_USER}" else to, ts)
    for meta in threads.values():
        for member, info in (meta.get("members") or {}).items():
            bump(member, info.get("joined_at"))
    out = [
        {
            "id": identity,
            "name": identity[len(_HUMAN_PREFIX):] if identity != UI_USER else None,
            "last_active": ts,
        }
        for identity, ts in last_active.items()
    ]
    out.sort(key=lambda h: h["last_active"] or "", reverse=True)
    return out


def threads_digest(repo_dir: Path, *, agent_id: str | None, role: str | None, limit: int = 5) -> dict:
    """Catch-up block for registration / orient: open threads worth knowing about.

    Returns ``{}`` when there is nothing to show (or on any error), so callers
    can splat it into a response.
    """
    try:
        threads, total = list_threads(repo_dir, agent_id=agent_id, role=role, limit=limit)
    except Exception:  # noqa: BLE001
        return {}
    if not threads:
        return {}
    keep = ("id", "title", "kind", "idea_id", "summary", "message_count",
            "member_count", "joined", "last_activity", "last_message")
    return {
        "active_threads": [{k: t.get(k) for k in keep if t.get(k) is not None} for t in threads],
        "active_threads_total": total,
        "active_threads_hint": (
            "Ongoing discussions. Read one with GET /api/v1/threads/{id}, join with "
            "POST /api/v1/threads/{id}/join, find more with GET /api/v1/threads?q=<topic>."
        ),
    }
