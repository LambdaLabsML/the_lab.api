"""Bounded off-loop execution for filesystem and subprocess operations.

Keep mutations synchronous inside the worker: their existing store lock orders
disk writes and cache publication. Callers await completion before acknowledging
a write. Slow NFS operations cannot occupy the HTTP/WebSocket event loop.
"""
from __future__ import annotations

import asyncio
import functools
import os
import weakref

import anyio

_limiters: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


async def run_blocking(func, /, *args, **kwargs):
    loop = asyncio.get_running_loop()
    limiter = _limiters.get(loop)
    if limiter is None:
        limiter = anyio.CapacityLimiter(max(1, int(os.getenv("THE_LAB_IO_WORKERS", "8"))))
        _limiters[loop] = limiter
    return await anyio.to_thread.run_sync(
        functools.partial(func, *args, **kwargs), limiter=limiter,
    )
