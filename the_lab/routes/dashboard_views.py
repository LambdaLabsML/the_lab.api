"""Immutable dashboard view snapshots for readable links to complex layouts."""
from __future__ import annotations

import hashlib
import json
import re
import threading
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from ..deps import REPO_DIR
from ..jsonio import read_json, write_json

router = APIRouter(prefix="/api/v1/dashboard/views")
_lock = threading.RLock()


class ViewCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state: dict
    name: str = Field(default="view", max_length=60)


def _dashboard(request: Request):
    if (getattr(request.state, "auth_tier", "admin") != "admin"
            or getattr(request.state, "agent_id", None)
            or request.headers.get("x-agent-id")
            or request.headers.get("x-the-lab-source") != "dashboard"):
        raise HTTPException(403, "dashboard views are for dashboard users")


@router.post("")
def save_view(body: ViewCreate, request: Request):
    _dashboard(request)
    encoded = json.dumps(body.state, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    if len(encoded.encode()) > 100_000:
        raise HTTPException(413, "dashboard view is too large")
    digest = hashlib.sha256(encoded.encode()).hexdigest()[:16]
    name = re.sub(r"[^a-z0-9]+", "-", body.name.lower()).strip("-")[:36] or "view"
    ident = f"{name}-{digest}"
    path = REPO_DIR / ".the_lab" / "dashboard_views.json"
    with _lock:
        data = read_json(path) if path.exists() else {}
        if ident in data and data[ident] != body.state:
            raise HTTPException(409, "view identifier collision")
        if ident not in data:
            data[ident] = body.state
            path.parent.mkdir(parents=True, exist_ok=True)
            write_json(path, data)
    return {"id": ident}


@router.get("/{ident}")
def load_view(ident: str, request: Request):
    _dashboard(request)
    if not re.fullmatch(r"[a-z0-9-]{1,80}", ident):
        raise HTTPException(404, "dashboard view not found")
    path = REPO_DIR / ".the_lab" / "dashboard_views.json"
    with _lock:
        data = read_json(path) if path.exists() else {}
        state = data.get(ident)
    if state is None:
        raise HTTPException(404, "dashboard view not found")
    return {"state": state}
