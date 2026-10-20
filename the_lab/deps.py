"""Shared state and helper functions used by route modules."""
from __future__ import annotations

import json
import os

# Read-only demo mode (set by `the-lab serve --demo` before app import):
# mutations are blocked, the runner never reconciles/schedules, and nothing
# writes to .the_lab/ — safe for showing a live DB snapshot to an audience.
DEMO_MODE = os.environ.get("THE_LAB_DEMO", "").strip().lower() in ("1", "true", "yes")
from pathlib import Path

from fastapi import HTTPException

from . import jsonio
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

PREAMBLE_SOURCE = """\
# A project preamble is optional, but a readable one is part of the experiment
# contract: never hide its diagnostics or continue after it explicitly fails.
if [ -r .the_lab/preamble.sh ]; then
  # Source as a plain command, never an ``if`` condition: Bash disables
  # errexit for conditional bodies, which would silently change a project's
  # intended preamble/body semantics.  If control returns, report an explicit
  # non-zero return; with ``set -e`` the shell naturally stops at the failure.
  source .the_lab/preamble.sh
  _the_lab_preamble_rc=$?
  if [ "$_the_lab_preamble_rc" -ne 0 ]; then
    echo "ERROR: .the_lab/preamble.sh failed (exit $_the_lab_preamble_rc)" >&2
    exit "$_the_lab_preamble_rc"
  fi
fi
"""

_LOWER_IS_BETTER_PATTERNS = (
    "loss", "bpb", "perplexity", "error", "mse", "mae", "rmse",
    "cost", "latency", "time", "bytes", "regret", "cer", "wer",
    "fid", "distance", "penalty", "ttft", "_ms",
)

_INTERNAL_META_KEYS = {"git_branch", "git_commit", "worktree"}


# Every ``meta`` key the Lab's own code paths write at RUNTIME (after
# creation) — never values a caller sets intentionally at submit time. This
# is the single source of truth for "lab-written runtime keys" referenced by
# create/batch-create/clone validation: a caller supplying one of these is
# almost always copying a parent/source run's meta wholesale (e.g. cloning
# manually) and ending up with the Lab's bookkeeping from that OTHER run —
# the dashboard then shows the parent's Slurm state/scores on a brand-new,
# not-yet-started arm. Derived directly from the write sites, not guessed:
#
#   store.py        Store._SERVER_OWNED_META_KEYS   (outdir/worktree/git_*)
#   runner.py        dispatch meta (slurm_job_id, slurm_native_requeue,
#                    slurm_state, slurm_restart_count, slurm_run_token,
#                    slurm_last_restart_at, assigned_resource, assigned_units,
#                    git_branch, git_commit)
#   routes/experiments.py  slurm_attempts (requeue bookkeeping),
#                    budget_completion_recovery /
#                    context_budget_completion_recovery (recovery audits)
#   The result-JSON merge the runner performs at completion
#   (``_metrics_from_result``) always carries card_id/competition_mode/
#   scorecard — see /home/davidh/architects/arc3_lab/postamble.sh's
#   ``_META_KEYS`` — the ARC-AGI-3 solver's own final-score report, merged
#   into experiment.meta by the Lab, not set by the caller.
LAB_WRITTEN_META_KEYS = frozenset({
    # Server-owned git/output state (store.py Store._SERVER_OWNED_META_KEYS)
    "outdir", "worktree", "git_branch", "git_commit",
    # Set by the clone endpoint (the source experiment's label)
    "cloned_from",
    # Slurm scheduler/requeue bookkeeping (runner.py, executors/slurm.py)
    "slurm_job_id", "slurm_native_requeue", "slurm_state",
    "slurm_restart_count", "slurm_run_token", "slurm_last_restart_at",
    "slurm_started_at", "slurm_first_started_at", "slurm_finished_at",
    "slurm_attempts", "assigned_resource", "assigned_units",
    # ARC-AGI-3 solver result merged into meta at completion (postamble.sh)
    "card_id", "competition_mode", "scorecard",
    # ARC postamble diagnostics are results too.  Copying a completed run's
    # curve into a live clone makes the clone appear to have the parent's score.
    "score_curve", "score_curve_matches_final", "score_curve_error",
    # Budget-recovery audit trails (routes/experiments.py recover-* routes)
    "budget_completion_recovery", "context_budget_completion_recovery",
})


# Key-NAME hints for secret-ish meta values (mirrors app.py's
# ``_SECRET_BODY_FIELDS`` used to redact the stats replay buffer) — reused
# here to mask values in the clone settings-diff response so a cloned
# experiment's diff never echoes a secret back to the caller.
_SECRET_META_KEY_HINTS = (
    "password", "passwd", "secret", "token", "api_key", "apikey",
    "authorization", "credential", "private_key",
)


def is_secret_meta_key(key: str) -> bool:
    """True if *key*'s name suggests it holds a secret value (case-insensitive
    substring match against the same hint list app.py redacts in request-body
    logging)."""
    low = key.lower()
    return any(hint in low for hint in _SECRET_META_KEY_HINTS)


