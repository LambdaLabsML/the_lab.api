"""The Lab — Experiment Management API."""
from __future__ import annotations

import base64
import json as _json
import logging
import os
import secrets
import threading
import time as _time_mod
from datetime import datetime, timezone
from pathlib import Path

import math

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from . import token_registry as _token_registry
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.responses import Response

# ---------------------------------------------------------------------------
# HTTP Basic Auth
#
# Set THE_LAB_USER and THE_LAB_PASSWORD to enable. Both must be set or
# auth is disabled (default: open, safe for local use).
# ---------------------------------------------------------------------------
_AUTH_USER = os.environ.get("THE_LAB_USER", "").strip()
_AUTH_PASSWORD = os.environ.get("THE_LAB_PASSWORD", "").strip()
_AUTH_ENABLED = bool(_AUTH_USER and _AUTH_PASSWORD)

if _AUTH_ENABLED:
    _AUTH_EXPECTED = base64.b64encode(
        f"{_AUTH_USER}:{_AUTH_PASSWORD}".encode()
    ).decode()

logger = logging.getLogger(__name__)

from .store import Store
from .runner import ExperimentRunner
from .stats import ApiStats, normalize_path as _normalize_path
from . import deps
from . import jsonio
from . import perf_log
from . import ws as _ws_mod

# --- Configuration ---
REPO_DIR = Path(os.environ.get("THE_LAB_REPO", os.getcwd())).resolve()

store = Store(REPO_DIR)
runner = ExperimentRunner(store)
api_stats = ApiStats(REPO_DIR / ".the_lab" / "api_stats.json")

# Initialise shared state so route modules can import from deps
deps.init(store, runner, api_stats, REPO_DIR)

# Module start time — used by the /health endpoint to report uptime.
_START_TIME = _time_mod.monotonic()


# ---------------------------------------------------------------------------
# Live per-agent "API call" ticker  (dashboard: agent activity feed)
#
# Emit one lightweight ``agent_api_call`` WS event per routed request made by a
# registered agent, so the dashboard can show a live ticker of what each agent
# is calling. This fires in the response path (inject_notifications), so it must
# be cheap and self-limiting — see _should_emit_api_call below.
#
# THROTTLE: coalesce per-agent to at most one event per _API_CALL_WINDOW_SEC.
# We keep a plain dict {agent_id: last_emit_monotonic}. It's only touched from
# the async HTTP middleware (single-threaded under asyncio), so no lock is
# needed; a stray race would at worst emit one extra event, which is harmless.
# ---------------------------------------------------------------------------
_API_CALL_WINDOW_SEC = 1.0
_api_call_last_emit: dict[str, float] = {}

# Path prefixes whose traffic is polling/messaging plumbing (or would feed back
# on itself). We want AGENT work calls, not this noise.
_API_CALL_SKIP_PREFIXES = (
    "/api/v1/messages",       # covers /messages and /messages/ws
    "/api/v1/notifications",
    "/api/v1/ws",
    "/api/v1/health",
)


def _should_emit_api_call(method: str, path: str, status: int) -> bool:
    """Denoise filter for the agent_api_call ticker.

    Skip polling/messaging plumbing, GETs to /agents* (dashboard poll traffic),
    and server errors. Only genuine agent work calls get through.
    """
    if status >= 500:
        return False
    for prefix in _API_CALL_SKIP_PREFIXES:
        if path.startswith(prefix):
            return False
    # GETs to /agents* are dashboard/poll traffic; keep POST/DELETE (real work).
    if method == "GET" and path.startswith("/api/v1/agents"):
        return False
    return True


def _emit_agent_api_call(agent_id: str, method: str, path: str, status: int,
                         duration_ms: int | None = None,
                         resp_bytes: int | None = None,
                         resp_keys: list | None = None) -> None:
    """Throttled per-agent broadcast of an agent_api_call event. Fail-soft.

    Coalesces to at most one event per agent per _API_CALL_WINDOW_SEC; drops the
    rest. Never raises — event emission must not break the response.
    /wait completions bypass the throttle: they pair with a pending-start event
    (see inject_notifications) and the UI needs the completion to clear it.
    """
    try:
        now = _time_mod.monotonic()
        if path != "/api/v1/wait":
            last = _api_call_last_emit.get(agent_id)
            if last is not None and (now - last) < _API_CALL_WINDOW_SEC:
                return
        _api_call_last_emit[agent_id] = now
        event = {
            "type": "agent_api_call",
            "agent_id": agent_id,
            "method": method,
            "path": path,
            "status": status,
        }
        if duration_ms is not None:
            event["duration_ms"] = duration_ms
        if resp_bytes is not None:
            event["resp_bytes"] = resp_bytes
        if resp_keys:
            event["resp_keys"] = resp_keys
        _ws_mod.broadcaster.broadcast_soon(event)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Per-agent "seen notifications" store  (feedback: STUCK-NOTIFICATION NOISE)
