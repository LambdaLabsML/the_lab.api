#!/usr/bin/env python3
"""Generate pre-seeded Lab fixtures for API comprehension tests.

Each fixture is a self-contained git repo with .the_lab/ data,
ready to be copied to a temp dir and served by the Lab.

Usage:
    python optimization/tests/seed_fixture.py            # regenerate all
    python optimization/tests/seed_fixture.py t1          # regenerate just T1
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

TESTS_DIR = Path(__file__).parent
SHARED_PROJECT = TESTS_DIR / "shared_project"

_BASE_TIME = datetime(2026, 3, 15, 10, 0, 0, tzinfo=timezone.utc)


def _ts(offset_hours: float = 0) -> str:
    """ISO timestamp offset from base time."""
    return (_BASE_TIME + timedelta(hours=offset_hours)).isoformat()


class FixtureBuilder:
    """Builds a pre-seeded Lab fixture directory."""

    def __init__(self, dest: Path):
        self.dest = dest
        self.lab_dir = dest / ".the_lab" / "experiments"
        self._next_exp_id = 1
        self._time_offset = 0.0

    def _tick(self, hours: float = 0.5) -> str:
        self._time_offset += hours
        return _ts(self._time_offset)

    def init_git(self, solver_content: str = "def guess() -> float:\n    return 0.0\n"):
        """Initialize git repo with shared project files."""
        self.dest.mkdir(parents=True, exist_ok=True)
        # Copy shared project
        for f in SHARED_PROJECT.iterdir():
            if f.name != "__pycache__":
                shutil.copy2(f, self.dest / f.name)
        # Override solver.py
        (self.dest / "solver.py").write_text(solver_content)
        # Gitignore .the_lab
        (self.dest / ".gitignore").write_text(".the_lab/\n__pycache__/\n")
        # Git init
        self._git("init")
        self._git("config", "user.name", "seed")
        self._git("config", "user.email", "seed@local")
        self._git("branch", "-m", "main")
        self._git("add", "-A")
        self._git("commit", "-m", "initial project")

    def add_idea(
        self,
        idea_id: int,
        description: str,
        parent_ids: list[int],
        status: str = "concluded",
        conclusion: str | None = None,
        solver_content: str | None = None,
    ):
        """Create idea metadata and git branch."""
        idea_dir = self.lab_dir / str(idea_id)
        idea_dir.mkdir(parents=True, exist_ok=True)

        idea = {
            "id": idea_id,
            "description": description,
            "parent_ids": parent_ids,
            "status": status,
            "conclusion": conclusion,
            "branch": f"idea/{idea_id}",
            "source": "agent",
            "priority": "normal",
            "resources": [],
            "created_at": self._tick(),
        }
        (idea_dir / "idea.json").write_text(json.dumps(idea, indent=2))
        (idea_dir / "notes.json").write_text("[]")

        # Create git branch and optionally modify solver.py
        parent_branch = f"idea/{parent_ids[0]}" if parent_ids else "main"
        self._git("branch", f"idea/{idea_id}", parent_branch)

        if solver_content:
            self._git("checkout", f"idea/{idea_id}")
            (self.dest / "solver.py").write_text(solver_content)
            self._git("add", "solver.py")
            # Only commit if there are actual changes
            result = subprocess.run(
                ["git", "diff", "--cached", "--quiet"], cwd=str(self.dest),
                capture_output=True,
            )
            if result.returncode != 0:  # has changes
                self._git("commit", "-m", f"idea {idea_id}: {description[:50]}")
            self._git("checkout", "main")

    def add_experiment(
        self,
        idea_id: int,
        description: str,
        status: str = "completed",
        metrics: dict | None = None,
        error: str | None = None,
        script_content: str = "#!/bin/bash\nset -euo pipefail\npython eval_harness.py",
        log_content: str = "",
        err_content: str = "",
        tags: list[str] | None = None,
    ) -> int:
        """Add an experiment to an idea. Returns exp_id."""
        exp_id = self._next_exp_id
        self._next_exp_id += 1
        idea_dir = self.lab_dir / str(idea_id)

        # Count existing experiments for this idea to get seq
        existing = [f for f in idea_dir.iterdir() if f.suffix == ".json" and f.stem.isdigit()]
        seq = len(existing) + 1

        created = self._tick(0.1)
        started = self._tick(0.05) if status != "pending" else None
        finished = self._tick(0.2) if status in ("completed", "failed") else None

        exp = {
            "id": exp_id,
            "idea_id": idea_id,
            "seq": seq,
            "label": f"{idea_id}.{seq}",
            "description": description,
            "script": f".the_lab/experiments/{idea_id}/{exp_id}.sh",
            "status": status,
            "meta": {},
            "metrics": metrics,
            "error": error,
            "pid": None,
            "tags": tags or [],
            "created_at": created,
            "started_at": started,
            "finished_at": finished,
        }

        (idea_dir / f"{exp_id}.json").write_text(json.dumps(exp, indent=2))
        (idea_dir / f"{exp_id}.sh").write_text(script_content)
        if log_content:
            (idea_dir / f"{exp_id}.log").write_text(log_content)
        if err_content:
            (idea_dir / f"{exp_id}.err").write_text(err_content)
        return exp_id

    def add_note(self, idea_id: int, text: str, level: str = "observation"):
        """Append a note to an idea."""
        notes_path = self.lab_dir / str(idea_id) / "notes.json"
        notes = json.loads(notes_path.read_text()) if notes_path.exists() else []
        notes.append({"text": text, "level": level, "created_at": self._tick(0.05)})
        notes_path.write_text(json.dumps(notes, indent=2))

    def finalize(self):
        """Ensure main branch is checked out."""
        self._git("checkout", "main")

    def _git(self, *args):
        subprocess.run(
            ["git", *args], cwd=str(self.dest),
            capture_output=True, check=True,
        )


# ---------------------------------------------------------------------------
# T1: Branching
# ---------------------------------------------------------------------------

def build_t1_branching(dest: Path):
    """15 ideas forming a DAG. Agent must find best and branch correctly."""
    fb = FixtureBuilder(dest)
    fb.init_git("def guess() -> float:\n    return 10.0\n")

    #  DAG structure:
    #  1 (root, score=0.24)
    #  ├── 2 (score=0.52)
    #  │   ├── 4 (score=0.71)
    #  │   │   └── 7 (score=0.88)
    #  │   │       └── 10 (score=0.95) ← BEST
    #  │   └── 5 (score=0.40)
    #  ├── 3 (score=0.33)
    #  │   └── 6 (score=0.55)
    #  │       ├── 8 (score=0.62)
    #  │       └── 9 (score=0.70)
    #  11 (root, abandoned, score=0.15)
    #  12 (root, score=0.45)
    #  └── 13 (score=0.60)
    #      └── 14 (score=0.50)  ← regression
    #  15 (merge of 10+9, active, no experiments yet)

    ideas = [
        (1, "Explore range 0-50", [], "concluded", 0.24, 10.0, "Low score, wide range"),
        (2, "Narrow to 15-40", [1], "concluded", 0.52, 22.0, "Better, moving up"),
        (3, "Try low range 5-25", [1], "concluded", 0.33, 14.0, "Worse direction"),
        (4, "Focus on 25-45", [2], "concluded", 0.71, 30.0, "Good improvement"),
        (5, "Try 10-20 range", [2], "concluded", 0.40, 17.0, "Dead end"),
        (6, "Explore 20-35", [3], "concluded", 0.55, 23.0, "Moderate"),
        (7, "Narrow to 35-45", [4], "concluded", 0.88, 37.0, "Strong direction"),
        (8, "Try 25-30 subrange", [6], "concluded", 0.62, 26.0, "Okay"),
        (9, "Push to 28-35", [6], "concluded", 0.70, 29.5, "Getting closer"),
        (10, "Fine-tune around 40", [7], "concluded", 0.95, 41.0, "Best so far!"),
        (11, "Wild guess: negative", [], "abandoned", 0.15, -5.0, "Abandoned: wrong direction"),
        (12, "Start from 20", [], "concluded", 0.45, 19.0, "Alternative root"),
        (13, "Move to 25-30", [12], "concluded", 0.60, 25.0, "Progress"),
        (14, "Try 22", [13], "concluded", 0.50, 22.0, "Regression from 13"),
        (15, "Merge best approaches", [10, 9], "active", None, None, None),
    ]

    for idea_id, desc, parents, status, score, guess_val, conclusion in ideas:
        solver = f"def guess() -> float:\n    return {guess_val}\n" if guess_val is not None else None
        fb.add_idea(idea_id, desc, parents, status, conclusion, solver)
        if score is not None:
            fb.add_experiment(
                idea_id, f"Eval guess={guess_val}", "completed",
                metrics={"score": score, "guess": guess_val},
                log_content=json.dumps({"metrics": {"score": score, "guess": guess_val}}),
            )
        if conclusion:
            fb.add_note(idea_id, conclusion, "milestone" if score and score > 0.7 else "observation")

    fb.finalize()


# ---------------------------------------------------------------------------
# T2: Experiment Management
# ---------------------------------------------------------------------------

def build_t2_experiment_mgmt(dest: Path):
    """Minimal seed: 2 ideas, 3 experiments. Agent must iterate and improve."""
    fb = FixtureBuilder(dest)
    fb.init_git("def guess() -> float:\n    return 5.0\n")

    # Idea 1: baseline with low score
    fb.add_idea(1, "Initial baseline", [], "active", solver_content="def guess() -> float:\n    return 5.0\n")
    fb.add_experiment(1, "First guess: 5.0", "completed",
                      metrics={"score": 0.12, "guess": 5.0},
                      log_content='{"metrics": {"score": 0.12, "guess": 5.0}}')
    fb.add_note(1, "Score only 0.12 — need to explore higher values", "observation")

    # Idea 2: slightly better, active
    fb.add_idea(2, "Try higher values", [1], "active", solver_content="def guess() -> float:\n    return 15.0\n")
    fb.add_experiment(2, "Guess: 15.0", "completed",
                      metrics={"score": 0.36, "guess": 15.0},
                      log_content='{"metrics": {"score": 0.36, "guess": 15.0}}')
    fb.add_experiment(2, "Guess: 20.0", "completed",
                      metrics={"score": 0.52, "guess": 20.0},
                      log_content='{"metrics": {"score": 0.52, "guess": 20.0}}')
    fb.add_note(2, "Improving. Score 0.52 at guess=20. Keep going higher.", "milestone")

    fb.finalize()


# ---------------------------------------------------------------------------
# T3: Error Recovery
# ---------------------------------------------------------------------------

def build_t3_error_recovery(dest: Path):
    """3 ideas, mix of completed and failed experiments with clear error logs."""
    fb = FixtureBuilder(dest)
    fb.init_git("def guess() -> float:\n    return 21.0\n")

    # Idea 1: one success, two failures
    fb.add_idea(1, "Baseline approach", [], "active",
                solver_content="def guess() -> float:\n    return 21.0\n")
    fb.add_experiment(1, "Working baseline", "completed",
                      metrics={"score": 0.50, "guess": 21.0},
                      log_content='{"metrics": {"score": 0.50, "guess": 21.0}}')
    fb.add_experiment(1, "Try numpy optimization", "failed",
                      error="exit code 1",
                      script_content="#!/bin/bash\nset -euo pipefail\npython -c 'import numpy; print(numpy.sqrt(42))'",
                      log_content="Traceback (most recent call last):\n  File \"<string>\", line 1, in <module>\nImportError: No module named 'numpy'",
                      err_content="exit code 1\n\nImportError: No module named 'numpy'")
    fb.add_experiment(1, "Fix with type coercion", "failed",
                      error="exit code 1",
                      script_content='#!/bin/bash\nset -euo pipefail\npython -c "print(\'score: \' + 42)"',
                      log_content="Traceback (most recent call last):\n  File \"<string>\", line 1, in <module>\nTypeError: can only concatenate str (not \"int\") to str",
                      err_content="exit code 1\n\nTypeError: can only concatenate str (not \"int\") to str")
    fb.add_note(1, "Two experiments failed. Need to investigate.", "debug")

    # Idea 2: timeout failure
    fb.add_idea(2, "Alternative with long computation", [1], "active",
                solver_content="def guess() -> float:\n    import time; time.sleep(999); return 42.0\n")
    fb.add_experiment(2, "Long-running attempt", "failed",
                      error="timeout",
                      script_content="#!/bin/bash\nset -euo pipefail\ntimeout 5 python eval_harness.py",
                      log_content="guess=... (killed: exceeded timeout of 5s)",
                      err_content="timeout: killed after 5 seconds")
    fb.add_note(2, "Experiment timed out — solver has an infinite sleep", "debug")

    # Idea 3: script syntax error
    fb.add_idea(3, "Refactored solver", [1], "active",
                solver_content="def guess() -> float:\n    return 30.0\n")
    fb.add_experiment(3, "Run with broken script", "failed",
                      error="exit code 2",
                      script_content="#!/bin/bash\nset -euo pipefail\npython eval_harnss.py",  # typo
                      log_content="python: can't open file 'eval_harnss.py': [Errno 2] No such file or directory",
                      err_content="exit code 2\npython: can't open file 'eval_harnss.py'")

    fb.finalize()


# ---------------------------------------------------------------------------
# T4: Leaderboard & Search
# ---------------------------------------------------------------------------

def build_t4_leaderboard_search(dest: Path):
    """12 ideas across 3 research directions. Agent must navigate and choose."""
    fb = FixtureBuilder(dest)
    fb.init_git("def guess() -> float:\n    return 0.0\n")

    # Direction A: "Binary search" — ideas 1-4 (best: 0.85)
    fb.add_idea(1, "Binary search: start at 50", [], "concluded", "Established binary search baseline",
                solver_content="def guess() -> float:\n    return 50.0\n")
    fb.add_experiment(1, "Guess 50", "completed", metrics={"score": 0.19, "guess": 50.0},
                      log_content='{"metrics": {"score": 0.19, "guess": 50.0}}', tags=["binary-search"])
    fb.add_note(1, "Score 0.19. Too high. Need to go lower.", "observation")

    fb.add_idea(2, "Binary search: try 25", [1], "concluded", "Narrowing range",
                solver_content="def guess() -> float:\n    return 25.0\n")
    fb.add_experiment(2, "Guess 25", "completed", metrics={"score": 0.60, "guess": 25.0},
                      log_content='{"metrics": {"score": 0.60, "guess": 25.0}}', tags=["binary-search"])

    fb.add_idea(3, "Binary search: try 37", [2], "concluded", "Getting closer",
                solver_content="def guess() -> float:\n    return 37.0\n")
    fb.add_experiment(3, "Guess 37", "completed", metrics={"score": 0.88, "guess": 37.0},
                      log_content='{"metrics": {"score": 0.88, "guess": 37.0}}', tags=["binary-search"])
    fb.add_note(3, "Score 0.88! Binary search converging.", "milestone")

    fb.add_idea(4, "Binary search: try 40", [3], "concluded", "Close to optimum",
                solver_content="def guess() -> float:\n    return 40.0\n")
    fb.add_experiment(4, "Guess 40", "completed", metrics={"score": 0.95, "guess": 40.0},
                      log_content='{"metrics": {"score": 0.95, "guess": 40.0}}', tags=["binary-search"])
    fb.add_note(4, "Score 0.95. Very close!", "milestone")

    # Direction B: "Gradient-like" — ideas 5-8 (best: 0.90)
    fb.add_idea(5, "Gradient: start at 10, step +5", [], "concluded", "Gradient approach baseline",
                solver_content="def guess() -> float:\n    return 10.0\n")
    fb.add_experiment(5, "Guess 10", "completed", metrics={"score": 0.24, "guess": 10.0},
                      log_content='{"metrics": {"score": 0.24, "guess": 10.0}}', tags=["gradient"])

    fb.add_idea(6, "Gradient: step to 20", [5], "concluded", "Improving",
                solver_content="def guess() -> float:\n    return 20.0\n")
    fb.add_experiment(6, "Guess 20", "completed", metrics={"score": 0.52, "guess": 20.0},
                      log_content='{"metrics": {"score": 0.52, "guess": 20.0}}', tags=["gradient"])

    fb.add_idea(7, "Gradient: step to 30", [6], "concluded", "Steady progress",
                solver_content="def guess() -> float:\n    return 30.0\n")
    fb.add_experiment(7, "Guess 30", "completed", metrics={"score": 0.71, "guess": 30.0},
                      log_content='{"metrics": {"score": 0.71, "guess": 30.0}}', tags=["gradient"])

    fb.add_idea(8, "Gradient: step to 38", [7], "concluded", "Approaching optimum",
                solver_content="def guess() -> float:\n    return 38.0\n")
    fb.add_experiment(8, "Guess 38", "completed", metrics={"score": 0.90, "guess": 38.0},
                      log_content='{"metrics": {"score": 0.90, "guess": 38.0}}', tags=["gradient"])
    fb.add_note(8, "Score 0.90. Good but binary search is better.", "milestone")

    # Direction C: "Random sampling" — ideas 9-12 (best: 0.60)
    fb.add_idea(9, "Random: try 70", [], "concluded", "Random exploration",
                solver_content="def guess() -> float:\n    return 70.0\n")
    fb.add_experiment(9, "Guess 70", "completed", metrics={"score": 0.33, "guess": 70.0},
                      log_content='{"metrics": {"score": 0.33, "guess": 70.0}}', tags=["random"])

    fb.add_idea(10, "Random: try 5", [9], "concluded", "Bad direction",
                solver_content="def guess() -> float:\n    return 5.0\n")
    fb.add_experiment(10, "Guess 5", "completed", metrics={"score": 0.12, "guess": 5.0},
                      log_content='{"metrics": {"score": 0.12, "guess": 5.0}}', tags=["random"])

    fb.add_idea(11, "Random: try 35", [9], "concluded", "Better but inconsistent",
                solver_content="def guess() -> float:\n    return 35.0\n")
    fb.add_experiment(11, "Guess 35", "completed", metrics={"score": 0.83, "guess": 35.0},
                      log_content='{"metrics": {"score": 0.83, "guess": 35.0}}', tags=["random"])

    fb.add_idea(12, "Random: try 60", [9], "abandoned", "Abandoned — too far off",
                solver_content="def guess() -> float:\n    return 60.0\n")
    fb.add_experiment(12, "Guess 60", "completed", metrics={"score": 0.57, "guess": 60.0},
                      log_content='{"metrics": {"score": 0.57, "guess": 60.0}}', tags=["random"])

    fb.finalize()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

BUILDERS = {
    "t1": ("t1_branching/fixture", build_t1_branching),
    "t2": ("t2_experiment_mgmt/fixture", build_t2_experiment_mgmt),
    "t3": ("t3_error_recovery/fixture", build_t3_error_recovery),
    "t4": ("t4_leaderboard_search/fixture", build_t4_leaderboard_search),
}


def main():
    targets = sys.argv[1:] or list(BUILDERS.keys())
    for key in targets:
        if key not in BUILDERS:
            print(f"Unknown fixture: {key}. Available: {', '.join(BUILDERS)}", file=sys.stderr)
            sys.exit(1)
        rel_path, builder = BUILDERS[key]
        dest = TESTS_DIR / rel_path
        if dest.exists():
            # Git objects may be read-only
            import stat
            for p in dest.rglob("*"):
                p.chmod(stat.S_IRWXU)
            shutil.rmtree(dest)
        print(f"Building {key} → {rel_path}/ ...", file=sys.stderr)
        builder(dest)
        print(f"  Done.", file=sys.stderr)


if __name__ == "__main__":
    main()
