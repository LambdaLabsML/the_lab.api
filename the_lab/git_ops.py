"""Git operations for idea branch management."""
from __future__ import annotations

import subprocess
from pathlib import Path


class GitError(Exception):
    pass


def _run(args: list[str], cwd: str | Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result


def get_default_branch(cwd: str | Path | None = None) -> str:
    """Get the current HEAD branch name."""
    result = _run(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)
    return result.stdout.strip()


def get_current_branch(cwd: str | Path | None = None) -> str:
    """Get the current checked-out branch name."""
    return get_default_branch(cwd=cwd)


def get_head_commit(cwd: str | Path | None = None) -> str:
    """Get the short hash of HEAD."""
    result = _run(["rev-parse", "--short", "HEAD"], cwd=cwd)
    return result.stdout.strip()


def branch_exists(branch: str, cwd: str | Path | None = None) -> bool:
    result = _run(["rev-parse", "--verify", branch], cwd=cwd, check=False)
    return result.returncode == 0


def create_branch_from(new_branch: str, source_branch: str, cwd: str | Path | None = None):
    """Create a new branch from a source branch (does not checkout)."""
    _run(["branch", new_branch, source_branch], cwd=cwd)


def has_uncommitted_changes(cwd: str | Path | None = None) -> bool:
    """Check if there are any uncommitted changes (staged or unstaged)."""
    result = _run(["status", "--porcelain"], cwd=cwd)
    # Filter out .the_lab/ entries — those aren't tracked
    lines = [l for l in result.stdout.strip().split("\n") if l and not l.endswith(".the_lab/") and ".the_lab/" not in l]
    return len(lines) > 0


def auto_commit(cwd: str | Path | None = None, message: str = "auto-commit before checkout") -> bool:
    """Stage all changes and commit. Returns True if a commit was made."""
    if not has_uncommitted_changes(cwd):
        return False
    _run(["add", "-A"], cwd=cwd)
    # Check if there's actually anything staged after add
    result = _run(["diff", "--cached", "--quiet"], cwd=cwd, check=False)
    if result.returncode == 0:
        return False
    _run(["commit", "-m", message], cwd=cwd)
    return True


def checkout(branch: str, cwd: str | Path | None = None):
    """Checkout a branch."""
    _run(["checkout", branch], cwd=cwd)


def checkout_idea(idea_id: int, cwd: str | Path | None = None) -> dict:
    """Auto-commit current changes and checkout an idea's branch.

    Returns a dict with info about what happened.
    """
    current = get_current_branch(cwd=cwd)
    target = f"idea/{idea_id}"

    if current == target:
        return {"status": "already_on_branch", "branch": target}

    committed = auto_commit(cwd=cwd, message=f"auto-commit before switching to {target}")
    checkout(target, cwd=cwd)

    return {
        "status": "checked_out",
        "branch": target,
        "previous_branch": current,
        "auto_committed": committed,
    }


def create_branch_from_merge(
    new_branch: str, parent_branches: list[str], cwd: str | Path | None = None
) -> list[str] | None:
    """Create a new branch by merging multiple parent branches.

    Auto-commits, checks out first parent, creates branch, merges the rest.
    Returns None on success, or a list of conflicting file paths on failure.
    """
    auto_commit(cwd=cwd)

    original_branch = get_current_branch(cwd=cwd)

    # Checkout first parent and create branch from it
    checkout(parent_branches[0], cwd=cwd)
    _run(["checkout", "-b", new_branch], cwd=cwd)

    # Merge remaining parents
    for parent_branch in parent_branches[1:]:
        result = _run(
            ["merge", parent_branch, "--no-edit", "-m", f"Merge {parent_branch} into {new_branch}"],
            cwd=cwd,
            check=False,
        )
        if result.returncode != 0:
            conflict_result = _run(["diff", "--name-only", "--diff-filter=U"], cwd=cwd, check=False)
            conflicts = [f for f in conflict_result.stdout.strip().split("\n") if f]

            # Abort and clean up
            _run(["merge", "--abort"], cwd=cwd, check=False)
            checkout(original_branch, cwd=cwd)
            _run(["branch", "-D", new_branch], cwd=cwd, check=False)
            return conflicts

    return None
