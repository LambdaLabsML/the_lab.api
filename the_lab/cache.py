"""Tiny response cache for read endpoints, invalidated by Store.version.

Cache entries are keyed by (endpoint_key, query_params, store.version). Any
write in the Store bumps its version counter, so cache entries for the old
version become unreachable (and eventually evicted by FIFO).

Designed for the handful of hot dashboard endpoints (ideas, chart-data,
graph, backlog) that re-serialize the same JSON 2-3x/s while the underlying
data is static.

Also supports in-flight request coalescing: if N concurrent callers miss on
the same key, only the first runs the wrapped function; the others wait on
a threading.Event and share the result. This matters for synchronous FastAPI
routes (``def`` rather than ``async def``) dispatched on the threadpool when
many requests race to the handler on page load.
"""
from __future__ import annotations

import threading
from functools import wraps
from typing import Any, Callable

_cache: dict[tuple, Any] = {}
_lock = threading.Lock()
_MAX = 64  # FIFO-ish cap — small because each entry can be several MB

# In-flight coalescing state. Keys are the full cache key tuple. Each entry
# holds a threading.Event that fires once the first caller has populated
# _inflight_results (or recorded an exception). Late arrivals wait on the
# event and read the result rather than recomputing.
_inflight: dict[tuple, threading.Event] = {}
_inflight_results: dict[tuple, Any] = {}
_inflight_errors: dict[tuple, BaseException] = {}


def clear() -> None:
    """Drop all cached entries. Used in tests."""
    with _lock:
        _cache.clear()
        # Also clear coalescing state — any waiters will see missing results
        # and fall through, which is fine for tests.
        _inflight.clear()
        _inflight_results.clear()
        _inflight_errors.clear()


def size() -> int:
    with _lock:
        return len(_cache)


def cached_response(key_fn: Callable[..., tuple]):
    """Cache a route's return value, keyed by key_fn(*args, **kwargs) + store.version.

    The wrapped function is called only on a miss. On hit the cached value is
    returned directly — callers must not mutate it. ``key_fn`` receives the
    same args/kwargs as the route and returns a hashable tuple.

    Concurrent misses with the same key are coalesced: the first caller runs
    the wrapped function, and all other waiters share its result.
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

            # Fast path + coalescing decision, all under a single lock
            event: threading.Event | None = None
            is_leader = False
            with _lock:
                if key in _cache:
                    return _cache[key]
                existing = _inflight.get(key)
                if existing is not None:
                    event = existing
                else:
                    event = threading.Event()
                    _inflight[key] = event
                    is_leader = True

            if not is_leader:
                # Follower: wait for the leader to finish, then read its result.
                event.wait()
                with _lock:
                    if key in _inflight_results:
                        return _inflight_results[key]
                    err = _inflight_errors.get(key)
                if err is not None:
                    # Re-raise the same exception the leader hit so followers
                    # see the same failure rather than silently falling through.
                    raise err
                # Fallback: result was cleaned up before we read it. Recompute
                # directly — no coalescing this time, but correctness preserved.
                return fn(*args, **kwargs)

            # Leader path: run the function outside the lock, then publish.
            try:
                result = fn(*args, **kwargs)
            except BaseException as e:
                with _lock:
                    _inflight_errors[key] = e
                    _inflight.pop(key, None)
                event.set()
                # Schedule cleanup of the error record on next lock acquisition
                # (followers may still be racing to read it). We defer with a
                # tiny helper thread to avoid blocking the caller.
                def _cleanup_err():
                    with _lock:
                        _inflight_errors.pop(key, None)
                threading.Timer(0.1, _cleanup_err).start()
                raise

            with _lock:
                if len(_cache) >= _MAX:
                    # FIFO eviction — dicts preserve insertion order in 3.7+
                    try:
                        _cache.pop(next(iter(_cache)))
                    except StopIteration:
                        pass
                _cache[key] = result
                _inflight_results[key] = result
                _inflight.pop(key, None)
            event.set()
            # Clean up the shared-result record shortly after — followers that
            # were already blocked on .wait() will have read it; future callers
            # hit _cache instead. Using a timer keeps the caller off the hook.
            def _cleanup_result():
                with _lock:
                    _inflight_results.pop(key, None)
            threading.Timer(0.5, _cleanup_result).start()
            return result
        return wrapper
    return deco
