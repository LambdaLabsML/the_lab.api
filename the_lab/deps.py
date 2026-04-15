"""Shared state and helper functions used by route modules."""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import HTTPException

from .git_ops import branch_diff
from .store import Store
from .runner import ExperimentRunner
from .stats import ApiStats

# --- Module-level globals (set by init()) ---
store: Store = None  # type: ignore[assignment]
runner: ExperimentRunner = None  # type: ignore[assignment]
api_stats: ApiStats = None  # type: ignore[assignment]
REPO_DIR: Path = None  # type: ignore[assignment]


def init(
    _store: Store,
    _runner: ExperimentRunner,
    _api_stats: ApiStats,
    _repo_dir: Path,
) -> None:
    """Initialise module-level globals so route modules can import them."""
    global store, runner, api_stats, REPO_DIR
    store = _store
    runner = _runner
    api_stats = _api_stats
    REPO_DIR = _repo_dir


# --- Constants ---

SCRIPT_GUARD = """\
if [ -z "$THE_LAB_TOKEN" ]; then
  echo "ERROR: This script must be run via the-lab API (POST /experiments/<id>/start)." >&2
  exit 1
fi
"""

PREAMBLE_SOURCE = 'source .the_lab/preamble.sh 2>/dev/null || true\n'

_LOWER_IS_BETTER_PATTERNS = (
    "loss", "bpb", "perplexity", "error", "mse", "mae", "rmse",
    "cost", "latency", "time", "bytes", "regret", "cer", "wer",
    "fid", "distance", "penalty", "ttft", "_ms",
)

_INTERNAL_META_KEYS = {"git_branch", "git_commit", "worktree"}


# --- Helper functions ---

def _wrap_script(content: str) -> str:
    """Inject a guard + optional preamble that prevents running outside the backend."""
    lines = content.split("\n", 1)
    if lines[0].startswith("#!"):
        return lines[0] + "\n" + SCRIPT_GUARD + PREAMBLE_SOURCE + (lines[1] if len(lines) > 1 else "")
    return "#!/bin/bash\n" + SCRIPT_GUARD + PREAMBLE_SOURCE + content


def _resolve_exp(exp_ref) -> dict:
    """Resolve experiment by label ('1.2') or legacy global ID. Raises 404/400."""
    try:
        exp = store.resolve_experiment(str(exp_ref))
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not exp:
        raise HTTPException(404, (
            f"experiment '{exp_ref}' not found. "
            f"Use GET /api/v1/experiments/<label> with a label like '1.2' (idea.seq) or a global ID. "
            f"To list experiments for an idea: GET /api/v1/ideas/<id>/experiments"
        ))
    return exp


def _idea_context(idea_id: int) -> dict:
    """Build a compact context dict for an idea."""
    idea = store.get_idea(idea_id)
    if not idea:
        return {"idea_id": idea_id}
    return {
        "idea_id": idea["id"],
        "idea_description": idea["description"],
        "branch": idea["branch"],
    }


def _branch_diff_summary(idea_id: int) -> dict | None:
    """Return a compact diff summary for an idea branch vs its parent."""
    idea = store.get_idea(idea_id)
    if not idea or not idea.get("branch"):
        return None
    parent_ids = idea.get("parent_ids", [])
    if parent_ids:
        parent = store.get_idea(parent_ids[0])
        base = parent["branch"] if parent and parent.get("branch") else "main"
    else:
        base = "main"
    try:
        diff_info = branch_diff(idea["branch"], base, cwd=REPO_DIR)
    except Exception:
        return None
    if "error" in diff_info:
        return None
    stat = diff_info.get("stat", "").strip()
    files_changed = [
        line.strip().split("|")[0].strip()
        for line in stat.splitlines()
        if "|" in line
    ]
    summary = {
        "base_branch": base,
        "files_changed": files_changed,
        "stat": stat,
    }
    if not files_changed:
        summary["warning"] = (
            "No code changes detected on this branch vs parent. "
            "Did you forget to edit and save files before creating the experiment?"
        )
    return summary


def _read_metric_directions() -> dict:
    """Read custom metric direction overrides from .the_lab/metric_directions.json."""
    path = REPO_DIR / ".the_lab" / "metric_directions.json"
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def metric_direction(key: str) -> str:
    """Infer whether a metric should be minimized or maximized from its name.

    Checks custom overrides in .the_lab/metric_directions.json first,
    then falls back to pattern matching.
    """
    overrides = _read_metric_directions()
    if key in overrides:
        return overrides[key]
    k = key.lower()
    if any(p in k for p in _LOWER_IS_BETTER_PATTERNS):
        return "minimize"
    return "maximize"


def _read_task() -> dict | None:
    task_path = REPO_DIR / ".the_lab" / "task.json"
    if task_path.exists():
        try:
            return json.loads(task_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return None


def _write_task(text: str) -> dict:
    from .store import _now
    task_path = REPO_DIR / ".the_lab" / "task.json"
    task = {"text": text, "updated_at": _now()}
    task_path.write_text(json.dumps(task, indent=2) + "\n")
    return task