#
# Notifications for failures / suggestions / agent-lifecycle events used to be
# re-emitted on EVERY API response — a permanent token tax for weeks-old
# failures that can't be cleared. We now deliver each distinct event to a given
# agent exactly ONCE by tracking the stable notification keys an agent has
# already seen. Stored as .the_lab/agents/notif_seen.json:
#     {"<agent_id>": ["failure:13.4", "suggestion:42", ...], ...}
#
# A freshly-registered agent (no entry yet) is BASELINED: all currently-existing
# keys are marked seen up-front so it isn't spammed with the pre-existing
# backlog — it only receives notifications for events that happen AFTER it
# registered (feedback: NEW-ONLY-ON-REGISTER). Message previews are handled
# separately (messages have their own read tracking) and are NOT routed here.
#
# File IO is fail-soft (a corrupt/missing store never breaks a response) and
# guarded by a module-level lock, mirroring agents.py note_message_poll().
# Store logic lives here because the feedback restricts edits to app.py.
# ---------------------------------------------------------------------------
_notif_seen_lock = threading.Lock()


def _notif_seen_path() -> Path:
    p = REPO_DIR / ".the_lab" / "agents"
    p.mkdir(parents=True, exist_ok=True)
    return p / "notif_seen.json"


def _read_notif_seen() -> dict:
    """agent_id -> set of seen notification keys. Fail-soft to {}."""
    path = _notif_seen_path()
    try:
        if not path.exists():
            return {}
        data = _json.loads(path.read_text())
        if not isinstance(data, dict):
            return {}
        return {k: set(v) for k, v in data.items() if isinstance(v, list)}
    except (ValueError, TypeError, OSError):
        return {}


def _write_notif_seen(data: dict) -> None:
    """Persist agent_id -> seen-key sets. Fail-soft: never raise."""
    try:
        serializable = {k: sorted(v) for k, v in data.items()}
        jsonio.write_json(_notif_seen_path(), serializable)
    except (TypeError, OSError):
        pass


def _filter_and_mark_seen(agent_id: str, keyed: list[tuple[str, dict]],
                          all_keys: set[str]) -> list[dict]:
    """Return only the notifications this agent hasn't seen, marking them seen.

    ``keyed`` is a list of (stable_key, notification) for the currently-emittable
    failure/suggestion/lifecycle events. ``all_keys`` is every key that currently
    exists — used to baseline a brand-new agent so it skips the backlog.

    Under the lock we: baseline unknown agents to ``all_keys`` (emit nothing for
    pre-existing events), else emit the unseen keys and add them to the seen set.
    """
    if not agent_id:
        # Unidentified caller (dashboard) — can't track per-agent state, so
        # fall back to emitting everything (unchanged behaviour for the UI).
        return [n for _, n in keyed]
    out: list[dict] = []
    with _notif_seen_lock:
        store_data = _read_notif_seen()
        is_new_agent = agent_id not in store_data
        seen = store_data.get(agent_id, set())
        if is_new_agent:
            # NEW-ONLY-ON-REGISTER: baseline this agent — treat every existing
            # key as already-seen so no pre-registration backlog is delivered.
            store_data[agent_id] = set(all_keys)
            _write_notif_seen(store_data)
            return out
        changed = False
        for key, notif in keyed:
            if key in seen:
                continue
            out.append(notif)
            seen.add(key)
            changed = True
        if changed:
            store_data[agent_id] = seen
            _write_notif_seen(store_data)
    return out


