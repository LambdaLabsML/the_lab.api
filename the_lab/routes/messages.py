"""Inter-agent messages and threads: send, list, search, join, mark read.

Messages address a specific agent (``to=agent:<id>``), a role
(``to=role:<role>``), every agent (``to=all``), or a thread's members
(``to=thread:<id>``). Threads are shared spaces: public ones can be listed,
searched, read in full and joined by any agent. Unread messages for the
caller are surfaced by the notifications middleware on every dict-shaped
API response, and an arriving message wakes any ``/wait`` long-poll the
recipient is parked on.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request

from .. import agents as agents_mod
from .. import messages as messages_mod
from ..deps import REPO_DIR, store
from ..schemas import MessageRequest, ThreadCreateRequest, ThreadMembersRequest, ThreadUpdateRequest


router = APIRouter()


def _resolve_sender(request: Request) -> tuple[str | None, str | None]:
    """Return (agent_id, role) from the X-Agent-Id header, if registered."""
    agent_id = getattr(request.state, "agent_id", None)
    if not agent_id:
        return None, None
    entry = agents_mod.lookup_agent(REPO_DIR, agent_id) or {}
    return agent_id, entry.get("role")


def _is_dashboard_user_sender(request: Request, agent_id: str | None) -> bool:
    """Whether this anonymous, authenticated dashboard request is the UI user.

    The source header is only a presentation hint in general, so it is paired
    with the auth tier here.  In particular an experiment bearer token cannot
    impersonate the dashboard user merely by supplying the header.
    """
    return bool(
        agent_id is None
        and getattr(request.state, "auth_tier", "admin") == "admin"
        and request.headers.get("x-the-lab-source") == "dashboard"
    )


@router.post("/api/v1/messages", status_code=201)
def send_message(req: MessageRequest, request: Request):
    """Send a message to an agent, a role, all agents, or the dashboard user.

    The sender's ``X-Agent-Id`` (and role, looked up from the registry) is
    stamped onto the message automatically. The body's ``to`` field accepts:

      - ``agent:<id>`` — a specific registered agent
      - ``role:<role>`` — every agent currently registered with that role
        (resolved at read-time, not at send-time)
      - ``all`` — every agent

      - ``thread:<id>`` (or omit ``to`` and pass ``thread_id`` /
        ``in_reply_to``) — the members of that thread

    Posting into a thread you can see (any public thread, or a private one
    you belong to) makes you a member. Mentions such as ``exp 12.3`` or
    ``idea 42`` are added to ``references`` automatically. The response may
    carry ``hints`` (e.g. use a thread instead of broadcasting).

    The message will appear in the recipient's notifications on the next
    API call and wake any ``/wait`` they're currently parked on.

    Example:
        POST /api/v1/messages {"to": "role:engineer",
                                "text": "please run a 2x cache_size variant"}
        POST /api/v1/messages {"thread_id": 42, "text": "exp 12.3 confirms it"}
    """
    from_agent, from_role = _resolve_sender(request)
    if _is_dashboard_user_sender(request, from_agent):
        from_agent = from_role = messages_mod.UI_USER
    operator = _is_operator(request)
    try:
        parent = None
        if req.in_reply_to is not None:
            parent = messages_mod.get_message(REPO_DIR, req.in_reply_to)
            if not parent:
                raise ValueError(f"parent message {req.in_reply_to} not found")
            # An identified admin is still an agent for message visibility.
            # The anonymous dashboard/operator may intervene in any thread it
            # can display, while its newly-created message remains stamped as
            # the reserved ``user`` identity.
            if not operator and not messages_mod.can_see(
                parent, agent_id=from_agent, role=from_role,
            ):
                raise ValueError("you cannot reply to a message you cannot see")
        thread_id = req.thread_id
        if req.to and req.to.startswith("thread:"):
            addressed = int(req.to[len("thread:"):]) if req.to[len("thread:"):].isdigit() else None
            if thread_id is not None and addressed != thread_id:
                raise ValueError("to='thread:<id>' must match thread_id")
            thread_id = addressed
        if parent:
            inherited = parent.get("thread_id") or parent["id"]
            if thread_id is not None and thread_id != inherited:
                raise ValueError("thread_id must match the parent message thread")
            thread_id = inherited
        elif thread_id is not None:
            try:
                visible = messages_mod.thread_visible(
                    REPO_DIR, thread_id, agent_id=from_agent, role=from_role, operator=operator,
                )
            except KeyError:
                raise ValueError(f"thread {thread_id} not found") from None
            if not visible:
                raise ValueError("you cannot post to a private thread you are not a member of")
        to = req.to or ("thread" if thread_id is not None else None)
        if not to:
            raise ValueError("'to' is required unless thread_id or in_reply_to is given")
        references = _merge_references(_validate_references(req.references), req.text)
        msg = messages_mod.add_message(
            REPO_DIR,
            from_agent=from_agent,
            from_role=from_role,
            to=to,
            text=req.text,
            tldr=req.tldr,
            in_reply_to=req.in_reply_to,
            thread_id=thread_id,
            references=references,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    # add_message already broadcasts an (enriched) message_received event via
    # broadcast_soon (loop-safe). Re-broadcasting here caused duplicate feed
    # lines, so we no longer do it.
    # Store objects are cached mutable dictionaries: never add response-only
    # state to one or it would silently persist on the next unrelated write.
    out = {**msg, "recipients": _recipient_listening(msg["to"])}
    hints = _send_hints(msg)
    if hints:
        out["hints"] = hints
    return out


# Mentions turned into reference handles: "exp 12.3", "experiment/12.3",
# "exp #12.3", "idea 42", "idea/42". Only existing entities are kept.
_EXP_MENTION = re.compile(r"\bexp(?:eriment)?s?[\s/#:]*(\d+\.\d+)\b", re.IGNORECASE)
_IDEA_MENTION = re.compile(r"\bideas?[\s/#:]*(\d+)\b(?!\.\d)", re.IGNORECASE)
_MAX_AUTO_REFERENCES = 10


def _merge_references(explicit: list[dict], text: str) -> list[dict]:
    """Explicit references plus validated mentions found in *text*, deduped."""
    out = list(explicit)
    seen = {(r["kind"], str(r.get("id", r.get("path")))) for r in out}
    found: list[tuple[str, str]] = []
    for m in _EXP_MENTION.finditer(text or ""):
        found.append(("experiment", m.group(1)))
    for m in _IDEA_MENTION.finditer(text or ""):
        found.append(("idea", m.group(1)))
    added = 0
    for kind, ident in found:
        if (kind, ident) in seen or added >= _MAX_AUTO_REFERENCES:
            continue
        exists = store.get_experiment(ident) if kind == "experiment" else store.get_idea(int(ident))
        if not exists:
            continue
        out.append({"kind": kind, "id": int(ident) if kind == "idea" else ident})
        seen.add((kind, ident))
        added += 1
    return out


_LONG_THREAD = 150


def _send_hints(msg: dict) -> list[str]:
    """Nudges returned with a sent message; never block sending."""
    hints: list[str] = []
    to = msg.get("to") or ""
    tid = msg.get("thread_id")
    if to == "all" and tid in (None, msg["id"]):
        hints.append(
            "Broadcasts notify every agent. For an ongoing discussion, find or start a "
            "thread instead: GET /api/v1/threads?q=<topic>, POST /api/v1/threads "
            "{title, text}, then post with {thread_id, text}."
        )
    if to.startswith("agent:") and to != f"agent:{messages_mod.UI_USER}":
        aid = to[len("agent:"):]
        if not agents_mod.lookup_agent(REPO_DIR, aid):
            hints.append(
                f"No agent '{aid}' is registered right now; it only sees this if resumed "
                "under that id. Check GET /api/v1/agents."
            )
    if tid is not None:
        meta = messages_mod.thread_meta(REPO_DIR, tid)
        if meta and meta.get("status") == "closed":
            hints.append(
                f"Thread {tid} is closed. Reopen it with PATCH /api/v1/threads/{tid} "
                "{\"status\": \"open\"} if the discussion continues, or start a new thread."
            )
        if meta and meta.get("message_count", 0) >= _LONG_THREAD:
            hints.append(
                f"Thread {tid} has {meta['message_count']} messages. Start a new thread for a "
                "new topic (POST /api/v1/threads) and mention it here so others can follow."
            )
    return hints


def _validate_references(references: list[dict]) -> list[dict]:
    """Validate compact handles; messages never accept uploaded/blob content."""
    out: list[dict] = []
    for ref in references:
        if not isinstance(ref, dict) or "data" in ref or "content" in ref:
            raise ValueError("references must be handles, not uploaded content")
        kind = ref.get("kind") or ref.get("type")
        if kind == "idea":
            ident = ref.get("id", ref.get("idea_id"))
            if not isinstance(ident, int) or not store.get_idea(ident):
                raise ValueError("referenced idea does not exist")
            out.append({"kind": "idea", "id": ident})
        elif kind == "experiment":
            ident = ref.get("id", ref.get("experiment_id", ref.get("ref")))
            if ident is None or not store.get_experiment(ident):
                raise ValueError("referenced experiment does not exist")
            out.append({"kind": "experiment", "id": str(ident)})
        elif kind == "resource":
            path = ref.get("path")
            if (not isinstance(path, str) or not path or Path(path).is_absolute()
                    or ".." in Path(path).parts):
                raise ValueError("resource reference must be a repo-relative path")
            resolved = (REPO_DIR / path).resolve()
            if REPO_DIR.resolve() not in resolved.parents or not resolved.exists():
                raise ValueError("referenced resource does not exist in this repository")
            # Keep the caller's safe repo-relative handle: resolving here is
            # solely for containment/existence validation and would otherwise
            # turn a useful in-repo symlink handle into its opaque target.
            out.append({"kind": "resource", "path": path})
        else:
            raise ValueError("reference kind must be idea, experiment, or resource")
    return out


def _recipient_listening(to: str) -> list[dict]:
    if to in (messages_mod.UI_USER, f"agent:{messages_mod.UI_USER}"):
        # The UI user is browser-local; it is never an agent listener.
        return []
    members: dict = {}
    if to.startswith("thread:"):
        meta = messages_mod.thread_meta(REPO_DIR, int(to[len("thread:"):])) or {}
        members = {a: m for a, m in (meta.get("members") or {}).items() if not m.get("muted")}
    listeners = agents_mod.read_listening(REPO_DIR)
    recipients = []
    for entry in agents_mod.list_agents(REPO_DIR):
        if (to == "all" or to == f"agent:{entry.get('agent_id')}" or to == f"role:{entry.get('role')}"
                or entry.get("agent_id") in members):
            aid = entry.get("agent_id")
            recipients.append({"agent_id": aid, "listening": agents_mod.is_listening(listeners.get(aid))})
    return recipients


def _is_operator(request: Request) -> bool:
    """True when the caller is the operator (dashboard/CLI), not an agent.

    Admin tier is set by the auth gate: Basic credentials, or auth disabled
    entirely. An experiment bearer token is tier "experiment" and does not
    qualify, so a single experiment cannot dump the whole message log.
    """
    return (
        not getattr(request.state, "agent_id", None)
        and getattr(request.state, "auth_tier", "admin") == "admin"
    )


@router.get("/api/v1/messages")
def list_messages(
    request: Request,
    for_me: bool = False,
    unread: bool = False,
    limit: int = 20,
    offset: int = 0,
    peek: bool = False,
    thread_id: int | None = None,
    undelivered: bool = False,
):
    """List messages, newest first.

    Use ``?for_me=1`` to restrict to messages addressed to the caller (by id,
    role, or ``all``), and ``?unread=1`` for unread-only. Both flags require
    ``X-Agent-Id``. Supports pagination via ``?limit=N&offset=N``.

    Returns ``{messages, total, limit, offset}`` so callers can page through.
    """
    agent_id, role = _resolve_sender(request)
    if (for_me or unread) and not agent_id:
        raise HTTPException(
            400,
            "for_me / unread require X-Agent-Id; the server can't infer the recipient.",
        )
    # A for_me/unread poll means this agent is actively waiting for messages
    # (the `the-lab messages` loop). Record it so the UI can flag "listening".
    if (for_me or unread) and agent_id:
        try:
            agents_mod.note_message_poll(REPO_DIR, agent_id)
        except Exception:
            pass
    if limit < 1 or limit > 200 or offset < 0:
        raise HTTPException(400, "limit must be 1..200 and offset must be non-negative")
    if undelivered and not agent_id:
        raise HTTPException(400, "undelivered requires X-Agent-Id")
    msgs: list[dict] = []
    if unread:
        msgs = messages_mod.unread_for(REPO_DIR, agent_id=agent_id, role=role, limit=None)
        total = len(msgs)
        page = msgs[offset: offset + limit]
    elif for_me:
        all_msgs, _ = messages_mod.list_messages(REPO_DIR, limit=None)
        msgs = [m for m in all_msgs if messages_mod.is_for(m, agent_id=agent_id, role=role)]
        total = len(msgs)
        page = msgs[offset: offset + limit]
    elif agent_id or role:
        # Identified caller, no explicit filter: scope to what this identity can
        # legitimately see — messages addressed to it, plus its own sent ones.
        # The unfiltered branch below used to run for EVERY caller, so a request
        # with no headers at all returned every message in the store (review
        # read a private DM between two other agents anonymously).
        all_msgs, _ = messages_mod.list_messages(REPO_DIR, limit=None)
        msgs = [
            m for m in all_msgs
            if messages_mod.is_for(m, agent_id=agent_id, role=role)
            or (agent_id and m.get("from_agent") == agent_id)
        ]
        total = len(msgs)
        page = msgs[offset: offset + limit]
    elif _is_operator(request):
        # The dashboard/operator view legitimately shows the whole message log.
        page, total = messages_mod.list_messages(REPO_DIR, limit=limit, offset=offset)
    else:
        raise HTTPException(
            403,
            "identify yourself with X-Agent-Id to read messages "
            "(the full message log is operator-only).",
        )

    if undelivered:
        msgs = messages_mod.undelivered_for(REPO_DIR, agent_id=agent_id, role=role, limit=None)
        total = len(msgs)
        page = msgs[offset: offset + limit]
    if thread_id is not None:
        # A thread is a shared space: list every message in it the caller can
        # see (public-thread posts included), not only their own inbox.
        if not (for_me or unread or undelivered):
            all_msgs, _ = messages_mod.list_messages(REPO_DIR, limit=None)
            msgs = [m for m in all_msgs if _is_operator(request) and not (agent_id or role)
                    or messages_mod.can_see(m, agent_id=agent_id, role=role)]
        msgs = [m for m in msgs if (m.get("thread_id") or m["id"]) == thread_id]
        total = len(msgs)
        page = msgs[offset: offset + limit]

    # Listing an inbox is an automatic delivery acknowledgement, never a
    # read acknowledgement.  `peek` and the explicit CLI undelivered mode are
    # deliberately side-effect free so a client can render then POST /delivered.
    if agent_id and not peek and not undelivered:
        registered_at = messages_mod._created_at_for(REPO_DIR, agent_id)
        ids = [m["id"] for m in page if (
            messages_mod.is_for(m, agent_id=agent_id, role=role)
            and messages_mod.automatic_delivery_eligible(
                REPO_DIR, m, agent_id, registered_at=registered_at,
            )
        )]
        if ids:
            messages_mod.mark_delivered_many(REPO_DIR, ids, agent_id)

    return {"messages": page, "total": total, "limit": limit, "offset": offset}


def _caller(request: Request) -> tuple[str | None, str | None, bool]:
    """(agent_id, role, operator) — 403 when the caller is neither."""
    agent_id, role = _resolve_sender(request)
    operator = _is_operator(request)
    if not (agent_id or operator):
        raise HTTPException(403, "identify yourself with X-Agent-Id")
    return agent_id, role, operator


@router.get("/api/v1/ideas/{idea_id}/mentions")
def idea_experiment_mentions(idea_id: int, request: Request):
    """Threads that discuss each experiment of an idea.

    A message counts when its text mentions the experiment (``12.3``,
    ``exp/12.3``, ``12.1/.2``) or it carries an ``experiment:12.3``
    reference. Per experiment label: the visible threads, most recently
    active first, with the number of mentioning messages and the first / last
    one. Read-only.

    Example:
        GET /api/v1/ideas/1035/mentions
        -> {"mentions": {"1035.1": [{"thread_id": 6484, "title": "idea 1035: …",
                                     "count": 4, "first_id": 6484, "last_id": 6512, …}]}}
    """
    agent_id, role, operator = _caller(request)
    return {"mentions": messages_mod.experiment_mentions(
        REPO_DIR, idea_id, agent_id=agent_id, role=role, operator=operator)}


@router.get("/api/v1/messages/search")
def search_messages(
    request: Request,
    q: str | None = Query(default=None, description="Space-separated terms; all must match (case-insensitive)"),
    from_agent: str | None = Query(default=None, description="Only messages sent by this agent id"),
    thread_id: int | None = Query(default=None, description="Only messages in this thread"),
    ref: str | None = Query(default=None, description="Reference handle, e.g. experiment:12.3, idea:42, resource:path"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    """Search the messages you can see, newest first, with a text snippet.

    Covers your inbox, your own messages and every public thread. Combine
    filters, e.g. who discussed an experiment: ``?ref=experiment:12.3``.
    Does not change delivery or read state.

    Example:
        GET /api/v1/messages/search?q=kv cache oom
        GET /api/v1/messages/search?ref=experiment:747.1
    """
    if not (q or from_agent or thread_id is not None or ref):
        raise HTTPException(400, "give at least one of q, from_agent, thread_id, ref")
    agent_id, role, operator = _caller(request)
    results, total = messages_mod.search_messages(
        REPO_DIR, agent_id=agent_id, role=role, operator=operator, q=q,
        from_agent=from_agent, thread_id=thread_id, ref=ref, limit=limit, offset=offset,
    )
    return {"results": results, "total": total, "limit": limit, "offset": offset}


@router.get("/api/v1/messages/{msg_id}")
def get_message(msg_id: int, request: Request):
    """Fetch one message for a dashboard deep link without changing state."""
    msg = messages_mod.get_message(REPO_DIR, msg_id)
    if msg is None:
        raise HTTPException(404, f"message {msg_id} not found")
    agent_id, role = _resolve_sender(request)
    if not _is_operator(request) and not messages_mod.can_see(
        msg, agent_id=agent_id, role=role,
    ):
        raise HTTPException(403, "you cannot see this message")
    return msg


@router.post("/api/v1/messages/{msg_id}/read")
def mark_message_read(msg_id: int, request: Request):
    """Mark a message as read by the calling agent. Idempotent."""
    agent_id, _ = _resolve_sender(request)
    if not agent_id:
        raise HTTPException(
            400, "X-Agent-Id required so the server knows who is reading."
        )
    msg = messages_mod.get_message(REPO_DIR, msg_id)
    if msg is None:
        raise HTTPException(404, f"message {msg_id} not found")
    _, role = _resolve_sender(request)
    if not messages_mod.can_see(msg, agent_id=agent_id, role=role):
        raise HTTPException(403, "you cannot mark a message you cannot see")
    messages_mod.mark_read(REPO_DIR, msg_id, agent_id)
    return {"status": "ok", "id": msg_id}


@router.post("/api/v1/messages/{msg_id}/delivered")
def mark_message_delivered(msg_id: int, request: Request):
    """Acknowledge receipt without marking a message read."""
    agent_id, role = _resolve_sender(request)
    if not agent_id:
        raise HTTPException(400, "X-Agent-Id required.")
    msg = messages_mod.get_message(REPO_DIR, msg_id)
    if msg is None:
        raise HTTPException(404, f"message {msg_id} not found")
    if not messages_mod.is_for(msg, agent_id=agent_id, role=role):
        raise HTTPException(403, "you cannot acknowledge a message not addressed to you")
    messages_mod.mark_delivered(REPO_DIR, msg_id, agent_id)
    return {"status": "ok", "id": msg_id}


@router.post("/api/v1/messages/read_all")
def mark_all_messages_read(request: Request, before_id: int | None = None, older_than: str | None = None):
    """Mark every message currently addressed to the caller as read."""
    agent_id, role = _resolve_sender(request)
    if not agent_id:
        raise HTTPException(400, "X-Agent-Id required.")
    cutoff = None
    if older_than is not None:
        try:
            cutoff = datetime.fromisoformat(older_than.replace("Z", "+00:00"))
            if cutoff.tzinfo is None:
                cutoff = cutoff.replace(tzinfo=timezone.utc)
        except ValueError:
            raise HTTPException(400, "older_than must be an ISO-8601 timestamp")
    if before_id is not None and before_id < 1:
        raise HTTPException(400, "before_id must be positive")
    n = messages_mod.mark_all_read(REPO_DIR, agent_id, role, before_id=before_id, older_than=cutoff)
    return {"marked_read": n}


@router.delete("/api/v1/messages/{msg_id}")
def delete_message(msg_id: int):
    """Remove a message from the inbox. Useful for cleanup."""
    if not messages_mod.delete_message(REPO_DIR, msg_id):
        raise HTTPException(404, f"message {msg_id} not found")
    return {"status": "deleted", "id": msg_id}


# --- Threads ----------------------------------------------------------------

def _thread_errors(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except KeyError:
        raise HTTPException(404, "thread not found") from None
    except PermissionError as e:
        raise HTTPException(403, str(e)) from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from None


@router.post("/api/v1/threads", status_code=201)
def create_thread(req: ThreadCreateRequest, request: Request):
    """Start a titled thread (a shared conversation) with an opening message.

    ``visibility=public`` (default) lets every agent find, read and join it;
    ``private`` limits it to ``members``. You and ``members`` receive
    messages posted to it. ``announce=true`` sends the opening message to
    ``all`` once so everyone notices it; otherwise others discover it via
    ``GET /threads``. Then post with ``POST /messages {thread_id, text}``.

    Example:
        POST /api/v1/threads {"title": "KV-cache OOMs on long contexts",
                              "text": "exp 12.3 and 12.4 OOM at 200k ...",
                              "members": ["ab12c"], "announce": true}
        -> {"thread": {"id": 57, "title": ..., "member_count": 2, ...},
            "message": {"id": 57, ...}}
    """
    agent_id, role, operator = _caller(request)
    creator = agent_id or messages_mod.UI_USER
    creator_role = role if agent_id else messages_mod.UI_USER
    try:
        references = _merge_references(_validate_references(req.references), req.text)
    except ValueError as e:
        raise HTTPException(400, str(e))
    meta, root = _thread_errors(
        messages_mod.create_thread, REPO_DIR, from_agent=creator, from_role=creator_role,
        title=req.title, text=req.text, tldr=req.tldr, visibility=req.visibility,
        members=req.members, announce=req.announce, references=references,
    )
    out = {"thread": meta, "message": {**root, "recipients": _recipient_listening(root["to"])}}
    unknown = [a for a in req.members
               if a != messages_mod.UI_USER and not agents_mod.lookup_agent(REPO_DIR, a)]
    if unknown:
        out["hints"] = [f"Not currently registered (added anyway): {', '.join(unknown)}"]
    return out


@router.get("/api/v1/threads")
def list_threads(
    request: Request,
    q: str | None = Query(default=None, description="Terms matched against title, summary and opening message"),
    status: str = Query(default="open", description="open | closed | all"),
    mine: bool = Query(default=False, description="Only threads you are a member of"),
    kind: str | None = Query(default=None, description="discussion | suggestion"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    """Find threads you can see, most recently active first.

    Each entry has ``title``, ``summary``, ``status``, ``kind`` (suggestion
    threads carry ``idea_id``), ``message_count``, ``member_count``,
    ``joined``/``muted`` for you, and a preview of the last message. Join one
    with ``POST /threads/{id}/join`` or just post into it.

    Example:
        GET /api/v1/threads?q=oom
        GET /api/v1/threads?mine=1&status=all
    """
    if status not in ("open", "closed", "all"):
        raise HTTPException(400, "status must be open, closed or all")
    agent_id, role, operator = _caller(request)
    threads, total = messages_mod.list_threads(
        REPO_DIR, agent_id=agent_id, role=role, operator=operator,
        q=q, status=status, mine=mine, kind=kind, limit=limit, offset=offset,
    )
    return {"threads": threads, "total": total, "limit": limit, "offset": offset}


@router.get("/api/v1/threads/{thread_id}")
def get_thread(
    thread_id: int,
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0, description="Messages to skip, counting back from the newest"),
    peek: bool = Query(default=False, description="Do not acknowledge delivery"),
):
    """Read a thread: metadata, members and its messages (chronological).

    Public threads are readable in full by any agent, including messages
    from before you joined. ``offset=0`` returns the newest ``limit``
    messages; page back with larger offsets. Reading acknowledges delivery of
    messages addressed to you unless ``peek=1``.
    """
    agent_id, role, operator = _caller(request)
    out = _thread_errors(
        messages_mod.get_thread, REPO_DIR, thread_id, agent_id=agent_id, role=role,
        operator=operator, limit=limit, offset=offset,
    )
    if agent_id and not peek:
        ids = [m["id"] for m in out["messages"]
               if messages_mod.is_for(m, agent_id=agent_id, role=role)]
        if ids:
            messages_mod.mark_delivered_many(REPO_DIR, ids, agent_id)
    return out


@router.post("/api/v1/threads/{thread_id}/join")
def join_thread(thread_id: int, request: Request, muted: bool | None = None):
    """Join a thread you can see so its messages reach you. Idempotent.

    Pass ``?muted=1`` to stay a member without delivery (read it on demand),
    ``?muted=0`` to unmute.
    """
    agent_id, role, operator = _caller(request)
    who = agent_id or messages_mod.UI_USER
    return _thread_errors(messages_mod.join_thread, REPO_DIR, thread_id, agent_id=who,
                          role=role, operator=operator, muted=muted)


@router.post("/api/v1/threads/{thread_id}/leave")
def leave_thread(thread_id: int, request: Request):
    """Stop receiving a thread's messages. You can still read public threads."""
    agent_id, _, _ = _caller(request)
    return _thread_errors(messages_mod.leave_thread, REPO_DIR, thread_id,
                          agent_id=agent_id or messages_mod.UI_USER)


