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


def agent_cwd(request) -> Path:
    """Return the cwd a git-touching route should use for *request*.

    - If X-Agent-Id was provided and matches a registered agent, returns
      that agent's worktree path.
    - If the header was provided but the id is unknown, returns REPO_DIR
      and flags the request so the notifications middleware appends a
      "register first" nudge.
    - If no X-Agent-Id at all, returns REPO_DIR and flags a milder nudge
      reminding the caller that isolated mode is recommended.
    """
    cwd = getattr(request.state, "agent_cwd", None)
    if cwd is not None:
        return cwd
    # No matching worktree — check whether the caller even provided a header.
    if not getattr(request.state, "agent_id", None):
        request.state.git_no_agent_warning = True
    return REPO_DIR


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

def _description_short(desc: str | None, limit: int = 120) -> str:
    """First line of a description, truncated to *limit* chars with an ellipsis.

    Synthesised field for the ``fields=`` projection — agents working under a
    context budget can fetch ``description_short`` instead of the full
    multi-paragraph descriptions some ideas accumulate.
    """
    if not desc:
        return ""
    line = desc.splitlines()[0].strip()
    if len(line) <= limit:
        return line
    return line[: limit - 1].rstrip() + "…"


def resolve_metric(metrics: dict | None, key: str):
    """Look up a metric by flat key, falling back to dot-notation traversal.

    Progress and result files sometimes nest related metrics in sub-dicts
    (e.g. ``{"subagent": {"cache_hits": 12}}``); supporting ``a.b.c`` in
    metric queries lets the leaderboard / search / aggregate endpoints
    reach those values without flattening every script's output.

    A literal flat key always wins on collision: ``{"a.b": 1}`` resolves
    to ``1`` for ``"a.b"`` even if a dotted walk would also succeed. The
    walk only returns a value when each hop is a dict and the leaf is
    numeric; otherwise None.
    """
    if not metrics or not key:
        return None
    if key in metrics:
        return metrics[key]
    if "." not in key:
        return None
    node = metrics
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    if isinstance(node, (int, float)):
        return node
    return None


def project_fields(data, fields: str | None):
    """Restrict a response to the given comma-separated field list.

    - List response → apply per-item (each dict gets keys filtered).
    - Dict response → apply at top level.
    - Scalars / other types → return unchanged.

    Unknown field names are silently dropped (consistent with sparse-fieldset
    conventions in REST APIs). ``fields`` is ``None`` or empty → no-op.

    Items that are dicts get the magic ``description_short`` field synthesised
    on demand so callers can opt into a compact description view.
    """
    if not fields:
        return data
    wanted = [f.strip() for f in fields.split(",") if f.strip()]
    if not wanted:
        return data

    def _pick(d: dict) -> dict:
        out: dict = {}
        for f in wanted:
            if f == "description_short":
                out[f] = _description_short(d.get("description"))
            elif f in d:
                out[f] = d[f]
        return out

    if isinstance(data, list):
        return [_pick(x) if isinstance(x, dict) else x for x in data]
    if isinstance(data, dict):
        return _pick(data)
    return data


def _wrap_script(content: str) -> str:
    """Inject a guard + optional preamble that prevents running outside the backend.

    Export lines are hoisted before the preamble so variables like
    MAX_WALL_SECONDS are visible when preamble.sh sets the deadline.
    """
    lines = content.split("\n")
    shebang = ""
    if lines and lines[0].startswith("#!"):
        shebang = lines[0] + "\n"
        lines = lines[1:]
    export_lines = [l for l in lines if l.strip().startswith("export ")]
    other_lines = [l for l in lines if not l.strip().startswith("export ")]
    exports_block = "\n".join(export_lines) + "\n" if export_lines else ""
    rest = "\n".join(other_lines)
    return shebang + SCRIPT_GUARD + exports_block + PREAMBLE_SOURCE + rest


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
    # Bump store version so /ideas and /backlog caches (which embed the task
    # via _read_task) get invalidated on task changes.
    if store is not None:
        store._version += 1
    return task