def mask_secret_meta_value(key: str, value):
    """Return *value*, or ``"[REDACTED]"`` when *key* looks secret-ish."""
    return "[REDACTED]" if is_secret_meta_key(key) else value


def reject_runtime_meta_keys(meta: dict | None, *, where: str = "meta") -> None:
    """422 if *meta* sets any key the Lab itself writes at runtime.

    Previously these were silently ACCEPTED on create/batch-create, so a
    caller who copied ~85 keys from a parent run's experiment.json (then
    forgot — or didn't know — to strip the Lab's own bookkeeping) got a new,
    not-yet-started experiment whose meta already claimed the PARENT's
    card_id/scorecard/slurm_state. Reject loudly instead.
    """
    if not meta:
        return
    bad = sorted(set(meta) & LAB_WRITTEN_META_KEYS)
    if bad:
        raise HTTPException(
            422,
            f"{where} sets lab-written runtime key(s) {bad} — these are "
            f"recorded by the Lab itself while a job runs/finishes and "
            f"cannot be set by the caller. Remove them from {where} "
            f"(see LAB_WRITTEN_META_KEYS in the_lab/deps.py).",
        )


def meta_settings_diff(source: dict | None, new: dict | None) -> dict:
    """Added / changed / removed keys between a source run's meta and a new
    one — used by ``POST /experiments/clone`` to report what the clone
    actually differs by (lab-written runtime keys already stripped out of
    both sides by the caller). Secret-ish values (see ``is_secret_meta_key``)
    are masked in every bucket: this diff goes straight into an API
    response, so a secret copied verbatim from the source run must never be
    echoed back in the clear.
    """
    source, new = source or {}, new or {}
    added: dict = {}
    changed: dict = {}
    removed: dict = {}
    for k, v in new.items():
        if k not in source:
            added[k] = mask_secret_meta_value(k, v)
        elif source[k] != v:
            changed[k] = {"from": mask_secret_meta_value(k, source[k]),
                          "to": mask_secret_meta_value(k, v)}
    for k, v in source.items():
        if k not in new:
            removed[k] = mask_secret_meta_value(k, v)
    return {"added": added, "changed": changed, "removed": removed}


# --- Helper functions ---

def sanitize_error(exc: BaseException | str) -> str:
    """Error text with host filesystem layout redacted.

    Exception strings from git/subprocess routinely embed absolute paths, and
    several routes returned them verbatim to the caller — leaking the operator's
    username, NFS mount layout and repo location. The message itself is useful
    to an agent ("branch already exists", "index.lock present"), so rewrite the
    paths rather than dropping the text: REPO_DIR becomes "<repo>" and the home
    directory becomes "~".
    """
    text = str(exc)
    try:
        repo = str(REPO_DIR)
        if repo and repo != "/":
            text = text.replace(repo, "<repo>")
        home = str(Path.home())
        if home and home != "/":
            text = text.replace(home, "~")
    except Exception:
        pass
    return text



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


def _agents_by_branch(exclude_agent_id: str | None = None) -> dict[str, list[dict]]:
    """Map branch name -> live agents whose worktree has it checked out.

    One HEAD read per registered agent (no git subprocess). Callers that need
    claims for many ideas (``/backlog``) should build this once per request
    instead of calling ``_agents_on_idea`` per idea — the old per-idea loop
    forked ``git`` ideas × agents times (3.8k forks, ~20 s on the arc3 lab).
    Excludes *exclude_agent_id* (typically the requesting agent).
    """
    from . import agents as _agents_mod
    from .git_ops import get_current_branch, read_head_branch
    out: dict[str, list[dict]] = {}
    for entry in _agents_mod.list_agents(REPO_DIR):
        aid = entry.get("agent_id")
        if exclude_agent_id and aid == exclude_agent_id:
            continue
        worktree = entry.get("worktree")
        if not worktree:
            continue
        cb = read_head_branch(worktree)
        if cb is None:
            # Unusual layout / detached HEAD: defer to git's own answer.
            try:
                cb = get_current_branch(cwd=worktree)
            except Exception:
                cb = None
        if cb:
            out.setdefault(cb, []).append({"agent_id": aid, "role": entry.get("role")})
    return out


def _agents_on_idea(
    idea_id: int,
    exclude_agent_id: str | None = None,
    by_branch: dict[str, list[dict]] | None = None,
) -> list[dict]:
    """Return live agents whose worktree is checked out on idea/<idea_id>.

    Excludes *exclude_agent_id* (typically the requesting agent) so an agent
    doesn't see itself as a competitor on its own idea.  Returns slim dicts:
    [{agent_id, role}]. Pass a precomputed ``by_branch`` (from
    ``_agents_by_branch``) when looking up many ideas in one request.
    """
    if by_branch is None:
        by_branch = _agents_by_branch(exclude_agent_id)
    return list(by_branch.get(f"idea/{idea_id}", ()))


