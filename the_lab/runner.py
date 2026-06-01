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
from .queue import Allocator, load_config, match_resource
from .sandbox import build_sandbox_command, load_sandbox_config, sandbox_capabilities
from .store import Store
from . import token_registry


def _parse_log_progress(lines: list[str]) -> dict | None:
    """Scan log lines and return a progress snapshot with the same schema as
    the final metrics, plus ``pct_complete`` (0–100).

    Parses Tetris turn lines:
      [  42] move=LEFT  score=100  restarts=2  tick=0.1s  latency=144ms  t_remaining=230s

    Collects latencies from the last 500 turn lines to compute live percentiles.
    Extracts model name and duration from earlier log lines.
    """
    import re
    import statistics

    turn_pat = re.compile(
        r'\[\s*(\d+)\]\s+move=(\S+)\s+score=(\d+)\s+restarts=(\d+)'
        r'.*?latency=(\d+)ms.*?t_remaining=([0-9.]+)s'
    )
    model_pat  = re.compile(r"model='([^']+)'|serve\s+([\w/.\-]+)\s+--host")
    dur_pat    = re.compile(r"'duration':\s*(\d+)")

    # ── collect all turn lines (scan whole log, keep last 500 latencies) ──
    latencies: list[int] = []
    last_turn = last_score = last_restarts = 0
    last_t_remaining = 0.0
    for line in lines:
        m = turn_pat.search(line)
        if m:
            last_turn      = int(m.group(1))
            last_score     = int(m.group(3))
            last_restarts  = int(m.group(4))
            latencies.append(int(m.group(5)))
            last_t_remaining = float(m.group(6))

    if not latencies:
        return None

    # ── model name ──
    model = ""
    for line in lines:
        mm = model_pat.search(line)
        if mm:
            model = mm.group(1) or mm.group(2) or ""
            if model:
                break

    # ── duration (from server-ready state line) ──
    duration = 300
    for line in lines:
        dm = dur_pat.search(line)
        if dm:
            duration = int(dm.group(1))
            break

    # 0–99 while running (t_remaining=0 ≠ completed — cleanup/GIF still in progress).
    # The runner writes pct_complete=100 when the experiment actually finishes.
    raw = (1.0 - last_t_remaining / duration) * 100 if duration else 0.0
    pct_complete = round(max(0.0, min(99.0, raw)), 1)

    # ── latency stats (last 500 samples) ──
    lats = sorted(latencies[-500:])
    def pct(p: float) -> int:
        if not lats: return 0
        return lats[min(int(len(lats) * p / 100), len(lats) - 1)]

    return {
        "model":                  model,
        "score":                  last_score,
        "lines":                  0,          # not tracked live
        "level":                  1,
        "restarts":               last_restarts,
        "turns":                  last_turn,
        "latency_mean_ms":        int(statistics.mean(lats)) if lats else 0,
        "latency_p50_ms":         pct(50),
        "latency_p90_ms":         pct(90),
        "latency_p95_ms":         pct(95),
        "latency_p99_ms":         pct(99),
        "latency_min_ms":         lats[0]  if lats else 0,
        "latency_max_ms":         lats[-1] if lats else 0,
        # ── progress-specific ──
        "pct_complete":           round(pct_complete, 4),
        "t_remaining":            last_t_remaining,
    }


