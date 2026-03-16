"""Git operations for idea branch management."""
from __future__ import annotations

import subprocess
from pathlib import Path


class GitError(Exception):
    pass


class MergeConflictError(GitError):
    def __init__(self, conflicts: list[str]):
        self.conflicts = conflicts
        super().__init__(f"Merge conflicts in: {', '.join(conflicts)}")


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


def get_repo_root(cwd: str | Path | None = None) -> Path:
    result = _run(["rev-parse", "--show-toplevel"], cwd=cwd)
    return Path(result.stdout.strip())


def get_default_branch(cwd: str | Path | None = None) -> str:
    """Get the current HEAD branch name as the base for root ideas."""
    result = _run(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)
    return result.stdout.strip()


def branch_exists(branch: str, cwd: str | Path | None = None) -> bool:
    result = _run(["rev-parse", "--verify", branch], cwd=cwd, check=False)
    return result.returncode == 0


def create_branch_from(new_branch: str, source_branch: str, cwd: str | Path | None = None):
    """Create a new branch from a source branch."""
    _run(["branch", new_branch, source_branch], cwd=cwd)


def create_branch_from_merge(
    new_branch: str, parent_branches: list[str], cwd: str | Path | None = None
) -> list[str] | None:
    """Create a new branch by merging multiple parent branches.

    Returns None on success, or a list of conflicting file paths on failure.
    """
    repo_root = get_repo_root(cwd)

    # Start from the first parent
    _run(["branch", new_branch, parent_branches[0]], cwd=repo_root)

    # Create a temporary worktree to do the merge
    worktree_path = repo_root / ".worktrees" / new_branch.replace("/", "_")
    worktree_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        _run(["worktree", "add", str(worktree_path), new_branch], cwd=repo_root)

        # Merge remaining parents one by one
        for parent_branch in parent_branches[1:]:
            result = _run(
                ["merge", parent_branch, "--no-edit", "-m", f"Merge {parent_branch} into {new_branch}"],
                cwd=worktree_path,
                check=False,
            )
            if result.returncode != 0:
                # Get conflicting files
                conflict_result = _run(["diff", "--name-only", "--diff-filter=U"], cwd=worktree_path, check=False)
                conflicts = [f for f in conflict_result.stdout.strip().split("\n") if f]

                # Abort the merge and clean up
                _run(["merge", "--abort"], cwd=worktree_path, check=False)
                _run(["worktree", "remove", str(worktree_path), "--force"], cwd=repo_root, check=False)
                _run(["branch", "-D", new_branch], cwd=repo_root, check=False)
                return conflicts

        return None
    except Exception:
        # Clean up on any error
        _run(["worktree", "remove", str(worktree_path), "--force"], cwd=repo_root, check=False)
        _run(["branch", "-D", new_branch], cwd=repo_root, check=False)
        raise
    finally:
        # Always remove worktree if it still exists
        _run(["worktree", "remove", str(worktree_path), "--force"], cwd=repo_root, check=False)


def get_worktree_path(idea_id: int, cwd: str | Path | None = None) -> Path:
    """Get or create a worktree for an idea's branch."""
    repo_root = get_repo_root(cwd)
    branch = f"idea/{idea_id}"
    worktree_path = repo_root / ".worktrees" / f"idea_{idea_id}"

    if not worktree_path.exists():
        worktree_path.parent.mkdir(parents=True, exist_ok=True)
        # Prune stale worktree registrations first
        _run(["worktree", "prune"], cwd=repo_root, check=False)
        _run(["worktree", "add", str(worktree_path), branch], cwd=repo_root)

    return worktree_path


def remove_worktree(idea_id: int, cwd: str | Path | None = None):
    """Remove a worktree for an idea."""
    repo_root = get_repo_root(cwd)
    worktree_path = repo_root / ".worktrees" / f"idea_{idea_id}"
    if worktree_path.exists():
        _run(["worktree", "remove", str(worktree_path), "--force"], cwd=repo_root, check=False)
