"""Prompt file management.

All role-specific prompts live in ``.the_lab/``:

    .the_lab/PROMPT.md                -- default role (no suffix)
    .the_lab/PROMPT.<role>.md         -- named role (e.g. PROMPT.instructor.md)

For backward compatibility, ``read_prompt(..., "default")`` also checks
``<repo>/PROMPT.md`` as a fallback. ``the-lab init`` offers to migrate.

Role names are restricted to ``[a-z0-9_-]{1,32}``; ``default`` is the
reserved name for the no-suffix file.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

_ROLE_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
_PREFIX = "PROMPT"
_SUFFIX = ".md"
DEFAULT_ROLE = "default"


class InvalidRoleError(ValueError):
    pass


def _prompts_dir(repo_dir: Path) -> Path:
    d = repo_dir / ".the_lab"
    d.mkdir(parents=True, exist_ok=True)
    return d


def validate_role(role: str) -> None:
    """Raise InvalidRoleError if *role* doesn't match the allowed pattern.

    ``default`` is always accepted even though it doesn't match the pattern.
    """
    if role == DEFAULT_ROLE:
        return
    if not isinstance(role, str) or not _ROLE_RE.match(role):
        raise InvalidRoleError(
            f"invalid role '{role}': lowercase a-z / 0-9 / _ / -, max 32 chars"
        )


def _role_path(repo_dir: Path, role: str) -> Path:
    """Path to the prompt file for *role* inside ``.the_lab/``."""
    if role == DEFAULT_ROLE:
        return _prompts_dir(repo_dir) / f"{_PREFIX}{_SUFFIX}"
    validate_role(role)
    return _prompts_dir(repo_dir) / f"{_PREFIX}.{role}{_SUFFIX}"


def _legacy_default_path(repo_dir: Path) -> Path:
    """Repo-root PROMPT.md — read as a fallback until `the-lab init` migrates."""
    return repo_dir / f"{_PREFIX}{_SUFFIX}"


def list_roles(repo_dir: Path) -> list[dict]:
    """Return metadata for all defined roles, ``default`` first, then alpha.

    Each row: ``{"role": str, "size": int, "updated_at": iso8601}``.
    """
    rows: list[dict] = []
    d = _prompts_dir(repo_dir)

    def _stat_row(role: str, path: Path) -> dict:
        st = path.stat()
        return {
            "role": role,
            "size": st.st_size,
            "updated_at": datetime.fromtimestamp(
                st.st_mtime, tz=timezone.utc
            ).isoformat(),
        }

    default_file = d / f"{_PREFIX}{_SUFFIX}"
    legacy = _legacy_default_path(repo_dir)
    if default_file.exists():
        rows.append(_stat_row(DEFAULT_ROLE, default_file))
    elif legacy.exists():
        rows.append(_stat_row(DEFAULT_ROLE, legacy))

    named: list[dict] = []
    for path in d.glob(f"{_PREFIX}.*{_SUFFIX}"):
        stem = path.stem  # "PROMPT.instructor"
        if not stem.startswith(f"{_PREFIX}."):
            continue
        role = stem[len(_PREFIX) + 1:]
        if not _ROLE_RE.match(role):
            # Skip files that look close but don't match the rule
            continue
        named.append(_stat_row(role, path))
    named.sort(key=lambda r: r["role"])
    rows.extend(named)
    return rows


def read_prompt(repo_dir: Path, role: str) -> str | None:
    """Return the prompt content for *role*, or None if no file exists.

    For ``role == "default"``, falls back to repo-root ``PROMPT.md`` if the
    canonical ``.the_lab/PROMPT.md`` doesn't exist.
    """
    try:
        path = _role_path(repo_dir, role)
    except InvalidRoleError:
        return None
    if path.exists():
        return path.read_text()
    if role == DEFAULT_ROLE:
        legacy = _legacy_default_path(repo_dir)
        if legacy.exists():
            return legacy.read_text()
    return None


def write_prompt(repo_dir: Path, role: str, content: str) -> Path:
    """Create or replace the prompt file for *role*. Returns the path written."""
    path = _role_path(repo_dir, role)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def delete_prompt(repo_dir: Path, role: str) -> bool:
    """Delete the prompt file for *role*. Refuses the default role.

    Returns True if a file was removed, False if no file existed.
    """
    if role == DEFAULT_ROLE:
        raise ValueError("cannot delete the default role")
    path = _role_path(repo_dir, role)
    if path.exists():
        path.unlink()
        return True
    return False


def role_exists(repo_dir: Path, role: str) -> bool:
    """True if a file for *role* exists (uses the same fallback as read_prompt)."""
    return read_prompt(repo_dir, role) is not None
