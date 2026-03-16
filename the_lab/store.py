"""File-based storage for ideas and experiments.

All state lives in ./experiments/{idea_id}/ within the idea's git worktree:
  idea.json       — idea metadata
  notes.json      — append-only list of notes
  {exp_id}.json   — experiment metadata + results
  {exp_id}.sh     — experiment script
  {exp_id}.log    — stdout+stderr capture
  {exp_id}.err    — error details
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from .git_ops import get_worktree_path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path) -> dict | list:
    if path.exists():
        return json.loads(path.read_text())
    return {}


def _write_json(path: Path, data):
    path.write_text(json.dumps(data, indent=2) + "\n")


class Store:
    """File-based store. All paths are resolved through git worktrees."""

    def __init__(self, repo_dir: Path):
        self.repo_dir = repo_dir
        self._lock = threading.Lock()
        # In-memory counters (recovered from disk on init)
        self._next_idea_id = 1
        self._next_exp_id = 1
        self._recover_counters()

    def _recover_counters(self):
        """Scan existing worktrees to recover the next IDs."""
        worktrees_dir = self.repo_dir / ".worktrees"
        if not worktrees_dir.exists():
            return
        max_idea = 0
        max_exp = 0
        for d in worktrees_dir.iterdir():
            if not d.name.startswith("idea_"):
                continue
            try:
                idea_id = int(d.name.split("_", 1)[1])
                max_idea = max(max_idea, idea_id)
            except ValueError:
                continue
            exp_dir = d / "experiments" / str(idea_id)
            if exp_dir.exists():
                for f in exp_dir.glob("*.json"):
                    if f.stem.isdigit():
                        max_exp = max(max_exp, int(f.stem))
        self._next_idea_id = max_idea + 1
        self._next_exp_id = max_exp + 1

    def _idea_dir(self, idea_id: int) -> Path:
        worktree = get_worktree_path(idea_id, cwd=self.repo_dir)
        return worktree / "experiments" / str(idea_id)

    # --- Ideas ---

    def create_idea(self, description: str, parent_ids: list[int], branch: str) -> dict:
        with self._lock:
            idea_id = self._next_idea_id
            self._next_idea_id += 1

        idea = {
            "id": idea_id,
            "description": description,
            "parent_ids": parent_ids,
            "status": "active",
            "conclusion": None,
            "branch": branch,
            "created_at": _now(),
        }
        return idea

    def save_idea(self, idea: dict):
        """Write idea.json + initialize notes.json. Call after git branch/worktree is set up."""
        idea_dir = self._idea_dir(idea["id"])
        idea_dir.mkdir(parents=True, exist_ok=True)
        _write_json(idea_dir / "idea.json", idea)
        notes_path = idea_dir / "notes.json"
        if not notes_path.exists():
            _write_json(notes_path, [])

    def get_idea(self, idea_id: int) -> dict | None:
        idea_dir = self._idea_dir(idea_id)
        idea_path = idea_dir / "idea.json"
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

    def list_ideas(self, status: str | None = None) -> list[dict]:
        ideas = []
        worktrees_dir = self.repo_dir / ".worktrees"
        if not worktrees_dir.exists():
            return ideas
        for d in sorted(worktrees_dir.iterdir()):
            if not d.name.startswith("idea_"):
                continue
            try:
                idea_id = int(d.name.split("_", 1)[1])
            except ValueError:
                continue
            idea_path = d / "experiments" / str(idea_id) / "idea.json"
            if idea_path.exists():
                idea = _read_json(idea_path)
                if status is None or idea.get("status") == status:
                    ideas.append(idea)
        return ideas

    # --- Notes ---

    LISTING_LEVELS = {"insight", "milestone"}
    DETAIL_LEVELS = {"insight", "milestone", "observation"}
    ALL_LEVELS = {"insight", "milestone", "observation", "debug"}

    def add_note(self, idea_id: int, text: str, level: str = "observation") -> dict:
        if level not in self.ALL_LEVELS:
            raise ValueError(f"invalid note level: {level}, must be one of {self.ALL_LEVELS}")
        note = {"text": text, "level": level, "created_at": _now()}
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

        script_rel = f"experiments/{idea_id}/{exp_id}.sh"
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

        # Create the directory
        idea_dir = self._idea_dir(idea_id)
        idea_dir.mkdir(parents=True, exist_ok=True)

        # Save experiment json
        _write_json(idea_dir / f"{exp_id}.json", exp)
        return exp

    def get_experiment(self, exp_id: int) -> dict | None:
        """Find an experiment by ID (scans all ideas)."""
        worktrees_dir = self.repo_dir / ".worktrees"
        if not worktrees_dir.exists():
            return None
        for d in worktrees_dir.iterdir():
            if not d.name.startswith("idea_"):
                continue
            try:
                idea_id = int(d.name.split("_", 1)[1])
            except ValueError:
                continue
            exp_path = d / "experiments" / str(idea_id) / f"{exp_id}.json"
            if exp_path.exists():
                return _read_json(exp_path)
        return None

    def update_experiment(self, exp_id: int, **fields) -> dict | None:
        exp = self.get_experiment(exp_id)
        if not exp:
            return None
        exp.update(fields)
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
                exps.append(_read_json(f))
        return sorted(exps, key=lambda e: e.get("created_at", ""))

    def list_experiments_by_status(self, status: str) -> list[dict]:
        """List all experiments across all ideas with a given status."""
        results = []
        worktrees_dir = self.repo_dir / ".worktrees"
        if not worktrees_dir.exists():
            return results
        for d in worktrees_dir.iterdir():
            if not d.name.startswith("idea_"):
                continue
            try:
                idea_id = int(d.name.split("_", 1)[1])
            except ValueError:
                continue
            exp_dir = d / "experiments" / str(idea_id)
            if not exp_dir.exists():
                continue
            for f in exp_dir.glob("*.json"):
                if f.stem.isdigit():
                    exp = _read_json(f)
                    if exp.get("status") == status:
                        results.append(exp)
        return results
