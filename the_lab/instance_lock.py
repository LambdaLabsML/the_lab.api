"""Single-writer process lock for a The Lab instance.

This is a POSIX advisory ``flock``.  It is reliable for processes which see
the same lock inode on a local filesystem (and on NFS only when the NFS
server/client mounting it correctly implements POSIX locks).  It is not a
distributed-consensus primitive and must not be used to claim exclusivity
across arbitrary hosts or independently replicated filesystems.
"""
from __future__ import annotations

import errno
import os
from pathlib import Path

try:  # The server is POSIX-only today; keep import errors actionable.
    import fcntl
except ImportError:  # pragma: no cover - Windows has no flock
    fcntl = None


class InstanceLockError(RuntimeError):
    pass


class InstanceLock:
    """Hold an advisory lock without ever deleting/replacing its inode."""

    def __init__(self, repo_dir: Path):
        self.path = Path(repo_dir) / ".the_lab" / "instance.lock"
        self._fd: int | None = None

    def acquire(self) -> None:
        if self._fd is not None:
            return
        if fcntl is None:
            raise InstanceLockError("The Lab requires POSIX flock for writable mode")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise InstanceLockError(
                    f"Another writable The Lab server already owns {self.path}. "
                    "Stop that server before starting this one; do not delete the lock file."
                ) from exc
            raise InstanceLockError(f"Unable to lock {self.path}: {exc}") from exc
        # Helpful for an operator, but the lock itself is the authority.
        try:
            os.ftruncate(fd, 0)
            os.write(fd, f"pid={os.getpid()}\n".encode())
            os.fsync(fd)
        except OSError:
            pass
        self._fd = fd

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

