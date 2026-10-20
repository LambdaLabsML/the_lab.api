"""Atomic, persistent ownership of answering individual messages.

Separate from delivery/read state and from conversation permissions. Always
reload under a file lock so concurrent threads and server processes agree.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
from pathlib import Path
import threading
from . import jsonio

_lock = threading.Lock()


@contextmanager
def _locked(repo_dir: Path):
    root = repo_dir / ".the_lab"
    root.mkdir(parents=True, exist_ok=True)
    with _lock, (root / "message_claims.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            path = root / "message_claims.json"
            yield path, jsonio.read_json(path)
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _visible(record: dict | None, now: datetime) -> dict | None:
    if not record or record.get("status") == "released":
        return None
    if record.get("status") == "active" and datetime.fromisoformat(record["expires_at"]) <= now:
        return None
    return dict(record)


def list_claims(repo_dir: Path) -> dict[str, dict]:
    """Current active/completed claims. Reading does not renew a lease."""
    now = datetime.now(timezone.utc)
    with _locked(repo_dir) as (_, data):
        return {key: current for key, value in data.items() if (current := _visible(value, now))}


def change_claim(repo_dir: Path, message_id: int, *, owner: str, action: str = "acquire",
                 ttl_seconds: int = 3600, result_message_id: int | None = None,
                 operator: bool = False) -> dict:
    """First owner wins. Only the owner can renew/complete; an operator can release.

    Acquiring one's existing claim is idempotent and does not extend its lease.
    Completion keeps the answer owned until explicitly released, avoiding a
    second answer when an agent encounters an already-finished question later.
    """
    if action not in ("acquire", "renew", "release", "complete"):
        raise ValueError("unknown claim action")
    now = datetime.now(timezone.utc)
    key = str(message_id)
    with _locked(repo_dir) as (path, data):
        current = _visible(data.get(key), now)
        if current and current["owner"] != owner:
            if action != "release" or not operator:
                return {"acquired": False, "changed": False, "claim": current}
        if action == "acquire" and current:
            return {"acquired": current["status"] != "completed", "changed": False, "claim": current}
        if action != "acquire" and current is None:
            return {"acquired": False, "changed": False, "claim": None}
        if action == "renew" and current["status"] == "completed":
            return {"acquired": current["status"] != "completed", "changed": False, "claim": current}
        record = current or {"message_id": message_id, "owner": owner, "claimed_at": now.isoformat()}
        record.update(status="released" if action == "release" else "completed" if action == "complete" else "active",
                      expires_at=(now + timedelta(seconds=ttl_seconds)).isoformat() if action in ("acquire", "renew") else None)
        if action == "complete":
            record["result_message_id"] = result_message_id
            record["completed_at"] = now.isoformat()
        data[key] = record
        # Synchronous atomic persistence before reporting acquisition.
        jsonio.write_json(path, data)
        return {"acquired": action != "release", "changed": True, "claim": dict(record)}
