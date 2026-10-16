"""Saved experiment filters: listed and used by everyone, created and edited
by humans (the dashboard) only. See the_lab/filters.py for the language."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from .. import filters as filters_mod
from .. import messages as messages_mod
from ..deps import REPO_DIR, store, experiment_selection, FILTER_QUERY_DOC, WHERE_QUERY_DOC

router = APIRouter(prefix="/api/v1")


def _human(request: Request, as_user: str | None) -> str:
    """The human making a dashboard request (``user:<name>``, or ``user``).
    Agents can read and use filters but never create or change them: agents
    tend to create many, which humans then can't look through."""
    if not (getattr(request.state, "auth_tier", "admin") == "admin"
            and not getattr(request.state, "agent_id", None)
            and not request.headers.get("x-agent-id")
            and request.headers.get("x-the-lab-source") == "dashboard"):
        raise HTTPException(403, "saved filters are created and edited by humans in the dashboard; "
                                 "agents can list and use them, and pass ad-hoc expressions as `where`")
    if as_user:
        try:
            return messages_mod.human_id(as_user)
        except ValueError as e:
            raise HTTPException(400, str(e))
    return "user"


class FilterCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(..., description="Unique name, e.g. 'long'")
    expr: str = Field("", description="Filter expression (empty = all experiments)")
    description: str = ""
    shared: bool = Field(False, description="Shared filters are listed for everyone; personal ones are grouped under their owner")
    exclude: list[str] = Field(default_factory=list, description="Experiment labels always left out")
    as_user: str | None = Field(None, description="The human creating it (dashboard)")


class FilterUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = None
    expr: str | None = None
    description: str | None = None
    shared: bool | None = None
    exclude: list[str] | None = None
    add_exclude: list[str] | None = Field(None, description="Labels to add to the exclusions")
    remove_exclude: list[str] | None = Field(None, description="Labels to remove from the exclusions")
    as_user: str | None = None


class FilterCopy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    shared: bool = False
    as_user: str | None = None


def _get_or_404(ref: str) -> dict:
    rec = filters_mod.get_filter(REPO_DIR, ref)
    if rec is None:
        raise HTTPException(404, f"no saved filter {ref!r}")
    return rec


def _matches(filter: str | None, where: str | None) -> tuple[list[dict], int]:
    exps = store.list_all_experiments()
    pred = experiment_selection(filter, where)
    return (exps if pred is None else [e for e in exps if pred(e)]), len(exps)


@router.get("/filters")
def list_filters(include_archived: bool = False):
    """Saved experiment filters, shared ones first.

    Use one on the read endpoints with ``?filter=<name>`` (e.g.
    ``GET /api/v1/leaderboard?metric=score&filter=long``), or reference it
    inside an expression as ``@long``. Each has ``expr`` (the expression),
    ``exclude`` (labels always left out), ``owner`` and ``shared``. Only
    humans create and edit them; pass your own expression as ``where=``.
    """
    return {"filters": filters_mod.list_filters(REPO_DIR, include_archived=include_archived)}


@router.get("/filters/check")
def check_filter(
    where: str | None = Query(None, description=WHERE_QUERY_DOC),
    filter: str | None = Query(None, description=FILTER_QUERY_DOC),
    sample: int = Query(10, ge=0, le=200, description="How many matching labels to return"),
):
    """Validate an expression (and/or saved filter) and count what it selects.

    Returns ``{ok, error, matched, total, sample: [labels]}``; a bad
    expression gives ``ok: false`` with the error and its position instead
    of an HTTP error, so it can be used for live validation.
    """
    try:
        selected, total = _matches(filter, where)
    except HTTPException as e:
        return {"ok": False, "error": e.detail, "matched": 0, "total": None, "sample": []}
    selected = sorted(selected, key=lambda e: e.get("created_at") or "", reverse=True)  # newest first
    return {"ok": True, "error": None, "matched": len(selected), "total": total,
            "sample": [e.get("label") or str(e.get("id")) for e in selected[:sample]]}


_vocab_cache: dict = {}