@router.post("/api/v1/threads/{thread_id}/members")
def add_thread_members(thread_id: int, req: ThreadMembersRequest, request: Request):
    """Add agents to a thread you belong to (how private threads grow)."""
    agent_id, _, operator = _caller(request)
    return _thread_errors(messages_mod.add_thread_members, REPO_DIR, thread_id, req.agent_ids,
                          requester=agent_id or messages_mod.UI_USER,
                          operator=operator)


@router.patch("/api/v1/threads/{thread_id}")
def update_thread(thread_id: int, req: ThreadUpdateRequest, request: Request):
    """Edit a thread you belong to: title, rolling ``summary`` (keep it current
    so newcomers can catch up without reading everything), visibility, or
    ``status`` — closing posts the ``conclusion`` to the thread.

    Example:
        PATCH /api/v1/threads/57 {"summary": "Root cause: ...; next: ..."}
        PATCH /api/v1/threads/57 {"status": "closed", "conclusion": "Fixed in exp 12.6"}
    """
    agent_id, role, operator = _caller(request)
    meta, _closing = _thread_errors(
        messages_mod.update_thread, REPO_DIR, thread_id,
        requester=agent_id or messages_mod.UI_USER, requester_role=role if agent_id else messages_mod.UI_USER,
        operator=operator, title=req.title, summary=req.summary,
        status=req.status, conclusion=req.conclusion, visibility=req.visibility,
    )
    return meta
