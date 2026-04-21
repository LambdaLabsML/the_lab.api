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
    """Get the default branch name (main or master)."""
    # Try symbolic-ref first (works even in empty repos)
    result = _run(["symbolic-ref", "--short", "HEAD"], cwd=cwd, check=False)
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    # Fallback: try rev-parse (works after first commit)
    result = _run(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd, check=False)
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    return "main"


def get_current_branch(cwd: str | Path | None = None) -> str:
    """Get the current checked-out branch name."""
    return get_default_branch(cwd=cwd)


def get_head_commit(cwd: str | Path | None = None) -> str:
    """Get the short hash of HEAD, or empty string for empty repos."""
    result = _run(["rev-parse", "--short", "HEAD"], cwd=cwd, check=False)
    if result.returncode == 0:
        return result.stdout.strip()
    return ""


def branch_exists(branch: str, cwd: str | Path | None = None) -> bool:
    result = _run(["rev-parse", "--verify", branch], cwd=cwd, check=False)
    return result.returncode == 0


def _has_commits(cwd: str | Path | None = None) -> bool:
    """Check if the repo has any commits."""
    result = _run(["rev-parse", "HEAD"], cwd=cwd, check=False)
    return result.returncode == 0


def create_branch_from(new_branch: str, source_branch: str, cwd: str | Path | None = None):
    """Create a new branch from a source branch (does not checkout)."""
    if not _has_commits(cwd=cwd):
        # Empty repo: create an initial commit so branches can exist
        _run(["commit", "--allow-empty", "-m", "initial commit"], cwd=cwd)
    _run(["branch", new_branch, source_branch], cwd=cwd)


def resolve_branch_commit(branch: str, cwd: str | Path | None = None) -> str:
    """Get the full commit hash that a branch points to."""
    result = _run(["rev-parse", branch], cwd=cwd)
    return result.stdout.strip()


def branch_diff(branch: str, base: str, cwd: str | Path | None = None) -> dict:
    """Get the diff between a branch and a base branch.

    Returns {stat, diff} where stat is the --stat summary and diff is the
    full patch.  If either branch doesn't exist, returns an error dict.
    """
    # Verify both branches exist
    if not branch_exists(branch, cwd=cwd) or not branch_exists(base, cwd=cwd):
        return {"error": f"branch '{branch}' or '{base}' not found"}

    merge_base = _run(["merge-base", base, branch], cwd=cwd, check=False)
    base_ref = merge_base.stdout.strip() if merge_base.returncode == 0 else base

    stat = _run(["diff", "--stat", base_ref, branch], cwd=cwd, check=False)
    diff = _run(["diff", base_ref, branch], cwd=cwd, check=False)

    return {
        "base": base,
        "branch": branch,
        "merge_base": base_ref[:12],
        "stat": stat.stdout,
        "diff": diff.stdout,
    }


def create_worktree(path: str | Path, commit: str, cwd: str | Path | None = None):
    """Create a detached worktree at *path* checked out to *commit*."""
    _run(["worktree", "add", "--detach", str(path), commit], cwd=cwd)


def remove_worktree(path: str | Path, cwd: str | Path | None = None):
    """Remove a worktree. Ignores errors (already removed, etc.)."""
    _run(["worktree", "remove", "--force", str(path)], cwd=cwd, check=False)


def prune_worktrees(cwd: str | Path | None = None):
    """Clean up stale worktree bookkeeping."""
    _run(["worktree", "prune"], cwd=cwd, check=False)


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


def checkout_idea_carry(idea_id: int, cwd: str | Path | None = None) -> dict:
    """Switch to idea/<id>, carrying uncommitted changes to commit *there*.

    Unlike checkout_idea (which commits on the old branch first), this stashes
    current work, checks out the new branch, then pops and commits — so the
    changes land on the new idea branch, not the one we left.
    """
    current = get_current_branch(cwd=cwd)
    target = f"idea/{idea_id}"

    if current == target:
        return {"status": "already_on_branch", "branch": target}

    stash_result = _run(
        ["stash", "push", "--include-untracked", "-m", f"carry to {target}"],
        cwd=cwd, check=False,
    )
    had_stash = (
        stash_result.returncode == 0
        and "No local changes" not in stash_result.stdout
    )

    try:
        checkout(target, cwd=cwd)
    except GitError:
        if had_stash:
            _run(["stash", "pop"], cwd=cwd, check=False)
        raise

    carried = False
    if had_stash:
        pop = _run(["stash", "pop"], cwd=cwd, check=False)
        if pop.returncode == 0:
            try:
                carried = auto_commit(cwd=cwd, message=f"carry-over from {current}")
            except GitError:
                pass

    return {
        "status": "checked_out",
        "branch": target,
        "previous_branch": current,
        "auto_committed": carried,
    }


