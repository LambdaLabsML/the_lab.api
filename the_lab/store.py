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
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path


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
    if path.exists():
        return json.loads(path.read_text())
    return {}


def _write_json(path: Path, data):
    path.write_text(json.dumps(data, indent=2) + "\n")


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
}


class Store:
    """File-based store with in-memory caches.

    Caches:
      _ideas:        {idea_id: dict}     — all idea metadata
      _experiments:  {exp_id: dict}      — all experiment metadata (enriched)
      _exp_by_idea:  {idea_id: set[exp_id]} — reverse index for per-idea lookups
      _notes:        {idea_id: list[dict]} — notes per idea
    """

    def __init__(self, repo_dir: Path):
        self.repo_dir = repo_dir
        self.lab_dir = repo_dir / ".the_lab" / "experiments"
        self.lab_dir.mkdir(parents=True, exist_ok=True)
        self.instance_id = self._load_or_create_instance_id()
        self._ensure_gitignore()
        self._lock = threading.Lock()
        self._next_idea_id = 1
        # In-memory caches — experiments keyed by label string ("10.1")
        self._ideas: dict[int, dict] = {}
        self._experiments: dict[str, dict] = {}
        self._exp_by_idea: dict[int, set[str]] = {}
        self._notes: dict[int, list[dict]] = {}
        # Monotonic counter bumped on every write. The response cache keys
        # entries by (endpoint, params, version), so any write naturally
        # invalidates all cached reads.
        self._version = 0

        self._load_all()

    def _load_or_create_instance_id(self) -> str:
        """Return the 8-char instance ID, creating it on first run."""
        id_path = self.repo_dir / ".the_lab" / "instance.id"
        if id_path.exists():
            return id_path.read_text().strip()
        import secrets
        instance_id = secrets.token_hex(4)  # 8 hex chars
        id_path.write_text(instance_id + "\n")
        return instance_id

    @property
    def version(self) -> int:
        """Monotonic version counter — bumped on every state-mutating write."""
        return self._version

    def _ensure_gitignore(self):
        """Make sure .the_lab/ is gitignored on ALL branches."""
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
        """Scan filesystem once on startup to populate all caches.

        Experiments are identified by label "{idea_id}.{file_stem}".
        Old projects use global IDs as file stems (e.g. 671.json),
        new projects use per-idea seq (e.g. 1.json). Both work —
        the file stem IS the seq number in the label.
        """
        if not self.lab_dir.exists():
            return
        max_idea = 0
        for d in self.lab_dir.iterdir():
            if not d.is_dir():
                continue
            try:
                idea_id = int(d.name)
            except ValueError:
                continue
            max_idea = max(max_idea, idea_id)

            # Load idea
            idea_path = d / "idea.json"
            if idea_path.exists():
                self._ideas[idea_id] = json.loads(idea_path.read_text())

            # Load notes
            notes_path = d / "notes.json"
            if notes_path.exists():
                self._notes[idea_id] = json.loads(notes_path.read_text())

            # Load experiments — supports both old flat layout and new subfolder layout
            exp_labels: set[str] = set()
            # New layout: {seq}/experiment.json (subfolder per experiment)
            for sub in d.iterdir():
                if sub.is_dir() and sub.name.isdigit():
                    exp_json = sub / "experiment.json"
                    if exp_json.exists():
                        seq = int(sub.name)
                        label = f"{idea_id}.{seq}"
                        exp = _enrich_experiment(json.loads(exp_json.read_text()))
                        exp["id"] = label
                        exp["seq"] = seq
                        exp["label"] = label
                        exp["idea_id"] = idea_id
                        exp["script"] = f".the_lab/experiments/{idea_id}/{seq}/script.sh"
                        self._experiments[label] = exp
                        exp_labels.add(label)
            # Old layout: {seq}.json (flat files in idea dir)
            for f in d.glob("*.json"):
                if f.stem.isdigit():
                    seq = int(f.stem)
                    label = f"{idea_id}.{seq}"
                    if label in exp_labels:
                        continue  # already loaded from subfolder
                    exp = _enrich_experiment(json.loads(f.read_text()))
                    exp["id"] = label
                    exp["seq"] = seq
                    exp["label"] = label
                    exp["idea_id"] = idea_id
                    exp["script"] = f".the_lab/experiments/{idea_id}/{seq}.sh"
                    self._experiments[label] = exp
                    exp_labels.add(label)
            self._exp_by_idea[idea_id] = exp_labels

        self._next_idea_id = max_idea + 1

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
            idea_id = self._next_idea_id
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
        idea_dir.mkdir(parents=True, exist_ok=True)
        _write_json(idea_dir / "idea.json", idea)
        notes_path = idea_dir / "notes.json"
        if not notes_path.exists():
            _write_json(notes_path, [])
        # Update cache
        is_new = idea_id not in self._ideas
        with self._lock:
            self._ideas[idea_id] = idea
            if idea_id not in self._notes:
                self._notes[idea_id] = []
            if idea_id not in self._exp_by_idea:
                self._exp_by_idea[idea_id] = set()
            self._version += 1
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
        idea = self.get_idea(idea_id)
        if not idea:
            return None
        old_status = idea.get("status")
        idea.update(fields)
        _write_json(self._idea_dir(idea_id) / "idea.json", idea)
        with self._lock:
            self._ideas[idea_id] = idea
            self._version += 1
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
            notes = self._notes.get(idea_id, [])
            notes.append(note)
            self._notes[idea_id] = notes
            _write_json(notes_path, notes)
            self._version += 1
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
        """Next per-idea seq number: max existing stem + 1, or 1."""
        existing = self._exp_by_idea.get(idea_id, set())
        if not existing:
            return 1
        max_seq = max(self._experiments[lbl]["seq"] for lbl in existing
                      if lbl in self._experiments)
        return max_seq + 1

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

    def create_experiment(
        self,
        idea_id: int,
        description: str,
        meta: dict | None = None,
        tags: list[str] | None = None,
    ) -> dict:
        with self._lock:
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
            "meta": meta or {},
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
        _write_json(exp_dir / "experiment.json", exp)
        # Update cache
        with self._lock:
            self._experiments[label] = exp
            self._exp_by_idea.setdefault(idea_id, set()).add(label)
            self._version += 1
        return exp

    def get_experiment(self, exp_ref) -> dict | None:
        """Get experiment by label string (e.g. '10.1')."""
        return self._experiments.get(str(exp_ref))

    def update_experiment(self, exp_ref, **fields) -> dict | None:
        label = str(exp_ref)
        exp = self._experiments.get(label)
        if not exp:
            return None
        # Diff renderable fields BEFORE mutating. Only fields that are actually
        # serialized by the cached aggregation endpoints should invalidate the
        # response cache — meta-only writes (e.g. worktree bookkeeping from the
        # runner) must not bump _version, or the dashboard churns on every tick.
        renderable_changed = any(
            k in RENDERABLE_EXPERIMENT_FIELDS and exp.get(k) != v
            for k, v in fields.items()
        )
        exp.update(fields)
        exp = _enrich_experiment(exp)
        json_path = self._exp_json_path(exp["idea_id"], exp["seq"])
        json_path.parent.mkdir(parents=True, exist_ok=True)
        _write_json(json_path, exp)
        with self._lock:
            self._experiments[label] = exp
            if renderable_changed:
                self._version += 1
        return exp

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

        outdir = (exp.get("meta") or {}).get("outdir")
        if outdir:
            cleanup_paths.append(Path(outdir))

        worktree = (exp.get("meta") or {}).get("worktree")
        if worktree:
            cleanup_paths.append(Path(worktree))

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
            labels = self._exp_by_idea.get(idea_id)
            if labels is not None:
                labels.discard(label)
            self._version += 1

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