def _sanitize_floats(obj):
    """Replace NaN/Infinity with None so JSON serialization doesn't crash."""
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, dict):
        return {k: _sanitize_floats(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize_floats(v) for v in obj]
    return obj


class SafeJSONResponse(HTMLResponse):
    """JSONResponse that replaces NaN/Infinity with null instead of crashing."""
    media_type = "application/json"

    def render(self, content) -> bytes:
        return _json.dumps(_sanitize_floats(content)).encode("utf-8")


app = FastAPI(title="The Lab", version="0.1.0", default_response_class=SafeJSONResponse)


# --- Middleware ---

@app.middleware("http")
async def basic_auth(request: Request, call_next):
    """HTTP Basic Auth gate. Active only when THE_LAB_USER + THE_LAB_PASSWORD are set.

    Exempts the /assets/ path so the browser can load the JS/CSS bundle
    after the auth dialog has been accepted. Every other path — including
    the SPA root and all /api/v1/ routes — requires a valid credential.
    """
    if not _AUTH_ENABLED:
        return await call_next(request)
    # Static assets are fetched by the browser after the page is authenticated;
    # they don't send credentials themselves, so exempt them.
    if request.url.path.startswith("/assets/"):
        return await call_next(request)
    # /health is an unauthenticated liveness probe so a monitor/bridge can hit
    # it without credentials (feedback: /health ENDPOINT).
    if request.url.path == "/api/v1/health":
        return await call_next(request)
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Basic "):
        provided = auth_header[len("Basic "):].strip()
        if secrets.compare_digest(provided, _AUTH_EXPECTED):
            return await call_next(request)
    # Also accept Bearer tokens issued by the runner for experiment processes.
    # This lets preamble/scripts call the API without needing admin credentials.
    if auth_header.startswith("Bearer "):
        token = auth_header[len("Bearer "):].strip()
        if _token_registry.is_valid(token):
            return await call_next(request)
    return Response(
        content="Unauthorized",
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="The Lab"'},
    )


@app.middleware("http")
async def resolve_agent(request, call_next):
    """Read X-Agent-Id, look up the agent's worktree, stash on request.state.

    Routes that touch git can call ``deps.agent_cwd(request)`` to get the
    correct worktree as cwd. When the header is absent, request.state.agent_id
    is None and routes fall back to REPO_DIR.
    """
    from . import agents as _agents_mod
    agent_id = request.headers.get("x-agent-id") or None
    request.state.agent_id = agent_id
    request.state.agent_cwd = None
    request.state.agent_unknown = False
    if agent_id:
        entry = _agents_mod.lookup_agent(REPO_DIR, agent_id)
        if entry:
            from pathlib import Path as _P
            wt = _P(entry["worktree"])
            if wt.exists():
                request.state.agent_cwd = wt
            else:
                request.state.agent_unknown = True
        else:
            request.state.agent_unknown = True
    return await call_next(request)


@app.middleware("http")
async def track_api_stats(request, call_next):
    import time as _time

    # Capture body preview for POST/PUT before passing to handler
    body_preview = ""
    body_bytes = 0
    if request.method in ("POST", "PUT") and request.url.path.startswith("/api/v1/"):
        try:
            raw = await request.body()
            body_bytes = len(raw)
            body_preview = raw.decode(errors="replace")[:200]
        except Exception:
            pass
    t0 = _time.perf_counter()
    response = await call_next(request)
    duration_ms = (_time.perf_counter() - t0) * 1000.0
    path = request.url.path
    # Classify source:
    #   "dashboard" — explicit header from dashboard JS, or browser UA fallback
    #   "mcp"       — MCP bridge (sets X-MCP-Proxy)
    #   "agent"     — anything else hitting /api/v1/ (curl, python, httpx)
    is_dashboard = request.headers.get("x-the-lab-source") == "dashboard"
    if not is_dashboard:
        ua = request.headers.get("user-agent", "")
        is_dashboard = "Mozilla/" in ua
    is_mcp = request.headers.get("x-mcp-proxy") == "true"
    source = "dashboard" if is_dashboard else ("mcp" if is_mcp else "agent")

    if (path.startswith("/api/v1/")
            and not path.startswith("/api/v1/stats")
            and not is_dashboard):
        client = request.client.host if request.client else ""
        api_stats.record(
            request.method, path, client_ip=client,
            query=str(request.url.query) if request.url.query else "",
            body_preview=body_preview,
            status_code=response.status_code,
            mcp=is_mcp,
        )

    # Perf log: opt-in via THE_LAB_PERF_LOG. Unlike stats, we log dashboard
    # calls too — that's the point. Skip the stats endpoint itself to avoid
    # self-referential noise.
    if perf_log.enabled() and not path.startswith("/api/v1/stats"):
        resp_len_hdr = response.headers.get("content-length")
        try:
            resp_bytes = int(resp_len_hdr) if resp_len_hdr else 0
        except ValueError:
            resp_bytes = 0
        perf_log.log_request(
            method=request.method,
            path=path,
            normalized_path=_normalize_path(path),
            status=response.status_code,
            duration_ms=duration_ms,
            source=source,
            query=str(request.url.query) if request.url.query else "",
            body_bytes=body_bytes,
            response_bytes=resp_bytes,
            client_ip=request.client.host if request.client else "",
        )
    return response


def build_notifications(request) -> list[dict]:
    """Collect every actionable notification for the caller.

    Single source of truth used by both the response-rewriting middleware
    and the dedicated ``GET /api/v1/notifications`` endpoint. The caller is
    identified by X-Agent-Id (already resolved by the resolve_agent
    middleware into request.state.agent_id), which gates the per-agent
    message inbox.
    """
    from . import messages as messages_mod
    from . import agents as agents_mod

    agent_id = getattr(request.state, "agent_id", None)

    # N1: NO blanket WS-presence suppression. Piggybacked _notifications are
    # always built; cross-channel dedup rests on read/seen state instead — the
    # message-preview block below uses unread_for (previews stop the instant the
    # WS claims a message via claim_unread_for), and the shown-once seen-store
    # dedups failures/suggestions/lifecycle. This avoids starving an agent whose
    # socket is open but not being read.

    # ── Shown-once notifications (failures / suggestions / agent lifecycle) ──
    # Each gets a STABLE key so the per-agent seen-store can deliver it exactly
    # once (feedback: STUCK-NOTIFICATION NOISE). We collect (key, notification)
    # pairs plus the full set of currently-existing keys (for baselining a new
    # agent — feedback: NEW-ONLY-ON-REGISTER), then filter through the store.
    keyed: list[tuple[str, dict]] = []
    all_keys: set[str] = set()

    # Suggestion queue — gives any caller a quick scan over pending ideas.
    # Keyed per idea (suggestion:<idea_id>) so each suggestion fires once.
    try:
        suggested = store.list_ideas(status="suggested")
        for idea in suggested:
            p = idea.get("priority", "normal")
            desc = (idea.get("description") or "").split("\n")[0][:120]
            key = f"suggestion:{idea['id']}"
            all_keys.add(key)
            keyed.append((key, {
                "type": "suggestion",
                "priority": p,
                "message": f"Suggested idea #{idea['id']}: {desc}",
                "action": f"POST /api/v1/ideas/{idea['id']}/adopt" if p == "high"
                          else f"POST /api/v1/ideas/{idea['id']}/abandon",
            }))
    except Exception:
        pass

    # Failures — one notification per failed experiment id (failure:<exp_id>) so
    # a given failure is delivered to a given agent exactly once, instead of the
    # old "N experiment(s) failed" banner re-sent on every response.
    try:
        failed = store.list_experiments_by_status("failed")
        for e in failed:
            exp_id = e["id"]
            label = e.get("label", str(exp_id))
            key = f"failure:{exp_id}"
            all_keys.add(key)
            keyed.append((key, {
                "type": "failure",
                "message": f"experiment {label} failed",
                "action": "GET /api/v1/experiments/log",
            }))
    except Exception:
        pass

    # ── Agent lifecycle (feedback: AGENT LIFECYCLE NOTIFICATIONS) ──
    # Surface agents that registered / completed recently (last ~10 min).
    # Keyed agent_registered:<id> / agent_done:<id> so the shown-once mechanism
    # delivers each exactly once; the baseline rule keeps a new agent from
    # seeing lifecycle events that predate its own registration.
    _LIFECYCLE_WINDOW_SEC = 600

    def _recent(iso: str | None) -> bool:
        if not iso:
            return False
        try:
            t = datetime.fromisoformat(iso)
        except (ValueError, TypeError):
            return False
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - t).total_seconds() <= _LIFECYCLE_WINDOW_SEC

    try:
        for a in agents_mod.list_agents(REPO_DIR):
            aid = a.get("agent_id")
            if not aid:
                continue
            key = f"agent_registered:{aid}"
            all_keys.add(key)
            if _recent(a.get("created_at")):
                role = a.get("role") or "default"
                keyed.append((key, {
                    "type": "agent",
                    "message": f"agent {aid} ({role}) registered",
                    "action": "GET /api/v1/agents",
                }))
    except Exception:
        pass
    try:
        for a in agents_mod.list_past_agents(REPO_DIR):
            aid = a.get("agent_id")
            if not aid:
                continue
            key = f"agent_done:{aid}"
            all_keys.add(key)
            if _recent(a.get("completed_at")):
                role = a.get("role") or "default"
                keyed.append((key, {
                    "type": "agent",
                    "message": f"agent {aid} ({role}) completed",
                    "action": "GET /api/v1/agents",
                }))
    except Exception:
        pass

    # Filter through the per-agent seen-store (shown-once + new-agent baseline).
    notifications: list[dict] = _filter_and_mark_seen(agent_id, keyed, all_keys)

    # Per-agent inbox: unread directed messages.
    if agent_id:
        try:
            entry = agents_mod.lookup_agent(REPO_DIR, agent_id) or {}
            role = entry.get("role")
            unread = messages_mod.unread_for(
                REPO_DIR, agent_id=agent_id, role=role, limit=20,
            )
            fully_shown: list[int] = []
            for idx, m in enumerate(unread):
                origin = m.get("from_role") or m.get("from_agent") or "system"
                text = m.get("text") or ""
                truncated = len(text) > 60
                snippet = text[:60].rstrip()
                preview = (snippet + "…") if truncated else snippet
                notif = {
                    "type": "message",
                    "priority": "high",
                    "message_id": m["id"],
                    "from": origin,
                    "to": m.get("to"),
                    "message": f"new message from {origin}: {preview}",
                    "action": f"GET /api/v1/messages (read full text), then POST /api/v1/messages/{m['id']}/read",
                }
                # EXCERPT NOTE: hint once (on the first message) that the CLI
                # message loop is best run as a background task — otherwise
                # agents only see these 60-char previews piggy-backed on other
                # responses. Kept to a single field to avoid per-message spam.
                if idx == 0:
                    notif["tip"] = (
                        "run `the-lab messages` in the background to receive these"
                    )
                notifications.append(notif)
                if not truncated:
                    fully_shown.append(m["id"])
            # A short message is delivered in full right here, so mark it read —
            # otherwise it lingers as unread even after the agent has clearly
            # seen (and answered) it. Long messages keep a preview and are marked
            # read when the agent fetches their full text via GET /messages.
            if fully_shown:
                try:
                    messages_mod.mark_read_many(REPO_DIR, fully_shown, agent_id)
                except Exception:
                    pass
        except Exception:
            pass
    # Mild nudge when a git-touching route ran in the main repo because no
    # X-Agent-Id was provided (and the agent_cwd helper flagged it).
    if getattr(request.state, "git_no_agent_warning", False):
        notifications.append({
            "type": "agent",
            "message": (
                "X-Agent-Id header missing; this git operation ran in the "
                "main repo. Register an agent with POST /api/v1/agents/register "
                "and pass the returned id back in the X-Agent-Id header."
            ),
            "action": "POST /api/v1/agents/register",
        })
    elif getattr(request.state, "agent_unknown", False):
        notifications.append({
            "type": "agent",
            "message": (
                f"X-Agent-Id '{getattr(request.state, 'agent_id', '?')}' is not "
                "registered; falling back to the main repo. Re-register with "
                "POST /api/v1/agents/register."
            ),
            "action": "POST /api/v1/agents/register",
        })
    return notifications


