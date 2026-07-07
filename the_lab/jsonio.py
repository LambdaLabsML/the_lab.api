"""Atomic JSON file I/O — the single persistence choke point.

Every JSON state file (store records, messages, agent registry, stats,
queue config, notification seen-store) is written through here. The old
pattern — ``path.write_text(...)`` — truncates in place, so a concurrent
reader (another thread, the CLI, or an agent worktree sharing ``.the_lab/``
over NFS) could observe a half-written file. ``os.replace`` of a temp file
in the same directory is atomic on POSIX *and* on NFS: readers see either
the old or the new file, never a torn one.

fsync is optional (off by default): the durability concern here is crash
consistency of a research journal, not a transaction log, and per-write
fsync on NFS costs a full round-trip. ``os.replace`` alone already
guarantees the file is never torn.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def read_json(path: Path, default=None):
    """Load JSON from *path*; return *default* if the file is missing."""
    if path.exists():
        return json.loads(path.read_text())
    return {} if default is None else default


def write_json(path: Path, data, *, indent: int = 2, sort_keys: bool = False,
               fsync: bool = False) -> None:
    """Atomically write *data* as JSON to *path* (temp file + os.replace)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=indent, sort_keys=sort_keys) + "\n"
    fd, tmp = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
            if fsync:
                fh.flush()
                os.fsync(fh.fileno())
        # mkstemp creates 0600; match the 0644 that write_text produced so
        # sibling processes sharing .the_lab/ keep read access.
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def append_line(path: Path, line: str) -> None:
    """Append one line to *path* (creates parents). Not atomic across
    writers, but appends of a single line are effectively so on POSIX; used
    for line-oriented sidecars (heartbeats, event journal)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(line.rstrip("\n") + "\n")
