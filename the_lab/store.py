"""File-based storage for ideas and experiments.

All state lives in {repo}/.the_lab/experiments/{idea_id}/ — a fixed location
at the repo root, outside of any git worktree. This data is not subject to
branch switches or worktree operations.

  {idea_id}/idea.json       — idea metadata
  {idea_id}/notes.json      — append-only list of notes
  {idea_id}/{exp_id}.json   — experiment metadata + results
  {idea_id}/{exp_id}.sh     — experiment script
  {idea_id}/{exp_id}.log    — stdout+stderr capture
  {idea_id}/{exp_id}.err    — error details
"""
from __future__ import annotations

import json
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


class Store:
    """File-based store. All data lives in {repo}/.the_lab/experiments/."""

    def __init__(self, repo_dir: Path):
        self.repo_dir = repo_dir
        self.lab_dir = repo_dir / ".the_lab" / "experiments"
        self.lab_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_gitignore()
        self._lock = threading.Lock()
        self._next_idea_id = 1
        self._next_exp_id = 1
        self._recover_counters()

    def _ensure_gitignore(self):
        """Make sure .the_lab/ is gitignored on ALL branches.

        We add it to .git/info/exclude (repo-level, not branch-level)
        so it works regardless of which branch is checked out.
        """
        exclude_file = self.repo_dir / ".git" / "info" / "exclude"
        exclude_file.parent.mkdir(parents=True, exist_ok=True)
        entry = ".the_lab/"
        if exclude_file.exists():
            content = exclude_file.read_text()
            if entry not in content.split("\n"):
                exclude_file.write_text(content.rstrip("\n") + f"\n{entry}\n")
        else:
            exclude_file.write_text(f"{entry}\n")

    def _recover_counters(self):
        """Scan existing data to recover the next IDs."""
        max_idea = 0
        max_exp = 0
        if not self.lab_dir.exists():
            return
        for d in self.lab_dir.iterdir():
            if not d.is_dir():
                continue
            try:
                idea_id = int(d.name)
                max_idea = max(max_idea, idea_id)
            except ValueError:
                continue
            for f in d.glob("*.json"):
                if f.stem.isdigit():
                    max_exp = max(max_exp, int(f.stem))
        self._next_idea_id = max_idea + 1
        self._next_exp_id = max_exp + 1

    def _idea_dir(self, idea_id: int) -> Path:
        return self.lab_dir / str(idea_id)

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

    def save_idea(self, idea: dict):
        """Write idea.json + initialize notes.json."""
        idea_dir = self._idea_dir(idea["id"])
        idea_dir.mkdir(parents=True, exist_ok=True)
        _write_json(idea_dir / "idea.json", idea)
        notes_path = idea_dir / "notes.json"
        if not notes_path.exists():
            _write_json(notes_path, [])

    def get_idea(self, idea_id: int) -> dict | None:
        idea_path = self._idea_dir(idea_id) / "idea.json"
        if not idea_path.exists():
            return None
        return _read_json(idea_path)

    def update_idea(self, idea_id: int, **fields) -> dict | None:
        idea = self.get_idea(idea_id)
        if not idea:
            return None
        idea.update(fields)
        _write_json(self._idea_dir(idea_id) / "idea.json", idea)
        return idea

    def list_ideas(self, status: str | None = None, source: str | None = None) -> list[dict]:
        ideas = []
        if not self.lab_dir.exists():
            return ideas
        for d in sorted(self.lab_dir.iterdir()):
            if not d.is_dir():
                continue
            try:
                int(d.name)
            except ValueError:
                continue
            idea_path = d / "idea.json"
            if idea_path.exists():
                idea = _read_json(idea_path)
                if status is not None and idea.get("status") != status:
                    continue
                if source is not None and idea.get("source") != source:
                    continue
                ideas.append(idea)
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
            notes = _read_json(notes_path) if notes_path.exists() else []
            notes.append(note)
            _write_json(notes_path, notes)
        return note

    def get_notes(self, idea_id: int, levels: set[str] | None = None) -> list[dict]:
        notes_path = self._idea_dir(idea_id) / "notes.json"
        notes = _read_json(notes_path) if notes_path.exists() else []
        if levels is not None:
            notes = [n for n in notes if n.get("level", "observation") in levels]
        return notes

    # --- Experiments ---

    def create_experiment(self, idea_id: int, description: str, meta: dict | None = None) -> dict:
        with self._lock:
            exp_id = self._next_exp_id
            self._next_exp_id += 1

        script_rel = f".the_lab/experiments/{idea_id}/{exp_id}.sh"
        exp = {
            "id": exp_id,
            "idea_id": idea_id,
            "description": description,
            "script": script_rel,
            "status": "pending",
            "meta": meta or {},
            "metrics": None,
            "error": None,
            "pid": None,
            "created_at": _now(),
            "started_at": None,
            "finished_at": None,
        }

        idea_dir = self._idea_dir(idea_id)
        idea_dir.mkdir(parents=True, exist_ok=True)
        _write_json(idea_dir / f"{exp_id}.json", exp)
        return exp

    def get_experiment(self, exp_id: int) -> dict | None:
        """Find an experiment by ID (scans all idea dirs)."""
        if not self.lab_dir.exists():
            return None
        for d in self.lab_dir.iterdir():
            if not d.is_dir():
                continue
            exp_path = d / f"{exp_id}.json"
            if exp_path.exists():
                return _enrich_experiment(_read_json(exp_path))
        return None

    def update_experiment(self, exp_id: int, **fields) -> dict | None:
        exp = self.get_experiment(exp_id)
        if not exp:
            return None
        exp.update(fields)
        # Re-enrich so runtime is persisted to disk
        exp = _enrich_experiment(exp)
        idea_dir = self._idea_dir(exp["idea_id"])
        _write_json(idea_dir / f"{exp_id}.json", exp)
        return exp

    def list_experiments(self, idea_id: int) -> list[dict]:
        idea_dir = self._idea_dir(idea_id)
        if not idea_dir.exists():
            return []
        exps = []
        for f in sorted(idea_dir.glob("*.json")):
            if f.stem.isdigit():
                exps.append(_enrich_experiment(_read_json(f)))
        return sorted(exps, key=lambda e: e.get("created_at", ""))

    def list_experiments_by_status(self, status: str) -> list[dict]:
        """List all experiments across all ideas with a given status."""
        results = []
        if not self.lab_dir.exists():
            return results
        for d in self.lab_dir.iterdir():
            if not d.is_dir():
                continue
            for f in d.glob("*.json"):
                if f.stem.isdigit():
                    exp = _enrich_experiment(_read_json(f))
                    if exp.get("status") == status:
                        results.append(exp)
        return results

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
        for idea in self.list_ideas():
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
        exp = self.get_experiment(exp_id)
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
        results = []
        if not self.lab_dir.exists():
            return results
        for d in self.lab_dir.iterdir():
            if not d.is_dir():
                continue
            for f in d.glob("*.json"):
                if f.stem.isdigit():
                    results.append(_enrich_experiment(_read_json(f)))
        return results
