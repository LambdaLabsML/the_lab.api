"""WebSocket broadcaster: server-push event distribution.

Events flow: emitter (runner/store/messages) → broadcast() → per-subscriber queues → WebSocket.
Each event has a monotonic seq number. A ring buffer of 500 events lets reconnecting
clients replay missed events via ?since=N.

Persistence: when a journal is attached (app startup), every event is also
appended to ``.the_lab/events.jsonl`` by a background writer thread. The seq
counter continues across restarts (loaded from the journal tail) and
``replay_with_journal()`` serves ?since= values that predate the in-memory
ring — so a dashboard that slept through a deploy catches up instead of
resnapshotting, and the activity feed survives restarts.
"""
from __future__ import annotations

import asyncio
import json as _json
import os
import queue as _queue
import threading
import time
from collections import deque
from pathlib import Path
from typing import Iterator


class Broadcaster:
    """Distributes events to all connected WebSocket subscribers.

    Thread-safety:
      - ``broadcast()``      — call from async context (coroutines/tasks)
      - ``broadcast_soon()`` — call from sync threadpool threads (store/messages)
    """

    _RING_SIZE = 500
    _QUEUE_MAX = 100
    _JOURNAL_MAX_BYTES = 5 * 1024 * 1024  # rotate to .1 past this

    def __init__(self) -> None:
        self._seq = 0
        self._ring: deque[dict] = deque(maxlen=self._RING_SIZE)
        self._subscribers: list[asyncio.Queue] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._journal_path: Path | None = None
        self._journal_q: _queue.Queue | None = None

    # ── Journal ────────────────────────────────────────────────────────────

    def attach_journal(self, path: Path) -> None:
        """Enable persistence: continue seq from the journal tail, preload
        the ring with the most recent events, and start the writer thread.
        Call once at app startup, before traffic."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._journal_path = path
        last_events = self._journal_tail(self._RING_SIZE)
        if last_events:
            self._seq = max(self._seq, last_events[-1].get("seq", 0))
            self._ring.extend(last_events)
        self._journal_q = _queue.Queue()
        threading.Thread(target=self._journal_writer, daemon=True,
                         name="ws-journal").start()

    def _journal_files(self) -> list[Path]:
        """Journal files oldest-first (rotated .1 first, then current)."""
        assert self._journal_path is not None
        rotated = self._journal_path.with_suffix(".jsonl.1")
        return [p for p in (rotated, self._journal_path) if p.exists()]

    def _journal_tail(self, n: int) -> list[dict]:
        """Last *n* events from the journal (oldest-first). Fail-soft."""
        if self._journal_path is None:
            return []
        events: deque[dict] = deque(maxlen=n)
        try:
            for p in self._journal_files():
                with p.open() as fh:
                    for line in fh:
                        try:
                            ev = _json.loads(line)
                            if isinstance(ev, dict) and ev.get("seq"):
                                events.append(ev)
                        except ValueError:
                            continue
        except OSError:
            return []
        return list(events)

    def _journal_writer(self) -> None:
        """Background thread: drain the queue, append lines, rotate."""
        assert self._journal_path is not None and self._journal_q is not None
        path = self._journal_path
        fh = None
        try:
            while True:
                event = self._journal_q.get()
                try:
                    if fh is None:
                        fh = path.open("a")
                    fh.write(_json.dumps(event, default=str) + "\n")
                    fh.flush()
                    if fh.tell() > self._JOURNAL_MAX_BYTES:
                        fh.close()
                        fh = None
                        os.replace(path, path.with_suffix(".jsonl.1"))
                except (OSError, ValueError):
                    # Persistence is best-effort; never kill the writer.
                    try:
                        if fh is not None:
                            fh.close()
                    except OSError:
                        pass
                    fh = None
        except Exception:
            pass

    def replay_with_journal(self, seq: int) -> list[dict]:
        """Like replay_since, but falls back to reading the journal when the
        requested seq predates the in-memory ring (e.g. across a restart).
        Reads files — call from a thread, not the event loop."""
        ring = list(self._ring)
        if ring and seq >= ring[0].get("seq", 0) - 1:
            return [e for e in ring if e.get("seq", 0) > seq]
        if self._journal_path is None:
            return [e for e in ring if e.get("seq", 0) > seq]
        out: list[dict] = []
        try:
            for p in self._journal_files():
                with p.open() as fh:
                    for line in fh:
                        try:
                            ev = _json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(ev, dict) and ev.get("seq", 0) > seq:
                            out.append(ev)
        except OSError:
            pass
        # Ring may hold events newer than the last journaled line (writer lag).
        last = out[-1]["seq"] if out else seq
        out.extend(e for e in ring if e.get("seq", 0) > last)
        return out

    def capture_loop(self) -> None:
        """Adopt the current running loop (call from app startup) so
        broadcast_soon() works before any WS subscriber has connected."""
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            pass

    def _get_loop(self) -> asyncio.AbstractEventLoop | None:
        if self._loop is not None:
            return self._loop
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            pass
        return self._loop

    def broadcast(self, event: dict) -> None:
        """Stamp seq, append to ring buffer, enqueue to all subscribers.

        Must be called from the asyncio event loop (async context).
        Overflow protection: if a subscriber's queue exceeds _QUEUE_MAX,
        the oldest item is dropped before enqueuing the new one.
        """
        event = _enrich(event)
        self._seq += 1
        # Stamp the REAL event time: reconnecting clients replay the ring
        # buffer in one burst, so arrival time is meaningless for ages.
        event = {**event, "seq": self._seq, "ts": time.time()}
        self._ring.append(event)
        if self._journal_q is not None:
            self._journal_q.put(event)  # persisted off-loop by the writer thread
        # Cache the loop reference the first time broadcast() is called
        # from inside the loop (so broadcast_soon can use it later).
        self._get_loop()
        for q in list(self._subscribers):
            if q.qsize() >= self._QUEUE_MAX:
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def broadcast_soon(self, event: dict) -> None:
        """Thread-safe broadcast for sync callers (store/messages threadpool).

        Schedules ``broadcast()`` on the running event loop via
        ``call_soon_threadsafe``.  If no loop is known yet, the call is
        silently dropped (no subscribers can exist yet either).
        """
        loop = self._loop
        if loop is None:
            # Try to find the loop one more time (belt-and-suspenders).
            try:
                loop = asyncio.get_event_loop()
                if not loop.is_running():
                    loop = None
            except RuntimeError:
                loop = None
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(self.broadcast, event)
        except RuntimeError:
            pass

    def subscribe(self) -> asyncio.Queue:
        """Register a new subscriber queue and return it."""
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.append(q)
        # Capture the loop reference while we're definitely in async context.
        self._get_loop()
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        """Remove a subscriber queue."""
        try:
            self._subscribers.remove(q)
        except ValueError:
            pass

    def replay_since(self, seq: int) -> list[dict]:
        """Return all buffered events with seq > given value (in order)."""
        return [e for e in self._ring if e.get("seq", 0) > seq]


# ── Event entity payloads ─────────────────────────────────────────────────────
# Events carry the changed entity (trimmed) so clients can PATCH their state
# instead of refetching whole collections on every doorbell. Shapes mirror the
# corresponding REST rows; keep them small — every subscriber gets every event.

def experiment_payload(exp: dict | None) -> dict | None:
    """Trimmed experiment row for experiment_* events (chart-row shape)."""
    if not exp:
        return None
    desc = exp.get("description") or ""
    if len(desc) > 160:
        desc = desc[:157] + "..."
    metrics = exp.get("metrics")
    if isinstance(metrics, dict):
        metrics = {k: v for k, v in metrics.items()
                   if isinstance(v, (int, float)) and not isinstance(v, bool)}
    else:
        metrics = {}
    return {
        "id": exp.get("id"),
        "label": exp.get("label"),
        "idea_id": exp.get("idea_id"),
        "seq": exp.get("seq"),
        "status": exp.get("status"),
        "status_msg": exp.get("status_msg"),
        "description": desc or None,
        "metrics": metrics,
        "tags": exp.get("tags") or [],
        "created_at": exp.get("created_at"),
        "started_at": exp.get("started_at"),
        "finished_at": exp.get("finished_at"),
        "runtime": exp.get("runtime"),
    }


def idea_payload(idea: dict | None) -> dict | None:
    """Trimmed idea row for idea_changed events (graph-node shape)."""
    if not idea:
        return None
    return {
        "id": idea.get("id"),
        "description": idea.get("description"),
        "status": idea.get("status"),
        "source": idea.get("source"),
        "priority": idea.get("priority"),
        "branch": idea.get("branch"),
        "conclusion": idea.get("conclusion"),
        "created_at": idea.get("created_at"),
        "parent_ids": idea.get("parent_ids") or [],
    }


_EXP_EVENT_TYPES = frozenset({
    "experiment_queued", "experiment_started",
    "experiment_finished", "experiment_cancelled",
})


def _enrich(event: dict) -> dict:
    """Attach the changed entity to known event types, centrally.

    Every emit site (present and future) gets a patchable payload for free:
    experiment_* events gain ``experiment`` (trimmed row), idea_changed gains
    ``idea``, queue_changed gains queued/running counts. Lookups hit the
    in-memory store dicts — no disk. Fail-soft: enrichment never blocks an
    event. Runs on the event loop right before seq-stamping, so the payload
    reflects the record AFTER the mutation that triggered the event.
    """
    etype = event.get("type")
    try:
        if etype in _EXP_EVENT_TYPES and "experiment" not in event:
            from . import deps
            store = getattr(deps, "store", None)
            exp = store.get_experiment(event.get("label")) if store else None
            if exp:
                event = {**event, "experiment": experiment_payload(exp)}
        elif etype == "idea_changed" and "idea" not in event:
            from . import deps
            store = getattr(deps, "store", None)
            idea = store.get_idea(event.get("idea_id")) if store else None
            if idea:
                event = {**event, "idea": idea_payload(idea)}
        elif etype == "queue_changed" and "queued" not in event:
            from . import deps
            store = getattr(deps, "store", None)
            if store:
                event = {
                    **event,
                    "queued": len(store.list_experiments_by_status("queued")),
                    "running": len(store.list_experiments_by_status("running")),
                }
    except Exception:
        pass
    return event


# Module-level singleton used by all callers.
broadcaster = Broadcaster()

# N1: the live "listening agents" registry (formerly used by
# build_notifications to SUPPRESS piggybacked _notifications for an agent whose
# messages WebSocket was open) has been removed. Cross-channel dedup now rests
# on claim-on-delivery (messages.claim_unread_for) + read/seen state, so there
# is no need to track socket presence for suppression.