@app.middleware("http")
async def inject_notifications(request, call_next):
    """Append _notifications to JSON API responses when there's something actionable.

    For dict-shaped responses we attach ``_notifications`` to the body.
    For list-shaped responses (which we can't safely re-shape) we attach an
    ``X-Notifications-Count`` header instead so agents know to fetch
    ``GET /api/v1/notifications`` for the full payload.
    """
    # In-flight marker for the long-poll endpoint: /wait can block for minutes,
    # so tell the activity view the call is being processed BEFORE it completes
    # (pending: true; the completion event above clears it). resolve_agent runs
    # inside this middleware, so read the header directly. Fail-soft.
    if request.url.path == "/api/v1/wait":
        _wid = (request.headers.get("x-agent-id") or "").strip()
        if _wid:
            try:
                _ws_mod.broadcaster.broadcast_soon({
                    "type": "agent_api_call",
                    "agent_id": _wid,
                    "method": request.method,
                    "path": "/api/v1/wait",
                    "status": None,
                    "pending": True,
                })
            except Exception:
                pass

    _t0 = _time_mod.monotonic()
    response = await call_next(request)
    _call_ms = int((_time_mod.monotonic() - _t0) * 1000)
    path = request.url.path

    # Live agent ticker: a throttled agent_api_call event for real work calls
    # made by a registered agent. Emission is deferred until we know the body
    # (below) so the event can carry duration + response size + top-level keys
    # for the sidebar hover card; early-return paths emit without body info.
    agent_id = getattr(request.state, "agent_id", None)
    _want_call_emit = bool(
        agent_id
        and not getattr(request.state, "agent_unknown", False)
        and _should_emit_api_call(request.method, path, response.status_code)
    )

    def _emit_call(resp_bytes=None, resp_keys=None):
        nonlocal _want_call_emit
        if not _want_call_emit:
            return
        _want_call_emit = False
        _emit_agent_api_call(
            agent_id, request.method, path, response.status_code,
            duration_ms=_call_ms, resp_bytes=resp_bytes, resp_keys=resp_keys,
        )

    # Only enrich /api/v1/ JSON responses (skip openapi, docs, stats, dashboard).
    # Skip the dedicated /notifications endpoint to avoid self-reference.
    if (not path.startswith("/api/v1/")
            or path in ("/api/v1/openapi.json", "/api/v1/docs", "/api/v1/redoc")
            or path.startswith("/api/v1/stats")
            or path == "/api/v1/notifications"
            or path == "/api/v1/health"  # liveness probe — keep it minimal
            or response.status_code >= 400):
        _emit_call()
        return response
    content_type = response.headers.get("content-type", "")
    if "application/json" not in content_type:
        _emit_call()
        return response
    body_parts = []
    async for chunk in response.body_iterator:
        body_parts.append(chunk if isinstance(chunk, bytes) else chunk.encode())
    body = b"".join(body_parts)
    try:
        data = _json.loads(body)
    except (ValueError, TypeError):
        _emit_call(resp_bytes=len(body))
        return Response(content=body, status_code=response.status_code,
                        headers=dict(response.headers), media_type=response.media_type)
    _emit_call(
        resp_bytes=len(body),
        resp_keys=list(data.keys())[:8] if isinstance(data, dict) else None,
    )
    notifications = build_notifications(request)
    is_mcp_req = request.headers.get("x-mcp-proxy") == "true"

    def _track_size(final_body: bytes) -> None:
        if is_mcp_req and response.status_code < 400:
            api_stats.record_response_size(request.method, path, len(final_body))

    if not notifications:
        _track_size(body)
        return Response(content=body, status_code=response.status_code,
                        headers=dict(response.headers), media_type=response.media_type)
    # Dict body → inline. List body → header-only signal.
    if isinstance(data, dict):
        data["_notifications"] = notifications
        try:
            new_body = _json.dumps(data, allow_nan=True).encode()
        except (ValueError, TypeError):
            _track_size(body)
            return Response(content=body, status_code=response.status_code,
                            headers=dict(response.headers), media_type=response.media_type)
        _track_size(new_body)
        return Response(content=new_body, status_code=response.status_code,
                        media_type="application/json")
    headers = dict(response.headers)
    headers["X-Notifications-Count"] = str(len(notifications))
    headers.pop("content-length", None)  # body unchanged but defensive
    _track_size(body)
    return Response(content=body, status_code=response.status_code,
                    headers=headers, media_type=response.media_type)


