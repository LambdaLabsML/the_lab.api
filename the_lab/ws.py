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
