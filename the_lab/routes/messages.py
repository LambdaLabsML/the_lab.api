"""Inter-agent messages: send, list, mark read.

Messages address a specific agent (``to=agent:<id>``), a role
(``to=role:<role>``), or every agent (``to=all``). Unread messages for the
caller are surfaced by the notifications middleware on every dict-shaped
API response, and an arriving message wakes any ``/wait`` long-poll the
recipient is parked on.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from .. import agents as agents_mod
from .. import messages as messages_mod
from ..deps import REPO_DIR
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
async def send_message(req: MessageRequest, request: Request):
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
        msg = messages_mod.add_message(
            REPO_DIR,
            from_agent=from_agent,
            from_role=from_role,
            to=req.to,
            text=req.text,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    # Broadcast from async context so the loop is guaranteed available.
    try:
        from .. import ws as ws_mod
        ws_mod.broadcaster.broadcast({
            "type": "message_received",
            "id": msg["id"],
            "to": msg["to"],
            "from_role": msg.get("from_role"),
        })
    except Exception:
        pass
    return msg


@router.get("/api/v1/messages")
def list_messages(
    request: Request,
    for_me: bool = False,
    unread: bool = False,
    limit: int = 20,
    offset: int = 0,
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
    if unread:
        msgs = messages_mod.unread_for(REPO_DIR, agent_id=agent_id, role=role, limit=None)
        total = len(msgs)
        page = msgs[offset: offset + limit]
        return {"messages": page, "total": total, "limit": limit, "offset": offset}
    if for_me:
        all_msgs, _ = messages_mod.list_messages(REPO_DIR, limit=None)
        msgs = [m for m in all_msgs if messages_mod.is_for(m, agent_id=agent_id, role=role)]
        total = len(msgs)
        page = msgs[offset: offset + limit]
        return {"messages": page, "total": total, "limit": limit, "offset": offset}
    page, total = messages_mod.list_messages(REPO_DIR, limit=limit, offset=offset)
    return {"messages": page, "total": total, "limit": limit, "offset": offset}


@router.post("/api/v1/messages/{msg_id}/read")
def mark_message_read(msg_id: int, request: Request):
    """Mark a message as read by the calling agent. Idempotent."""
    agent_id, _ = _resolve_sender(request)
    if not agent_id:
        raise HTTPException(
            400, "X-Agent-Id required so the server knows who is reading."
        )
    msg = messages_mod.mark_read(REPO_DIR, msg_id, agent_id)
    if msg is None:
        raise HTTPException(404, f"message {msg_id} not found")
    return {"status": "ok", "id": msg_id}


@router.post("/api/v1/messages/read_all")
def mark_all_messages_read(request: Request):
    """Mark every message currently addressed to the caller as read."""
    agent_id, role = _resolve_sender(request)
    if not agent_id:
        raise HTTPException(400, "X-Agent-Id required.")
    n = messages_mod.mark_all_read(REPO_DIR, agent_id, role)
    return {"marked_read": n}


@router.delete("/api/v1/messages/{msg_id}")
def delete_message(msg_id: int):
    """Remove a message from the inbox. Useful for cleanup."""
    if not messages_mod.delete_message(REPO_DIR, msg_id):
        raise HTTPException(404, f"message {msg_id} not found")
    return {"status": "deleted", "id": msg_id}