@app.get("/api/v1/notifications")
def get_notifications(request: Request):
    """Return the current caller's notifications without any other payload.

    Useful from contexts where the response middleware can't reshape the
    body (list endpoints), or when an agent just wants to poll its inbox.
    The response shape is ``{"notifications": [...]}``.
    """
    return {"notifications": build_notifications(request)}


@app.get("/api/v1/health")
def health():
    """Unauthenticated liveness probe (feedback: /health ENDPOINT).

    Exempted from both basic_auth and the notifications middleware so a
    monitor/bridge can poll it cheaply without credentials. Reports the live
    agent count and process uptime; fail-soft on the registry read.
    """
    try:
        from . import agents as agents_mod
        agent_count = len(agents_mod.list_agents(REPO_DIR))
    except Exception:
        agent_count = 0
    return {
        "status": "ok",
        "agents": agent_count,
        "uptime_sec": int(_time_mod.monotonic() - _START_TIME),
    }


# GZip compression. Starlette's add_middleware() inserts at position 0 of the
# user_middleware list, and the list is applied in reverse when building the
# ASGI stack — so the LAST middleware added is the OUTERMOST wrapper. Adding
# GZip here (after the two @app.middleware("http") decorators) puts it outside
# both custom middlewares: it sees the request first and the fully-assembled
# response last, which is exactly when we want to compress (after notifications
# have been injected). JSON compresses ~4x; 1 KB threshold skips tiny responses.
# ---------------------------------------------------------------------------
# CORS
#
# Default: allow all origins.
# To restrict, set THE_LAB_CORS_ORIGINS to a comma-separated list, e.g.:
#   THE_LAB_CORS_ORIGINS=http://myapp.example.com,http://localhost:5173
# ---------------------------------------------------------------------------
_cors_env = os.environ.get("THE_LAB_CORS_ORIGINS", "").strip()
if _cors_env:
    # Explicit list supplied — restrict to those origins only
    _cors_origins = [o.strip() for o in _cors_env.split(",") if o.strip()]
