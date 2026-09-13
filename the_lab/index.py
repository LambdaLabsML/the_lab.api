"""Disposable SQLite projections of The Lab's filesystem state.

This module deliberately never writes ``.the_lab``.  JSON files remain the
source of truth; an index can be deleted at any time and rebuilt by scanning
those files.  Callers must treat :class:`IndexUnavailable` as a cache miss and
continue with their normal filesystem/in-memory implementation.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import uuid
from typing import Any, Iterator

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 4
_NETWORK_FILESYSTEMS = {"nfs", "nfs4", "cifs", "smbfs", "sshfs", "fuse.sshfs", "9p", "afs"}


class IndexUnavailable(RuntimeError):
    """The derived index cannot safely answer a query.

    This exception is intentionally distinct from a missing record.  A route
    should catch it, log the diagnostic, and use its existing Store data.
    """


class IndexCorrupt(IndexUnavailable):
    """The on-disk derived database is unreadable or from another schema."""


class IndexBusy(IndexUnavailable):
    """A bounded lock wait expired; do not repair or quarantine anything."""


class FilesystemScanError(IndexUnavailable):
    """An authoritative scan was incomplete, so it cannot replace the cache."""


def _mount_filesystem(path: Path) -> str | None:
    """Return Linux mount type for *path*, without following a DB into use.

    ``statvfs`` does not expose the filesystem magic on all supported Python
    versions.  Linux mountinfo does and also handles bind mounts correctly.
    Unknown platforms return ``None`` (the default is still local /var/tmp).
    """
    mountinfo = Path("/proc/self/mountinfo")
    if not mountinfo.exists():
        return None
    try:
        target = path.resolve(strict=False)
        best: tuple[int, str] | None = None
        for line in mountinfo.read_text().splitlines():
            fields = line.split()
            if "-" not in fields or len(fields) < 7:
                continue
            dash = fields.index("-")
            mount = Path(fields[4].replace("\\040", " "))
            if mount == Path("/") or mount == target or mount in target.parents:
                candidate = (len(str(mount)), fields[dash + 1])
                if best is None or candidate[0] > best[0]:
                    best = candidate
        return best[1].lower() if best else None
    except (OSError, ValueError, IndexError):
        return None


def is_network_filesystem(path: Path) -> bool:
    """Whether SQLite WAL is unsafe at *path* according to mountinfo."""
    fs_type = _mount_filesystem(path)
    return fs_type in _NETWORK_FILESYSTEMS if fs_type else False


def index_path_for(repo_dir: Path, instance_id: str, index_dir: str | Path | None = None) -> Path:
    """Choose a machine-local derived-cache location, never under the repo."""
    configured = index_dir or os.getenv("THE_LAB_INDEX_DIR")
    root = Path(configured).expanduser() if configured else Path("/var/tmp") / f"the-lab-{os.getuid()}"
    canonical = str(repo_dir.resolve(strict=False)) + "\0" + str(instance_id)
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    return root / digest / "index.sqlite3"


def _read_json(path: Path) -> dict[str, Any] | list[Any]:
    try:
        parsed = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise FilesystemScanError(f"cannot read authoritative JSON {path}: {exc}") from exc
    if not isinstance(parsed, (dict, list)):
        raise FilesystemScanError(f"authoritative JSON {path} is not an object or array")
    return parsed


def scan_filesystem(repo_dir: Path) -> dict[str, list[dict[str, Any]]]:
    """Read authoritative idea/experiment JSON without constructing Store.

    In particular this function creates no instance ID, gitignore, directory,
    or other repository file.  Broken individual JSON documents are skipped so
    a later repair does not make the complete cache unusable.
    """
    ideas: list[dict[str, Any]] = []
    experiments: list[dict[str, Any]] = []
    lab = Path(repo_dir) / ".the_lab" / "experiments"
    if not lab.exists():
        return {"ideas": ideas, "experiments": experiments}
    try:
        idea_dirs = list(lab.iterdir())
    except OSError as exc:
        raise FilesystemScanError(f"cannot enumerate authoritative directory {lab}: {exc}") from exc
    for directory in idea_dirs:
        if not directory.is_dir() or not directory.name.isdigit():
            continue
        idea_id = int(directory.name)
        idea_path = directory / "idea.json"
        if idea_path.exists():
            idea = _read_json(idea_path)
            if not isinstance(idea, dict):
                raise FilesystemScanError(f"authoritative idea is not an object: {idea_path}")
            ideas.append({**idea, "id": idea.get("id", idea_id)})
        labels: set[str] = set()
        try:
            children = list(directory.iterdir())
        except OSError as exc:
            raise FilesystemScanError(f"cannot enumerate authoritative directory {directory}: {exc}") from exc
        for child in children:
            if child.is_dir() and child.name.isdigit():
                exp_path = child / "experiment.json"
                if not exp_path.exists():
                    continue
                raw = _read_json(exp_path)
                seq = int(child.name)
                if not isinstance(raw, dict):
                    raise FilesystemScanError(f"authoritative experiment is not an object: {exp_path}")
                label = f"{idea_id}.{seq}"
                experiments.append({**raw, "id": label, "label": label, "seq": seq,
                                    "idea_id": idea_id,
                                    "script": f".the_lab/experiments/{idea_id}/{seq}/script.sh"})
                labels.add(label)
        for child in children:
            if not child.is_file() or child.suffix != ".json" or not child.stem.isdigit():
                continue
            seq = int(child.stem)
            label = f"{idea_id}.{seq}"
            if label in labels:
                continue
            raw = _read_json(child)
            if not isinstance(raw, dict):
                raise FilesystemScanError(f"authoritative experiment is not an object: {child}")
            experiments.append({**raw, "id": label, "label": label, "seq": seq,
                                "idea_id": idea_id,
                                "script": f".the_lab/experiments/{idea_id}/{seq}.sh"})
    return {"ideas": ideas, "experiments": experiments}


def numeric_metric_leaves(metrics: Any) -> dict[str, float]:
    """Chart-compatible finite numeric leaves with deterministic collisions.

    Top-level literal keys win over a dotted key derived from a nested object;
    this prevents duplicate ``metric_values`` primary keys and is independent
    of JSON object key order.
    """
    if not isinstance(metrics, dict):
        return {}
    def finite(value: Any) -> float | None:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return None
        try:
            converted = float(value)
        except (OverflowError, ValueError):
            return None
        return converted if math.isfinite(converted) else None
    out: dict[str, float] = {}
    for key, value in sorted(metrics.items(), key=lambda item: str(item[0])):
        converted = finite(value)
        if converted is not None:
            out[str(key)] = converted
    for key, value in sorted(metrics.items(), key=lambda item: str(item[0])):
        if not isinstance(value, dict):
            continue
        for child, leaf in sorted(value.items(), key=lambda item: str(item[0])):
            name = f"{key}.{child}"
            converted = finite(leaf)
            if converted is not None:
                out.setdefault(name, converted)
            elif isinstance(leaf, dict):
                # Preserve the legacy root→dict→dict flatten depth (a.b.c),
                # while declining any deeper per-game/object expansion.
                for grandchild, deep_leaf in sorted(leaf.items(), key=lambda item: str(item[0])):
                    deep_name = f"{name}.{grandchild}"
                    converted = finite(deep_leaf)
                    if converted is not None:
                        out.setdefault(deep_name, converted)
    return dict(sorted(out.items()))


class FilesystemIndex:
    """A thread/process-safe, disposable SQL projection for one lab instance."""

    def __init__(self, repo_dir: Path | str, instance_id: str, *, index_dir: str | Path | None = None,
                 enabled: bool | None = None, lock_timeout: float = 2.0):
        self.repo_dir = Path(repo_dir).resolve(strict=False)
        self.instance_id = str(instance_id)
        self.path = index_path_for(self.repo_dir, self.instance_id, index_dir)
        self.lock_timeout = lock_timeout
        self.enabled = (os.getenv("THE_LAB_INDEX", "1").lower() not in {"0", "false", "off", "no"}
                        if enabled is None else enabled)
        self._lock = threading.RLock()
        self.last_error: str | None = None
        self.dirty = False
        if self.enabled and is_network_filesystem(self.path.parent):
            self.enabled = False
            self.last_error = (f"refusing SQLite WAL on network filesystem {_mount_filesystem(self.path.parent)!r}: "
                               f"{self.path.parent}; set THE_LAB_INDEX_DIR to a local disk")
            logger.warning("%s", self.last_error)

    @property
    def available(self) -> bool:
        return self.enabled and self.last_error is None

    def mark_dirty(self, reason: str) -> None:
        """Fail closed after a post-filesystem derived-write failure."""
        with self._lock:
            self.dirty = True
            self.last_error = reason

    def _unavailable(self, message: str, exc: BaseException | None = None) -> IndexUnavailable:
        detail = f"{message}: {exc}" if exc else message
        self.last_error = detail
        logger.warning("SQLite index unavailable (%s)", detail)
        return IndexUnavailable(detail)

    @contextlib.contextmanager
    def _process_lock(self, exclusive: bool = True) -> Iterator[None]:
        """Bounded advisory lock shared by rebuilds and incremental writers."""
        if not self.enabled:
            raise self._unavailable(self.last_error or "SQLite index is disabled")
        try:
            import fcntl
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            # Cache payloads include the original experiment JSON (which can
            # contain run credentials in meta); do not rely solely on umask.
            with contextlib.suppress(OSError):
                os.chmod(self.path.parent, 0o700)
            with open(str(self.path) + ".lock", "a+", encoding="utf-8") as handle:
                with contextlib.suppress(OSError):
                    os.chmod(handle.name, 0o600)
                end = time.monotonic() + self.lock_timeout
                flag = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
                while True:
                    try:
                        fcntl.flock(handle.fileno(), flag | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= end:
                            self.last_error = "timed out waiting for SQLite index lock"
                            raise IndexBusy(self.last_error)
                        time.sleep(0.02)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except IndexUnavailable:
            raise
        except OSError as exc:
            raise self._unavailable("cannot lock SQLite index", exc) from exc

    def _connect(self, path: Path | None = None) -> sqlite3.Connection:
        target = path or self.path
        try:
            conn = sqlite3.connect(target, timeout=self.lock_timeout, isolation_level=None,
                                   check_same_thread=False)
            conn.execute(f"PRAGMA busy_timeout = {max(1, int(self.lock_timeout * 1000))}")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            return conn
        except sqlite3.Error as exc:
            message = str(exc).lower()
            if "not a database" in message or "malformed" in message or "file is encrypted" in message:
                self.last_error = f"cannot open corrupt SQLite index: {exc}"
                raise IndexCorrupt(self.last_error) from exc
            raise self._unavailable("cannot open SQLite index", exc) from exc

    def _create_schema(self, conn: sqlite3.Connection, revision: int = 0) -> None:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS records (
              kind TEXT NOT NULL, record_id TEXT NOT NULL, idea_id INTEGER,
              status TEXT, created_at TEXT, updated_at TEXT, started_at TEXT, finished_at TEXT,
              description TEXT, runtime TEXT, tags TEXT, has_metrics INTEGER NOT NULL,
              payload TEXT NOT NULL, PRIMARY KEY(kind, record_id)
            );
            CREATE INDEX IF NOT EXISTS records_kind_created ON records(kind, created_at DESC, record_id DESC);
            CREATE INDEX IF NOT EXISTS records_kind_status_created ON records(kind, status, created_at DESC, record_id DESC);
            CREATE INDEX IF NOT EXISTS records_idea_created ON records(idea_id, created_at DESC, record_id DESC);
            CREATE TABLE IF NOT EXISTS metric_values (
              kind TEXT NOT NULL, record_id TEXT NOT NULL, metric TEXT NOT NULL, value REAL NOT NULL,
              PRIMARY KEY(kind, record_id, metric),
              FOREIGN KEY(kind, record_id) REFERENCES records(kind, record_id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS metric_lookup ON metric_values(kind, metric, value);
        """)
        values = {"schema_version": str(SCHEMA_VERSION), "repo_identity": str(self.repo_dir),
                  "instance_id": self.instance_id, "revision": str(revision)}
        for key, value in values.items():
            conn.execute("INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)", (key, value))

    def _validate(self, conn: sqlite3.Connection, required_revision: int | None = None,
                  allow_dirty: bool = False) -> None:
        try:
            meta = dict(conn.execute("SELECT key, value FROM metadata").fetchall())
            if meta.get("schema_version") != str(SCHEMA_VERSION):
                self.last_error = "SQLite index schema mismatch; rebuild required"
                raise IndexCorrupt(self.last_error)
            if meta.get("repo_identity") != str(self.repo_dir) or meta.get("instance_id") != self.instance_id:
                self.last_error = "SQLite index belongs to a different repository or instance"
                raise IndexCorrupt(self.last_error)
            if self.dirty and not allow_dirty:
                self.last_error = "SQLite index is dirty after a failed post-filesystem update; reconciliation required"
                raise IndexUnavailable(self.last_error)
            if required_revision is not None and int(meta.get("revision", "-1")) < required_revision:
                self.last_error = "SQLite index revision trails authoritative Store; reconciliation required"
                raise IndexUnavailable(self.last_error)
        except (sqlite3.Error, ValueError) as exc:
            self.last_error = f"invalid SQLite index: {exc}"
            raise IndexCorrupt(self.last_error) from exc

    def _open_validated(self, required_revision: int | None = None, *, allow_dirty: bool = False) -> sqlite3.Connection:
        conn = self._connect()
        try:
            self._validate(conn, required_revision, allow_dirty)
            return conn
        except BaseException:
            conn.close()
            raise

    @staticmethod
    def _record_parts(kind: str, record: dict[str, Any]) -> tuple[str, int | None, str | None, str | None, str | None, str | None, str | None, str | None, str]:
        record_id = str(record.get("label") or record.get("id"))
        if not record_id or record_id == "None":
            raise ValueError(f"{kind} record has no id")
        idea_id = record.get("idea_id") if kind == "experiments" else record.get("id")
        try:
            idea_id = int(idea_id) if idea_id is not None else None
        except (TypeError, ValueError):
            idea_id = None
        # Preserve source JSON's permissive NaN spelling in the opaque cache
        # payload; only numeric metric projections reject non-finite values.
        encoded = json.dumps(record, separators=(",", ":"), allow_nan=True, default=str)
        tags = record.get("tags")
        tags_json = json.dumps(tags if isinstance(tags, list) else [], separators=(",", ":"))
        return (record_id, idea_id, record.get("status"), record.get("created_at"),
                record.get("updated_at"), record.get("started_at"), record.get("finished_at"), record.get("description"),
                record.get("runtime"), tags_json, int(bool(record.get("metrics"))), encoded)

    def _upsert_conn(self, conn: sqlite3.Connection, kind: str, record: dict[str, Any]) -> None:
        record_id, idea_id, status, created, updated, started, finished, description, runtime, tags, has_metrics, payload = self._record_parts(kind, record)
        conn.execute("""INSERT INTO records(kind,record_id,idea_id,status,created_at,updated_at,started_at,finished_at,description,runtime,tags,has_metrics,payload)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                     ON CONFLICT(kind,record_id) DO UPDATE SET idea_id=excluded.idea_id,status=excluded.status,
                     created_at=excluded.created_at,updated_at=excluded.updated_at,started_at=excluded.started_at,finished_at=excluded.finished_at,
                     description=excluded.description,runtime=excluded.runtime,tags=excluded.tags,has_metrics=excluded.has_metrics,payload=excluded.payload""",
                     (kind, record_id, idea_id, status, created, updated, started, finished, description, runtime, tags, has_metrics, payload))
        conn.execute("DELETE FROM metric_values WHERE kind=? AND record_id=?", (kind, record_id))
        if kind == "experiments":
            conn.executemany("INSERT INTO metric_values(kind,record_id,metric,value) VALUES (?,?,?,?)",
                             [(kind, record_id, key, value) for key, value in numeric_metric_leaves(record.get("metrics")).items()])

    def rebuild(self, snapshot: dict[str, list[dict[str, Any]]] | None = None, *, quarantine: bool = False) -> dict[str, int]:
        """Atomically replace this cache using an authoritative snapshot/scan."""
        snapshot = scan_filesystem(self.repo_dir) if snapshot is None else snapshot
        revision = int(snapshot.get("version", 0))
        counts = {"ideas": len(snapshot.get("ideas", [])), "experiments": len(snapshot.get("experiments", []))}
        with self._lock, self._process_lock():
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with contextlib.suppress(OSError):
                os.chmod(self.path.parent, 0o700)
            if quarantine:
                self._quarantine_unusable_database()
            temp = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.rebuild")
            try:
                conn = self._connect(temp)
                try:
                    self._create_schema(conn, revision)
                    conn.execute("BEGIN IMMEDIATE")
                    for kind in ("ideas", "experiments"):
                        for record in snapshot.get(kind, []):
                            if isinstance(record, dict):
                                self._upsert_conn(conn, kind, record)
                    conn.execute("COMMIT")
                    # A complete DB is published in one rename; readers with an
                    # old open handle retain the old consistent snapshot.
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                finally:
                    conn.close()
                os.replace(temp, self.path)
                with contextlib.suppress(OSError):
                    os.chmod(self.path, 0o600)
                for suffix in ("-wal", "-shm"):
                    Path(str(temp) + suffix).unlink(missing_ok=True)
                self.last_error = None
                self.dirty = False
                return counts
            except (FilesystemScanError, IndexUnavailable):
                raise
            except (sqlite3.Error, OSError, ValueError, TypeError) as exc:
                with contextlib.suppress(OSError):
                    temp.unlink(missing_ok=True)
                raise self._unavailable("rebuild failed (authoritative files were not changed)", exc) from exc

    def reconcile(self, snapshot: dict[str, list[dict[str, Any]]] | None = None) -> dict[str, int]:
        """Reconcile a full FS snapshot, touching only changed derived rows.

        It intentionally considers every authoritative record so edits and
        deletions made outside this process are found without inotify.  Unlike
        ``rebuild``, unchanged payloads keep their existing SQL rows/indexes.
        """
        snapshot = scan_filesystem(self.repo_dir) if snapshot is None else snapshot
        snapshot_revision = snapshot.get("version")
        if snapshot_revision is not None:
            snapshot_revision = int(snapshot_revision)
        wanted: dict[tuple[str, str], dict[str, Any]] = {}
        for kind in ("ideas", "experiments"):
            for record in snapshot.get(kind, []):
                if isinstance(record, dict):
                    record_id = str(record.get("label") or record.get("id"))
                    if record_id and record_id != "None":
                        wanted[(kind, record_id)] = record
        try:
            with self._lock, self._process_lock():
                conn = self._open_validated(allow_dirty=True)
                try:
                    current_revision = int(dict(conn.execute("SELECT key, value FROM metadata WHERE key='revision'").fetchall()).get("revision", "0"))
                    # A slow periodic snapshot taken before a later successful
                    # Store mutation must not publish old derived rows over it.
                    if snapshot_revision is not None and snapshot_revision < current_revision:
                        return {"ideas": len(snapshot.get("ideas", [])), "experiments": len(snapshot.get("experiments", [])),
                                "changed": 0, "deleted": 0, "skipped_stale_snapshot": True}
                    existing = dict(conn.execute("SELECT kind || char(0) || record_id, payload FROM records"))
                    changed = deleted = 0
                    conn.execute("BEGIN IMMEDIATE")
                    for (kind, record_id), record in wanted.items():
                        payload = self._record_parts(kind, record)[-1]
                        if existing.get(kind + "\0" + record_id) != payload:
                            self._upsert_conn(conn, kind, record)
                            changed += 1
                    for composite in existing:
                        kind, record_id = composite.split("\0", 1)
                        if (kind, record_id) not in wanted:
                            conn.execute("DELETE FROM metric_values WHERE kind=? AND record_id=?", (kind, record_id))
                            conn.execute("DELETE FROM records WHERE kind=? AND record_id=?", (kind, record_id))
                            deleted += 1
                    if snapshot_revision is not None:
                        conn.execute("UPDATE metadata SET value=? WHERE key='revision'", (str(snapshot_revision),))
                    conn.execute("COMMIT")
                    self.last_error = None
                    self.dirty = False
                except sqlite3.Error:
                    with contextlib.suppress(sqlite3.Error): conn.execute("ROLLBACK")
                    raise
                finally:
                    conn.close()
            return {"ideas": len(snapshot.get("ideas", [])), "experiments": len(snapshot.get("experiments", [])),
                    "changed": changed, "deleted": deleted}
        except IndexCorrupt:
            # A corrupt/mismatched cache is never repaired in place.  A fresh,
            # atomically published rebuild keeps readers from seeing partial data.
            return self.rebuild(snapshot, quarantine=True)

    def _quarantine_unusable_database(self) -> None:
        """Preserve a bad derived DB for diagnosis without touching source JSON."""
        try:
            if self.path.exists():
                quarantine = self.path.with_name(f"{self.path.name}.corrupt-{int(time.time())}-{uuid.uuid4().hex[:8]}")
                os.replace(self.path, quarantine)
                os.chmod(quarantine, 0o600)
                logger.warning("quarantined unusable SQLite index at %s", quarantine)
            for suffix in ("-wal", "-shm"):
                Path(str(self.path) + suffix).unlink(missing_ok=True)
        except OSError as exc:
            # Rebuild can still atomically replace the path; a failed optional
            # quarantine must not make authoritative data less available.
            logger.warning("could not quarantine SQLite index %s: %s", self.path, exc)

    def upsert(self, kind: str, record: dict[str, Any], *, revision: int | None = None) -> None:
        """Apply a post-filesystem-commit update.  Never call before FS success."""
        if kind not in {"ideas", "experiments"}:
            raise ValueError("kind must be ideas or experiments")
        with self._lock, self._process_lock():
            conn = self._open_validated()
            try:
                conn.execute("BEGIN IMMEDIATE")
                current_revision = int(dict(conn.execute("SELECT key, value FROM metadata WHERE key='revision'").fetchall()).get("revision", "0"))
                if revision is not None and revision < current_revision:
                    conn.execute("ROLLBACK")
                    self.dirty = True
                    raise IndexUnavailable("out-of-order SQLite index callback; reconciliation required")
                if revision is not None and revision > current_revision + 1:
                    conn.execute("ROLLBACK")
                    self.dirty = True
                    raise IndexUnavailable("SQLite index callback revision gap; reconciliation required")
                self._upsert_conn(conn, kind, record)
                if revision is not None:
                    conn.execute("UPDATE metadata SET value=? WHERE key='revision'", (str(revision),))
                conn.execute("COMMIT")
                self.last_error = None
                self.dirty = False
            except sqlite3.Error as exc:
                with contextlib.suppress(sqlite3.Error): conn.execute("ROLLBACK")
                self.dirty = True
                raise self._unavailable("incremental index update failed", exc) from exc
            finally:
                conn.close()

    def delete(self, kind: str, record_id: str | int, *, revision: int | None = None) -> None:
        if kind not in {"ideas", "experiments"}:
            raise ValueError("kind must be ideas or experiments")
        with self._lock, self._process_lock():
            conn = self._open_validated()
            try:
                conn.execute("BEGIN IMMEDIATE")
                current_revision = int(dict(conn.execute("SELECT key, value FROM metadata WHERE key='revision'").fetchall()).get("revision", "0"))
                if revision is not None and revision < current_revision:
                    conn.execute("ROLLBACK")
                    self.dirty = True
                    raise IndexUnavailable("out-of-order SQLite index callback; reconciliation required")
                if revision is not None and revision > current_revision + 1:
                    conn.execute("ROLLBACK")
                    self.dirty = True
                    raise IndexUnavailable("SQLite index callback revision gap; reconciliation required")
                conn.execute("DELETE FROM records WHERE kind=? AND record_id=?", (kind, str(record_id)))
                conn.execute("DELETE FROM metric_values WHERE kind=? AND record_id=?", (kind, str(record_id)))
                if revision is not None:
                    conn.execute("UPDATE metadata SET value=? WHERE key='revision'", (str(revision),))
                conn.execute("COMMIT")
                self.last_error = None
                self.dirty = False
            except sqlite3.Error as exc:
                with contextlib.suppress(sqlite3.Error): conn.execute("ROLLBACK")
                self.dirty = True
                raise self._unavailable("incremental index delete failed", exc) from exc
            finally:
                conn.close()

    def touch(self, revision: int) -> None:
        """Advance the authoritative revision when a non-indexed file changed."""
        with self._lock, self._process_lock():
            conn = self._open_validated()
            try:
                conn.execute("BEGIN IMMEDIATE")
                current = int(dict(conn.execute("SELECT key, value FROM metadata WHERE key='revision'").fetchall()).get("revision", "0"))
                if revision >= current:
                    conn.execute("UPDATE metadata SET value=? WHERE key='revision'", (str(revision),))
                conn.execute("COMMIT")
                self.last_error = None
                self.dirty = False
            except sqlite3.Error as exc:
                with contextlib.suppress(sqlite3.Error): conn.execute("ROLLBACK")
                self.dirty = True
                raise self._unavailable("index revision update failed", exc) from exc
            finally:
                conn.close()

    def list_records(self, kind: str, *, status: str | None = None, idea_id: int | None = None,
                     metric: str | None = None, page: int = 1, page_size: int = 100,
                     required_revision: int | None = None) -> dict[str, Any]:
        """SQL-side status/idea/metric filtering and deterministic timestamp paging."""
        if kind not in {"ideas", "experiments"} or page < 1 or page_size < 1:
            raise ValueError("invalid index query")
        joins, clauses, values = "", ["r.kind=?"], [kind]
        if status is not None:
            clauses.append("r.status=?"); values.append(status)
        if idea_id is not None:
            clauses.append("r.idea_id=?"); values.append(int(idea_id))
        if metric is not None:
            joins = " JOIN metric_values m ON m.kind=r.kind AND m.record_id=r.record_id"
            clauses.append("m.metric=?"); values.append(metric)
        where = " WHERE " + " AND ".join(clauses)
        sql_count = "SELECT COUNT(*) FROM records r" + joins + where
        sql_rows = ("SELECT r.payload FROM records r" + joins + where +
                    " ORDER BY COALESCE(r.finished_at,'') DESC, COALESCE(r.created_at,'') DESC, r.record_id DESC LIMIT ? OFFSET ?")
        try:
            with self._lock, self._process_lock(False):
                conn = self._open_validated(required_revision)
                try:
                    total = conn.execute(sql_count, values).fetchone()[0]
                    rows = conn.execute(sql_rows, values + [page_size, (page - 1) * page_size]).fetchall()
                finally:
                    conn.close()
            return {"records": [json.loads(row[0]) for row in rows], "page": page,
                    "page_size": page_size, "total": total}
        except (sqlite3.Error, OSError, json.JSONDecodeError) as exc:
            raise self._unavailable("index query failed", exc) from exc

    def completed_history(self, *, limit: int | None = None, metrics: list[str] | None = None,
                          compact: bool = False, required_revision: int | None = None) -> dict[str, Any]:
        """Return the SQL-backed completed portion of the ``/chart-data`` shape.

        ``metrics`` selects numeric leaf names (the exact dotted flattening used
        by the legacy chart); SQL performs the projection and ordering.  The
        full metric catalog is returned even when the history is limited.
        """
        selected = [str(m) for m in (metrics or []) if str(m)]
        if limit is not None and limit < 1:
            raise ValueError("limit must be positive")
        where = " WHERE e.kind='experiments' AND e.status='completed' AND e.has_metrics=1"
        idea_join = " JOIN records i ON i.kind='ideas' AND i.record_id=CAST(e.idea_id AS TEXT)"
        count_sql = "SELECT COUNT(*) FROM records e" + idea_join + where
        # Never select ``payload`` here: it may contain a multi-MB meta blob.
        rows_sql = ("SELECT e.record_id,e.idea_id,e.status,e.created_at,e.started_at,e.finished_at,e.description,e.runtime,e.tags,"
                    "i.description,i.status FROM records e " + idea_join + where +
                    " ORDER BY COALESCE(NULLIF(e.finished_at,''),NULLIF(e.created_at,''),'') DESC,e.record_id DESC")
        if limit is not None:
            rows_sql += " LIMIT ?"
        try:
            with self._lock, self._process_lock(False):
                conn = self._open_validated(required_revision)
                try:
                    total = conn.execute(count_sql).fetchone()[0]
                    rows = conn.execute(rows_sql, ([limit] if limit is not None else [])).fetchall()
                    catalog = conn.execute("""SELECT m.metric, COUNT(*), MIN(m.value), MAX(m.value)
                        FROM metric_values m JOIN records e ON e.kind=m.kind AND e.record_id=m.record_id
                        JOIN records i ON i.kind='ideas' AND i.record_id=CAST(e.idea_id AS TEXT)
                        WHERE m.kind='experiments' AND e.status='completed' AND e.has_metrics=1
                        GROUP BY m.metric ORDER BY m.metric""").fetchall()
                    # Fetch leaves only from the normalized numeric table.
                    # No JSON metrics/meta is parsed on the chart hot path.
                    value_rows = []
                    if rows:
                        ids = [row[0] for row in rows]
                        id_marks = ",".join("?" for _ in ids)
                        if selected:
                            metric_marks = ",".join("?" for _ in selected)
                            value_rows = conn.execute(
                                f"SELECT record_id,metric,value FROM metric_values WHERE kind='experiments' "
                                f"AND record_id IN ({id_marks}) AND metric IN ({metric_marks})",
                                ids + selected).fetchall()
                        else:
                            value_rows = conn.execute(
                                f"SELECT record_id,metric,value FROM metric_values WHERE kind='experiments' "
                                f"AND record_id IN ({id_marks})", ids).fetchall()
                finally:
                    conn.close()
            metric_map: dict[str, dict[str, float]] = {}
            for record_id, metric, value in value_rows:
                metric_map.setdefault(record_id, {})[metric] = value
            history = []
            for record_id, idea_id, status, created_at, started_at, finished_at, description, runtime, tags, idea_description, idea_status in rows:
                try:
                    parsed_tags = json.loads(tags) if tags else []
                except json.JSONDecodeError:
                    parsed_tags = []
                item = {"id": record_id, "label": record_id, "idea_id": idea_id, "description": description,
                        "status": status, "started_at": started_at, "finished_at": finished_at, "created_at": created_at,
                        "runtime": runtime, "tags": parsed_tags, "metrics": metric_map.get(record_id, {}),
                        "idea_description": idea_description, "idea_status": idea_status}
                if compact:
                    item.pop("tags", None)
                    item.pop("description", None)
                history.append(item)
            return {"experiments": history, "total": total, "history_loaded": len(history),
                    "has_more": len(history) < total,
                    "metric_keys": [r[0] for r in catalog],
                    "metric_catalog": [{"metric": r[0], "count": r[1], "min": r[2], "max": r[3]} for r in catalog]}
        except (sqlite3.Error, OSError, json.JSONDecodeError) as exc:
            raise self._unavailable("completed history query failed", exc) from exc

    def metric_catalog(self) -> list[dict[str, Any]]:
        """Metric names/count/range, computed in SQLite rather than JSON scans."""
        try:
            with self._lock, self._process_lock(False):
                conn = self._open_validated()
                try:
                    rows = conn.execute("""SELECT metric, COUNT(*), MIN(value), MAX(value)
                        FROM metric_values WHERE kind='experiments' GROUP BY metric ORDER BY metric""").fetchall()
                finally:
                    conn.close()
            return [{"metric": r[0], "count": r[1], "min": r[2], "max": r[3]} for r in rows]
        except (sqlite3.Error, OSError) as exc:
            raise self._unavailable("metric catalog query failed", exc) from exc

    def status(self) -> dict[str, Any]:
        """Small diagnostic/status payload safe for the CLI or health endpoint."""
        base = {"enabled": self.enabled, "path": str(self.path), "error": self.last_error,
                "network_filesystem": _mount_filesystem(self.path.parent)}
        if not self.enabled or not self.path.exists():
            return base | {"ready": False}
        try:
            with self._lock, self._process_lock(False):
                conn = self._open_validated()
                try:
                    counts = dict(conn.execute("SELECT kind, COUNT(*) FROM records GROUP BY kind").fetchall())
                finally:
                    conn.close()
            return base | {"ready": True, "counts": counts}
        except IndexUnavailable:
            return base | {"ready": False, "error": self.last_error}
