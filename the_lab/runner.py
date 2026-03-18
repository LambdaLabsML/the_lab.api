"""Experiment subprocess runner."""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import signal
from datetime import datetime, timezone
from pathlib import Path

from .git_ops import (
    auto_commit,
    create_worktree,
    get_current_branch,
    get_head_commit,
    prune_worktrees,
    remove_worktree,
    resolve_branch_commit,
)
from .store import Store


class ExperimentRunner:
    def __init__(self, store: Store):
        self._store = store
        self._finished_queue: asyncio.Queue[int] = asyncio.Queue()
        self._tasks: dict[int, asyncio.Task] = {}
        self._git_lock = None  # initialized as asyncio.Lock in reattach_running
        self._worktree_dir = store.repo_dir / ".the_lab" / "worktrees"
        self._worktree_dir.mkdir(parents=True, exist_ok=True)

        # Recover orphaned experiments on startup.
        # If the process is still alive, re-attach to it. Otherwise mark as failed.
        self._reattach_running = []
        running_worktrees = set()
        for exp in self._store.list_experiments_by_status("running"):
            pid = exp.get("pid")
            if pid and self._pid_alive(pid):
                self._reattach_running.append(exp)
                wt = (exp.get("meta") or {}).get("worktree")
                if wt:
                    running_worktrees.add(wt)
            else:
                # Clean up its worktree
                wt = (exp.get("meta") or {}).get("worktree")
                if wt and Path(wt).exists():
                    remove_worktree(wt, cwd=store.repo_dir)
                self._store.update_experiment(
                    exp["id"],
                    status="failed",
                    error="server restarted while experiment was running (process gone)",
                    pid=None,
                    finished_at=datetime.now(timezone.utc).isoformat(),
                )

        # Clean up stale worktrees (from previous crashes)
        if self._worktree_dir.exists():
            for d in self._worktree_dir.iterdir():
                if d.is_dir() and str(d) not in running_worktrees:
                    remove_worktree(d, cwd=store.repo_dir)
        prune_worktrees(cwd=store.repo_dir)

        # Mark all already-finished experiments as seen on startup,
        # so /wait only returns experiments that finish from now on.
        self._seen: set[int] = set()
        for status in ("completed", "failed", "cancelled"):
            for exp in self._store.list_experiments_by_status(status):
                self._seen.add(exp["id"])

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        """Check if a process is still running."""
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False

    def _symlink_venvs(self, worktree_path: Path):
        """Symlink .venv directories from the main repo into the worktree.

        Worktrees only contain git-tracked files, but experiments often need
        virtual environments (in .gitignore). Walk the main repo for .venv
        dirs and create matching symlinks in the worktree.
        """
        repo = self._store.repo_dir
        for venv_dir in repo.rglob(".venv"):
            if not venv_dir.is_dir():
                continue
            # Skip .venvs inside .the_lab/ or other worktrees
            try:
                rel = venv_dir.relative_to(repo)
            except ValueError:
                continue
            if ".the_lab" in rel.parts:
                continue
            # Skip nested .venvs (e.g. .venv/.venv)
            if sum(1 for p in rel.parts if p == ".venv") > 1:
                continue
            target = worktree_path / rel
            if target.exists() or target.is_symlink():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(venv_dir)

    async def reattach_running(self):
        """Re-attach to experiments that survived a server restart.
        Call this once after the event loop is running."""
        self._git_lock = asyncio.Lock()
        for exp in self._reattach_running:
            task = asyncio.create_task(self._monitor_pid(exp))
            self._tasks[exp["id"]] = task
        if self._reattach_running:
            ids = [e["id"] for e in self._reattach_running]
            print(f"[the-lab] re-attached to {len(ids)} running experiment(s): {ids}")
        self._reattach_running = []

    async def _monitor_pid(self, exp: dict):
        """Poll a PID until it exits, then capture results from the log file."""
        exp_id = exp["id"]
        pid = exp["pid"]
        script_path = self._store.repo_dir / exp["script"]
        base = script_path.with_suffix("")
        log_path = base.with_suffix(".log")
        err_path = base.with_suffix(".err")

        # Poll until the process exits
        while self._pid_alive(pid):
            await asyncio.sleep(2)

        # Process is done — read the log file (which the original bash process wrote to)
        now = datetime.now(timezone.utc).isoformat()
        output = log_path.read_text() if log_path.exists() else ""

        result = self._extract_json(output)
        if result is not None:
            metrics = result.get("metrics", {})
            meta = {**exp.get("meta", {}), **result.get("meta", {})}
            self._store.update_experiment(
                exp_id, status="completed", metrics=metrics, meta=meta,
                pid=None, finished_at=now,
            )
        else:
            # Metrics-optional: assume success if process was running normally
            self._store.update_experiment(
                exp_id, status="completed", metrics=None,
                pid=None, finished_at=now,
            )

        # Clean up worktree
        wt = (exp.get("meta") or {}).get("worktree")
        if wt and Path(wt).exists():
            try:
                remove_worktree(wt, cwd=self._store.repo_dir)
            except Exception:
                pass

        self._tasks.pop(exp_id, None)
        self._finished_queue.put_nowait(exp_id)

    async def start(self, exp_id: int, timeout: float | None = None) -> dict:
        exp = self._store.get_experiment(exp_id)
        if not exp:
            return {"status": "error", "reason": f"experiment {exp_id} not found"}
        if exp["status"] not in ("pending", "failed", "cancelled"):
            return {"status": "error", "reason": f"experiment is {exp['status']}, expected pending, failed, or cancelled"}

        script_path = self._store.repo_dir / exp["script"]
        if not script_path.exists():
            return {
                "status": "error",
                "reason": f"script not found at {exp['script']}. "
                          f"Write the script first, or pass script_content when creating the experiment.",
            }

        os.chmod(script_path, os.stat(script_path).st_mode | 0o755)

        try:
            desc = exp.get("description", "")
            auto_commit(cwd=self._store.repo_dir, message=f"exp {exp_id}: {desc}")
        except Exception:
            pass

        # Resolve the idea's branch to a commit, then create a worktree so
        # this experiment runs in isolation (concurrent experiments don't
        # interfere with each other or the main checkout).
        idea = self._store.get_idea(exp["idea_id"])
        branch = idea["branch"] if idea else get_current_branch(cwd=self._store.repo_dir)
        worktree_path = None
        run_cwd = str(self._store.repo_dir)  # fallback

        try:
            commit = resolve_branch_commit(branch, cwd=self._store.repo_dir)
            worktree_path = self._worktree_dir / str(exp_id)
            if worktree_path.exists():
                remove_worktree(worktree_path, cwd=self._store.repo_dir)
            if self._git_lock:
                await self._git_lock.acquire()
            try:
                create_worktree(worktree_path, commit, cwd=self._store.repo_dir)
            finally:
                if self._git_lock:
                    self._git_lock.release()
            # Symlink .venv directories from the main repo into the worktree.
            # Worktrees only contain git-tracked files, but experiments often
            # need virtual environments which are in .gitignore.
            self._symlink_venvs(worktree_path)
            run_cwd = str(worktree_path)
        except Exception:
            worktree_path = None  # fall back to main repo

        try:
            cur_branch = get_current_branch(cwd=self._store.repo_dir)
            cur_commit = get_head_commit(cwd=self._store.repo_dir)
            meta = {
                **exp.get("meta", {}),
                "git_branch": branch,
                "git_commit": cur_commit,
            }
            if worktree_path:
                meta["worktree"] = str(worktree_path)
            self._store.update_experiment(exp_id, meta=meta)
            exp["meta"] = meta
        except Exception:
            pass

        base = script_path.with_suffix("")
        log_path = base.with_suffix(".log")
        err_path = base.with_suffix(".err")
        progress_path = base.with_suffix(".progress")

        # Clear previous log/err/progress on restart
        for p in (log_path, err_path, progress_path):
            if p.exists():
                p.unlink()

        # Set env vars so scripts can verify they were launched by the backend
        run_token = secrets.token_hex(16)
        env = {
            **os.environ,
            "THE_LAB_TOKEN": run_token,
            "THE_LAB_EXP_ID": str(exp_id),
            "THE_LAB_IDEA_ID": str(exp["idea_id"]),
            "THE_LAB_PROGRESS": str(progress_path),
        }

        # Write stdout/stderr directly to the log file so it survives server restarts.
        log_file = open(log_path, "w")
        process = await asyncio.create_subprocess_exec(
            "bash", str(script_path),
            stdout=log_file,
            stderr=asyncio.subprocess.STDOUT,
            cwd=run_cwd,
            env=env,
        )
        log_file.close()  # the child process has inherited the fd

        exp = self._store.update_experiment(
            exp_id,
            status="running",
            pid=process.pid,
            error=None,
            started_at=datetime.now(timezone.utc).isoformat(),
            finished_at=None,
        )

        task = asyncio.create_task(
            self._monitor(exp_id, process, err_path, timeout=timeout)
        )
        self._tasks[exp_id] = task

        return {"status": "running", "pid": process.pid, "experiment": exp}

    async def _monitor(
        self,
        exp_id: int,
        process: asyncio.subprocess.Process,
        err_path: Path,
        timeout: float | None = None,
    ):
        timed_out = False
        if timeout is not None:
            try:
                await asyncio.wait_for(process.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                timed_out = True
                try:
                    os.kill(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), timeout=10)
                except asyncio.TimeoutError:
                    try:
                        os.kill(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    await process.wait()
        else:
            await process.wait()

        exp = self._store.get_experiment(exp_id)
        if not exp or exp["status"] == "cancelled":
            return

        log_path = (self._store.repo_dir / exp["script"]).with_suffix(".log")
        output = log_path.read_text() if log_path.exists() else ""
        now = datetime.now(timezone.utc).isoformat()

        if timed_out:
            error = f"killed: exceeded timeout of {timeout}s"
            self._store.update_experiment(
                exp_id, status="failed", error=error, pid=None, finished_at=now,
            )
            err_path.write_text(f"{error}\n\n{output[-2000:]}")
        elif process.returncode == 0:
            result = self._extract_json(output)
            if result is not None:
                metrics = result.get("metrics", {})
                meta = {**exp.get("meta", {}), **result.get("meta", {})}
                self._store.update_experiment(
                    exp_id, status="completed", metrics=metrics, meta=meta,
                    pid=None, finished_at=now,
                )
            else:
                self._store.update_experiment(
                    exp_id, status="completed", metrics=None,
                    pid=None, finished_at=now,
                )
        else:
            error = f"exit code {process.returncode}"
            self._store.update_experiment(
                exp_id, status="failed", error=error, pid=None, finished_at=now,
            )
            err_path.write_text(f"{error}\n\n{output[-2000:]}")

        # Clean up worktree
        wt = (exp.get("meta") or {}).get("worktree")
        if wt and Path(wt).exists():
            try:
                remove_worktree(wt, cwd=self._store.repo_dir)
            except Exception:
                pass

        self._tasks.pop(exp_id, None)
        self._finished_queue.put_nowait(exp_id)

    def _extract_json(self, output: str) -> dict | None:
        lines = output.strip().split("\n")
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
        return None

    async def cancel(self, exp_id: int) -> dict | None:
        exp = self._store.get_experiment(exp_id)
        if not exp:
            return None
        if exp["status"] != "running":
            return exp

        if exp.get("pid"):
            try:
                os.kill(exp["pid"], signal.SIGTERM)
            except ProcessLookupError:
                pass
            await asyncio.sleep(10)
            try:
                os.kill(exp["pid"], signal.SIGKILL)
            except ProcessLookupError:
                pass

        # Clean up worktree
        wt = (exp.get("meta") or {}).get("worktree")
        if wt and Path(wt).exists():
            try:
                remove_worktree(wt, cwd=self._store.repo_dir)
            except Exception:
                pass

        return self._store.update_experiment(
            exp_id,
            status="cancelled",
            pid=None,
            finished_at=datetime.now(timezone.utc).isoformat(),
        )

    def get_log(self, exp_id: int, tail: int | None = None) -> str | None:
        """Read the .log file for an experiment, optionally tail N lines."""
        exp = self._store.get_experiment(exp_id)
        if not exp:
            return None
        log_path = self._store.repo_dir / exp["script"].replace(".sh", ".log")
        if not log_path.exists():
            return ""
        content = log_path.read_text()
        if tail is not None:
            lines = content.split("\n")
            content = "\n".join(lines[-tail:])
        return content

    def reset_seen(self):
        """Mark all currently finished experiments as seen, so /wait starts fresh."""
        self._seen.clear()
        for status in ("completed", "failed", "cancelled"):
            for exp in self._store.list_experiments_by_status(status):
                self._seen.add(exp["id"])

    async def wait_any(
        self,
        timeout: float = 3600,
        experiment_id: int | None = None,
        idea_id: int | None = None,
    ) -> dict:
        """Block until an experiment finishes.

        Optional filters:
          experiment_id — only return when this specific experiment finishes
          idea_id       — only return experiments belonging to this idea
        """
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            # Check store for any finished experiments we haven't returned yet.
            # This catches results across server restarts and race conditions.
            for status in ("completed", "failed"):
                for exp in self._store.list_experiments_by_status(status):
                    if exp["id"] in self._seen:
                        continue
                    if experiment_id is not None and exp["id"] != experiment_id:
                        continue
                    if idea_id is not None and exp.get("idea_id") != idea_id:
                        continue
                    self._seen.add(exp["id"])
                    return {
                        "event": exp["status"],
                        "experiment": exp,
                    }

            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                running = self._store.list_experiments_by_status("running")
                if experiment_id is not None:
                    running = [e for e in running if e["id"] == experiment_id]
                if idea_id is not None:
                    running = [e for e in running if e.get("idea_id") == idea_id]
                return {
                    "event": "timeout",
                    "running": [e["id"] for e in running],
                }

            # Wait for queue notification OR poll every 5s (whichever comes first)
            poll_timeout = min(remaining, 5.0)
            try:
                await asyncio.wait_for(
                    self._finished_queue.get(), timeout=poll_timeout
                )
            except asyncio.TimeoutError:
                pass
            # In both cases (queue event or poll timeout), loop back and
            # re-check the store. The queue is used purely as a wake-up
            # signal; filtering happens in the store scan above.