else:
    # Default: open to all origins
    _cors_origins = ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# GZip goes outermost (added last) so it compresses the already-assembled
# response after CORS and auth headers have been injected.
app.add_middleware(GZipMiddleware, minimum_size=1000)


# --- Lifecycle ---

@app.on_event("startup")
async def startup():
    await runner.reattach_running()
    # Reap agent worktrees whose registered PID is gone (a CLI wrapper that
    # crashed before unregistering). Safe to skip on errors.
    try:
        from . import agents as _agents_mod
        removed = _agents_mod.prune_dead_agents(REPO_DIR)
        if removed:
            logger.info(
                "Pruned %d stale agent worktree(s): %s",
                len(removed), ", ".join(removed),
            )
    except Exception as e:  # pragma: no cover
        logger.warning("agent prune failed at startup: %s", e)


@app.on_event("shutdown")
async def shutdown():
    api_stats.flush()


# --- Static files ---

_DEV_MODE = os.environ.get("THE_LAB_DEV") == "1"
_STATIC_DIR = Path(__file__).parent / "static"

# Serve Vite build output if it exists, otherwise fall back to legacy dashboard.html
if _STATIC_DIR.exists() and (_STATIC_DIR / "index.html").exists():
    _SPA_HTML = (_STATIC_DIR / "index.html").read_text()
    # Inject the WS auth token so the dashboard can authenticate WebSocket
    # connections. Browsers cannot send Authorization headers on WebSocket
    # upgrades, so the token is passed as a query param; the dashboard reads
    # it from localStorage["the-lab:wsToken"].
    if _AUTH_ENABLED:
        _token_script = (
            f'<script>try{{localStorage.setItem("the-lab:wsToken","{_AUTH_EXPECTED}")}}catch(e){{}}</script>'
        )
        _SPA_HTML = _SPA_HTML.replace("</head>", f"{_token_script}</head>", 1)
    _ASSETS_DIR = _STATIC_DIR / "assets"
    if _ASSETS_DIR.exists():
        app.mount("/assets", StaticFiles(directory=_ASSETS_DIR), name="assets")
else:
    _SPA_HTML = None

_DASHBOARD_HTML: str | None = None


def _load_dashboard() -> str:
    global _DASHBOARD_HTML
    if _DEV_MODE or _DASHBOARD_HTML is None:
        html_path = Path(__file__).parent / "dashboard.html"
        if html_path.exists():
            _DASHBOARD_HTML = html_path.read_text()
        else:
            _DASHBOARD_HTML = "<html><body>Dashboard not found. Run npm run build in dashboard/.</body></html>"
    return _DASHBOARD_HTML


# --- Register routers ---

from .routes.ideas import router as ideas_router
from .routes.experiments import router as experiments_router
from .routes.overview import router as overview_router
from .routes.operational import router as operational_router
from .routes.prompts import router as prompts_router
from .routes.agents import router as agents_router
from .routes.queue import router as queue_router
from .routes.messages import router as messages_router

app.include_router(ideas_router)
app.include_router(experiments_router)
app.include_router(overview_router)
app.include_router(operational_router)
app.include_router(prompts_router)
app.include_router(agents_router)
app.include_router(queue_router)
app.include_router(messages_router)


# --- WebSocket endpoint ---

@app.websocket("/api/v1/ws")
async def ws_endpoint(websocket: WebSocket, since: int = 0, token: str = ""):
    """Server-push WebSocket channel.

    Connect with ``ws[s]://host/api/v1/ws?since=N`` to receive all events
    with seq > N from the ring buffer, then live events as they occur.

    When auth is enabled pass ``?token=<base64 user:pass>`` (same value as
    the HTTP Basic Auth credential).  Connections with invalid tokens are
    rejected with close code 1008.

    The server sends a ``{"type": "ping", "seq": -1}`` frame every 30 s to
    keep the connection alive through proxies.  Client→server messages are
    accepted but discarded.
    """
    import asyncio as _asyncio

    # Complete the HTTP→WS handshake unconditionally first — ASGI requires
    # the 101 response to be sent before any WebSocket-level frame (including
    # a close frame). Rejecting before accept() triggers the uvicorn error
    # "ASGI callable returned without sending handshake".
    await websocket.accept()

    # Auth gate — mirror the HTTP Basic Auth logic.
    if _AUTH_ENABLED:
        if not secrets.compare_digest(token, _AUTH_EXPECTED):
            await websocket.close(code=1008)
            return

    # Replay any missed events.
    for event in _ws_mod.broadcaster.replay_since(since):
        try:
            await websocket.send_json(event)
        except Exception:
            return

    q = _ws_mod.broadcaster.subscribe()
    try:
        async def _send_loop():
            while True:
                event = await q.get()
                await websocket.send_json(event)

        async def _recv_loop():
            while True:
                await websocket.receive_text()

        async def _ping_loop():
            while True:
                await _asyncio.sleep(30)
                await websocket.send_json({"type": "ping", "seq": -1})

        send_task = _asyncio.create_task(_send_loop())
        recv_task = _asyncio.create_task(_recv_loop())
        ping_task = _asyncio.create_task(_ping_loop())
        try:
            done, pending = await _asyncio.wait(
                [send_task, recv_task, ping_task],
                return_when=_asyncio.FIRST_EXCEPTION,
            )
        finally:
            for t in (send_task, recv_task, ping_task):
                t.cancel()
            # Drain cancellation. CancelledError is BaseException in Python
            # 3.8+, not Exception — must be caught explicitly.
            for t in (send_task, recv_task, ping_task):
                try:
                    await t
                except (_asyncio.CancelledError, Exception):
                    pass
    except (WebSocketDisconnect, _asyncio.CancelledError):
        pass
    finally:
        _ws_mod.broadcaster.unsubscribe(q)


