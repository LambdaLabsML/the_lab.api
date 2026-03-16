"""Experiment subprocess runner."""
from __future__ import annotations

import asyncio
import json
import os
import signal
from datetime import datetime, timezone
from pathlib import Path

from .git_ops import get_worktree_path
from .store import Store


class ExperimentRunner:
    def __init__(self, store: Store):
        self._store = store
        self._finish_event: asyncio.Event = asyncio.Event()
        self._tasks: dict[int, asyncio.Task] = {}

    async def start(self, exp_id: int) -> dict:
        exp = self._store.get_experiment(exp_id)
        if not exp:
            return {"status": "error", "reason": f"experiment {exp_id} not found"}
        if exp["status"] != "pending":
            return {"status": "error", "reason": f"experiment is {exp['status']}, expected pending"}

        worktree = get_worktree_path(exp["idea_id"], cwd=self._store.repo_dir)
        script_path = worktree / exp["script"]
        if not script_path.exists():
            return {"status": "error", "reason": f"script not found: {exp['script']}"}

        os.chmod(script_path, os.stat(script_path).st_mode | 0o755)

        base = script_path.with_suffix("")
        log_path = base.with_suffix(".log")
        json_path = base.with_suffix(".json")
        err_path = base.with_suffix(".err")

        process = await asyncio.create_subprocess_exec(
            "bash", str(script_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(worktree),
        )

        exp = self._store.update_experiment(
            exp_id,
            status="running",
            pid=process.pid,
            started_at=datetime.now(timezone.utc).isoformat(),
        )

        task = asyncio.create_task(
            self._monitor(exp_id, process, log_path, json_path, err_path)
        )
        self._tasks[exp_id] = task

        return {"status": "running", "pid": process.pid, "experiment": exp}

    async def _monitor(
        self,
        exp_id: int,
        process: asyncio.subprocess.Process,
        log_path: Path,
        json_path: Path,
        err_path: Path,
    ):
        stdout_data, _ = await process.communicate()
        output = stdout_data.decode("utf-8", errors="replace") if stdout_data else ""
        log_path.write_text(output)

        exp = self._store.get_experiment(exp_id)
        if not exp or exp["status"] == "cancelled":
            return

        now = datetime.now(timezone.utc).isoformat()

        if process.returncode == 0:
            result = self._extract_json(output)
            if result is not None:
                metrics = result.get("metrics", {})
                # Merge script meta into existing meta
                meta = {**exp.get("meta", {}), **result.get("meta", {})}
                self._store.update_experiment(
                    exp_id,
                    status="completed",
                    metrics=metrics,
                    meta=meta,
                    pid=None,
                    finished_at=now,
                )
                # Rewrite the experiment json (store already did), but also the result .json
                # Note: the .json path is the same as the experiment json, so update_experiment handles it.
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
        self._finish_event.set()
        self._finish_event.clear()

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

    async def wait_any(self, timeout: float = 3600) -> dict:
        try:
            await asyncio.wait_for(self._finish_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            running = self._store.list_experiments_by_status("running")
            return {
                "event": "timeout",
                "running": [e["id"] for e in running],
            }

        # Find the most recently finished experiment
        for status in ("completed", "failed"):
            exps = self._store.list_experiments_by_status(status)
            if exps:
                latest = max(exps, key=lambda e: e.get("finished_at", ""))
                return {
                    "event": status,
                    "experiment": latest,
                }
        return {"event": "timeout", "running": []}
