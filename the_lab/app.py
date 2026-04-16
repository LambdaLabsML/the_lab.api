"""The Lab — Experiment Management API."""
from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

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

app = FastAPI(title="The Lab", version="0.1.0")


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