@router.get("/filters/vocabulary")
def filter_vocabulary(values_per_key: int = Query(12, ge=0, le=50)):
    """What filter expressions can refer to, for autocomplete and discovery.

    ``meta`` / ``metrics``: keys found on experiments with how many have them
    and their most common values; ``tags`` with counts; ``statuses``;
    ``saved``: names of saved filters (usable as ``@name``).
    """
    import collections
    exps = store.list_all_experiments()
    key = (len(exps), getattr(store, "_version", None), values_per_key)
    if _vocab_cache.get("key") != key:
        meta_vals: dict[str, collections.Counter] = {}
        metric_count: collections.Counter = collections.Counter()
        tags: collections.Counter = collections.Counter()
        statuses: collections.Counter = collections.Counter()
        for e in exps:
            for k, v in (e.get("meta") or {}).items():
                c = meta_vals.setdefault(k, collections.Counter())
                if isinstance(v, (str, int, float, bool)) and len(str(v)) <= 60:
                    c[str(v) if not isinstance(v, bool) else str(v).lower()] += 1
                else:
                    c[""] += 0
                c["\0count"] += 1
            for k, v in (e.get("metrics") or {}).items():
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    metric_count[k] += 1
            tags.update(e.get("tags") or [])
            if e.get("status"):
                statuses[e["status"]] += 1
        meta = []
        for k, c in meta_vals.items():
            n = c.pop("\0count", 0)
            c.pop("", None)
            meta.append({"key": k, "count": n,
                         "values": [v for v, _ in c.most_common(values_per_key)]})
        meta.sort(key=lambda m: (-m["count"], m["key"]))
        _vocab_cache["key"] = key
        _vocab_cache["data"] = {
            "meta": meta,
            "metrics": [{"key": k, "count": n} for k, n in metric_count.most_common()],
            "tags": [{"tag": t, "count": n} for t, n in tags.most_common()],
            "statuses": [s for s, _ in statuses.most_common()],
        }
    return {**_vocab_cache["data"],
            "saved": [f["name"] for f in filters_mod.list_filters(REPO_DIR)]}


@router.get("/filters/match", include_in_schema=False)
def match_filter(where: str | None = None, filter: str | None = None):
    """All labels a selection matches (dashboard: charts, table, idea means)."""
    selected, total = _matches(filter, where)
    return {"labels": [e.get("label") or str(e.get("id")) for e in selected], "total": total}


@router.get("/filters/{ref}")
def get_filter(ref: str):
    """One saved filter by name or id."""
    return _get_or_404(ref)


@router.post("/filters", status_code=201, include_in_schema=False)
def create_filter(req: FilterCreate, request: Request):
    owner = _human(request, req.as_user)
    try:
        return filters_mod.create_filter(REPO_DIR, name=req.name, expr=req.expr, owner=owner,
                                         description=req.description, shared=req.shared,
                                         exclude=req.exclude)
    except filters_mod.FilterError as e:
        raise HTTPException(400, str(e))


@router.patch("/filters/{ref}", include_in_schema=False)
def update_filter(ref: str, req: FilterUpdate, request: Request):
    me = _human(request, req.as_user)
    rec = _get_or_404(ref)
    exclusion_only = (req.name is None and req.expr is None and req.description is None
                      and req.shared is None and req.exclude is None)
    # The owner edits; on a shared filter any human may adjust exclusions
    # (the dashboard's "exclude from this filter").
    if rec.get("owner") != me and not (rec.get("shared") and exclusion_only):
        raise HTTPException(403, f"only its owner ({rec.get('owner')}) can change this filter; copy it to make your own")
    exclude = req.exclude
    if req.add_exclude or req.remove_exclude:
        cur = set(exclude if exclude is not None else rec.get("exclude") or [])
        cur |= {str(x) for x in req.add_exclude or []}
        cur -= {str(x) for x in req.remove_exclude or []}
        exclude = sorted(cur)
    try:
        return filters_mod.update_filter(REPO_DIR, rec["id"], name=req.name, expr=req.expr,
                                         description=req.description, shared=req.shared, exclude=exclude)
    except filters_mod.FilterError as e:
        raise HTTPException(400, str(e))


@router.delete("/filters/{ref}", include_in_schema=False)
def archive_filter(ref: str, request: Request, as_user: str | None = None):
    me = _human(request, as_user)
    rec = _get_or_404(ref)
    if rec.get("owner") != me and not rec.get("shared"):
        raise HTTPException(403, "only its owner can remove a personal filter")
    return filters_mod.update_filter(REPO_DIR, rec["id"], archived=True)


@router.post("/filters/{ref}/copy", status_code=201, include_in_schema=False)
def copy_filter(ref: str, req: FilterCopy, request: Request):
    owner = _human(request, req.as_user)
    rec = _get_or_404(ref)
    try:
        return filters_mod.create_filter(REPO_DIR, name=req.name, expr=rec.get("expr") or "", owner=owner,
                                         description=rec.get("description") or "", shared=req.shared,
                                         exclude=rec.get("exclude"), copied_from=rec["id"])
    except filters_mod.FilterError as e:
        raise HTTPException(400, str(e))
