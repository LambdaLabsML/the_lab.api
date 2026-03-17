"""Experiment subprocess runner."""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import signal
from datetime import datetime, timezone
from pathlib import Path

from .git_ops import get_current_branch, get_head_commit
from .store import Store


class ExperimentRunner:
    def __init__(self, store: Store):
        self._store = store
        self._finished_queue: asyncio.Queue[int] = asyncio.Queue()
        self._tasks: dict[int, asyncio.Task] = {}
        # Recover orphaned experiments on startup.
        # If the process is still alive, re-attach to it. Otherwise mark as failed.
        self._reattach_running = []
        for exp in self._store.list_experiments_by_status("running"):
            pid = exp.get("pid")
            if pid and self._pid_alive(pid):
                # Process still running — we'll re-attach in an async task
                self._reattach_running.append(exp)
            else:
                self._store.update_experiment(
                    exp["id"],
                    status="failed",
                    error="server restarted while experiment was running (process gone)",
                    pid=None,
                    finished_at=datetime.now(timezone.utc).isoformat(),
                )

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

    async def reattach_running(self):
        """Re-attach to experiments that survived a server restart.
        Call this once after the event loop is running."""
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

        # Try to determine exit status from the output
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
                exp_id, status="failed",
                error="process exited but no valid JSON found in output",
                pid=None, finished_at=now,
            )
            err_path.write_text("process exited but no valid JSON found in output")

        self._tasks.pop(exp_id, None)
        self._finished_queue.put_nowait(exp_id)

    async def start(self, exp_id: int) -> dict:
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

        # Record git state in experiment meta
        try:
            branch = get_current_branch(cwd=self._store.repo_dir)
            commit = get_head_commit(cwd=self._store.repo_dir)
            meta = {**exp.get("meta", {}), "git_branch": branch, "git_commit": commit}
            self._store.update_experiment(exp_id, meta=meta)
            exp["meta"] = meta
        except Exception:
            pass

        base = script_path.with_suffix("")
        log_path = base.with_suffix(".log")
        err_path = base.with_suffix(".err")

        # Clear previous log/err on restart
        for p in (log_path, err_path):
            if p.exists():
                p.unlink()

        # Set env vars so scripts can verify they were launched by the backend
        run_token = secrets.token_hex(16)
        env = {
            **os.environ,
            "THE_LAB_TOKEN": run_token,
            "THE_LAB_EXP_ID": str(exp_id),
            "THE_LAB_IDEA_ID": str(exp["idea_id"]),
        }

        # Write stdout/stderr directly to the log file so it survives server restarts.
        # The bash process owns the file handle — if we restart, it keeps writing.
        log_file = open(log_path, "w")
        process = await asyncio.create_subprocess_exec(
            "bash", str(script_path),
            stdout=log_file,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(self._store.repo_dir),
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
            self._monitor(exp_id, process, err_path)
        )
        self._tasks[exp_id] = task

        return {"status": "running", "pid": process.pid, "experiment": exp}

    async def _monitor(
        self,
        exp_id: int,
        process: asyncio.subprocess.Process,
        err_path: Path,
    ):
        await process.wait()

        exp = self._store.get_experiment(exp_id)
        if not exp or exp["status"] == "cancelled":
            return

        # Read the log file (written directly by the bash process)
        log_path = (self._store.repo_dir / exp["script"]).with_suffix(".log")
        output = log_path.read_text() if log_path.exists() else ""
        now = datetime.now(timezone.utc).isoformat()

        if process.returncode == 0:
            result = self._extract_json(output)
            if result is not None:
                metrics = result.get("metrics", {})
                meta = {**exp.get("meta", {}), **result.get("meta", {})}
                self._store.update_experiment(
                    exp_id, status="completed", metrics=metrics, meta=meta,
                    pid=None, finished_at=now,
                )
            else:
                error = "script exited 0 but no valid JSON found in last stdout line"
                self._store.update_experiment(
                    exp_id, status="failed", error=error, pid=None, finished_at=now,
                )
                err_path.write_text(error)
        else:
            error = f"exit code {process.returncode}"
            self._store.update_experiment(
                exp_id, status="failed", error=error, pid=None, finished_at=now,
            )
            err_path.write_text(f"{error}\n\n{output[-2000:]}")

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

    async def wait_any(self, timeout: float = 3600) -> dict:
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            # Check store for any finished experiments we haven't returned yet.
            # This catches results across server restarts and race conditions.
            for status in ("completed", "failed"):
                for exp in self._store.list_experiments_by_status(status):
                    if exp["id"] not in self._seen:
                        self._seen.add(exp["id"])
                        return {
                            "event": exp["status"],
                            "experiment": exp,
                        }

            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                running = self._store.list_experiments_by_status("running")
                return {
                    "event": "timeout",
                    "running": [e["id"] for e in running],
                }

            # Wait for queue notification OR poll every 5s (whichever comes first)
            poll_timeout = min(remaining, 5.0)
            try:
                exp_id = await asyncio.wait_for(
                    self._finished_queue.get(), timeout=poll_timeout
                )
            except asyncio.TimeoutError:
                # No queue event — loop back and re-check store
                continue

            if exp_id in self._seen:
                continue

            self._seen.add(exp_id)
            exp = self._store.get_experiment(exp_id)
            if exp:
                return {
                    "event": exp["status"],
                    "experiment": exp,
                }
            # Experiment vanished — loop and try again
