"""File-based storage for ideas and experiments.

All state lives in {repo}/.the_lab/experiments/{idea_id}/ — a fixed location
at the repo root, outside of any git worktree. This data is not subject to
branch switches or worktree operations.

New layout (subfolder per experiment):
  {idea_id}/idea.json               — idea metadata
  {idea_id}/notes.json              — append-only list of notes
  {idea_id}/{seq}/experiment.json   — experiment metadata + results
  {idea_id}/{seq}/script.sh         — experiment script
  {idea_id}/{seq}/script.log        — stdout+stderr capture (runner writes via .with_suffix)
  {idea_id}/{seq}/script.err        — error details

Old layout (backwards compatible, read-only):
  {idea_id}/{seq}.json   — experiment metadata
  {idea_id}/{seq}.sh     — script
  {idea_id}/{seq}.log    — log

Both layouts are auto-detected on startup. New experiments always use subfolders.

Performance: all reads are served from in-memory caches populated on startup.
Writes go to disk first, then update the cache. This makes list/get operations
O(1) instead of O(I×E) filesystem scans.
"""
from __future__ import annotations

import json
import logging
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path

from . import jsonio
from .index import FilesystemIndex, IndexUnavailable

logger = logging.getLogger(__name__)


class StoreDegradedError(RuntimeError):
    """Writes are unsafe because authoritative records could not be read."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _enrich_experiment(exp: dict) -> dict:
    """Add computed fields like runtime_seconds."""
    started = exp.get("started_at")
    finished = exp.get("finished_at")
    if started and finished:
        try:
            t0 = datetime.fromisoformat(started)
            t1 = datetime.fromisoformat(finished)
            diff = (t1 - t0).total_seconds()
            exp["runtime_seconds"] = round(diff, 1)
            # Human-readable
            if diff < 60:
                exp["runtime"] = f"{diff:.1f}s"
            elif diff < 3600:
                exp["runtime"] = f"{diff/60:.1f}m"
            else:
                exp["runtime"] = f"{diff/3600:.1f}h"
        except (ValueError, TypeError):
            pass
    return exp


def _read_json(path: Path) -> dict | list:
    return jsonio.read_json(path)


def _write_json(path: Path, data):
    # Atomic (temp file + os.replace) — see jsonio.py. Readers sharing
    # .the_lab/ over NFS never observe a torn file.
    jsonio.write_json(path, data)


# Fields whose values flow into the cached aggregation endpoints
# (/ideas, /ideas/{id}, /chart-data, /graph, /backlog). Updates that
# only touch OTHER fields (notably ``meta`` — used for internal bookkeeping
# like worktree paths) must NOT bump Store._version, since bumping would
# invalidate every cached response for the entire dashboard.
RENDERABLE_EXPERIMENT_FIELDS = {
    "status",
    "metrics",
    "started_at",
    "finished_at",
    "description",
    "error",
    "tags",
    "label",
    "seq",
    "conclusion",
    "progress",
    "runtime",
    # M3 feedback: short human-readable terminal-state reason
    # (timeout / budget-exhausted / cap-abandon / sigterm / error).
    # Must be renderable so it serializes into aggregation endpoints and
    # invalidates the cache when it changes.
    "status_msg",
}


class Store:
    """File-based store with in-memory caches.

    Caches:
      _ideas:        {idea_id: dict}     — all idea metadata
      _experiments:  {exp_id: dict}      — all experiment metadata (enriched)
      _exp_by_idea:  {idea_id: set[exp_id]} — reverse index for per-idea lookups
      _notes:        {idea_id: list[dict]} — notes per idea
    """

    def __init__(self, repo_dir: Path, read_only: bool = False):
        self.repo_dir = repo_dir
        self.lab_dir = repo_dir / ".the_lab" / "experiments"
        self.read_only = read_only
        if not read_only:
            self.lab_dir.mkdir(parents=True, exist_ok=True)
        self.instance_id = self._load_or_create_instance_id()
        self._ensure_gitignore()
        self._lock = threading.Lock()
        self._next_idea_id = 1
        self._next_seq_by_idea: dict[int, int] = {}
        # In-memory caches — experiments keyed by label string ("10.1")
        self._ideas: dict[int, dict] = {}
        self._experiments: dict[str, dict] = {}
        self._exp_by_idea: dict[int, set[str]] = {}
        self._notes: dict[int, list[dict]] = {}
        # Monotonic counter bumped on every write. The response cache keys
        # entries by (endpoint, params, version), so any write naturally
        # invalidates all cached reads.
        self._version = 0
        # Every successful authoritative filesystem mutation advances this
        # fence, including notes/meta that have no SQL row.  Routes compare it
        # before trusting the optional index.
        self._index_version = 0
        self._completed_version = 0
        # Structured diagnostics, keyed by authoritative pathname.  We never
        # rewrite a malformed record during recovery: fixing it is an explicit
        # operator action, after which ``reconcile()`` can be called.
        self._load_errors: dict[str, str] = {}
        self._fs_signature: tuple[tuple[str, int, int], ...] | None = None

        self.index: FilesystemIndex | None = None
        self._load_all()
        if not self.read_only:
            try:
                self.index = FilesystemIndex(self.repo_dir, self.instance_id)
                # A new Store process starts its revision epoch at zero.  An
                # authoritative full rebuild avoids trusting a higher revision
                # left by a previous process against changed/deleted files.
                if self.is_ready:
                    self.index.rebuild(self.index_snapshot())
                else:
                    self.index.mark_dirty("authoritative filesystem scan has errors; index rebuild deferred")
            except Exception as exc:
                logger.warning("SQLite index disabled/unavailable; Store remains filesystem-authoritative: %s", exc)
                if self.index is None or not self.index.enabled:
                    self.index = None

    def _load_or_create_instance_id(self) -> str:
        """Return the 8-char instance ID, creating it on first run."""
        id_path = self.repo_dir / ".the_lab" / "instance.id"
        if id_path.exists():
            return id_path.read_text().strip()
        if self.read_only:
            return "readonly"
        import secrets
        instance_id = secrets.token_hex(4)  # 8 hex chars
        id_path.write_text(instance_id + "\n")
        return instance_id

    @property
    def load_errors(self) -> dict[str, str]:
        """Copy of startup/reconciliation problems in authoritative files."""
        # Reconciliation swaps this dict atomically; never wait behind a slow
        # authoritative write merely to answer readiness/liveness.
        return dict(self._load_errors)

    @property
    def is_ready(self) -> bool:
        return not self._load_errors

    def assert_writable(self) -> None:
        if self.read_only:
            raise StoreDegradedError("store is read-only")
        if self._load_errors:
            raise StoreDegradedError(
                "authoritative experiment files have load errors; repair them "
                "and reconcile before mutating state"
            )

    @property
    def version(self) -> int:
        """Monotonic version counter — bumped on every state-mutating write."""
        return self._version

    @property
    def index_version(self) -> int:
        return self._index_version

    def _index_revision_locked(self) -> int:
        self._index_version += 1
        return self._index_version

    def _index_upsert(self, kind: str, record: dict, revision: int) -> None:
        if self.index is None:
            return
        try:
            self.index.upsert(kind, record, revision=revision)
        except Exception as exc:
            self.index.mark_dirty(str(exc))
            logger.warning("SQLite index update failed after successful filesystem commit: %s", exc)

    def _index_delete(self, kind: str, record_id: str, revision: int) -> None:
        if self.index is None:
            return
        try:
            self.index.delete(kind, record_id, revision=revision)
        except Exception as exc:
            self.index.mark_dirty(str(exc))
            logger.warning("SQLite index delete failed after successful filesystem commit: %s", exc)

    def _index_touch(self, revision: int) -> None:
        if self.index is None:
            return
        try:
            self.index.touch(revision)
        except Exception as exc:
            self.index.mark_dirty(str(exc))
            logger.warning("SQLite index revision update failed after successful filesystem commit: %s", exc)

    @property
    def completed_version(self) -> int:
        """Revision of completed chart rows (live progress does not invalidate it)."""
        return self._completed_version

    def _ensure_gitignore(self):
        """Make sure .the_lab/ is gitignored on ALL branches."""
        if self.read_only:
            return
        exclude_file = self.repo_dir / ".git" / "info" / "exclude"
        exclude_file.parent.mkdir(parents=True, exist_ok=True)
        entry = ".the_lab/"
        if exclude_file.exists():
            content = exclude_file.read_text()
            if entry not in content.split("\n"):
                exclude_file.write_text(content.rstrip("\n") + f"\n{entry}\n")
        else:
            exclude_file.write_text(f"{entry}\n")

        # Ensure shared artifacts directory exists
        artifacts_dir = self.repo_dir / ".the_lab" / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)

    def _load_all(self):
        """Initial authoritative scan.  See ``reconcile`` for later scans."""
        self.reconcile(initial=True)

    def _filesystem_signature(self) -> tuple[tuple[str, int, int], ...] | None:
        """Cheap stat-only change detector for periodic external-edit scans."""
        try:
            if not self.lab_dir.exists():
                return ()
            rows = []
            def add(path: Path) -> None:
                stat = path.stat()
                rows.append((str(path.relative_to(self.lab_dir)), stat.st_mtime_ns, stat.st_size))
            root_highwater = self.lab_dir / ".next_idea_id.json"
            if root_highwater.exists():
                add(root_highwater)
            for idea_dir in self.lab_dir.iterdir():
                if not idea_dir.is_dir() or not idea_dir.name.isdigit():
                    continue
                for name in ("idea.json", "notes.json", ".next_seq.json"):
                    path = idea_dir / name
                    if path.exists():
                        add(path)
                for child in idea_dir.iterdir():
                    if child.is_file() and child.suffix == ".json" and child.stem.isdigit():
                        add(child)
                    elif child.is_dir() and child.name.isdigit():
                        experiment = child / "experiment.json"
                        if experiment.exists():
                            add(experiment)
            return tuple(sorted(rows))
        except OSError:
            return None

    @staticmethod
    def _record_error(errors: dict[str, str], path: Path, exc: Exception | str) -> None:
        errors[str(path)] = f"{type(exc).__name__}: {exc}" if isinstance(exc, Exception) else str(exc)
        logger.warning("The Lab record skipped: %s: %s", path, errors[str(path)])

    def reconcile(self, *, initial: bool = False) -> bool:
        """Scan filesystem once on startup to populate all caches.

        Experiments are identified by label "{idea_id}.{file_stem}".
        Old projects use global IDs as file stems (e.g. 671.json),
        new projects use per-idea seq (e.g. 1.json). Both work —
        the file stem IS the seq number in the label.
        """
        signature = self._filesystem_signature()
        with self._lock:
            scan_version = self._index_version
            unchanged = not initial and signature is not None and signature == self._fs_signature
        if unchanged:
            # Avoid reparsing unchanged JSON, but still retry repair of a lost,
            # dirty, or transiently unavailable derived cache from this known
            # good in-memory authoritative snapshot.
            if self.index is not None and self.is_ready:
                try:
                    self.index.reconcile(self.index_snapshot())
                except Exception as exc:
                    logger.warning("SQLite index reconciliation failed; filesystem Store remains available: %s", exc)
            return not self._load_errors
        ideas: dict[int, dict] = {}
        experiments: dict[str, dict] = {}
        exp_by_idea: dict[int, set[str]] = {}
        notes: dict[int, list[dict]] = {}
        next_seq_by_idea: dict[int, int] = {}
        errors: dict[str, str] = {}
        scan_failed = False
        if not self.lab_dir.exists():
            self._record_error(errors, self.lab_dir, "authoritative experiments directory is missing")
            scan_failed = True
            entries = []
        else:
            try:
                entries = list(self.lab_dir.iterdir())
            except OSError as exc:
                self._record_error(errors, self.lab_dir, exc)
                entries = []
                scan_failed = True
        max_idea = 0
        for d in entries:
            if not d.is_dir():
                continue
            try:
                idea_id = int(d.name)
            except ValueError:
                continue
            max_idea = max(max_idea, idea_id)
            seq_marker = d / ".next_seq.json"
            try:
                marker = _read_json(seq_marker).get("next", 1) if seq_marker.exists() else 1
                if not isinstance(marker, int) or marker < 1:
                    raise ValueError("next experiment seq must be a positive integer")
                next_seq_by_idea[idea_id] = marker
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                self._record_error(errors, seq_marker, exc)

            # Load idea
            idea_path = d / "idea.json"
            if idea_path.exists():
                try:
                    value = _read_json(idea_path)
                    if not isinstance(value, dict):
                        raise ValueError("idea.json must contain an object")
                    if value.get("id", idea_id) != idea_id or not isinstance(value.get("description", ""), str):
                        raise ValueError("idea id/description does not match required schema")
                    if not isinstance(value.get("parent_ids", []), list):
                        raise ValueError("idea parent_ids must be a list")
                    ideas[idea_id] = value
                except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                    self._record_error(errors, idea_path, exc)

            # Load notes
            notes_path = d / "notes.json"
            if notes_path.exists():
                try:
                    value = _read_json(notes_path)
                    if not isinstance(value, list):
                        raise ValueError("notes.json must contain a list")
                    if any(not isinstance(n, dict) or not isinstance(n.get("text", ""), str) for n in value):
                        raise ValueError("notes entries must be objects with text")
                    notes[idea_id] = value
                except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                    self._record_error(errors, notes_path, exc)

            # Load experiments — supports both old flat layout and new subfolder layout
            exp_labels: set[str] = set()
            # New layout: {seq}/experiment.json (subfolder per experiment)
            try:
                children = list(d.iterdir())
            except OSError as exc:
                self._record_error(errors, d, exc)
                scan_failed = True
                continue
            for sub in children:
                if sub.is_dir() and sub.name.isdigit():
                    exp_json = sub / "experiment.json"
                    if exp_json.exists():
                        seq = int(sub.name)
                        label = f"{idea_id}.{seq}"
                        try:
                            exp = _read_json(exp_json)
                            if not isinstance(exp, dict):
                                raise ValueError("experiment.json must contain an object")
                            if not isinstance(exp.get("status", "pending"), str) or not isinstance(exp.get("meta", {}), dict):
                                raise ValueError("experiment status/meta does not match required schema")
                            if exp.get("metrics") is not None and not isinstance(exp.get("metrics"), dict):
                                raise ValueError("experiment metrics must be an object or null")
                            exp = _enrich_experiment(exp)
                        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                            self._record_error(errors, exp_json, exc)
                            continue
                        exp["id"] = label
                        exp["seq"] = seq
                        exp["label"] = label
                        exp["idea_id"] = idea_id
                        exp["script"] = f".the_lab/experiments/{idea_id}/{seq}/script.sh"
                        experiments[label] = exp
                        exp_labels.add(label)
            # Old layout: {seq}.json (flat files in idea dir)
            for f in children:
                if not (f.is_file() and f.suffix == ".json"):
                    continue
                if f.stem.isdigit():
                    seq = int(f.stem)
                    label = f"{idea_id}.{seq}"
                    if label in exp_labels:
                        continue  # already loaded from subfolder
                    try:
                        exp = _read_json(f)
                        if not isinstance(exp, dict):
                            raise ValueError("legacy experiment record must contain an object")
                        if not isinstance(exp.get("status", "pending"), str) or not isinstance(exp.get("meta", {}), dict):
                            raise ValueError("legacy experiment status/meta does not match required schema")
                        exp = _enrich_experiment(exp)
                    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                        self._record_error(errors, f, exc)
                        continue
                    exp["id"] = label
                    exp["seq"] = seq
                    exp["label"] = label
                    exp["idea_id"] = idea_id
                    exp["script"] = f".the_lab/experiments/{idea_id}/{seq}.sh"
                    experiments[label] = exp
                    exp_labels.add(label)
            exp_by_idea[idea_id] = exp_labels

        highwater = self.lab_dir / ".next_idea_id.json"
        try:
            high = _read_json(highwater).get("next", 1) if highwater.exists() else 1
            if not isinstance(high, int) or high < 1:
                raise ValueError("next idea id must be a positive integer")
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            self._record_error(errors, highwater, exc)
            high = max_idea + 1
        # A failed directory scan cannot safely imply deletions. Keep the last
        # good cache rather than replacing it with an incomplete snapshot.
        with self._lock:
            if not initial and self._index_version != scan_version:
                # Do not replace newer successful mutations with a scan that
                # began before them.  The next periodic/manual reconcile will
                # see a stable point-in-time snapshot.
                return False
            self._load_errors = errors
            if scan_failed and not initial:
                return False
            changed = (ideas != self._ideas or experiments != self._experiments or
                       exp_by_idea != self._exp_by_idea or notes != self._notes)
            self._ideas = ideas
            self._experiments = experiments
            self._exp_by_idea = exp_by_idea
            self._notes = notes
            self._next_idea_id = max(max_idea + 1, high)
            self._next_seq_by_idea = {
                idea: max(next_seq, max((int(x.rpartition(".")[2]) for x in labels), default=0) + 1)
                for idea, (next_seq, labels) in {
                    i: (next_seq_by_idea.get(i, 1), labels)
                    for i, labels in exp_by_idea.items()
                }.items()
            }
            if changed or initial:
                self._version += 1
                self._completed_version += 1
                self._index_revision_locked()
            self._fs_signature = signature
        healthy = not errors
        # Reconcile only after the complete authoritative snapshot has swapped
        # in. A concurrent later mutation carries a higher revision fence and
        # cannot be overwritten by this slower scan.
        if healthy and self.index is not None:
            try:
                self.index.reconcile(self.index_snapshot())
            except Exception as exc:
                logger.warning("SQLite index reconciliation failed; filesystem Store remains available: %s", exc)
        return healthy

    def _idea_dir(self, idea_id: int) -> Path:
        return self.lab_dir / str(idea_id)

    def _exp_json_path(self, idea_id: int, seq: int) -> Path:
        """Return the experiment JSON path, detecting old vs new layout.

        New layout: {idea_id}/{seq}/experiment.json  (subfolder per experiment)
        Old layout: {idea_id}/{seq}.json             (flat files in idea dir)
        """
        idea_dir = self._idea_dir(idea_id)
        new_path = idea_dir / str(seq) / "experiment.json"
        if new_path.exists():
            return new_path
        old_path = idea_dir / f"{seq}.json"
        if old_path.exists():
            return old_path
        # Default to new layout for new experiments
        return new_path

    @staticmethod
    def _exp_script_rel(idea_id: int, seq: int, subfolder: bool = True) -> str:
        """Return the relative script path for an experiment.

        New layout: .the_lab/experiments/{idea_id}/{seq}/script.sh
        Old layout: .the_lab/experiments/{idea_id}/{seq}.sh
        """
        if subfolder:
            return f".the_lab/experiments/{idea_id}/{seq}/script.sh"
        return f".the_lab/experiments/{idea_id}/{seq}.sh"

    # --- Ideas ---

    def create_idea(
        self,
        description: str,
        parent_ids: list[int],
        branch: str,
        source: str = "agent",
        status: str = "active",
        priority: str = "normal",
        resources: list[dict] | None = None,
    ) -> dict:
        with self._lock:
            self.assert_writable()
            idea_id = self._next_idea_id
            # Persist a high-water mark before handing out the ID.  A deleted
            # directory therefore cannot make an ID reusable after restart.
            _write_json(self.lab_dir / ".next_idea_id.json", {"next": idea_id + 1})
            self._next_idea_id += 1

        idea = {
            "id": idea_id,
            "description": description,
            "parent_ids": parent_ids,
            "status": status,
            "conclusion": None,
            "branch": branch,
            "source": source,
            "priority": priority,
            "resources": resources or [],
            "created_at": _now(),
        }
        return idea

    def release_unused_idea_id(self, idea_id: int) -> None:
        """Roll back an allocated but unsaved idea ID so the next create_idea()
        reuses it.  Only takes effect when idea_id was the most recently
        allocated ID and has not yet been persisted."""
        with self._lock:
            if idea_id not in self._ideas and self._next_idea_id == idea_id + 1:
                self._next_idea_id = idea_id

    def save_idea(self, idea: dict):
        """Write idea.json + initialize notes.json."""
        idea_id = idea["id"]
        idea_dir = self._idea_dir(idea_id)
        with self._lock:
            self.assert_writable()
            # Disk is authoritative: publish no portion of this record until
            # all required files have been atomically written.
            idea_dir.mkdir(parents=True, exist_ok=True)
            _write_json(idea_dir / "idea.json", idea)
            notes_path = idea_dir / "notes.json"
            if not notes_path.exists():
                _write_json(notes_path, [])
            is_new = idea_id not in self._ideas
            self._ideas[idea_id] = idea
            self._completed_version += 1
            if idea_id not in self._notes:
                self._notes[idea_id] = []
            if idea_id not in self._exp_by_idea:
                self._exp_by_idea[idea_id] = set()
            self._version += 1
            index_revision = self._index_revision_locked()
        self._index_upsert("ideas", idea, index_revision)
        if is_new:
            try:
                from . import ws as ws_mod
                ws_mod.broadcaster.broadcast_soon({
                    "type": "idea_changed",
                    "idea_id": idea_id,
                    "change": "created",
                })
            except Exception:
                pass

    def get_idea(self, idea_id: int) -> dict | None:
        return self._ideas.get(idea_id)

    def update_idea(self, idea_id: int, **fields) -> dict | None:
        # Copy-on-write: build a NEW record and swap it in under the lock, so
        # readers holding the old dict keep a stable snapshot (no read-during-
        # write races between runner tasks and threadpool handlers). The disk
        # write stays inside the lock so two concurrent updates can't publish
        # memory in one order and disk in the other.
        with self._lock:
            self.assert_writable()
            cur = self._ideas.get(idea_id)
            if not cur:
                return None
            idea = {**cur, **fields}
            _write_json(self._idea_dir(idea_id) / "idea.json", idea)
            self._ideas[idea_id] = idea
            self._completed_version += 1
            self._version += 1
            index_revision = self._index_revision_locked()
        self._index_upsert("ideas", idea, index_revision)
        # Determine change type for WebSocket emit.
        new_status = idea.get("status")
        if new_status == "concluded":
            change = "concluded"
        elif new_status == "abandoned":
            change = "abandoned"
        else:
            change = "updated"
        try:
            from . import ws as ws_mod
            ws_mod.broadcaster.broadcast_soon({
                "type": "idea_changed",
                "idea_id": idea_id,
                "change": change,
            })
        except Exception:
            pass
        return idea

    def list_ideas(self, status: str | None = None, source: str | None = None) -> list[dict]:
        ideas = sorted(self._ideas.values(), key=lambda i: i.get("id", 0))
        if status is not None:
            ideas = [i for i in ideas if i.get("status") == status]
        if source is not None:
            ideas = [i for i in ideas if i.get("source") == source]
        return ideas

    # --- Notes ---

    LISTING_LEVELS = {"insight", "milestone"}
    DETAIL_LEVELS = {"insight", "milestone", "observation"}
    ALL_LEVELS = {"insight", "milestone", "observation", "debug"}

    def add_note(
        self,
        idea_id: int,
        text: str,
        level: str = "observation",
        resources: list[dict] | None = None,
    ) -> dict:
        if level not in self.ALL_LEVELS:
            raise ValueError(f"invalid note level: {level}, must be one of {self.ALL_LEVELS}")
        note = {"text": text, "level": level, "created_at": _now()}
        if resources:
            note["resources"] = resources
        notes_path = self._idea_dir(idea_id) / "notes.json"
        with self._lock:
            self.assert_writable()
            # Copy-on-write list: readers iterating the old list are unaffected.
            notes = [*self._notes.get(idea_id, []), note]
            _write_json(notes_path, notes)
            self._notes[idea_id] = notes
            self._version += 1
            index_revision = self._index_revision_locked()
        self._index_touch(index_revision)
        try:
            from . import ws as ws_mod
            ws_mod.broadcaster.broadcast_soon({
                "type": "note_added",
                "idea_id": idea_id,
                "level": level,
            })
        except Exception:
            pass
        return note

    def get_notes(self, idea_id: int, levels: set[str] | None = None) -> list[dict]:
        notes = self._notes.get(idea_id, [])
        if levels is not None:
            notes = [n for n in notes if n.get("level", "observation") in levels]
        return notes

    # --- Experiments ---
    # Experiments are identified by label strings: "{idea_id}.{seq}"
    # where seq is the file stem (numeric). For new experiments, seq
    # is max(existing_stems)+1. For old projects, seq may be a legacy
    # global ID (e.g. "400.671") which is fine.

    def _next_seq(self, idea_id: int) -> int:
        """Next per-idea seq number: max existing stem + 1, or 1.

        Caller must hold ``self._lock``.

        The seq is parsed from the LABEL rather than looked up in
        ``self._experiments``, so a label reserved by an in-flight
        create_experiment (registered in ``_exp_by_idea`` before its record is
        written) still counts. Reading the record dict instead would skip
        reservations and hand the same seq to a concurrent create.
        """
        max_seq = 0
        for label in self._exp_by_idea.get(idea_id, set()):
            _, _, tail = str(label).rpartition(".")
            try:
                max_seq = max(max_seq, int(tail))
            except ValueError:
                # Not an idea.seq label — fall back to the stored record.
                rec = self._experiments.get(label)
                if rec and isinstance(rec.get("seq"), int):
                    max_seq = max(max_seq, rec["seq"])
        return max(max_seq + 1, self._next_seq_by_idea.get(idea_id, 1))

    def resolve_experiment(self, ref: str) -> dict | None:
        """Look up experiment by label (e.g. '10.1' or '400.671').

        Returns the experiment dict or None.
        """
        # Direct lookup by label
        ref = str(ref).strip()
        exp = self._experiments.get(ref)
        if exp:
            return exp
        # If bare integer, could be an old-style global ID — search all
        if "." not in ref:
            for lbl, e in self._experiments.items():
                if str(e.get("seq")) == ref or str(e.get("_legacy_id")) == ref:
                    return e
        return None

    # Meta keys the SERVER owns. A client that can set these is choosing paths
    # the server later deletes (see delete_experiment) or claiming git state it
    # does not control. Stripped at write time — filtering them only on read
    # (deps._INTERNAL_META_KEYS) hid them from responses while leaving the
    # stored values live.
    _SERVER_OWNED_META_KEYS = frozenset({"outdir", "worktree", "git_branch", "git_commit"})

    def create_experiment(
        self,
        idea_id: int,
        description: str,
        meta: dict | None = None,
        tags: list[str] | None = None,
        trusted_meta: bool = False,
    ) -> dict:
        """Create an experiment record.

        ``meta`` is client-supplied by default and has server-owned keys removed.
        Internal callers that legitimately set those keys (e.g. the runner
        recording the worktree it just created) pass ``trusted_meta=True``.
        """
        meta = dict(meta or {})
        if not trusted_meta:
            for key in self._SERVER_OWNED_META_KEYS:
                meta.pop(key, None)
        # Allocate and write under one lock.  In particular, an in-memory
        # reservation is not visible before authoritative disk publication.
        # _next_seq derives from self._exp_by_idea, so releasing the lock before
        # registering the label left a window where a concurrent create for the
        # same idea computed the same seq and silently clobbered the first one's
        # record and script. Reserving here (rather than holding the lock across
        # the disk write below) closes the window without serialising file I/O.
        with self._lock:
            self.assert_writable()
            seq = self._next_seq(idea_id)
            label = f"{idea_id}.{seq}"
            script_rel = self._exp_script_rel(idea_id, seq, subfolder=True)
            exp = {
            "id": label,
            "idea_id": idea_id,
            "seq": seq,
            "label": label,
            "description": description,
            "script": script_rel,
            "status": "pending",
            "meta": meta,
            "metrics": None,
            "error": None,
            "pid": None,
            "tags": tags or [],
            "created_at": _now(),
            "started_at": None,
            "finished_at": None,
        }

            exp_dir = self._idea_dir(idea_id) / str(seq)
            exp_dir.mkdir(parents=True, exist_ok=True)
            _write_json(self._idea_dir(idea_id) / ".next_seq.json", {"next": seq + 1})
            _write_json(exp_dir / "experiment.json", exp)
            self._experiments[label] = exp
            self._exp_by_idea.setdefault(idea_id, set()).add(label)
            self._next_seq_by_idea[idea_id] = seq + 1
            self._version += 1
            index_revision = self._index_revision_locked()
        self._index_upsert("experiments", exp, index_revision)
        return exp

    def get_experiment(self, exp_ref) -> dict | None:
        """Get experiment by label string (e.g. '10.1')."""
        return self._experiments.get(str(exp_ref))

    def update_experiment(self, exp_ref, **fields) -> dict | None:
        label = str(exp_ref)
        # Copy-on-write under the lock: the old record is never mutated, so
        # any reader holding a reference (chart-data mid-serialization, a
        # handler building a response) sees a stable snapshot. The disk write
        # happens inside the lock so concurrent updates to the same label
        # can't publish memory in one order and disk in the other.
        with self._lock:
            self.assert_writable()
            cur = self._experiments.get(label)
            if not cur:
                return None
            # Diff renderable fields BEFORE merging. Only fields that are
            # actually serialized by the cached aggregation endpoints should
            # invalidate the response cache — meta-only writes (e.g. worktree
            # bookkeeping from the runner) must not bump _version, or the
            # dashboard churns on every tick.
            renderable_changed = any(
                k in RENDERABLE_EXPERIMENT_FIELDS and cur.get(k) != v
                for k, v in fields.items()
            )
            exp = _enrich_experiment({**cur, **fields})
            json_path = self._exp_json_path(exp["idea_id"], exp["seq"])
            json_path.parent.mkdir(parents=True, exist_ok=True)
            _write_json(json_path, exp)
            self._experiments[label] = exp
            if renderable_changed:
                self._version += 1
                if cur.get("status") == "completed" or exp.get("status") == "completed":
                    self._completed_version += 1
            index_revision = self._index_revision_locked()
        self._index_upsert("experiments", exp, index_revision)
        return exp

    def transition_experiment(self, exp_ref, allowed: set[str] | tuple[str, ...],
                              *, expected_meta: dict | None = None,
                              meta_updates: dict | None = None,
                              meta_remove: tuple[str, ...] | list[str] = (),
                              **fields) -> dict | None:
        """Atomically update only when the record is in an expected state.

        Runner setup crosses many ``await`` boundaries.  This small compare and
        swap is the authority for queued→starting→running and ensures a cancel
        wins over a late completion/start write.
        """
        label = str(exp_ref)
        allowed = set(allowed)
        with self._lock:
            self.assert_writable()
            cur = self._experiments.get(label)
            if not cur or cur.get("status") not in allowed:
                return None
            if expected_meta and any((cur.get("meta") or {}).get(k) != v
                                     for k, v in expected_meta.items()):
                return None
            # Apply small meta patches under the same lock as the state CAS.
            # Monitor heartbeats must never replace a concurrently-written
            # result/attempt metadata dictionary with an older snapshot.
            if meta_updates is not None or meta_remove:
                patched_meta = dict(cur.get("meta") or {})
                patched_meta.update(meta_updates or {})
                for key in meta_remove:
                    patched_meta.pop(key, None)
                fields = {**fields, "meta": patched_meta}
            renderable_changed = any(
                k in RENDERABLE_EXPERIMENT_FIELDS and cur.get(k) != v
                for k, v in fields.items()
            )
            exp = _enrich_experiment({**cur, **fields})
            json_path = self._exp_json_path(exp["idea_id"], exp["seq"])
            json_path.parent.mkdir(parents=True, exist_ok=True)
            _write_json(json_path, exp)
            self._experiments[label] = exp
            if renderable_changed:
                self._version += 1
                if cur.get("status") == "completed" or exp.get("status") == "completed":
                    self._completed_version += 1
            index_revision = self._index_revision_locked()
        self._index_upsert("experiments", exp, index_revision)
        return exp

    def _safe_cleanup_path(self, value, *, key: str, label: str) -> Path | None:
        """Resolve *value* and return it only if it sits inside the repo.

        Refuses anything that escapes ``repo_dir`` (absolute paths elsewhere,
        ``..`` traversal, or a symlink pointing out), and refuses the repo root
        itself. Returns None — with a warning — when the path is rejected, so a
        malformed record degrades to "skip this cleanup" rather than deleting
        something it should not.
        """
        try:
            candidate = Path(value).expanduser()
            # strict=False: the path may already be gone, which is fine; we only
            # need its canonical form to test containment.
            resolved = candidate.resolve(strict=False)
            root = self.repo_dir.resolve(strict=False)
        except (OSError, RuntimeError, ValueError) as exc:
            logger.warning("experiment %s: unusable meta.%s (%r): %s", label, key, value, exc)
            return None
        if resolved == root or root not in resolved.parents:
            logger.warning(
                "experiment %s: refusing to delete meta.%s=%r — resolves to %s, "
                "outside the managed repo %s",
                label, key, value, resolved, root,
            )
            return None
        return resolved

    def delete_experiment(self, exp_ref) -> dict | None:
        label = str(exp_ref)
        exp = self._experiments.get(label)
        if not exp:
            return None

        idea_id = exp["idea_id"]
        idea_dir = self._idea_dir(idea_id)
        script_path = self.repo_dir / exp["script"]
        seq = exp["seq"]

        # New layout: entire subfolder
        exp_subdir = idea_dir / str(seq)
        if exp_subdir.is_dir():
            shutil.rmtree(exp_subdir, ignore_errors=True)

        # Old layout: flat files
        cleanup_paths = [
            idea_dir / f"{seq}.json",
            idea_dir / f"{seq}.sh",
            idea_dir / f"{seq}.log",
            idea_dir / f"{seq}.err",
            idea_dir / f"{seq}.progress",
            idea_dir / f"{seq}.metrics",
            idea_dir / f"{seq}.metrics.jsonl",
        ]

        # meta-derived cleanup paths are only followed when they resolve INSIDE
        # the repo. Belt-and-braces behind the write-time filter above: a record
        # written by an older version (or any future path that reaches meta)
        # must not be able to aim rmtree at an arbitrary directory.
        for key in ("outdir", "worktree"):
            value = (exp.get("meta") or {}).get(key)
            if not value:
                continue
            safe = self._safe_cleanup_path(value, key=key, label=label)
            if safe is not None:
                cleanup_paths.append(safe)

        for path in cleanup_paths:
            try:
                if path.is_dir():
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    path.unlink(missing_ok=True)
            except FileNotFoundError:
                pass

        with self._lock:
            self._experiments.pop(label, None)
            if exp.get("status") == "completed":
                self._completed_version += 1
            labels = self._exp_by_idea.get(idea_id)
            if labels is not None:
                labels.discard(label)
            self._version += 1
            index_revision = self._index_revision_locked()

        self._index_delete("experiments", label, index_revision)
        return exp

    def list_experiments(self, idea_id: int) -> list[dict]:
        labels = self._exp_by_idea.get(idea_id, set())
        exps = [self._experiments[lbl] for lbl in labels if lbl in self._experiments]
        return sorted(exps, key=lambda e: e.get("created_at", ""))

    def list_experiments_by_status(self, status: str) -> list[dict]:
        """List all experiments across all ideas with a given status."""
        return [exp for exp in self._experiments.values() if exp.get("status") == status]

    def find_similar_ideas(self, description: str, threshold: float = 0.4) -> list[dict]:
        """Find existing ideas with overlapping keywords (simple word-overlap)."""
        stop_words = {
            "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
            "have", "has", "had", "do", "does", "did", "will", "would", "could",
            "should", "may", "might", "can", "shall", "to", "of", "in", "for",
            "on", "with", "at", "by", "from", "as", "into", "through", "during",
            "before", "after", "and", "but", "or", "nor", "not", "so", "yet",
            "this", "that", "these", "those", "it", "its", "we", "our", "vs",
            "use", "using", "try", "test", "run", "add", "new",
        }

        def significant_words(text: str) -> set[str]:
            words = set(text.lower().split())
            return {w.strip(".,;:!?()[]{}\"'") for w in words} - stop_words

        new_words = significant_words(description)
        if not new_words:
            return []

        similar = []
        for idea in self._ideas.values():
            idea_words = significant_words(idea.get("description", ""))
            if not idea_words:
                continue
            overlap = len(new_words & idea_words) / min(len(new_words), len(idea_words))
            if overlap >= threshold:
                similar.append({"id": idea["id"], "description": idea["description"],
                                "status": idea["status"], "overlap": round(overlap, 2)})
        return similar

    def get_timeseries(self, exp_id: int) -> list[dict] | None:
        """Read the .metrics.jsonl file for an experiment."""
        exp = self._experiments.get(exp_id)
        if not exp:
            return None
        ts_path = (self.repo_dir / exp["script"]).with_suffix(".metrics.jsonl")
        if not ts_path.exists():
            return []
        points = []
        for line in ts_path.read_text().strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                points.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return points

    def list_all_experiments(self) -> list[dict]:
        """List all experiments across all ideas."""
        return list(self._experiments.values())

    def index_snapshot(self) -> dict:
        """A copy-only view for rebuildable derived indexes (never authority)."""
        with self._lock:
            return {
                "ideas": [dict(x) for x in self._ideas.values()],
                "experiments": [dict(x) for x in self._experiments.values()],
                "notes": {key: [dict(n) for n in value]
                          for key, value in self._notes.items()},
                "version": self._index_version,
            }

    def search_ideas_by_keywords(
        self,
        keywords: list[str],
        include_experiments: bool = False,
        notes_limit: int = 3,
    ) -> list[dict]:
        """Search ideas by multiple keywords, ranked by descending relevance.

        Relevance = fraction of query keywords found in the idea description.

        By default returns slim idea objects: metadata + ``experiment_summary``
        + the most recent ``notes_limit`` notes (insights/milestones).  Pass
        ``include_experiments=True`` to attach full experiment arrays (can be
        very large for busy ideas).
        """
        if not keywords:
            return []

        # Normalize keywords
        query_tokens = {k.lower().strip() for k in keywords if k.strip()}
        if not query_tokens:
            return []

        results = []
        for idea in self._ideas.values():
            desc_lower = idea.get("description", "").lower()
            matched = sum(1 for kw in query_tokens if kw in desc_lower)
            if matched == 0:
                continue
            relevance = matched / len(query_tokens)

            idea_copy = dict(idea)
            exps = self.list_experiments(idea["id"])
            if include_experiments:
                idea_copy["experiments"] = exps
            else:
                idea_copy.pop("experiments", None)
            # Always include a compact experiment_summary (computed freshly so
            # it's always present regardless of whether list_ideas was called).
            if "experiment_summary" not in idea_copy or idea_copy["experiment_summary"] is None:
                completed = [e for e in exps if e["status"] == "completed" and e.get("metrics")]
                latest = max(completed, key=lambda e: e.get("finished_at", "")) if completed else None
                idea_copy["experiment_summary"] = {
                    "total": len(exps),
                    "completed": len(completed),
                    "failed": sum(1 for e in exps if e["status"] == "failed"),
                    "running": sum(1 for e in exps if e["status"] == "running"),
                    "latest_metrics": latest["metrics"] if latest else None,
                    "latest_experiment_id": latest["id"] if latest else None,
                }
            # Attach a small note excerpt (most recent insights/milestones)
            all_notes = self.get_notes(idea["id"], levels=Store.DETAIL_LEVELS)
            if notes_limit > 0:
                idea_copy["notes"] = all_notes[-notes_limit:] if len(all_notes) > notes_limit else all_notes
            else:
                idea_copy["notes"] = all_notes
            idea_copy["relevance"] = round(relevance, 3)
            results.append(idea_copy)

        results.sort(key=lambda r: r["relevance"], reverse=True)
        return results