class ExperimentRunner:
    def __init__(self, store: Store):
        self._store = store
        self._finished_queue: asyncio.Queue[int] = asyncio.Queue()
        self._tasks: dict[int, asyncio.Task] = {}
        self._git_lock = None  # initialized as asyncio.Lock in reattach_running
        self._worktree_dir = store.repo_dir / ".the_lab" / "worktrees"
        self._worktree_dir.mkdir(parents=True, exist_ok=True)

        # Queue / scheduler state — populated lazily on the first schedule
        # tick so we don't need an event-loop at construction time.
        self._allocator = Allocator()
        self._scheduler_task: asyncio.Task | None = None
        self._scheduler_wake: asyncio.Event | None = None  # set when something queueable changes
        self._last_queue_broadcast: float = 0.0  # throttle for queue_changed events

        # Recover orphaned experiments on startup.
        # If the process is still alive, re-attach to it. Otherwise mark as failed.
        self._reattach_running = []
        running_worktrees = set()
        for exp in self._store.list_experiments_by_status("running"):
            pid = exp.get("pid")
            slurm_job_id = (exp.get("meta") or {}).get("slurm_job_id")
            if slurm_job_id:
                # Slurm experiment — always reattach via _monitor_slurm_job,
                # never mark as failed based on local PID (there is none).
                self._reattach_running.append(exp)
            elif pid and self._pid_alive(pid):
                self._reattach_running.append(exp)
                wt = (exp.get("meta") or {}).get("worktree")
                if wt:
                    running_worktrees.add(wt)
            else:
                if not self._reconcile_stale_running(exp):
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

    @staticmethod
    def _signal_experiment(pid: int, sig: int) -> None:
        """Signal the full experiment process group when possible."""
        try:
            os.killpg(pid, sig)
            return
        except (ProcessLookupError, PermissionError):
            return
        except OSError:
            pass
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    def _symlink_venvs(self, worktree_path: Path):
        """Symlink .venv directories from the main repo into the worktree.

        Worktrees only contain git-tracked files, but experiments often need
        virtual environments (in .gitignore). Only inspect the repo root and
        first-level package directories; traversing `.the_lab/worktrees` makes
        startup time grow with every historical worktree.
        """
        repo = self._store.repo_dir
        candidates = [repo / ".venv", repo / ".vllm"]
        for child in repo.iterdir():
            if not child.is_dir():
                continue
            if child.name in {".git", ".the_lab"}:
                continue
            candidates.append(child / ".venv")
            candidates.append(child / ".vllm")

        for venv_dir in candidates:
            if not venv_dir.is_dir():
                continue
            try:
                rel = venv_dir.relative_to(repo)
            except ValueError:
                continue
            target = worktree_path / rel
            if target.exists() or target.is_symlink():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(venv_dir)

    def _symlink_the_lab_files(self, worktree_path: Path):
        """Symlink .the_lab/preamble.sh and .the_lab/bin/ into the worktree.

        These files are gitignored (branch-independent infrastructure) but
        experiments need them in worktrees — same pattern as _symlink_venvs.
        """
        lab_dir = self._store.repo_dir / ".the_lab"

        src = lab_dir / "preamble.sh"
        if src.exists():
            dst = worktree_path / ".the_lab" / "preamble.sh"
            dst.parent.mkdir(parents=True, exist_ok=True)
            if not dst.exists() and not dst.is_symlink():
                dst.symlink_to(src)

        src_bin = lab_dir / "bin"
        if src_bin.is_dir():
            dst_bin = worktree_path / ".the_lab" / "bin"
            dst_bin.parent.mkdir(parents=True, exist_ok=True)
            if not dst_bin.exists() and not dst_bin.is_symlink():
                dst_bin.symlink_to(src_bin)

    async def reattach_running(self):
        """Re-attach to experiments that survived a server restart.
        Call this once after the event loop is running."""
        self._git_lock = asyncio.Lock()
        self._scheduler_wake = asyncio.Event()
        for exp in self._reattach_running:
            meta = exp.get("meta") or {}
            slurm_job_id = meta.get("slurm_job_id")
            if slurm_job_id:
                # Re-attach Slurm job monitor
                from .executors.slurm import SlurmExecutor
                from .queue import load_config
                resources, _ = load_config(self._store.repo_dir)
                resource_name = meta.get("assigned_resource")
                resource = next((r for r in resources if r.name == resource_name), None)
                if resource and resource.kind == "slurm":
                    ssh_host = resource.executor_config.get("ssh_host", "slurm")
                    executor = SlurmExecutor(ssh_host, resource.executor_config, instance_id=self._store.instance_id)
                    label = exp.get("label") or str(exp["id"])
                    script_path = self._store.repo_dir / exp["script"]
                    local_exp_dir = script_path.parent
                    # Re-register the slurm run token so wrapper callbacks
                    # (POST /pull, POST /progress) are accepted after restart.
                    run_token = meta.get("slurm_run_token")
                    if run_token:
                        token_registry.register(run_token)
                    task = asyncio.create_task(
                        self._monitor_slurm_job(
                            exp["id"], executor, slurm_job_id, label, local_exp_dir
                        )
                    )
                    self._tasks[exp["id"]] = task
                    continue
            task = asyncio.create_task(self._monitor_pid(exp))
            self._tasks[exp["id"]] = task
        if self._reattach_running:
            labels = [e.get("label", str(e["id"])) for e in self._reattach_running]
            print(f"[the-lab] re-attached to {len(labels)} running experiment(s): {labels}")
        # Restore allocator state so cancel/release work correctly for
        # re-attached experiments.
        self._allocator.restore_from_running(self._reattach_running)
        self._reattach_running = []
        # Start the scheduler loop.
        self._scheduler_task = asyncio.create_task(self._scheduler_loop())

    def wake_scheduler(self) -> None:
        """Nudge the scheduler — call when an experiment is queued/finished."""
        if self._scheduler_wake is not None:
            self._scheduler_wake.set()
        # Emit queue_changed at most once per second.
        import time as _time
        now = _time.monotonic()
        if now - self._last_queue_broadcast > 1.0:
            self._last_queue_broadcast = now
            from . import ws as ws_mod
            ws_mod.broadcaster.broadcast_soon({"type": "queue_changed"})

    async def _scheduler_loop(self) -> None:
        """Tick on a configurable interval (or when woken) and dispatch
        queued experiments onto free resources.
        """
        while True:
            try:
                resources, qc = load_config(self._store.repo_dir)
                interval = max(0.5, qc.dispatch_interval_s)
                if not qc.paused:
                    await self._dispatch_once(resources)
            except Exception as e:
                # Scheduler must never die — log and continue.
                print(f"[the-lab] scheduler error: {e}")
                interval = 5.0
            try:
                await asyncio.wait_for(self._scheduler_wake.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
            self._scheduler_wake.clear()

    async def _dispatch_once(self, resources: list) -> None:
        """One scheduler pass: walk queued experiments and start what fits."""
        if not resources:
            return
        # Queue-related fields live inside ``meta`` so we can extend the
        # model without an on-disk migration.
        def _qmeta(exp: dict) -> dict:
            return exp.get("meta") or {}

        # Find queued (or legacy "pending") experiments. Prefer priority
        # desc, then created_at asc.
        all_exps = self._store.list_all_experiments()
        queued = [
            e for e in all_exps
            if e.get("status") in ("queued", "pending")
        ]
        queued.sort(key=lambda e: (-int(_qmeta(e).get("priority", 0) or 0), e.get("created_at") or ""))
        # Index of experiments by label for dependency lookups.
        by_label: dict[str, dict] = {(e.get("label") or str(e["id"])): e for e in all_exps}

        for exp in queued:
            qm = _qmeta(exp)
            # Dependency check.
            deps = list(qm.get("depends_on") or [])
            on_success = bool(qm.get("depends_on_success", True))
            ready = True
            for dep_label in deps:
                dep = by_label.get(dep_label)
                if not dep:
                    # Missing dependency — fail the experiment with a clear reason.
                    self._store.update_experiment(
                        (exp.get("label") or str(exp["id"])),
                        status="failed",
                        error=f"depends_on references unknown experiment '{dep_label}'",
                        finished_at=datetime.now(timezone.utc).isoformat(),
                    )
                    ready = False
                    break
                if dep.get("status") in ("queued", "pending", "running"):
                    ready = False
                    break
                if on_success and dep.get("status") in ("failed", "cancelled"):
                    self._store.update_experiment(
                        (exp.get("label") or str(exp["id"])),
                        status="cancelled",
                        error=f"parent experiment '{dep_label}' {dep['status']}",
                        finished_at=datetime.now(timezone.utc).isoformat(),
                    )
                    ready = False
                    break
            if not ready:
                continue

            # Pick a resource matching requirements.
            req = dict(qm.get("requirements") or {})
            resource = match_resource(resources, req)
            if resource is None:
                continue
            units_wanted = int(req.get("units") or resource.default_units_per_job)
            label = exp.get("label") or str(exp["id"])
            # Re-check status: it may have been cancelled while we awaited
            # the previous dispatch (the queued list is a snapshot from above).
            current = self._store.get_experiment(label)
            if not current or current.get("status") not in ("queued", "pending"):
                continue
            assigned = self._allocator.reserve(label, resource, units_wanted)
            if assigned is None:
                continue  # not enough free units / max_parallel reached

            # Stash the assignment in meta so cancel/restart can release.
            try:
                meta = {
                    **(exp.get("meta") or {}),
                    "assigned_resource": resource.name,
                    "assigned_units": assigned,
                }
                self._store.update_experiment(label, meta=meta)
            except Exception:
                pass

            # Build env additions for the executor.
            extra_env: dict[str, str] = {}
            if resource.unit_kind == "gpu" and resource.kind == "local":
                # For local GPU resources: set CUDA_VISIBLE_DEVICES so the
                # subprocess only sees its allocated GPU unit(s).
                # For slurm resources: omit this — slurm's --gres=gpu:N binding
                # exposes the allocated GPU as index 0 inside the job; setting
                # a higher index (from our logical unit counter) would cause
                # NVMLError_InvalidArgument.
                extra_env["CUDA_VISIBLE_DEVICES"] = ",".join(str(u) for u in assigned)
            extra_env["THE_LAB_ASSIGNED_RESOURCE"] = resource.name
            extra_env["THE_LAB_ASSIGNED_UNITS"] = ",".join(str(u) for u in assigned)

            # The caller may have stashed a timeout on the experiment's
            # meta (POST /experiments/{ref}/start with {"timeout": N}).
            # Apply it here so the start path respects the same SIGTERM
            # deadline whether the user kicked it via /start or the
            # scheduler dispatched it on its own.
            try:
                meta_timeout = qm.get("timeout")
                start_timeout = float(meta_timeout) if meta_timeout is not None else None
            except (TypeError, ValueError):
                start_timeout = None

            try:
                if resource.kind == "slurm":
                    result = await self._start_slurm(label, exp, resource, assigned, extra_env)
                else:
                    result = await self.start(label, env_extra=extra_env, timeout=start_timeout)
                if result.get("status") == "error":
                    # Release the units we held; the start failed.
                    self._allocator.release(label)
                else:
                    from . import ws as ws_mod
                    ws_mod.broadcaster.broadcast({
                        "type": "experiment_started",
                        "label": label,
                        "idea_id": exp.get("idea_id"),
                        "assigned_resource": resource.name,
                    })
            except Exception as e:
                self._allocator.release(label)
                print(f"[the-lab] failed to start exp {label}: {e}")

    def _cleanup_worktree(self, exp: dict) -> None:
        wt = (exp.get("meta") or {}).get("worktree")
        if wt and Path(wt).exists():
            try:
                remove_worktree(wt, cwd=self._store.repo_dir)
            except Exception:
                pass

    def _reconcile_stale_running(self, exp: dict) -> bool:
        """Recover a running experiment whose PID is gone but output is complete.

        This happens when the backend loses the subprocess wait path but the
        experiment itself already wrote its final metrics/progress files.
        """
        exp_id = exp["id"]
        script_path = self._store.repo_dir / exp["script"]
        base = script_path.with_suffix("")
        log_path = base.with_suffix(".log")
        progress_path = base.with_suffix(".progress")
        now = datetime.now(timezone.utc).isoformat()

        output = log_path.read_text() if log_path.exists() else ""
        result, result_idx = self._extract_json(output)
        _label = exp.get("label") or str(exp_id)
        _idea_id = exp.get("idea_id")
        if result is not None:
            metrics = result.get("metrics", {})
            meta = {**exp.get("meta", {}), **result.get("meta", {})}
            self._store.update_experiment(
                exp_id,
                status="completed",
                metrics=metrics,
                meta=meta,
                pid=None,
                error=None,
                finished_at=now,
            )
            if result_idx is not None:
                self._strip_result_line(log_path, result_idx)
            self._cleanup_worktree(exp)
            self._allocator.release(_label)
            self.wake_scheduler()
            from . import ws as ws_mod
            ws_mod.broadcaster.broadcast_soon({"type": "experiment_finished", "label": _label,
                                               "idea_id": _idea_id, "status": "completed",
                                               "metrics": metrics})
            return True

        if progress_path.exists():
            try:
                progress = json.loads(progress_path.read_text())
            except json.JSONDecodeError:
                progress = {}
            if progress.get("pipeline_status") == "done" or progress.get("status") == "done":
                self._store.update_experiment(
                    exp_id,
                    status="completed",
                    metrics=exp.get("metrics"),
                    pid=None,
                    error=None,
                    finished_at=now,
                )
                self._cleanup_worktree(exp)
                self._allocator.release(_label)
                self.wake_scheduler()
                from . import ws as ws_mod
                ws_mod.broadcaster.broadcast_soon({"type": "experiment_finished", "label": _label,
                                                   "idea_id": _idea_id, "status": "completed",
                                                   "metrics": exp.get("metrics")})
                return True

        return False

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

        result, result_idx = self._extract_json(output)
        if result is not None:
            metrics = result.get("metrics", {})
            meta = {**exp.get("meta", {}), **result.get("meta", {})}
            self._store.update_experiment(
                exp_id, status="completed", metrics=metrics, meta=meta,
                pid=None, finished_at=now,
            )
            if result_idx is not None:
                self._strip_result_line(log_path, result_idx)
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

        label = exp.get("label") or str(exp_id)
        self._allocator.release(label)
        self.wake_scheduler()

        self._tasks.pop(exp_id, None)
        self._finished_queue.put_nowait(exp_id)

    async def start(
        self,
        exp_id,
        timeout: float | None = None,
        env_extra: dict[str, str] | None = None,
    ) -> dict:
        exp = self._store.get_experiment(exp_id)
        if not exp:
            return {"status": "error", "reason": f"experiment {exp_id} not found"}
        if exp["status"] not in ("queued", "pending", "failed", "cancelled"):
            return {"status": "error", "reason": f"experiment is {exp['status']}, expected queued, failed, or cancelled"}

        script_path = self._store.repo_dir / exp["script"]
        if not script_path.exists():
            msg = (
                f"script not found at {exp['script']!r}. "
                f"Provide script_content when creating the experiment, or use "
                f"POST /experiments/<source>/rerun on an experiment that has a script."
            )
            self._store.update_experiment(
                label, status="failed", error=msg,
                finished_at=datetime.now(timezone.utc).isoformat(),
            )
            return {"status": "error", "reason": msg}

        os.chmod(script_path, os.stat(script_path).st_mode | 0o755)

        try:
            desc = exp.get("description", "")
            label = exp.get("label", str(exp_id))
            auto_commit(cwd=self._store.repo_dir, message=f"exp {label}: {desc}")
        except Exception:
            pass

        # Resolve the idea's branch to a commit, then create a worktree so
        # this experiment runs in isolation (concurrent experiments don't
        # interfere with each other or the main checkout).
        idea = self._store.get_idea(exp["idea_id"])
        branch = idea["branch"] if idea else get_current_branch(cwd=self._store.repo_dir)
        worktree_path = None
        worktree_commit = None  # the actual commit the experiment runs on
        run_cwd = str(self._store.repo_dir)  # fallback

        try:
            worktree_commit = resolve_branch_commit(branch, cwd=self._store.repo_dir)
            worktree_path = self._worktree_dir / str(exp_id).replace(".", "_")
            if worktree_path.exists():
                remove_worktree(worktree_path, cwd=self._store.repo_dir)
            if self._git_lock:
                await self._git_lock.acquire()
            try:
                create_worktree(worktree_path, worktree_commit, cwd=self._store.repo_dir)
            finally:
                if self._git_lock:
                    self._git_lock.release()
            # Symlink .venv directories from the main repo into the worktree.
            # Worktrees only contain git-tracked files, but experiments often
            # need virtual environments which are in .gitignore.
            self._symlink_venvs(worktree_path)
            self._symlink_the_lab_files(worktree_path)
            run_cwd = str(worktree_path)
        except Exception:
            worktree_path = None  # fall back to main repo

        try:
            # Log the commit the experiment actually runs on (the worktree's commit),
            # not the main checkout's HEAD which may differ.
            actual_commit = worktree_commit if worktree_commit else get_head_commit(cwd=self._store.repo_dir)
            meta = {
                **exp.get("meta", {}),
                "git_branch": branch,
                "git_commit": actual_commit,
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
        metrics_path = base.with_suffix(".metrics.jsonl")

        for p in (log_path, err_path, progress_path, metrics_path):
            if p.exists():
                p.unlink()

        run_token = secrets.token_hex(16)
        token_registry.register(run_token)
        env = {
            **os.environ,
            "THE_LAB_TOKEN": run_token,
            "THE_LAB_EXP_ID": str(exp_id),
            "THE_LAB_IDEA_ID": str(exp["idea_id"]),
            "THE_LAB_PROGRESS": str(progress_path),
            "THE_LAB_METRICS": str(metrics_path),
        }
        if env_extra:
            env.update(env_extra)
        command = ["bash", str(script_path)]
        sandbox_config = load_sandbox_config(self._store.repo_dir)
        if sandbox_config.get("enabled", False) and not os.environ.get("THE_LAB_NO_SANDBOX"):
            capabilities = sandbox_capabilities()
            if not capabilities.get("available"):
                details = capabilities.get("details") or "sandbox runtime unavailable"
                return {"status": "error", "reason": f"sandbox is enabled but unavailable: {details}"}
            env["THE_LAB_SANDBOX_TARGET_UID"] = str(os.getuid())
            env["THE_LAB_SANDBOX_TARGET_GID"] = str(os.getgid())
            command = build_sandbox_command(
                self._store.repo_dir,
                "experiment",
                f"exp-{exp_id}",
                command,
                config=sandbox_config,
                cwd=run_cwd,
            )

        # Write stdout/stderr directly to the log file so it survives server restarts.
        log_file = open(log_path, "w")
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=log_file,
            stderr=asyncio.subprocess.STDOUT,
            cwd=run_cwd,
            env=env,
            start_new_session=True,
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
            self._monitor(exp_id, process, err_path, timeout=timeout, run_token=run_token)
        )
        self._tasks[exp_id] = task

        return {"status": "running", "pid": process.pid, "experiment": exp}

    async def _monitor(
        self,
        exp_id: int,
        process: asyncio.subprocess.Process,
        err_path: Path,
        timeout: float | None = None,
        run_token: str = "",
    ):
        timed_out = False
        if timeout is not None:
            try:
                await asyncio.wait_for(process.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                timed_out = True
                self._signal_experiment(process.pid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(process.wait(), timeout=10)
                except asyncio.TimeoutError:
                    self._signal_experiment(process.pid, signal.SIGKILL)
                    await process.wait()
        else:
            await process.wait()

        exp = self._store.get_experiment(exp_id)
        if not exp or exp["status"] == "cancelled":
            return

        log_path = (self._store.repo_dir / exp["script"]).with_suffix(".log")
        output = log_path.read_text() if log_path.exists() else ""
        now = datetime.now(timezone.utc).isoformat()

        from . import ws as ws_mod
        _label = exp.get("label") or str(exp_id)
        _idea_id = exp.get("idea_id")

        if timed_out:
            error = f"killed: exceeded timeout of {timeout}s"
            self._store.update_experiment(
                exp_id, status="failed", error=error, pid=None, finished_at=now,
            )
            err_path.write_text(f"{error}\n\n{output[-2000:]}")
            ws_mod.broadcaster.broadcast({"type": "experiment_finished", "label": _label,
                                          "idea_id": _idea_id, "status": "failed", "metrics": None})
        elif process.returncode == 0:
            result, result_idx = self._extract_json(output)
            if result is not None:
                metrics = result.get("metrics", {})
                meta = {**exp.get("meta", {}), **result.get("meta", {})}
                self._store.update_experiment(
                    exp_id, status="completed", metrics=metrics, meta=meta,
                    pid=None, finished_at=now,
                )
                if result_idx is not None:
                    self._strip_result_line(log_path, result_idx)
                ws_mod.broadcaster.broadcast({"type": "experiment_finished", "label": _label,
                                              "idea_id": _idea_id, "status": "completed",
                                              "metrics": metrics})
            else:
                self._store.update_experiment(
                    exp_id, status="completed", metrics=None,
                    pid=None, finished_at=now,
                )
                ws_mod.broadcaster.broadcast({"type": "experiment_finished", "label": _label,
                                              "idea_id": _idea_id, "status": "completed",
                                              "metrics": None})
        else:
            error = f"exit code {process.returncode}"
            self._store.update_experiment(
                exp_id, status="failed", error=error, pid=None, finished_at=now,
            )
            err_path.write_text(f"{error}\n\n{output[-2000:]}")
            ws_mod.broadcaster.broadcast({"type": "experiment_finished", "label": _label,
                                          "idea_id": _idea_id, "status": "failed", "metrics": None})

        # Clean up worktree
        wt = (exp.get("meta") or {}).get("worktree")
        if wt and Path(wt).exists():
            try:
                remove_worktree(wt, cwd=self._store.repo_dir)
            except Exception:
                pass

        # Release the resource units this experiment held, then wake the
        # scheduler so it can fill the freed capacity.
        try:
            label = exp.get("label") or str(exp_id)
            self._allocator.release(label)
            self.wake_scheduler()
        except Exception:
            pass

        self._tasks.pop(exp_id, None)
        self._finished_queue.put_nowait(exp_id)
        # Revoke the per-experiment bearer token now that the process is done.
        if run_token:
            token_registry.unregister(run_token)

    async def _start_slurm(
        self,
        label: str,
        exp: dict,
        resource,
        assigned: list[int],
        env_extra: dict[str, str],
    ) -> dict:
        """Submit an experiment to Slurm via SSH and start monitoring."""
        import base64
        from .executors.slurm import SlurmExecutor

        ssh_host = resource.executor_config.get("ssh_host", "slurm")
        executor = SlurmExecutor(ssh_host, resource.executor_config, instance_id=self._store.instance_id)

        # Build env dict that the wrapper script will expose to the experiment
        lab_user = os.environ.get("THE_LAB_USER", "")
        lab_password = os.environ.get("THE_LAB_PASSWORD", "")
        lab_auth = base64.b64encode(f"{lab_user}:{lab_password}".encode()).decode()

        # The callback URL must be reachable from the *compute node*, not just
        # localhost.  Priority order:
        #   1. THE_LAB_API_URL env var (explicit override)
        #   2. callback_url in the resource executor_config (per-resource config)
        #   3. Auto-detect: server's primary network IP + THE_LAB_PORT
        port = os.environ.get("THE_LAB_PORT", "9000")
        _callback_default = resource.executor_config.get("callback_url")
        if not _callback_default:
            try:
                import socket as _socket
                _s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
                _s.connect(("8.8.8.8", 80))
                _ip = _s.getsockname()[0]
                _s.close()
                _callback_default = f"http://{_ip}:{port}/api/v1"
            except Exception:
                _callback_default = f"http://localhost:{port}/api/v1"
        lab_api_url = os.environ.get("THE_LAB_API_URL", _callback_default)

        script_path = self._store.repo_dir / exp["script"]
        local_exp_dir = script_path.parent
        if not script_path.exists():
            msg = (
                f"script file missing: {exp['script']!r}. "
                f"The experiment was created without script_content — "
                f"provide script_content when creating, or use "
                f"POST /experiments/<source>/rerun on an experiment that has a script."
            )
            self._store.update_experiment(
                label, status="failed", error=msg,
                finished_at=datetime.now(timezone.utc).isoformat(),
            )
            print(f"[the-lab] errored experiment {label}: {msg}")
            return {"status": "error", "reason": msg}

        # Git context: resolve branch + commit from the idea so the executor
        # can push the right branch and create an isolated worktree.
        # (The local runner resolves this in start(); for slurm we do it here.)
        exp_meta = exp.get("meta") or {}
        git_branch = exp_meta.get("git_branch")
        git_commit = exp_meta.get("git_commit")
        if not git_branch or not git_commit:
            try:
                idea = self._store.get_idea(exp["idea_id"])
                git_branch = idea["branch"] if idea else get_current_branch(cwd=self._store.repo_dir)
                git_commit = resolve_branch_commit(git_branch, cwd=self._store.repo_dir)
                # Commit to auto-save any local changes on the idea branch.
                try:
                    auto_commit(cwd=self._store.repo_dir,
                                message=f"exp {label}: pre-slurm submit")
                    git_commit = resolve_branch_commit(git_branch, cwd=self._store.repo_dir)
                except Exception:
                    pass
            except Exception as e:
                print(f"[the-lab] could not resolve git context for {label}: {e}")

        # Generate a run token — the SCRIPT_GUARD in every experiment script
        # requires THE_LAB_TOKEN to be set (prevents accidental direct execution).
        # Register it so slurm jobs can call back to the API (push progress etc.)
        import secrets as _secrets
        run_token = _secrets.token_hex(16)
        token_registry.register(run_token)

        # Env vars the wrapper embeds as explicit exports so the job has them
        # even though it runs in a separate SSH/sbatch environment.
        wrapper_env = {
            "THE_LAB_TOKEN":   run_token,
            "THE_LAB_API_URL": lab_api_url,
            "THE_LAB_AUTH":    lab_auth,
            "THE_LAB_EXP_ID":  str(exp["id"]),
            "THE_LAB_IDEA_ID": str(exp.get("idea_id", "")),
            **{k: str(v) for k, v in env_extra.items()},
        }

        now = datetime.now(timezone.utc).isoformat()
        try:
            # Run all SSH/git operations in a thread so the asyncio event loop
            # (and therefore the HTTP server) stays responsive during submission.
            job_id = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: executor.submit(
                    label,
                    script_path,
                    wrapper_env,
                    local_exp_dir,
                    unit_kind=resource.unit_kind,
                    local_repo_dir=self._store.repo_dir,
                    git_branch=git_branch,
                    git_commit=git_commit,
                ),
            )
        except Exception as e:
            print(f"[the-lab] slurm submit for {label} failed: {e}")
            return {"status": "error", "reason": str(e)}

        meta = {
            **(exp.get("meta") or {}),
            "slurm_job_id": job_id,
            "slurm_run_token": run_token,  # stored so monitor can unregister on completion
            "assigned_resource": resource.name,
            "assigned_units": assigned,
            # Persist the resolved git context so the dashboard + PROMPT.md
            # can show which branch/commit the job ran on.
            **({"git_branch": git_branch} if git_branch else {}),
            **({"git_commit": git_commit} if git_commit else {}),
        }
        exp_id = exp["id"]
        self._store.update_experiment(
            exp_id,
            status="running",
            started_at=now,
            meta=meta,
            pid=None,
            error=None,
            finished_at=None,
        )

        task = asyncio.create_task(
            self._monitor_slurm_job(exp_id, executor, job_id, label, local_exp_dir)
        )
        self._tasks[exp_id] = task

        return {"status": "running", "slurm_job_id": job_id}

    async def _monitor_slurm_job(
        self,
        exp_id: int,
        executor,
        job_id: str,
        label: str,
        local_exp_dir: Path,
    ) -> None:
        """Poll squeue until the Slurm job finishes, then pull results."""
        from . import ws as ws_mod

        # Unregister the run token when the job finishes so the auth registry
        # stays clean.  The token is persisted in meta at dispatch time.
        _run_token: str = ""
        exp = self._store.get_experiment(exp_id)
        if exp:
            _run_token = (exp.get("meta") or {}).get("slurm_run_token", "")

        try:
            await self._monitor_slurm_job_inner(
                exp_id, executor, job_id, label, local_exp_dir, ws_mod
            )
        finally:
            if _run_token:
                token_registry.unregister(_run_token)

    async def _monitor_slurm_job_inner(
        self,
        exp_id: int,
        executor,
        job_id: str,
        label: str,
        local_exp_dir: Path,
        ws_mod,
    ) -> None:
        poll_interval = 30
        _live_pull_counter = 0
        while True:
            await asyncio.sleep(poll_interval)

            try:
                state = executor.poll_status(job_id)
            except Exception as e:
                print(f"[the-lab] poll_status for job {job_id} failed: {e}")
                state = ""  # treat as gone

            if state in ("RUNNING", "PENDING"):
                poll_interval = 30
                # Pull live logs every 30s so the dashboard shows progress
                _live_pull_counter += 1
                if _live_pull_counter % 1 == 0:
                    try:
                        await asyncio.get_event_loop().run_in_executor(None, lambda: executor.pull_results(label, local_exp_dir))
                        # After pulling, synthesize a progress snapshot from the
                        # most recent log lines so GET /progress is always useful
                        # even when the script doesn't write to $THE_LAB_PROGRESS.
                        _log_path = local_exp_dir / "script.log"
                        _prog_path = local_exp_dir / "script.progress"
                        if _log_path.exists():
                            try:
                                _lines = _log_path.read_text().splitlines()
                                _snap = _parse_log_progress(_lines)
                                if _snap:
                                    import json as _json
                                    _prog_path.write_text(_json.dumps(_snap))
                            except Exception:
                                pass
                        ws_mod.broadcaster.broadcast_soon({
                            "type": "experiment_log_updated",
                            "label": label,
                            "idea_id": exp_id,
                        })
                    except Exception:
                        pass
                continue
            elif state == "COMPLETING":
                poll_interval = 5
                continue
            elif state == "PREEMPTED":
                # Wrapper should have called /requeue already; handle it
                # defensively in case the HTTP call failed.
                exp = self._store.get_experiment(exp_id)
                if exp and exp.get("status") == "running":
                    now = datetime.now(timezone.utc).isoformat()
                    meta = dict(exp.get("meta") or {})
                    slurm_attempts = list(meta.get("slurm_attempts") or [])
                    slurm_attempts.append({
                        "job_id": job_id,
                        "reason": "preempted",
                        "at": now,
                    })
                    meta["slurm_attempts"] = slurm_attempts
                    meta.pop("slurm_job_id", None)
                    self._store.update_experiment(
                        exp_id,
                        status="queued",
                        queued_at=now,
                        meta=meta,
                        error=None,
                    )
                    self._allocator.release(label)
                    self.wake_scheduler()
                self._tasks.pop(exp_id, None)
                self._finished_queue.put_nowait(exp_id)
                return
            elif state in ("COMPLETED", ""):
                # Pull results from remote
                await asyncio.get_event_loop().run_in_executor(None, lambda: executor.pull_results(label, local_exp_dir))
                exp = self._store.get_experiment(exp_id)
                # If slurm_done already marked the experiment failed (non-zero exit
                # code), trust it — Slurm reports COMPLETED for the *job* even when
                # the *script* exits non-zero, so the monitor must not overwrite a
                # failure that slurm_done recorded from the actual exit code.
                if exp and exp.get("status") == "failed":
                    asyncio.get_event_loop().run_in_executor(None, lambda: executor.cleanup_remote(label, abs_bare_path=getattr(executor, "_resolved_bare_path", None)))
                    self._allocator.release(label)
                    self.wake_scheduler()
                    self._tasks.pop(exp_id, None)
                    self._finished_queue.put_nowait(exp_id)
                    return
                if not exp or exp.get("status") == "cancelled":
                    asyncio.get_event_loop().run_in_executor(None, lambda: executor.cleanup_remote(label, abs_bare_path=getattr(executor, "_resolved_bare_path", None)))
                    self._allocator.release(label)
                    self.wake_scheduler()
                    self._tasks.pop(exp_id, None)
                    self._finished_queue.put_nowait(exp_id)
                    return
                now = datetime.now(timezone.utc).isoformat()
                log_path = local_exp_dir / "script.log"
                output = log_path.read_text() if log_path.exists() else ""
                result, result_idx = self._extract_json(output)
                if result is not None:
                    # If result has a nested "metrics" key use it; otherwise
                    # treat the whole result as metrics (e.g. Tetris format
                    # where keys like "turns", "latency_mean_ms" are top-level).
                    if "metrics" in result:
                        metrics = result.get("metrics", {})
                    else:
                        metrics = {k: v for k, v in result.items()
                                   if k not in ("meta",)}
                    # Rewrite remote artifact paths to local pulled copies.
                    # Job root is the only sync target now (worktree excluded).
                    # Scripts must copy artifacts to the job root before exit.
                    for _key in ("gif",):
                        _remote = (metrics or {}).get(_key)
                        if _remote and isinstance(_remote, str):
                            _name = Path(_remote).name
                            _local = local_exp_dir / _name
                            if _local.exists():
                                metrics[_key] = str(_local)
                    meta = {**(exp.get("meta") or {}), **result.get("meta", {})}
                    self._store.update_experiment(
                        exp_id,
                        status="completed",
                        metrics=metrics,
                        meta=meta,
                        pid=None,
                        finished_at=now,
                    )
                    # Overwrite the progress file with final state so GET /progress
                    # never returns stale intermediate data after completion.
                    _progress_path = local_exp_dir / "script.progress"
                    try:
                        import json as _json
                        _progress_path.write_text(_json.dumps({
                            "_final": True, "pct_complete": 100, **(metrics or {})
                        }))
                    except OSError:
                        pass
                    if result_idx is not None:
                        self._strip_result_line(log_path, result_idx)
                    ws_mod.broadcaster.broadcast_soon({
                        "type": "experiment_finished",
                        "label": label,
                        "idea_id": exp.get("idea_id"),
                        "status": "completed",
                        "metrics": metrics,
                    })
                else:
                    # No JSON metrics in output. Check the log for fatal error
                    # indicators — if found, mark as failed rather than completed
                    # with metrics=null. This catches cases where the script exits
                    # 0 (or slurm_done never reached the API) but the log clearly
                    # shows a fatal failure.
                    _fatal_patterns = ("FATAL", "\nError:", "Traceback (most recent", "error: command")
                    _log_has_fatal = any(p in output for p in _fatal_patterns)
                    if _log_has_fatal:
                        _last_lines = "\n".join(output.splitlines()[-10:])
                        self._store.update_experiment(
                            exp_id,
                            status="failed",
                            error=f"fatal error detected in log (no metrics produced):\n{_last_lines}",
                            pid=None,
                            finished_at=now,
                        )
                        ws_mod.broadcaster.broadcast_soon({
                            "type": "experiment_finished",
                            "label": label,
                            "idea_id": exp.get("idea_id"),
                            "status": "failed",
                            "metrics": None,
                        })
                    else:
                        self._store.update_experiment(
                            exp_id,
                            status="completed",
                            metrics=None,
                            pid=None,
                            finished_at=now,
                        )
                        ws_mod.broadcaster.broadcast_soon({
                            "type": "experiment_finished",
                            "label": label,
                            "idea_id": exp.get("idea_id"),
                            "status": "completed",
                            "metrics": None,
                        })
                asyncio.get_event_loop().run_in_executor(None, lambda: executor.cleanup_remote(label, abs_bare_path=getattr(executor, "_resolved_bare_path", None)))
                self._allocator.release(label)
                self.wake_scheduler()
                self._tasks.pop(exp_id, None)
                self._finished_queue.put_nowait(exp_id)
                return
            elif state in ("FAILED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL", "CANCELLED"):
                await asyncio.get_event_loop().run_in_executor(None, lambda: executor.pull_results(label, local_exp_dir))
                exp = self._store.get_experiment(exp_id)
                if not exp or exp.get("status") == "cancelled":
                    asyncio.get_event_loop().run_in_executor(None, lambda: executor.cleanup_remote(label, abs_bare_path=getattr(executor, "_resolved_bare_path", None)))
                    self._allocator.release(label)
                    self.wake_scheduler()
                    self._tasks.pop(exp_id, None)
                    self._finished_queue.put_nowait(exp_id)
                    return
                now = datetime.now(timezone.utc).isoformat()
                log_path = local_exp_dir / "script.log"
                output = log_path.read_text() if log_path.exists() else ""
                # Try to extract JSON metrics — the script may have completed
                # successfully before Slurm killed/cancelled the wrapper.
                result, result_idx = self._extract_json(output)
                if result is not None:
                    if "metrics" in result:
                        metrics = result.get("metrics", {})
                    else:
                        metrics = {k: v for k, v in result.items() if k not in ("meta",)}
                    meta = {**(exp.get("meta") or {}), **result.get("meta", {})}
                    self._store.update_experiment(
                        exp_id,
                        status="completed",
                        metrics=metrics,
                        meta=meta,
                        pid=None,
                        finished_at=now,
                    )
                    if result_idx is not None:
                        self._strip_result_line(log_path, result_idx)
                    ws_mod.broadcaster.broadcast_soon({
                        "type": "experiment_finished",
                        "label": label,
                        "idea_id": exp.get("idea_id"),
                        "status": "completed",
                        "metrics": metrics,
                    })
                else:
                    # No metrics — mark as failed with the last lines of the log
                    # so the dashboard shows why the script died (not just the
                    # Slurm state which is always vague).
                    log_tail = "\n".join(output.splitlines()[-30:]) if output else ""
                    error = f"slurm job {job_id} ended with state {state}"
                    if log_tail:
                        error = f"{error}\n\n--- last log lines ---\n{log_tail}"
                    err_path = local_exp_dir / "script.err"
                    try:
                        err_path.write_text(error)
                    except OSError:
                        pass
                    self._store.update_experiment(
                        exp_id,
                        status="failed",
                        error=error,
                        pid=None,
                        finished_at=now,
                    )
                    from . import ws as ws_mod
                    ws_mod.broadcaster.broadcast_soon({
                        "type": "experiment_finished",
                        "label": label,
                        "idea_id": exp.get("idea_id"),
                        "status": "failed",
                        "metrics": None,
                    })
                asyncio.get_event_loop().run_in_executor(None, lambda: executor.cleanup_remote(label, abs_bare_path=getattr(executor, "_resolved_bare_path", None)))
                self._allocator.release(label)
                self.wake_scheduler()
                self._tasks.pop(exp_id, None)
                self._finished_queue.put_nowait(exp_id)
                return
            # Unknown state — keep polling
            poll_interval = 30

    def _extract_json(self, output: str) -> tuple[dict | None, int | None]:
        """Find the last line that parses as a JSON object, scanning from end.

        Returns ``(parsed_dict, line_index)`` or ``(None, None)``. Skips
        non-JSON lines (log messages, separators, etc.) so the result dict
        is found even when trailing log output appears after it.
        """
        lines = output.split("\n")
        for i in range(len(lines) - 1, -1, -1):
            stripped = lines[i].strip()
            if not stripped or stripped.startswith("{") is False:
                continue
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed, i
        return None, None

    @staticmethod
    def _strip_result_line(log_path: Path, line_idx: int) -> None:
        """Remove a specific line from a log file in place.

        Used after the runner has captured the JSON result from the log: we
        don't want the raw dump cluttering the "Show log" view, and the
        parsed data already lives on the experiment record. Best-effort —
        failures are swallowed because the experiment is already recorded.
        """
        if line_idx < 0:
            return
        try:
            text = log_path.read_text()
        except OSError:
            return
        lines = text.split("\n")
        if line_idx >= len(lines):
            return
        del lines[line_idx]
        try:
            log_path.write_text("\n".join(lines))
        except OSError:
            pass

    @staticmethod
    def _strip_trailing_json_line(text: str) -> str:
        """Drop a trailing JSON-object line (defense-in-depth for legacy logs).

        Newly-completed experiments have their result line stripped from
        disk via _strip_result_line, but older runs still have it. This
        runs on every read of the log so the "Show log" view stays clean.
        """
        if not text:
            return text
        lines = text.split("\n")
        for i in range(len(lines) - 1, -1, -1):
            stripped = lines[i].strip()
            if not stripped:
                continue
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, dict):
                    del lines[i]
                    return "\n".join(lines)
            except json.JSONDecodeError:
                pass
            return text
        return text

    async def cancel(self, exp_id) -> dict | None:
        from . import ws as ws_mod
        exp = self._store.get_experiment(exp_id)
        if not exp:
            return None
        # Release any resource the experiment held in the allocator (queued
        # exps don't hold any, but doesn't hurt to call).
        label = exp.get("label") or str(exp_id)
        idea_id = exp.get("idea_id")
        self._allocator.release(label)
        if exp["status"] in ("queued", "pending"):
            self.wake_scheduler()
            result = self._store.update_experiment(
                exp_id,
                status="cancelled",
                finished_at=datetime.now(timezone.utc).isoformat(),
            )
            ws_mod.broadcaster.broadcast({"type": "experiment_cancelled",
                                          "label": label, "idea_id": idea_id})
            return result
        if exp["status"] != "running":
            return exp

        # Handle Slurm jobs
        slurm_job_id = (exp.get("meta") or {}).get("slurm_job_id")
        if slurm_job_id:
            try:
                from .executors.slurm import SlurmExecutor
                from .queue import load_config
                resources, _ = load_config(self._store.repo_dir)
                resource_name = (exp.get("meta") or {}).get("assigned_resource")
                resource = next((r for r in resources if r.name == resource_name), None)
                if resource and resource.kind == "slurm":
                    ssh_host = resource.executor_config.get("ssh_host", "slurm")
                    executor = SlurmExecutor(ssh_host, resource.executor_config, instance_id=self._store.instance_id)
                    executor.cancel(slurm_job_id)
            except Exception as e:
                print(f"[the-lab] scancel for {label} failed: {e}")
        elif exp.get("pid"):
            self._signal_experiment(exp["pid"], signal.SIGTERM)
            await asyncio.sleep(10)
            self._signal_experiment(exp["pid"], signal.SIGKILL)

        # Clean up worktree
        wt = (exp.get("meta") or {}).get("worktree")
        if wt and Path(wt).exists():
            try:
                remove_worktree(wt, cwd=self._store.repo_dir)
            except Exception:
                pass

        result = self._store.update_experiment(
            exp_id,
            status="cancelled",
            pid=None,
            finished_at=datetime.now(timezone.utc).isoformat(),
        )
        ws_mod.broadcaster.broadcast({"type": "experiment_cancelled",
                                      "label": label, "idea_id": idea_id})
        return result

    def get_log(self, exp_id: int, tail: int | None = None) -> str | None:
        """Read the .log file for an experiment, optionally tail N lines.

        Trailing JSON-object lines (the script's final result dump) are
        filtered out — the parsed metrics already live on the experiment
        record and the raw blob clutters the "Show log" view. Newly-
        completed runs have the line stripped from disk; this filter is
        defense-in-depth for legacy logs.
        """
        exp = self._store.get_experiment(exp_id)
        if not exp:
            return None
        log_path = self._store.repo_dir / exp["script"].replace(".sh", ".log")
        if not log_path.exists():
            return ""
        content = self._strip_trailing_json_line(log_path.read_text())
        if tail is not None:
            lines = content.split("\n")
            content = "\n".join(lines[-tail:])
        return content

    async def pull_results_for_label(self, label: str) -> bool:
        """Trigger an immediate rsync pull for a slurm experiment. Used by
        the compute-node wrapper to confirm data is safe before cleanup."""
        from .executors.slurm import SlurmExecutor
        exp = self._store.get_experiment(label)
        if not exp:
            return False
        meta = exp.get("meta") or {}
        resource_name = meta.get("assigned_resource")
        if not resource_name:
            return False
        from .queue import load_config as _load_config
        _resources, _ = _load_config(self._store.repo_dir)
        resource = next(
            (r for r in _resources if r.name == resource_name),
            None,
        )
        if not resource or resource.kind != "slurm":
            return False
        ssh_host = resource.executor_config.get("ssh_host", "slurm")
        executor = SlurmExecutor(
            ssh_host, resource.executor_config,
            instance_id=self._store.instance_id,
        )
        script_path = self._store.repo_dir / exp["script"]
        local_exp_dir = script_path.parent
        try:
            await asyncio.get_event_loop().run_in_executor(
                None, lambda: executor.pull_results(label, local_exp_dir)
            )
            return True
        except Exception as e:
            logger.warning("pull_results_for_label %s failed: %s", label, e)
            return False

    def reset_seen(self):
        """Mark all currently finished experiments as seen, so /wait starts fresh."""
        self._seen.clear()
        for status in ("completed", "failed", "cancelled"):
            for exp in self._store.list_experiments_by_status(status):
                self._seen.add(exp["id"])

    async def wait_any(
        self,
        timeout: float = 3600,
        experiment_id=None,
        idea_id: int | None = None,
        agent_id: str | None = None,
        agent_role: str | None = None,
    ) -> dict:
        """Block until an experiment finishes — or a message arrives.

        Optional filters:
          experiment_id — only return when this specific experiment finishes
          idea_id       — only return experiments belonging to this idea
          agent_id      — also wake on unread messages addressed to this agent
          agent_role    — also wake on messages addressed to this role / 'all'

        When woken by a message, returns
        ``{"event": "message", "messages": [...], "running": [...]}``
        so the caller can react before the experiment completes.
        """
        from . import messages as messages_mod

        repo_dir = self._store.repo_dir
        wake_event = messages_mod._wake_event() if (agent_id or agent_role) else None

        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            # Reconcile stale running experiments whose process already exited.
            for exp in list(self._store.list_experiments_by_status("running")):
                pid = exp.get("pid")
                if pid is not None and not self._pid_alive(pid):
                    self._reconcile_stale_running(exp)

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

            # Check for unread messages addressed to this agent — return early
            # so the caller can react. Same recipient resolution as the
            # notifications middleware.
            if agent_id or agent_role:
                unread = messages_mod.unread_for(
                    repo_dir, agent_id=agent_id, role=agent_role, limit=20,
                )
                if unread:
                    running = self._store.list_experiments_by_status("running")
                    if experiment_id is not None:
                        running = [e for e in running if e["id"] == experiment_id]
                    if idea_id is not None:
                        running = [e for e in running if e.get("idea_id") == idea_id]
                    return {
                        "event": "message",
                        "messages": unread,
                        "running": [e["id"] for e in running],
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

            # Wait for queue notification OR message wake OR poll every 5s
            # (whichever comes first). We race two futures with asyncio.wait so
            # either source unblocks the loop immediately.
            poll_timeout = min(remaining, 5.0)
            waiters = [asyncio.create_task(self._finished_queue.get())]
            if wake_event is not None:
                wake_event.clear()
                waiters.append(asyncio.create_task(wake_event.wait()))
            try:
                done, pending = await asyncio.wait(
                    waiters,
                    timeout=poll_timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for t in pending:
                    t.cancel()
            except asyncio.TimeoutError:
                pass
            # In every case (queue event, message wake, or poll timeout) we
            # loop back and re-check the store. Filtering happens above.
