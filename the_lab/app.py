"""The Lab — Experiment Management API."""
from __future__ import annotations

import json as _json
import logging
import os
from pathlib import Path

import math

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response

logger = logging.getLogger(__name__)

from .store import Store
from .runner import ExperimentRunner
from .stats import ApiStats
from . import deps

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
async def track_api_stats(request, call_next):
    # Capture body preview for POST/PUT before passing to handler
    body_preview = ""
    if request.method in ("POST", "PUT") and request.url.path.startswith("/api/v1/"):
        try:
            raw = await request.body()
            body_preview = raw.decode(errors="replace")[:200]
        except Exception:
            pass
    response = await call_next(request)
    path = request.url.path
    # Skip stats tracking for dashboard/browser requests and stats endpoints.
    # Primary: explicit header set by dashboard JS.
    # Fallback: detect browser User-Agent (agents use curl/python/httpx).
    is_dashboard = request.headers.get("x-the-lab-source") == "dashboard"
    if not is_dashboard:
        ua = request.headers.get("user-agent", "")
        is_dashboard = "Mozilla/" in ua
    if (path.startswith("/api/v1/")
            and not path.startswith("/api/v1/stats")
            and not is_dashboard):
        client = request.client.host if request.client else ""
        is_mcp = request.headers.get("x-mcp-proxy") == "true"
        api_stats.record(
            request.method, path, client_ip=client,
            query=str(request.url.query) if request.url.query else "",
            body_preview=body_preview,
            status_code=response.status_code,
            mcp=is_mcp,
        )
    return response


@app.middleware("http")
async def inject_notifications(request, call_next):
    """Append _notifications to JSON API responses when there's something actionable."""
    response = await call_next(request)
    path = request.url.path
    # Only enrich /api/v1/ JSON responses (skip openapi, docs, stats, dashboard)
    if (not path.startswith("/api/v1/")
            or path in ("/api/v1/openapi.json", "/api/v1/docs", "/api/v1/redoc")
            or path.startswith("/api/v1/stats")
            or response.status_code >= 400):
        return response
    content_type = response.headers.get("content-type", "")
    if "application/json" not in content_type:
        return response
    # Read the response body
    body_parts = []
    async for chunk in response.body_iterator:
        body_parts.append(chunk if isinstance(chunk, bytes) else chunk.encode())
    body = b"".join(body_parts)
    try:
        data = _json.loads(body)
    except (ValueError, TypeError):
        return Response(content=body, status_code=response.status_code,
                        headers=dict(response.headers), media_type=response.media_type)
    # Only enrich dict responses (not lists)
    if not isinstance(data, dict):
        return Response(content=body, status_code=response.status_code,
                        headers=dict(response.headers), media_type=response.media_type)
    # Build notifications
    notifications = []
    try:
        suggested = store.list_ideas(status="suggested")
        for idea in suggested:
            p = idea.get("priority", "normal")
            notifications.append({
                "type": "suggestion",
                "priority": p,
                "message": f"Suggested idea #{idea['id']}: {idea.get('description', '')}",
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
    if notifications:
        data["_notifications"] = notifications
        try:
            new_body = _json.dumps(data, allow_nan=True).encode()
        except (ValueError, TypeError):
            # If re-serialization fails, return the original body unchanged
            return Response(content=body, status_code=response.status_code,
                            headers=dict(response.headers), media_type=response.media_type)
        return Response(content=new_body, status_code=response.status_code,
                        media_type="application/json")
    return Response(content=body, status_code=response.status_code,
                    headers=dict(response.headers), media_type=response.media_type)


# --- Lifecycle ---

@app.on_event("startup")
async def startup():
    await runner.reattach_running()


@app.on_event("shutdown")
async def shutdown():
    api_stats.flush()


# --- Static files ---

_DEV_MODE = os.environ.get("THE_LAB_DEV") == "1"
_STATIC_DIR = Path(__file__).parent / "static"

# Serve Vite build output if it exists, otherwise fall back to legacy dashboard.html
if _STATIC_DIR.exists() and (_STATIC_DIR / "index.html").exists():
    _SPA_HTML = (_STATIC_DIR / "index.html").read_text()
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

app.include_router(ideas_router)
app.include_router(experiments_router)
app.include_router(overview_router)
app.include_router(operational_router)


# --- SPA Fallback (must be last) ---
# Serves the Preact app for any non-API path (enables client-side routing).
# Falls back to legacy dashboard.html if Vite build output doesn't exist.

@app.get("/{path:path}", response_class=HTMLResponse, include_in_schema=False)
def spa_fallback(path: str):
    if path.startswith("api/"):
        raise HTTPException(404)
    if _SPA_HTML:
        return _SPA_HTML
    return _load_dashboard()
