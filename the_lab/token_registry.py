"""Per-experiment bearer tokens issued by the runner.

The runner creates a random hex token for each experiment and injects it as
THE_LAB_TOKEN into the experiment environment.  Experiment scripts (and their
preamble) can authenticate API calls with::

    Authorization: Bearer <THE_LAB_TOKEN>

without needing the admin Basic-auth credentials, which should never leak
into experiment processes (which run agent code that could exfiltrate them).

Lifecycle:
    1. Runner calls ``register(token)`` before launching the script.
    2. Script runs, makes authenticated API calls using the token.
    3. Runner calls ``unregister(token)`` after the process exits.

Tokens are in-memory only — a server restart clears the registry.  Running
experiments would need to re-register their tokens, but in practice a server
restart terminates in-flight experiments anyway.
"""
from __future__ import annotations

import threading

_lock = threading.Lock()
_active: set[str] = set()


def register(token: str) -> None:
    """Add a token to the active set (called by the runner at launch time)."""
    with _lock:
        _active.add(token)


def unregister(token: str) -> None:
    """Remove a token from the active set (called by the runner on exit)."""
    with _lock:
        _active.discard(token)


def is_valid(token: str) -> bool:
    """Return True when the token is currently registered."""
    if not token:
        return False
    with _lock:
        return token in _active
