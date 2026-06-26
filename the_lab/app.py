"""The Lab — Experiment Management API."""
from __future__ import annotations

import base64
import json as _json
import logging
import os
import secrets
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
from . import perf_log
from . import ws as _ws_mod

# --- Configuration ---
REPO_DIR = Path(os.environ.get("THE_LAB_REPO", os.getcwd())).resolve()

store = Store(REPO_DIR)
runner = ExperimentRunner(store)
api_stats = ApiStats(REPO_DIR / ".the_lab" / "api_stats.json")

# Initialise shared state so route modules can import from deps
deps.init(store, runner, api_stats, REPO_DIR)


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

    notifications: list[dict] = []
    # Suggestion queue — gives any caller a quick scan over pending ideas.
    try:
        suggested = store.list_ideas(status="suggested")
        for idea in suggested:
            p = idea.get("priority", "normal")
            desc = (idea.get("description") or "").split("\n")[0][:120]
            notifications.append({
                "type": "suggestion",
                "priority": p,
                "message": f"Suggested idea #{idea['id']}: {desc}",
                "action": f"POST /api/v1/ideas/{idea['id']}/adopt" if p == "high"
                          else f"POST /api/v1/ideas/{idea['id']}/abandon",
            })
    except Exception:
        pass
    try:
        failed = store.list_experiments_by_status("failed")
        if failed:
            labels = [e.get("label", str(e["id"])) for e in failed[:3]]
            notifications.append({
                "type": "failure",
                "message": f"{len(failed)} experiment(s) failed: {', '.join(labels)}",
                "action": "GET /api/v1/experiments/log",
            })
    except Exception:
        pass
    # Per-agent inbox: unread directed messages.
    agent_id = getattr(request.state, "agent_id", None)
    if agent_id:
        try:
            entry = agents_mod.lookup_agent(REPO_DIR, agent_id) or {}
            role = entry.get("role")
            unread = messages_mod.unread_for(
                REPO_DIR, agent_id=agent_id, role=role, limit=20,
            )
            for m in unread:
                origin = m.get("from_role") or m.get("from_agent") or "system"
                snippet = (m.get("text") or "")[:60].rstrip()
                preview = (snippet + "…") if len(m.get("text") or "") > 60 else snippet
                notifications.append({
                    "type": "message",
                    "priority": "high",
                    "message_id": m["id"],
                    "from": origin,
                    "to": m.get("to"),
                    "message": f"new message from {origin}: {preview}",
                    "action": f"GET /api/v1/messages (read full text), then POST /api/v1/messages/{m['id']}/read",
                })
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
    response = await call_next(request)
    path = request.url.path
    # Only enrich /api/v1/ JSON responses (skip openapi, docs, stats, dashboard).
    # Skip the dedicated /notifications endpoint to avoid self-reference.
    if (not path.startswith("/api/v1/")
            or path in ("/api/v1/openapi.json", "/api/v1/docs", "/api/v1/redoc")
            or path.startswith("/api/v1/stats")
            or path == "/api/v1/notifications"
            or response.status_code >= 400):
        return response
    content_type = response.headers.get("content-type", "")
    if "application/json" not in content_type:
        return response
    body_parts = []
    async for chunk in response.body_iterator:
        body_parts.append(chunk if isinstance(chunk, bytes) else chunk.encode())
    body = b"".join(body_parts)
    try:
        data = _json.loads(body)
    except (ValueError, TypeError):
        return Response(content=body, status_code=response.status_code,
                        headers=dict(response.headers), media_type=response.media_type)
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
