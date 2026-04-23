"""Tiny response cache for read endpoints, invalidated by Store.version.

Cache entries are keyed by (endpoint_key, query_params, store.version). Any
write in the Store bumps its version counter, so cache entries for the old
version become unreachable (and eventually evicted by FIFO).

Designed for the handful of hot dashboard endpoints (ideas, chart-data,
graph, backlog) that re-serialize the same JSON 2-3x/s while the underlying
data is static.
"""
from __future__ import annotations

import threading
from functools import wraps
from typing import Any, Callable

_cache: dict[tuple, Any] = {}
_lock = threading.Lock()
_MAX = 64  # FIFO-ish cap — small because each entry can be several MB


def clear() -> None:
    """Drop all cached entries. Used in tests."""
    with _lock:
        _cache.clear()


def size() -> int:
    with _lock:
        return len(_cache)


def cached_response(key_fn: Callable[..., tuple]):
    """Cache a route's return value, keyed by key_fn(*args, **kwargs) + store.version.

    The wrapped function is called only on a miss. On hit the cached value is
    returned directly — callers must not mutate it. ``key_fn`` receives the
    same args/kwargs as the route and returns a hashable tuple.
    """
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            # Local import: avoids a circular import at module load time
            # (deps is initialised from app.py, which imports route modules).
            from . import deps
            store = deps.store
            version = store.version if store is not None else 0
            key = (fn.__name__, key_fn(*args, **kwargs), version)
            with _lock:
                if key in _cache:
                    return _cache[key]
            result = fn(*args, **kwargs)
            with _lock:
                if len(_cache) >= _MAX:
                    # FIFO eviction — dicts preserve insertion order in 3.7+
                    try:
                        _cache.pop(next(iter(_cache)))
                    except StopIteration:
                        pass
                _cache[key] = result
            return result
        return wrapper
    return deco