def _branch_diff_summary(idea_id: int) -> dict | None:
    """Return a concise diff summary for an idea branch vs its parent.

    Returns which files changed and by how many lines (+/-), not the full patch.
    Shape: {"base_branch": str, "changes": [{"file": str, "added": int, "removed": int}, ...]}
    """
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

    # Parse --numstat output: "<added>\t<removed>\t<file>" per line
    changes = []
    for line in (diff_info.get("numstat") or "").splitlines():
        parts = line.split("\t", 2)
        if len(parts) == 3:
            added_s, removed_s, fname = parts
            try:
                changes.append({
                    "file": fname.strip(),
                    "added": int(added_s),
                    "removed": int(removed_s),
                })
            except ValueError:
                pass  # binary files show "-" — skip

    summary: dict = {"base_branch": base, "changes": changes}
    if not changes:
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
    jsonio.write_json(task_path, task)
    # Bump store version so /ideas and /backlog caches (which embed the task
    # via _read_task) get invalidated on task changes.
    if store is not None:
        store._version += 1
    return task


_LIVE_BASELINES: tuple[float, dict] = (0.0, {})


def live_baselines(max_age_s: float = 600.0) -> dict:
    """Per-level human baselines from the newest closed scorecard, cached (lab 6588). They stay
    Lab-side: the agent never receives them, so competition mode is unchanged."""
    global _LIVE_BASELINES
    import time as _time
    from .live_score import baselines_from_scorecard

    at, cached = _LIVE_BASELINES
    if cached and _time.time() - at < max_age_s:
        return cached
    done = [e for e in (store.list_all_experiments() if store is not None else []) if e.get("status") == "completed"]
    for exp in sorted(done, key=lambda e: str(e.get("finished_at") or ""), reverse=True):
        found = baselines_from_scorecard((exp.get("meta") or {}).get("scorecard"))
        if len(found) >= 25:
            _LIVE_BASELINES = (_time.time(), found)
            return found
    return cached


def with_progress_views(progress: dict, meta: dict | None) -> dict:
    """The one view of a running experiment's progress used by the chart, GET progress and the
    WebSocket: run_pct plus the exact live score (live_score.with_live_score)."""
    from .live_score import with_live_score

    return with_live_score(with_run_pct(progress, meta), live_baselines())


def with_run_pct(progress: dict, meta: dict | None) -> dict:
    """``progress`` plus ``run_pct``: how much of the run's wall-clock budget
    is used (0-100), when the progress reports ``budget_remaining_s`` and the
    run's settings give ``MAX_WALL_SECONDS``. The solver's own ``pct_complete``
    counts games picked up, so it reads 100 for the whole last stretch of a run.
    Returns a copy; the input (often a cached dict) is never modified."""
    if not isinstance(progress, dict):
        return progress
    try:
        remaining = float(progress.get("budget_remaining_s"))
        total = float((meta or {}).get("MAX_WALL_SECONDS"))
    except (TypeError, ValueError):
        return progress
    if not (total > 0) or remaining != remaining:   # NaN guard
        return progress
    pct = max(0.0, min(100.0, 100.0 * (1.0 - remaining / total)))
    return {**progress, "run_pct": round(pct, 1)}


# ── Experiment selection: saved filter (filter=) and/or expression (where=) ──

FILTER_QUERY_DOC = (
    "A saved filter's name or id (GET /api/v1/filters lists them; humans create "
    "them in the dashboard). Combined with `where` by AND."
)
WHERE_QUERY_DOC = (
    "An ad-hoc filter expression, e.g. `meta.MAX_WALL_SECONDS >= 9000 and tag != smoke`, "
    "`idea under 1169`, `metric.score > 20`, `@long` (a saved filter). Fields: meta.<KEY>, "
    "metric.<KEY>, tag, idea (idea under N), label, status, description, attempt, runtime, "
    "created_at/started_at/finished_at; operators = != > >= < <= ~ (contains) !~ in [..] "
    "exists missing; and/or/not, parentheses. Test one with GET /api/v1/filters/check."
)


def experiment_selection(filter: str | None = None, where: str | None = None):
    """A predicate over experiment records for the ``filter`` / ``where``
    query params, or ``None`` when neither is given (= everything).
    Raises HTTP 404 for an unknown saved filter, 400 for a bad expression."""
    from fastapi import HTTPException
    from . import filters as filters_mod

    if not (filter or "").strip() and not (where or "").strip():
        return None
    ctx = filters_mod.Context(
        children=filters_mod.idea_children(store.list_ideas()),
        saved=lambda ref: filters_mod.get_filter(REPO_DIR, ref),
    )
    preds = []
    try:
        if (filter or "").strip():
            rec = filters_mod.get_filter(REPO_DIR, filter)
            if rec is None:
                raise HTTPException(404, f"no saved filter {filter!r} (GET /api/v1/filters lists them)")
            preds.append(filters_mod.compile_expr(rec.get("expr") or "", ctx, exclude=rec.get("exclude"),
                                                  _seen=(rec["id"],)))
        if (where or "").strip():
            preds.append(filters_mod.compile_expr(where, ctx))
    except filters_mod.FilterError as e:
        raise HTTPException(400, f"filter: {e}")
    return lambda exp: all(p(exp) for p in preds)


def select_experiments(exps: list[dict], filter: str | None = None, where: str | None = None) -> list[dict]:
    pred = experiment_selection(filter, where)
    return exps if pred is None else [e for e in exps if pred(e)]
