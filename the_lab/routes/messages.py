"""Inter-agent messages: send, list, mark read.

Messages address a specific agent (``to=agent:<id>``), a role
(``to=role:<role>``), or every agent (``to=all``). Unread messages for the
caller are surfaced by the notifications middleware on every dict-shaped
API response, and an arriving message wakes any ``/wait`` long-poll the
recipient is parked on.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from .. import agents as agents_mod
from .. import messages as messages_mod
from ..deps import REPO_DIR, store
from ..schemas import MessageRequest


router = APIRouter()


def _resolve_sender(request: Request) -> tuple[str | None, str | None]:
    """Return (agent_id, role) from the X-Agent-Id header, if registered."""
    agent_id = getattr(request.state, "agent_id", None)
    if not agent_id:
        return None, None
    entry = agents_mod.lookup_agent(REPO_DIR, agent_id) or {}
    return agent_id, entry.get("role")


@router.post("/api/v1/messages", status_code=201)
def send_message(req: MessageRequest, request: Request):
    """Send a message to another agent, a role, or all agents.

    The sender's ``X-Agent-Id`` (and role, looked up from the registry) is
    stamped onto the message automatically. The body's ``to`` field accepts:

      - ``agent:<id>`` — a specific registered agent
      - ``role:<role>`` — every agent currently registered with that role
        (resolved at read-time, not at send-time)
      - ``all`` — every agent

    The message will appear in the recipient's notifications on the next
    API call and wake any ``/wait`` they're currently parked on.

    Example:
        POST /api/v1/messages {"to": "role:engineer",
                                "text": "please run a 2x cache_size variant"}
    """
    from_agent, from_role = _resolve_sender(request)
    try:
        parent = None
        if req.in_reply_to is not None:
            parent = messages_mod.get_message(REPO_DIR, req.in_reply_to)
            if not parent:
                raise ValueError(f"parent message {req.in_reply_to} not found")
            if not _is_operator(request) and not messages_mod.can_see(parent, agent_id=from_agent, role=from_role):
                raise ValueError("you cannot reply to a message you cannot see")
        thread_id = req.thread_id
        if parent:
            inherited = parent.get("thread_id") or parent["id"]
            if thread_id is not None and thread_id != inherited:
                raise ValueError("thread_id must match the parent message thread")
            thread_id = inherited
        elif thread_id is not None:
            root = messages_mod.get_message(REPO_DIR, thread_id)
            if not root:
                raise ValueError(f"thread {thread_id} not found")
            if not _is_operator(request) and not messages_mod.can_see(root, agent_id=from_agent, role=from_role):
                raise ValueError("you cannot post to a thread you cannot see")
        references = _validate_references(req.references)
        msg = messages_mod.add_message(
            REPO_DIR,
            from_agent=from_agent,
            from_role=from_role,
            to=req.to,
            text=req.text,
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
    return {**msg, "recipients": _recipient_listening(req.to)}


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
    listeners = agents_mod.read_listening(REPO_DIR)
    recipients = []
    for entry in agents_mod.list_agents(REPO_DIR):
        if to == "all" or to == f"agent:{entry.get('agent_id')}" or to == f"role:{entry.get('role')}":
            aid = entry.get("agent_id")
            recipients.append({"agent_id": aid, "listening": agents_mod.is_listening(listeners.get(aid))})
    return recipients


def _is_operator(request: Request) -> bool:
    """True when the caller is the operator (dashboard/CLI), not an agent.

    Admin tier is set by the auth gate: Basic credentials, or auth disabled
    entirely. An experiment bearer token is tier "experiment" and does not
    qualify, so a single experiment cannot dump the whole message log.
    """
    return getattr(request.state, "auth_tier", "admin") == "admin"


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
        # ``msgs`` is the complete selected set for identity-scoped queries;
        # filter it before applying the page window so a thread is pageable.
        if not msgs and not (agent_id or role) and _is_operator(request):
            msgs, _ = messages_mod.list_messages(REPO_DIR, limit=None)
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
