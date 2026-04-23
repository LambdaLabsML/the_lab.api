"""CRUD for role-specific prompts under .the_lab/."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..deps import REPO_DIR, store
from ..prompts import (
    DEFAULT_ROLE,
    InvalidRoleError,
    delete_prompt,
    list_roles,
    read_prompt,
    validate_role,
    write_prompt,
)

router = APIRouter()


class PromptContent(BaseModel):
    content: str


@router.get("/api/v1/prompts")
def list_prompts():
    """List all defined prompt roles.

    Returns a list of ``{role, size, updated_at}`` entries with ``default``
    first (if present). The ``content`` is NOT included — use
    ``GET /api/v1/prompts/{role}`` to read a single role's content.

    Example:
        GET /api/v1/prompts
        -> [{"role": "default", "size": 312, "updated_at": "..."},
            {"role": "instructor", "size": 920, "updated_at": "..."}]
    """
    return list_roles(REPO_DIR)


@router.get("/api/v1/prompts/{role}")
def get_prompt(role: str):
    """Read the content of a single role's prompt.

    ``role`` may be ``default`` for the no-suffix ``PROMPT.md`` file.

    Example:
        GET /api/v1/prompts/instructor
        -> {"role": "instructor", "content": "...", "updated_at": "..."}
    """
    if role != DEFAULT_ROLE:
        try:
            validate_role(role)
        except InvalidRoleError as e:
            raise HTTPException(400, str(e))
    content = read_prompt(REPO_DIR, role)
    if content is None:
        raise HTTPException(404, f"prompt role '{role}' not found")
    return {"role": role, "content": content}


@router.put("/api/v1/prompts/{role}")
def put_prompt(role: str, req: PromptContent):
    """Create or replace a role's prompt content.

    Writes ``.the_lab/PROMPT.<role>.md`` (or ``PROMPT.md`` for ``default``).
    Bumps ``store._version`` so cached ``/instructions?role=…`` responses
    invalidate.

    Example:
        PUT /api/v1/prompts/instructor  {"content": "You are ..."}
        -> {"role": "instructor", "size": 42, "updated_at": "..."}
    """
    if role != DEFAULT_ROLE:
        try:
            validate_role(role)
        except InvalidRoleError as e:
            raise HTTPException(400, str(e))
    path = write_prompt(REPO_DIR, role, req.content)
    if store is not None:
        store._version += 1
    st = path.stat()
    return {
        "role": role,
        "size": st.st_size,
        "updated_at": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
    }


@router.delete("/api/v1/prompts/{role}")
def delete_prompt_role(role: str):
    """Delete a role's prompt file. Refuses ``default``."""
    if role == DEFAULT_ROLE:
        raise HTTPException(400, "cannot delete the default role")
    try:
        validate_role(role)
    except InvalidRoleError as e:
        raise HTTPException(400, str(e))
    deleted = delete_prompt(REPO_DIR, role)
    if not deleted:
        raise HTTPException(404, f"prompt role '{role}' not found")
    if store is not None:
        store._version += 1
    return {"status": "deleted", "role": role}
