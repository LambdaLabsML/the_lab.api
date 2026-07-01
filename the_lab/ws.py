"""WebSocket broadcaster: server-push event distribution.

Events flow: emitter (runner/store/messages) → broadcast() → per-subscriber queues → WebSocket.
Each event has a monotonic seq number. A ring buffer of 500 events lets reconnecting
clients replay missed events via ?since=N.
"""
from __future__ import annotations

import asyncio
from collections import deque
from typing import Iterator


class Broadcaster:
    """Distributes events to all connected WebSocket subscribers.

    Thread-safety:
      - ``broadcast()``      — call from async context (coroutines/tasks)
      - ``broadcast_soon()`` — call from sync threadpool threads (store/messages)
    """

    _RING_SIZE = 500
    _QUEUE_MAX = 100

    def __init__(self) -> None:
        self._seq = 0
        self._ring: deque[dict] = deque(maxlen=self._RING_SIZE)
        self._subscribers: list[asyncio.Queue] = []
        self._loop: asyncio.AbstractEventLoop | None = None

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
        self._seq += 1
        event = {**event, "seq": self._seq}
        self._ring.append(event)
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


# Module-level singleton used by all callers.
broadcaster = Broadcaster()


# ---------------------------------------------------------------------------
# Live "listening agents" registry
#
# Authoritative record of which agents currently hold an open
# ``/api/v1/messages/ws`` connection. Distinct from the 20-second
# ``agents.is_listening`` poll heuristic: this is exact, updated on
# connect/disconnect, and used by ``build_notifications`` to SUPPRESS the
# piggy-backed ``_notifications`` for an agent that is receiving them live over
# its WebSocket instead.
#
# Concurrency: mutated only from the asyncio event loop (WS connect/disconnect
# handlers) and read from ``build_notifications`` which also runs in the async
# request path. asyncio is single-threaded so a plain set is race-free for
# add/discard/membership. We still take a lock and copy on the (rare) full-read
# path to stay correct if a future caller reads it from a threadpool worker —
# membership tests and mutations stay allocation-free on the hot path.
# ---------------------------------------------------------------------------

import threading as _threading

_listening: set[str] = set()
_listening_lock = _threading.Lock()


class _ListeningAgents:
    """Set-like registry of agents with a live messages WebSocket open.

    An agent may open more than one listener (reconnect races, multiple
    windows), so we reference-count per agent id and only consider it "not
    listening" once every connection has been discarded.
    """

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}
        self._lock = _threading.Lock()

    def add(self, agent_id: str) -> None:
        if not agent_id:
            return
        with self._lock:
            self._counts[agent_id] = self._counts.get(agent_id, 0) + 1

    def discard(self, agent_id: str) -> None:
        if not agent_id:
            return
        with self._lock:
            n = self._counts.get(agent_id, 0) - 1
            if n <= 0:
                self._counts.pop(agent_id, None)
            else:
                self._counts[agent_id] = n

    def is_listening(self, agent_id: str | None) -> bool:
        if not agent_id:
            return False
        # dict membership is atomic under the GIL; no lock needed for a read.
        return agent_id in self._counts

    def ids(self) -> list[str]:
        with self._lock:
            return list(self._counts)


# Module-level singleton: authoritative live-listener registry.
listening_agents = _ListeningAgents()
