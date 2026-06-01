"""Idea CRUD, suggest, and adopt endpoints."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request

from ..cache import cached_response
from ..deps import (
    store,
    REPO_DIR,
    agent_cwd,
    _idea_context,
    _agents_on_idea,
    _branch_diff_summary,
    _description_short,
    _read_task,
    project_fields,
    resolve_metric,
)
from ..git_ops import (
    GitError,
    branch_exists,
    checkout_idea,
    checkout_idea_carry,
    create_branch_from,
    create_branch_from_merge,
    get_current_branch,
    get_default_branch,
)
from ..schemas import (
    NewIdeaRequest,
    SuggestIdeaRequest,
    AdoptRequest,
    ConcludeRequest,
    AbandonRequest,
    ReopenRequest,
    NoteRequest,
)
from ..store import Store

router = APIRouter(prefix="/api/v1")


# --- Ideas ---

def _new_idea_response(idea: dict, similar: list, checked_out: bool) -> dict:
    """Slim creation response — just what the agent needs to proceed."""
    idea_id = idea["id"]
    out: dict = {
        "id":          idea_id,
        "branch":      idea.get("branch"),
        "status":      idea.get("status"),
        "checked_out": checked_out,
        "_more":       f"GET /api/v1/ideas/{idea_id}",
    }
    if similar:
        out["similar_ideas"] = [
            {"id": s["id"], "description": _description_short(s.get("description"))}
            for s in similar[:5]
        ]
    return out


@router.post("/ideas/new", status_code=201)
def create_idea(req: NewIdeaRequest, request: Request):
    """Create a new idea and its corresponding git branch.

    Creates an idea record and a git branch named ``idea/<id>``. If no
    ``parent_ids`` are supplied, the current branch is inspected: when you are
    already on ``idea/N``, that idea is automatically used as the parent. For
    multi-parent ideas the branches are merged; if the merge has conflicts, the
    response contains a ``conflicts`` list instead of the created idea. The
    response also includes a ``similar_ideas`` array when existing ideas have
    descriptions close to the new one, which helps avoid duplicate work.

    Example:
        POST /api/v1/ideas/new {"parent_ids": [1], "description": "test new hypothesis"}
        -> {"id": 3, "branch": "idea/3", "status": "active", "similar_ideas": [...]}
    """
    cwd = agent_cwd(request)
    # parent_ids semantics:
    #   None  → infer from the currently checked-out branch (idea/N → [N])
    #   []    → explicit "no parent" (root idea, branched from main)
    #   [..]  → use as given
    parent_ids = req.parent_ids
    if parent_ids is None:
        parent_ids = []
        current = get_current_branch(cwd=cwd)
        if current.startswith("idea/"):
            try:
                current_idea_id = int(current.split("/")[1])
                if store.get_idea(current_idea_id):
                    parent_ids = [current_idea_id]
            except (ValueError, IndexError):
                pass

    for pid in parent_ids:
        if not store.get_idea(pid):
            raise HTTPException(404, f"parent idea {pid} not found")

    idea = store.create_idea(req.description, parent_ids, branch="")
    idea_id = idea["id"]
    branch_name = f"idea/{idea_id}"
    idea["branch"] = branch_name

    try:
        if len(parent_ids) == 0:
            base = get_default_branch(cwd=cwd)
            create_branch_from(branch_name, base, cwd=cwd)
        elif len(parent_ids) == 1:
            parent = store.get_idea(parent_ids[0])
            create_branch_from(branch_name, parent["branch"], cwd=cwd)
        else:
            parent_branches = [store.get_idea(pid)["branch"] for pid in parent_ids]
            # carry=True: stash current changes, create merge branch, pop+commit there
            conflicts = create_branch_from_merge(branch_name, parent_branches, cwd=cwd, carry=True)
            if conflicts is not None:
                return {"status": "conflict", "conflicts": conflicts}
            # create_branch_from_merge already checked out the new branch
            store.save_idea(idea)
            similar = store.find_similar_ideas(req.description)
            similar = [s for s in similar if s["id"] != idea["id"]]
            return _new_idea_response(idea, similar=similar, checked_out=True)

        store.save_idea(idea)
        similar = store.find_similar_ideas(req.description)
        similar = [s for s in similar if s["id"] != idea["id"]]
        checked_out = False
        if req.auto_checkout:
            try:
                # carry: uncommitted changes land on new branch, not the old one
                checkout_idea_carry(idea_id, cwd=cwd)
                checked_out = True
            except GitError:
                pass
        return _new_idea_response(idea, similar=similar, checked_out=checked_out)
    except GitError as e:
        store.release_unused_idea_id(idea_id)
        raise HTTPException(500, str(e))


@router.post("/ideas/{idea_id}/checkout")
def checkout_idea_endpoint(idea_id: int, request: Request):
    """Switch the working tree to an idea's branch.

    Auto-commits any uncommitted changes on the current branch before
    switching. If there are staged or unstaged changes that cannot be
    committed, they are stashed so the checkout can proceed cleanly. Returns
    the new branch name along with the idea context.

    Example:
        POST /api/v1/ideas/2/checkout
        -> {"branch": "idea/2", "stashed": false, "idea_id": 2, "idea_description": "..."}
    """
    cwd = agent_cwd(request)
    idea = store.get_idea(idea_id)
    if not idea:
        raise HTTPException(404, "idea not found")
    branch_name = f"idea/{idea_id}"
    # Idempotent: if already on this branch, return success
    current = get_current_branch(cwd=cwd)
    if current == branch_name:
        return {
            "branch": current,
            "stashed": False,
            "already_on_branch": True,
            "idea_id": idea["id"],
            "idea_description": idea["description"],
        }
    # Refuse if some other worktree (another agent / the main repo) already
    # holds this branch — git would refuse anyway, but with a clearer 409.
    from .. import agents as _agents_mod
    holder = _agents_mod.find_branch_holder(REPO_DIR, branch_name)
    if holder and str(Path(holder["worktree"]).resolve()) != str(Path(cwd).resolve()):
        held_by = holder.get("agent_id") or "the main repo"
        raise HTTPException(
            409,
            f"branch '{branch_name}' is already checked out by {held_by} "
            f"(at {holder['worktree']}). Wait for that agent to finish, "
            "or unregister it via DELETE /api/v1/agents/<id>.",
        )
    try:
        result = checkout_idea(idea_id, cwd=cwd)
        return {
            **result,
            "idea_id": idea["id"],
            "idea_description": idea["description"],
        }
    except GitError as e:
        raise HTTPException(500, str(e))


@router.get("/ideas")
@cached_response(lambda status=None, source=None, fields=None: (status, source, fields))
def list_ideas(
    status: str | None = None,
    source: str | None = None,
    fields: str | None = Query(
        default=None,
        description="Comma-separated field projection (e.g. 'id,status,description_short'). "
                    "Use 'description_short' for the first line truncated to 120 chars. "
                    "Reduces response size dramatically for agents under a context budget.",
    ),
):
    """List all ideas with their notes and a compact experiment summary.

    Each idea in the response includes journal notes (insight/milestone/observation
    levels) and an ``experiment_summary`` object with counts of total, completed,
    failed, and running experiments plus the latest completed metrics. Use the
    ``status`` query parameter (e.g. ``?status=active``) to filter by idea
    status, ``source`` (e.g. ``?source=human``) to filter by origin, and
    ``fields`` to keep only specific fields per idea.

    Example:
        GET /api/v1/ideas?status=active
        GET /api/v1/ideas?fields=id,status,description_short
        -> [{"id": 1, "description": "...", "status": "active",
             "notes": [...], "experiment_summary": {"total": 5, "completed": 3, ...}}, ...]
    """
    ideas = store.list_ideas(status=status, source=source)
    # Skip per-idea note + summary enrichment when those fields aren't asked
    # for — building them dominates response time and is wasted work if the
    # caller will drop them in the projection.
    requested = {f.strip() for f in (fields or "").split(",") if f.strip()}
    want_notes = not requested or "notes" in requested
    want_summary = not requested or "experiment_summary" in requested
    for idea in ideas:
        if want_notes:
            idea["notes"] = store.get_notes(idea["id"], levels=Store.LISTING_LEVELS)
        if want_summary:
            exps = store.list_experiments(idea["id"])
            completed = [e for e in exps if e["status"] == "completed" and e.get("metrics")]
            latest = max(completed, key=lambda e: e.get("finished_at", "")) if completed else None
            idea["experiment_summary"] = {
                "total": len(exps),
                "completed": len(completed),
                "failed": sum(1 for e in exps if e["status"] == "failed"),
                "running": sum(1 for e in exps if e["status"] == "running"),
                "latest_metrics": latest["metrics"] if latest else None,
                "latest_experiment_id": latest["id"] if latest else None,
            }
    # When filtering for suggested ideas and none exist, include the
    # current task so agents always have direction.
    if status == "suggested" and not ideas:
        task = _read_task()
        if task:
            return {"ideas": [], "current_task": task}
    return project_fields(ideas, fields)


@router.get("/ideas/search")
def search_ideas(
    q: str = Query(..., description="Comma-separated keywords to search for in idea descriptions"),
    status: str | None = Query(default=None, description="Filter by idea status (active, concluded, abandoned, suggested)"),
    metric: str | None = Query(default=None, description="Metric key to filter/sort by (e.g. accuracy_per_mtoken)"),
    min_metric: float | None = Query(default=None, description="Minimum metric value — requires metric param"),
    after: str | None = Query(default=None, description="ISO datetime — only ideas created after this timestamp"),
    fields: str | None = Query(
        default=None,
        description="Comma-separated field projection (e.g. 'id,relevance,description_short'). "
                    "Drops everything else; useful when search results would otherwise blow the "
                    "agent's context budget.",
    ),
):
    """Search ideas by multiple keywords, ranked by descending relevance.

    Returns ideas whose descriptions contain the given keywords, sorted by how
    many keywords match (most relevant first). Each result includes its full
    list of experiments with their metrics, so you can evaluate prior results
    before creating a new idea. Use optional filters to narrow results.

    Example:
        GET /api/v1/ideas/search?q=temperature,sampling&status=concluded
        GET /api/v1/ideas/search?q=board&metric=group_accuracy_per_mtoken&min_metric=4.0
        -> [{"id": 5, "description": "...", "relevance": 1.0,
             "experiments": [{"id": 12, "metrics": {...}, ...}]}, ...]
    """
    from datetime import datetime

    keywords = [k.strip() for k in q.split(",") if k.strip()]
    if not keywords:
        raise HTTPException(400, "provide at least one keyword in the 'q' parameter")
    if min_metric is not None and not metric:
        raise HTTPException(400, "min_metric requires the metric parameter")

    results = store.search_ideas_by_keywords(keywords)

    if status is not None:
        results = [r for r in results if r.get("status") == status]

    if after is not None:
        try:
            datetime.fromisoformat(after)
        except ValueError:
            raise HTTPException(400, f"invalid ISO datetime: {after}")
        results = [r for r in results if r.get("created_at", "") > after]

    if metric is not None and min_metric is not None:
        def _has_min_metric(idea: dict) -> bool:
            for exp in idea.get("experiments", []):
                # resolve_metric honours dot-notation paths into nested
                # metric dicts (e.g. "subagent.cache_hits").
                v = resolve_metric(exp.get("metrics"), metric)
                if isinstance(v, (int, float)) and v >= min_metric:
                    return True
            return False
        results = [r for r in results if _has_min_metric(r)]

    return project_fields(results, fields)


@router.post("/ideas/suggest", status_code=201)
def suggest_idea(req: SuggestIdeaRequest):
    """Submit an idea suggestion from a human for the agent to consider.

    Creates an idea in ``suggested`` status with ``source=human``. The idea
    does not get a git branch until the agent adopts it. Use ``priority`` to
    flag urgent suggestions as ``high``, and attach ``resources`` (URL + label
    pairs) to provide context such as papers, datasets, or related issues.

    Example:
        POST /api/v1/ideas/suggest {"description": "Try cosine annealing schedule",
                                     "parent_ids": [1], "priority": "high",
                                     "resources": [{"url": "https://arxiv.org/abs/...", "label": "paper"}]}
        -> {"id": 5, "status": "suggested", "source": "human", "priority": "high", ...}
    """
    for pid in req.parent_ids:
        if not store.get_idea(pid):
            raise HTTPException(404, f"parent idea {pid} not found")
    resources = [r.model_dump() for r in req.resources] if req.resources else []
    idea = store.create_idea(
        req.description,
        parent_ids=req.parent_ids,
        branch="",
        source="human",
        status="suggested",
        priority=req.priority,
        resources=resources,
    )
    store.save_idea(idea)
    return idea


def _has_output_md(script_relpath: str | None) -> bool:
    """True if an output file (HTML or markdown) exists for this experiment."""
    if not script_relpath:
        return False
    p = REPO_DIR / script_relpath
    return (
        (p.parent / (p.stem + ".output.html")).exists()
        or (p.parent / (p.stem + ".output.md")).exists()
    )


@cached_response(lambda idea_id, notes=None: (idea_id, notes))
def _get_idea_cacheable(idea_id: int, notes: str | None):
    """Build the cacheable portion of /ideas/{id}: idea metadata + experiments
    + notes. Excludes ``has_output``, which depends on filesystem state that
    isn't tracked by Store._version. Returns None on miss.

    This function must NOT mutate the store's dicts — its return value is
    shared across cache hits.
    """
    idea = store.get_idea(idea_id)
    if not idea:
        return None
    result = {**idea}  # shallow copy so we don't mutate _ideas[idea_id]
    result["experiments"] = store.list_experiments(idea_id)
    if notes == "all":
        result["notes"] = store.get_notes(idea_id, levels=Store.ALL_LEVELS)
    else:
        result["notes"] = store.get_notes(idea_id, levels=Store.DETAIL_LEVELS)
    return result


_SLIM_EXP_KEEP = {
    "id", "idea_id", "seq", "label", "description", "status",
    "tags", "metrics", "error",
    "runtime_seconds", "runtime", "has_output",
    "created_at", "finished_at",
}
"""Fields kept in a slim experiment object.  The rest (meta, branch_diff,
script, pid, queue_position, queued_at, started_at) are stripped — they are
only relevant for infra debugging, not for an agent reading experiment results.
A note inside ``meta`` that agents *do* care about — the model / think /
max_tokens config — is promoted to a top-level ``config`` dict."""


def _slim_experiment(exp: dict) -> dict:
    """Strip infra-noise from an experiment object.

    Keeps: id, label, description, status, tags, metrics, error, runtime,
    has_output, created_at, finished_at.
    Promotes useful meta keys (model, think, max_tokens, temperature,
    drafter, quantization) to a ``config`` sub-dict.
    """
    out = {k: v for k, v in exp.items() if k in _SLIM_EXP_KEEP}
    meta = exp.get("meta") or {}
    config_keys = ("model", "think", "template", "max_tokens", "temperature",
                   "drafter", "quantization", "kv_cache_dtype", "gpu_mem_util")
    config = {k: meta[k] for k in config_keys if k in meta}
    if config:
        out["config"] = config
    return out


@router.get("/ideas/{idea_id}")
def get_idea(
    idea_id: int,
    request: Request,
    notes: str | None = Query(default=None, description="'all' includes debug-level notes"),
    experiments: str = Query(
        default="slim",
        description=(
            "How much experiment detail to include. "
            "'slim' (default) — id/label/description/status/tags/metrics/config, "
            "infra fields stripped. "
            "'none' — no experiments array, just experiment_summary. "
            "'full' — complete experiment objects (may be very large)."
        ),
    ),
    notes_limit: int = Query(
        default=10,
        description="Max number of notes to return (most recent first). 0 = all.",
    ),
):
    """Get detail for a single idea.

    By default returns slim experiment objects (metrics + config, no slurm/infra
    fields) and the 10 most recent notes.  Tune with query params:

    - ``experiments=none``   — drop experiments array entirely (fastest, just
      ``experiment_summary`` aggregate).
    - ``experiments=slim``   — default: metrics + config, no infra noise.
    - ``experiments=full``   — complete objects (may be 200 KB+ for busy ideas).
    - ``notes=all``          — include debug-level notes (default excludes them).
    - ``notes_limit=0``      — return all notes (default 10).

    For the complete unfiltered response use ``GET /ideas/{id}/full``.

    Example:
        GET /api/v1/ideas/1
        GET /api/v1/ideas/1?experiments=none
        GET /api/v1/ideas/1?experiments=full&notes=all&notes_limit=0
    """
    cached = _get_idea_cacheable(idea_id, notes)
    if cached is None:
        raise HTTPException(404, "idea not found")

    # Ensure experiment_summary is always present (may be None if list_ideas
    # was never called for this idea, since it mutates the store dict as a
    # side effect).
    def _ensure_summary(r: dict, exps: list) -> dict:
        if r.get("experiment_summary") is None:
            completed = [e for e in exps if e["status"] == "completed" and e.get("metrics")]
            latest = max(completed, key=lambda e: e.get("finished_at", "")) if completed else None
            return {**r, "experiment_summary": {
                "total": len(exps),
                "completed": len(completed),
                "failed": sum(1 for e in exps if e["status"] == "failed"),
                "running": sum(1 for e in exps if e["status"] == "running"),
                "latest_metrics": latest["metrics"] if latest else None,
                "latest_experiment_id": latest["id"] if latest else None,
            }}
        return r

    all_exps = cached.get("experiments", [])

    # Build experiment list according to requested detail level
    if experiments == "none":
        result = {k: v for k, v in cached.items() if k != "experiments"}
        result = _ensure_summary(result, all_exps)
    elif experiments == "full":
        fresh_exps = [
            {**exp, "has_output": _has_output_md(exp.get("script"))}
            for exp in all_exps
        ]
        result = {**cached, "experiments": fresh_exps}
        result = _ensure_summary(result, all_exps)
    else:  # "slim" (default)
        fresh_exps = [
            _slim_experiment({**exp, "has_output": _has_output_md(exp.get("script"))})
            for exp in all_exps
        ]
        result = {**cached, "experiments": fresh_exps}
        result = _ensure_summary(result, all_exps)

    # Cap notes list
    if notes_limit > 0 and len(result.get("notes", [])) > notes_limit:
        result = {**result, "notes": result["notes"][-notes_limit:]}

    # Show other agents currently working on this idea so callers can avoid
    # duplicate work. The requesting agent is excluded from the list.
    caller_agent_id = getattr(request.state, "agent_id", None)
    claimed = _agents_on_idea(idea_id, exclude_agent_id=caller_agent_id)
    if claimed:
        result = {**result, "claimed_by": claimed}

    return result


@router.get("/ideas/{idea_id}/full")
def get_idea_full(idea_id: int, notes: str | None = None):
    """Return the complete, unfiltered idea record — full experiment objects,
    all notes, all infra metadata.

    This is the equivalent of ``GET /ideas/{id}?experiments=full&notes_limit=0``
    and is provided as a stable URL for cases where the caller explicitly wants
    everything (e.g. diffing, archiving, full audit).

    For normal agent use prefer ``GET /ideas/{id}`` (slim, default) or
    ``GET /ideas/{id}?experiments=none`` (summary only).
    """
    cached = _get_idea_cacheable(idea_id, notes)
    if cached is None:
        raise HTTPException(404, "idea not found")
    fresh_exps = [
        {**exp, "has_output": _has_output_md(exp.get("script"))}
        for exp in cached.get("experiments", [])
    ]
    return {**cached, "experiments": fresh_exps}


@router.get("/ideas/{idea_id}/parent")
def get_idea_parent(idea_id: int):
    """Get the direct parent ideas with their experiments and metrics.

    Returns the immediate parents of an idea (from ``parent_ids``), each
    enriched with experiments and notes. Useful for understanding what an
    idea branched from and how the parent experiments performed.

    Example:
        GET /api/v1/ideas/5/parent
        -> [{"id": 2, "description": "...", "status": "concluded",
             "experiments": [...], "notes": [...]}]
    """
    idea = store.get_idea(idea_id)
    if not idea:
        raise HTTPException(404, "idea not found")
    parents = []
    for pid in idea.get("parent_ids", []):
        parent = store.get_idea(pid)
        if parent:
            parent = dict(parent)
            parent["experiments"] = store.list_experiments(pid)
            parent["notes"] = store.get_notes(pid)
            parents.append(parent)
    return parents


@router.get("/ideas/{idea_id}/tree")
def get_idea_tree(idea_id: int):
    """Get the ancestor and descendant tree for an idea, with notes.

    Walks the parent chain upward to collect all ancestors and the child chain
    downward to collect all descendants. Each node in the tree includes its
    listing-level notes (insight/milestone/observation). The root idea itself
    is returned with detail-level notes and its full experiment list.

    Example:
        GET /api/v1/ideas/3/tree
        -> {"idea": {"id": 3, ...}, "ancestors": [{"id": 1, ...}], "descendants": [{"id": 5, ...}]}
    """
    idea = store.get_idea(idea_id)
    if not idea:
        raise HTTPException(404, "idea not found")

    ancestors = []
    visited = set()
    queue = list(idea.get("parent_ids", []))
    while queue:
        pid = queue.pop(0)
        if pid in visited:
            continue
        visited.add(pid)
        parent = store.get_idea(pid)
        if parent:
            parent["notes"] = store.get_notes(pid, levels=Store.LISTING_LEVELS)
            ancestors.append(parent)
            queue.extend(parent.get("parent_ids", []))

    descendants = []
    visited = set()
    all_ideas = store.list_ideas()
    queue_ids = [i["id"] for i in all_ideas if idea_id in i.get("parent_ids", [])]
    while queue_ids:
        cid = queue_ids.pop(0)
        if cid in visited:
            continue
        visited.add(cid)
        child = store.get_idea(cid)
        if child:
            child["notes"] = store.get_notes(cid, levels=Store.LISTING_LEVELS)
            descendants.append(child)
            queue_ids.extend(i["id"] for i in all_ideas if cid in i.get("parent_ids", []))

    idea["experiments"] = store.list_experiments(idea_id)
    idea["notes"] = store.get_notes(idea_id, levels=Store.DETAIL_LEVELS)
    return {"idea": idea, "ancestors": ancestors, "descendants": descendants}


@router.get("/ideas/{idea_id}/diff")
def get_idea_diff(
    idea_id: int,
    base: str | None = Query(default=None, description="Base branch (default: first parent's branch, or main)"),
):
    """Get the git diff between this idea's branch and a base branch.

    Shows what changed on this idea's branch relative to a base. By default
    the base is the first parent idea's branch, or ``main`` if the idea has
    no parents. You can override this with the ``?base=`` query parameter to
    diff against any branch.

    Example:
        GET /api/v1/ideas/3/diff
        -> {"diff": "diff --git a/model.py b/model.py\\n...", "base": "idea/1", "head": "idea/3"}
    """
    from ..git_ops import branch_diff as _branch_diff

    idea = store.get_idea(idea_id)
    if not idea:
        raise HTTPException(404, "idea not found")
    idea_branch = idea.get("branch", "")
    if not idea_branch:
        raise HTTPException(400, "idea has no branch")

    if base is None:
        parent_ids = idea.get("parent_ids", [])
        if parent_ids:
            parent = store.get_idea(parent_ids[0])
            base = parent["branch"] if parent and parent.get("branch") else "main"
        else:
            base = "main"

    return _branch_diff(idea_branch, base, cwd=REPO_DIR)


@router.post("/ideas/{idea_id}/conclude")
def conclude_idea(idea_id: int, req: ConcludeRequest):
    """Mark an active idea as concluded with a conclusion text.

    Transitions the idea's status from ``active`` to ``concluded`` and stores
    the provided conclusion. Only active ideas can be concluded; attempting to
    conclude an idea in any other status returns a 400 error.

    Example:
        POST /api/v1/ideas/1/conclude {"conclusion": "Learning rate 3e-4 is optimal"}
        -> {"id": 1, "status": "concluded", "conclusion": "Learning rate 3e-4 is optimal", ...}
    """
    idea = store.get_idea(idea_id)
    if not idea:
        raise HTTPException(404, "idea not found")
    if idea["status"] != "active":
        raise HTTPException(400, f"idea is {idea['status']}, cannot conclude")
    store.update_idea(idea_id, status="concluded", conclusion=req.conclusion)
    return {
        "id": idea_id,
        "status": "concluded",
        "conclusion": req.conclusion,
        "undo": f"POST /api/v1/ideas/{idea_id}/reopen",
        "_more": f"GET /api/v1/ideas/{idea_id}",
    }


@router.post("/ideas/{idea_id}/abandon")
def abandon_idea(idea_id: int, req: AbandonRequest):
    """Abandon an idea that is no longer worth pursuing.

    Transitions the idea's status to ``abandoned`` and records the reason.
    Works on both ``active`` and ``suggested`` ideas. Ideas in other states
    (e.g. already concluded or abandoned) cannot be abandoned again and will
    return a 400 error.

    Example:
        POST /api/v1/ideas/2/abandon {"reason": "Approach too slow, superseded by idea 4"}
        -> {"id": 2, "status": "abandoned", "conclusion": "Approach too slow, superseded by idea 4", ...}
    """
    idea = store.get_idea(idea_id)
    if not idea:
        raise HTTPException(404, "idea not found")
    if idea["status"] not in ("active", "suggested"):
        raise HTTPException(400, f"idea is {idea['status']}, cannot abandon")
    store.update_idea(idea_id, status="abandoned", conclusion=req.reason)
    return {
        "id": idea_id,
        "status": "abandoned",
        "reason": req.reason,
        "undo": f"POST /api/v1/ideas/{idea_id}/reopen",
        "_more": f"GET /api/v1/ideas/{idea_id}",
    }


@router.post("/ideas/{idea_id}/reopen")
def reopen_idea(idea_id: int, req: ReopenRequest):
    """Reopen a concluded or abandoned idea, returning it to active status.

    Moves the idea back to ``active``. The previous conclusion or abandonment
    reason is preserved as an insight-level note so it remains visible in the
    idea's history. A milestone note is also added recording the reopen reason.
    Only ``concluded`` or ``abandoned`` ideas can be reopened.

    Example:
        POST /api/v1/ideas/1/reopen {"reason": "New data suggests this is worth revisiting"}
        -> {"id": 1, "status": "active", "conclusion": null, ...}
    """
    idea = store.get_idea(idea_id)
    if not idea:
        raise HTTPException(404, "idea not found")
    if idea["status"] not in ("concluded", "abandoned"):
        raise HTTPException(400, f"idea is {idea['status']}, cannot reopen")

    old_status = idea["status"]
    old_conclusion = idea.get("conclusion")
    if old_conclusion:
        store.add_note(
            idea_id,
            f"[reopened from {old_status}] previous conclusion: {old_conclusion}",
            level="insight",
        )

    store.add_note(idea_id, f"reopened: {req.reason}", level="milestone")
    return store.update_idea(idea_id, status="active", conclusion=None)


@router.post("/ideas/{idea_id}/note", status_code=201)
def add_note(idea_id: int, req: NoteRequest):
    """Add a journal note to an idea.

    Attaches a timestamped note to the idea. The ``level`` field controls
    visibility: ``insight`` for key findings, ``milestone`` for progress
    markers, ``observation`` (default) for general notes, and ``debug`` for
    low-level details hidden from most views. You can optionally attach
    ``resources`` (URL + label pairs) to link relevant files, papers, or
    dashboards.

    Example:
        POST /api/v1/ideas/1/note {"text": "Loss plateaued at 0.35", "level": "insight",
                                    "resources": [{"url": "https://wandb.ai/run/42", "label": "W&B run"}]}
        -> {"id": 7, "idea_id": 1, "text": "Loss plateaued at 0.35", "level": "insight", ...}
    """
    idea = store.get_idea(idea_id)
    if not idea:
        raise HTTPException(404, "idea not found")
    resources = [r.model_dump() for r in req.resources] if req.resources else None
    return store.add_note(idea_id, req.text, level=req.level, resources=resources)


@router.get("/ideas/{idea_id}/notes")
def get_notes(
    idea_id: int,
    level: str | None = Query(default=None, description="Filter by note level: insight, milestone, observation, debug"),
):
    """Get all notes for an idea.

    Returns the list of journal notes attached to the idea, ordered
    chronologically. Use the ``level`` query parameter to filter by a single
    level (e.g. ``?level=insight``). Without a filter, all levels are returned.

    Example:
        GET /api/v1/ideas/1/notes?level=insight
        -> [{"text": "Loss plateaued at 0.35", "level": "insight", "created_at": "..."}]
    """
    idea = store.get_idea(idea_id)
    if not idea:
        raise HTTPException(404, "idea not found")
    levels = {level} if level else None
    return store.get_notes(idea_id, levels=levels)


@router.post("/ideas/{idea_id}/adopt")
def adopt_idea(idea_id: int, request: Request, req: AdoptRequest | None = None):
    """Adopt a suggested idea, creating its git branch, activating it, and
    checking it out in the calling agent's worktree.

    Transitions a ``suggested`` idea to ``active`` status, creates its
    ``idea/<id>`` branch (from the parent branch or main), and switches the
    caller's working tree to that branch so subsequent edits and commits
    land on ``idea/<id>``. Only ideas in ``suggested`` status can be
    adopted. An optional ``agent_note`` is saved as an observation note on
    the idea. For multi-parent ideas, branches are merged; if conflicts
    arise, the response contains a ``conflicts`` list.

    Refuses with 409 if some other worktree already has ``idea/<id>``
    checked out (a previous agent holds it). Wait for them to finish or
    unregister them via ``DELETE /api/v1/agents/<id>``.

    Example:
        POST /api/v1/ideas/5/adopt {"agent_note": "Looks promising, starting now"}
        -> {"id": 5, "status": "active", "branch": "idea/5", "checked_out": true, ...}
    """
    cwd = agent_cwd(request)
    idea = store.get_idea(idea_id)
    if not idea:
        raise HTTPException(404, "idea not found")
    if idea["status"] != "suggested":
        raise HTTPException(400, f"idea is {idea['status']}, can only adopt suggested ideas")

    branch_name = f"idea/{idea_id}"
    parent_ids = idea.get("parent_ids", [])

    # Refuse if some other worktree already has this branch checked out —
    # git would refuse the subsequent checkout anyway; the 409 gives a
    # clearer error and matches /checkout_idea behaviour.
    from .. import agents as _agents_mod
    holder = _agents_mod.find_branch_holder(REPO_DIR, branch_name)
    if holder and str(Path(holder["worktree"]).resolve()) != str(Path(cwd).resolve()):
        held_by = holder.get("agent_id") or "the main repo"
        raise HTTPException(
            409,
            f"branch '{branch_name}' is already checked out by {held_by} "
            f"(at {holder['worktree']}). Wait for that agent to finish, "
            "or unregister it via DELETE /api/v1/agents/<id>.",
        )

    # Fast-path: if the branch was pre-created by the agent (e.g. manually
    # or after a partial-adopt git failure), skip the branch-creation step
    # and just mark the idea active. We still fall through to the checkout
    # so the worktree's HEAD actually moves to idea/<id>.
    branch_already_existed = branch_exists(branch_name, cwd=cwd)

    try:
        if not branch_already_existed:
            if len(parent_ids) == 0:
                base = get_default_branch(cwd=cwd)
                create_branch_from(branch_name, base, cwd=cwd)
            elif len(parent_ids) == 1:
                parent = store.get_idea(parent_ids[0])
                create_branch_from(branch_name, parent["branch"], cwd=cwd)
            else:
                parent_branches = [store.get_idea(pid)["branch"] for pid in parent_ids]
                # carry=True: uncommitted changes land on new branch, not the old one
                conflicts = create_branch_from_merge(branch_name, parent_branches, cwd=cwd, carry=True)
                if conflicts is not None:
                    return {"status": "conflict", "conflicts": conflicts}

        store.update_idea(idea_id, status="active", branch=branch_name)
        if req and req.agent_note:
            store.add_note(idea_id, req.agent_note, level="observation")

        # Move the agent's worktree onto the idea branch. Idempotent — if
        # they're already on it, checkout_idea returns "already_on_branch".
        # Auto-commits or stashes any uncommitted changes on the previous
        # branch, same as POST /ideas/<id>/checkout does.
        checkout_result = checkout_idea(idea_id, cwd=cwd)
        result = store.get_idea(idea_id)
        result["checked_out"] = True
        result["checkout"] = checkout_result
        return result
    except GitError as e:
        raise HTTPException(500, str(e))