# --- Agent-scoped messages WebSocket ---

class _NotifRequestShim:
    """Minimal stand-in for a Starlette Request so build_notifications() can run
    outside the HTTP path (from the messages WebSocket handler).

    build_notifications only reads ``request.state.agent_id`` and a couple of
    optional flags; we provide exactly those so the same single-source-of-truth
    notification builder is reused verbatim.
    """

    class _State:
        def __init__(self, agent_id):
            self.agent_id = agent_id
            self.agent_cwd = None
            self.agent_unknown = False
            self.git_no_agent_warning = False

    def __init__(self, agent_id):
        self.state = _NotifRequestShim._State(agent_id)


# Broadcast event types that should trigger a fresh notifications push to a
# listening agent (idea/experiment/agent-lifecycle churn that build_notifications
# derives its failure/suggestion/lifecycle items from). message_received is
# handled separately (it drives the per-agent inbox); M3: experiment_* events
# are additionally forwarded as their own "experiment" frame regardless of this
# set (the set only gates the follow-up notifications re-derive).
_NOTIF_TRIGGER_TYPES = frozenset({
    "experiment_finished",
    "experiment_cancelled",
    "experiment_queued",
    "experiment_started",
    "idea_changed",
    "note_added",
})


@app.websocket("/api/v1/messages/ws")
async def messages_ws_endpoint(websocket: WebSocket):
    """Agent-scoped listening WebSocket for `the-lab messages`.

    N1: messages are delivered here by CLAIMING them (claim_unread_for) so a
    message can't also arrive on the piggyback/poll channel — there is no
    blanket suppression anymore; dedup rests on claim-on-delivery + read state.

    Connect with ``ws[s]://host/api/v1/messages/ws``. The agent is identified by
    the ``X-Agent-Id`` request header (our CLI sends it in the handshake), with a
    ``?agent_id=`` query-string fallback. Auth mirrors ``/ws``: pass
    ``?token=<base64 user:pass>`` when auth is enabled (also accepts the standard
    ``Authorization: Basic`` handshake header for parity).

    Frames (JSON):
      * initial    — ``{"type":"init","messages":[...],"notifications":[...]}``
      * message    — ``{"type":"message","messages":[<full msg>...]}`` as they arrive
      * notify     — ``{"type":"notifications","notifications":[...]}`` on churn
      * experiment — ``{"type":"experiment","event":"experiment_...", ...payload}``
                     (M3 fold-in: experiment lifecycle events forwarded verbatim)
      * ping       — ``{"type":"ping","seq":-1}`` every 30 s keep-alive

    Pass ``?peek=1`` to leave delivered messages unread — then unread_for is used
    (select without claiming) so previews still fall through the other channels.
    """
    import asyncio as _asyncio

    # Handshake must complete before any WS-level frame (see /ws for rationale).
    await websocket.accept()

    # Query params: token (auth), agent_id (fallback), peek.
    qp = websocket.query_params
    token = qp.get("token", "") or ""
    peek = qp.get("peek", "") in ("1", "true", "yes")

    # Auth gate — mirror /ws (token query param) but also accept the standard
    # Authorization: Basic handshake header the CLI sends for parity.
    if _AUTH_ENABLED:
        ok = bool(token) and secrets.compare_digest(token, _AUTH_EXPECTED)
        if not ok:
            auth_header = websocket.headers.get("authorization", "")
            if auth_header.startswith("Basic "):
                provided = auth_header[len("Basic "):].strip()
                ok = secrets.compare_digest(provided, _AUTH_EXPECTED)
        if not ok:
            await websocket.close(code=1008)
            return

    # Identify the agent: X-Agent-Id header (CLI sends it), ?agent_id= fallback.
    agent_id = websocket.headers.get("x-agent-id") or qp.get("agent_id") or None
    if not agent_id:
        # Nothing to scope to — a listener with no identity can't have messages
        # suppressed or delivered. Close cleanly with a policy code.
        await websocket.close(code=1008)
        return

    from . import messages as messages_mod
    from . import agents as agents_mod

    entry = agents_mod.lookup_agent(REPO_DIR, agent_id) or {}
    role = entry.get("role")
    shim = _NotifRequestShim(agent_id)

    def _fetch_deliver() -> list[dict]:
        """Select this agent's unread messages for delivery.

        N1: normally CLAIM them (select + mark read atomically) so the same
        message can't also be delivered via the piggyback/poll channel. When
        ?peek=1, select WITHOUT claiming (unread_for) so the caller sees them
        but they remain deliverable elsewhere.
        """
        try:
            if peek:
                return messages_mod.unread_for(
                    REPO_DIR, agent_id=agent_id, role=role, limit=50,
                )
            return messages_mod.claim_unread_for(
                REPO_DIR, agent_id=agent_id, role=role, limit=50,
            )
        except Exception:
            return []

    # Record the poll-heuristic timestamp so the dashboard "listening" flag
    # (agents.is_listening) lights up immediately, without waiting for a GET.
    # (N1: no listening-registry suppression anymore — dedup is claim-based.)
    try:
        agents_mod.note_message_poll(REPO_DIR, agent_id)
    except Exception:
        pass

    q = None
    try:
        # ── Initial frame: current unread messages + current notifications ──
        init_unread = _fetch_deliver()
        try:
            init_notifs = build_notifications(shim)
        except Exception:
            init_notifs = []
        await websocket.send_json({
            "type": "init",
            "messages": init_unread,
            "notifications": init_notifs,
        })

        # Subscribe AFTER the initial snapshot so we don't miss events that land
        # between the snapshot read and the subscribe (they'll be re-derived on
        # the next relevant broadcast anyway).
        q = _ws_mod.broadcaster.subscribe()

        async def _send_loop():
            while True:
                event = await q.get()
                etype = event.get("type") or ""
                if etype == "message_received":
                    # A directed message landed. Claim this agent's unread and
                    # forward only what's addressed to it (is_for handles
                    # agent/role/all + self-exclusion). N1: claiming here dedups
                    # against the piggyback/poll channel.
                    unread = _fetch_deliver()
                    if unread:
                        await websocket.send_json({
                            "type": "message",
                            "messages": unread,
                        })
                elif etype.startswith("experiment_"):
                    # M3 fold-in: forward experiment lifecycle events to the
                    # listener verbatim (payload carries experiment_id/label/
                    # idea_id/status/status_msg). Always forwarded — these are
                    # their own frame type, independent of --no-notifications.
                    frame = {k: v for k, v in event.items() if k != "type"}
                    frame["type"] = "experiment"
                    frame["event"] = etype
                    await websocket.send_json(frame)
                    # Experiment churn may also produce new notifications for this
                    # agent (failures/lifecycle) — re-derive and push any.
                    if etype in _NOTIF_TRIGGER_TYPES:
                        try:
                            notifs = build_notifications(shim)
                        except Exception:
                            notifs = []
                        if notifs:
                            await websocket.send_json({
                                "type": "notifications",
                                "notifications": notifs,
                            })
                elif etype in _NOTIF_TRIGGER_TYPES:
                    # Non-experiment churn (idea_changed/note_added) that may have
                    # produced new notifications for this agent. Re-derive + push.
                    try:
                        notifs = build_notifications(shim)
                    except Exception:
                        notifs = []
                    if notifs:
                        await websocket.send_json({
                            "type": "notifications",
                            "notifications": notifs,
                        })

        async def _recv_loop():
            # Refresh the poll heuristic on any client frame; discard content.
            while True:
                await websocket.receive_text()
                try:
                    agents_mod.note_message_poll(REPO_DIR, agent_id)
                except Exception:
                    pass

        async def _ping_loop():
            while True:
                await _asyncio.sleep(30)
                await websocket.send_json({"type": "ping", "seq": -1})
                # Keep the poll-heuristic warm while the socket is held open.
                try:
                    agents_mod.note_message_poll(REPO_DIR, agent_id)
                except Exception:
                    pass

        send_task = _asyncio.create_task(_send_loop())
        recv_task = _asyncio.create_task(_recv_loop())
        ping_task = _asyncio.create_task(_ping_loop())
        try:
            done, pending = await _asyncio.wait(
                [send_task, recv_task, ping_task],
                return_when=_asyncio.FIRST_EXCEPTION,
            )
        finally:
            for t in (send_task, recv_task, ping_task):
                t.cancel()
            for t in (send_task, recv_task, ping_task):
                try:
                    await t
                except (_asyncio.CancelledError, Exception):
                    pass
    except (WebSocketDisconnect, _asyncio.CancelledError):
        pass
    except Exception:  # noqa: BLE001 — never let a listener crash the server.
        pass
    finally:
        # N1: no listening-registry to unregister — dedup is claim-based now.
        if q is not None:
            _ws_mod.broadcaster.unsubscribe(q)


# --- SPA Fallback (must be last) ---
# Serves the Preact app for any non-API path (enables client-side routing).
# Falls back to legacy dashboard.html if Vite build output doesn't exist.

@app.get("/{path:path}", response_class=HTMLResponse, include_in_schema=False)
def spa_fallback(path: str):
    if path.startswith("api/"):
        raise HTTPException(404)
    if _STATIC_DIR.exists() and (_STATIC_DIR / "index.html").exists():
        html = (_STATIC_DIR / "index.html").read_text()
        if _AUTH_ENABLED:
            html = html.replace("</head>", f"{_token_script}</head>", 1)
        return html
    return _load_dashboard()