def checkout_idea(idea_id: int, cwd: str | Path | None = None) -> dict:
    """Auto-commit current changes and checkout an idea's branch.

    Strategy:
      1. Try to commit all changes (tracked + untracked via ``git add -A``).
      2. Attempt checkout.
      3. If checkout fails (untracked files conflict with target, commit hook
         rejected the commit, etc.), stash everything remaining and retry.

    Returns a dict with info about what happened.
    """
    current = get_current_branch(cwd=cwd)
    target = f"idea/{idea_id}"

    if current == target:
        return {"status": "already_on_branch", "branch": target}

    # 1. Commit all possible changes on the current branch.
    committed = False
    try:
        committed = auto_commit(cwd=cwd, message=f"auto-commit before switching to {target}")
    except GitError:
        pass  # If commit fails (hook, no user config, etc.) we stash below.

    # 2. Try clean checkout.
    try:
        checkout(target, cwd=cwd)
        return {
            "status": "checked_out",
            "branch": target,
            "previous_branch": current,
            "auto_committed": committed,
        }
    except GitError:
        pass

    # 3. Checkout failed — stash remaining changes (untracked, ignored-but-
    #    conflicting files, uncommitted edits the commit couldn't capture) and
    #    retry.  --include-untracked covers new files that git add -A missed
    #    (e.g. files ignored on this branch but tracked on the target).
    stash_result = _run(
        ["stash", "push", "--include-untracked", "-m",
         f"auto-stash before switching to {target}"],
        cwd=cwd, check=False,
    )
    stashed = (
        stash_result.returncode == 0
        and "No local changes" not in stash_result.stdout
    )

    checkout(target, cwd=cwd)  # Propagate if this still fails.

    return {
        "status": "checked_out",
        "branch": target,
        "previous_branch": current,
        "auto_committed": committed,
        "stashed": stashed,
    }


def _ensure_clean_checkout(branch: str, cwd: str | Path | None = None):
    """Commit + stash to guarantee a clean checkout to *branch*."""
    try:
        auto_commit(cwd=cwd, message=f"auto-commit before switching to {branch}")
    except GitError:
        pass
    try:
        checkout(branch, cwd=cwd)
        return
    except GitError:
        pass
    _run(
        ["stash", "push", "--include-untracked", "-m",
         f"auto-stash before switching to {branch}"],
        cwd=cwd, check=False,
    )
    checkout(branch, cwd=cwd)


def create_branch_from_merge(
    new_branch: str, parent_branches: list[str], cwd: str | Path | None = None,
    carry: bool = False,
) -> list[str] | None:
    """Create a new branch by merging multiple parent branches.

    Auto-commits (or stashes when ``carry=True``), checks out first parent,
    creates branch, merges the rest.  When ``carry=True`` any uncommitted
    changes are stashed before the switch and popped+committed on *new_branch*
    so they land there rather than on the originating branch.
    Returns None on success, or a list of conflicting file paths on failure.
    """
    original_branch = get_current_branch(cwd=cwd)

    # Stash first so _ensure_clean_checkout finds a clean workspace and won't
    # commit anything to the old branch.
    had_stash = False
    if carry and has_uncommitted_changes(cwd=cwd):
        stash_result = _run(
            ["stash", "push", "--include-untracked", "-m", f"carry to {new_branch}"],
            cwd=cwd, check=False,
        )
        had_stash = (
            stash_result.returncode == 0
            and "No local changes" not in stash_result.stdout
        )

    # Checkout first parent (workspace is clean now)
    _ensure_clean_checkout(parent_branches[0], cwd=cwd)
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
            if had_stash:
                _run(["stash", "pop"], cwd=cwd, check=False)
            return conflicts

    # Pop carried changes onto new branch and commit them there
    if had_stash:
        pop = _run(["stash", "pop"], cwd=cwd, check=False)
        if pop.returncode == 0:
            try:
                auto_commit(cwd=cwd, message=f"carry-over from {original_branch}")
            except GitError:
                pass

    return None
