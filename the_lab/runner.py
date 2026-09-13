"""Experiment subprocess runner."""
from __future__ import annotations

from .blocking import run_blocking

import asyncio
import json
import logging
import os
import secrets
import signal
import shutil
import socket
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
from . import jsonio
from .queue import Allocator, load_config, match_resource
from .sandbox import (
    build_sandbox_command,
    gpu_host_state,
    load_sandbox_config,
    sandbox_capabilities,
)
from .store import Store
from . import token_registry


logger = logging.getLogger("the-lab.runner")


def _slurm_sync_seconds() -> float:
    """Bounded live-sync cadence; keep this configurable for slow clusters."""
    try:
        return max(2.0, min(float(os.environ.get("THE_LAB_SLURM_SYNC_SECONDS", "10")), 120.0))
    except ValueError:
        return 10.0


SLURM_LIVE_SYNC_SECONDS = _slurm_sync_seconds()


def _metrics_from_result(result: dict) -> tuple[dict, dict]:
    """Return final metrics/meta, preferring ARC scorecard score when present."""
    if "metrics" in result:
        metrics = dict(result.get("metrics") or {})
    else:
        metrics = {k: v for k, v in result.items() if k != "meta"}
    meta = dict(result.get("meta") or {})
    scorecard = meta.get("scorecard") if isinstance(meta, dict) else None
    if isinstance(scorecard, dict) and scorecard.get("score") is not None:
        raw_score = metrics.get("score")
        if raw_score is not None and raw_score != scorecard.get("score"):
            metrics["progress_score"] = raw_score
        metrics["score"] = scorecard.get("score")
        metrics["score_source"] = "scorecard"
    return metrics, meta


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


# Experiment wall-clock bounds (seconds). A caller-supplied timeout is capped at
# THE_LAB_MAX_EXPERIMENT_TIMEOUT; a caller that supplies none gets
# THE_LAB_DEFAULT_EXPERIMENT_TIMEOUT. Set either to 0 to disable that bound.
def _env_seconds(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


DEFAULT_EXPERIMENT_TIMEOUT = _env_seconds("THE_LAB_DEFAULT_EXPERIMENT_TIMEOUT", 24 * 3600)
MAX_EXPERIMENT_TIMEOUT = _env_seconds("THE_LAB_MAX_EXPERIMENT_TIMEOUT", 7 * 24 * 3600)


def _clamp_experiment_timeout(requested: float | None) -> float | None:
    """Apply the default/maximum wall-clock bounds to a requested timeout."""
    if requested is None or requested <= 0:
        requested = DEFAULT_EXPERIMENT_TIMEOUT
    if requested <= 0:
        return None            # default bound disabled by the operator
    if MAX_EXPERIMENT_TIMEOUT > 0:
        return min(requested, MAX_EXPERIMENT_TIMEOUT)
    return requested


class ExperimentRunner:
    def __init__(self, store: Store, read_only: bool = False):
        self._store = store
        self._read_only = read_only
        self._finished_queue: asyncio.Queue[int] = asyncio.Queue()
        self._tasks: dict[int, asyncio.Task] = {}
        # A wrapper callback and the periodic monitor can request rsync at the
        # same time.  One pull per remote job is enough (and avoids racing two
        # rsync writers into the same experiment directory).
        self._slurm_pull_locks: dict[str, asyncio.Lock] = {}
        self._git_lock = None  # initialized as asyncio.Lock in reattach_running
        self._loop = None
        self._worktree_dir = store.repo_dir / ".the_lab" / "worktrees"
        if not read_only:
            self._worktree_dir.mkdir(parents=True, exist_ok=True)

        # Queue / scheduler state — populated lazily on the first schedule
        # tick so we don't need an event-loop at construction time.
        self._allocator = Allocator()
        self._scheduler_task: asyncio.Task | None = None
        self._scheduler_wake: asyncio.Event | None = None  # set when something queueable changes
        self._last_queue_broadcast: float = 0.0  # throttle for queue_changed events
        self._stopping = False

        # Recover orphaned experiments on startup.
        # If the process is still alive, re-attach to it. Otherwise mark as failed.
        self._reattach_running = []
        running_worktrees = set()
        if read_only:
            # Demo mode: NEVER touch the DB — no stale-running reconcile (it
            # marks experiments failed), no worktree cleanup. The state is
            # someone else's snapshot; we only display it.
            self._seen: set[int] = set()
            return
        # ``starting`` has no durable child identity yet.  Never dispatch it
        # again after a crash: mark it inconclusive/failed so an operator can
        # explicitly requeue it rather than duplicate a possibly-submitted job.
        for exp in self._store.list_experiments_by_status("starting"):
            self._store.transition_experiment(
                exp["id"], {"starting"}, status="failed", pid=None,
                error="server restarted while experiment launch was in progress",
                status_msg="error: launch outcome unknown after restart",
                finished_at=datetime.now(timezone.utc).isoformat(),
            )
        for exp in self._store.list_experiments_by_status("running"):
            pid = exp.get("pid")
            slurm_job_id = (exp.get("meta") or {}).get("slurm_job_id")
            if slurm_job_id:
                # Slurm experiment — always reattach via _monitor_slurm_job,
                # never mark as failed based on local PID (there is none).
                self._reattach_running.append(exp)
                wt = (exp.get("meta") or {}).get("worktree")
                if wt:
                    running_worktrees.add(wt)
            elif pid and self._pid_matches_record(pid, exp):
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
                        # M3 feedback: terminal-state reason line.
                        status_msg="error: server restarted while experiment was running (process gone)",
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
    async def _read_text_off_loop(path: Path) -> str:
        """Read a (possibly multi-MB, possibly NFS) file on the threadpool so
        monitor/reconcile coroutines never stall the event loop on disk."""
        loop = asyncio.get_running_loop()

        def _read() -> str:
            return path.read_text() if path.exists() else ""

        return await loop.run_in_executor(None, _read)

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        """Check if a process is still running."""
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False

    @staticmethod
    def _process_identity(pid: int) -> dict | None:
        """Identity stronger than a recycled/copied local PID."""
        try:
            boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            start_ticks = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
            return {"run_host": socket.gethostname(), "boot_id": boot_id,
                    "pid_start_ticks": start_ticks}
        except (OSError, IndexError):
            return None

    @classmethod
    def _pid_matches_record(cls, pid: int, exp: dict) -> bool:
        identity = cls._process_identity(pid)
        meta = exp.get("meta") or {}
        # Legacy records have no identity: do not attach/signal a random PID.
        return bool(identity and all(meta.get(k) == identity[k]
                                     for k in ("run_host", "boot_id", "pid_start_ticks")))

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
        self._loop = asyncio.get_running_loop()
        if self._read_only:
            # Demo mode: no monitors, no scheduler loop, no dispatching.
            self._git_lock = asyncio.Lock()
            self._scheduler_wake = asyncio.Event()
            return
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
                resources, _ = (await run_blocking(load_config, self._store.repo_dir))
                resource_name = meta.get("assigned_resource")
                resource = next((r for r in resources if r.name == resource_name), None)
                if resource and resource.kind == "slurm":
                    ssh_host = resource.executor_config.get("ssh_host", "slurm")
                    executor = SlurmExecutor(ssh_host, resource.executor_config, instance_id=self._store.instance_id,
                                             repo_dir=self._store.repo_dir)
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
            if self._loop is not None:
                self._loop.call_soon_threadsafe(self._scheduler_wake.set)
            else:  # useful for isolated/unit-created runners
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
        while not self._stopping:
            try:
                resources, qc = (await run_blocking(load_config, self._store.repo_dir))
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

    async def shutdown(self) -> None:
        """Stop local scheduler/monitor tasks without touching child jobs.

        Children intentionally survive a server restart and are reattached by
        the next writer; cancelling their monitor coroutines must not signal
        their process groups.
        """
        self._stopping = True
        if self._scheduler_wake is not None:
            self._scheduler_wake.set()
        tasks = [t for t in [self._scheduler_task, *self._tasks.values()] if t]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._scheduler_task = None
        self._tasks.clear()

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
            # Compute a safe label up front so it is always defined before any
            # code below can raise — fixes "local variable 'label' referenced
            # before assignment" in the dispatch except handler.
            label = exp.get("label") or str(exp.get("id") or "?")
            qm = _qmeta(exp)
            # Dependency check.
            deps = list(qm.get("depends_on") or [])
            on_success = bool(qm.get("depends_on_success", True))
            ready = True
            for dep_label in deps:
                dep = by_label.get(dep_label)
                if not dep:
                    # Missing dependency — fail the experiment with a clear reason.
                    (await run_blocking(self._store.update_experiment,
                        label,
                        status="failed",
                        error=f"depends_on references unknown experiment '{dep_label}'",
                        # M3 feedback: terminal-state reason line.
                        status_msg=f"error: depends_on references unknown experiment '{dep_label}'",
                        finished_at=datetime.now(timezone.utc).isoformat(),
                    ))
                    ready = False
                    break
                if dep.get("status") in ("queued", "pending", "running"):
                    ready = False
                    break
                if on_success and dep.get("status") in ("failed", "cancelled"):
                    (await run_blocking(self._store.update_experiment,
                        label,
                        status="cancelled",
                        error=f"parent experiment '{dep_label}' {dep['status']}",
                        # M3 feedback: terminal-state reason line.
                        status_msg=f"cancelled: parent experiment '{dep_label}' {dep['status']}",
                        finished_at=datetime.now(timezone.utc).isoformat(),
                    ))
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
            # (label already computed at the top of the loop)
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
                (await run_blocking(self._store.update_experiment, label, meta=meta))
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
            # A run with no timeout held its resource units forever, so one
            # wedged experiment could stall the queue indefinitely. Apply a
            # default deadline when the caller didn't set one, and cap what a
            # caller may ask for. Both are env-overridable for long training
            # jobs; 0 / negative disables (explicitly opting back in to the old
            # unbounded behaviour).
            start_timeout = _clamp_experiment_timeout(start_timeout)

            try:
                if resource.kind == "slurm":
                    result = await self._start_slurm(label, exp, resource, assigned, extra_env)
                else:
                    result = await self.start(label, env_extra=extra_env, timeout=start_timeout)
                if result.get("status") == "error":
                    # Release the units we held; the start failed.
                    self._allocator.release(label)
                elif resource.kind == "slurm":
                    # Only the slurm path broadcasts here — start() (the local
                    # path) already emits experiment_started itself, and
                    # emitting from both sites doubled the activity-feed line.
                    from . import ws as ws_mod
                    ws_mod.broadcaster.broadcast({
                        "type": "experiment_started",
                        "experiment_id": exp.get("id"),  # M3 feedback
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
            # M3 cancel-bug fix: re-read status immediately before the terminal
            # write. If a user cancelled while we reconciled, don't clobber it.
            _cur = self._store.get_experiment(exp_id)
            if _cur and _cur.get("status") == "cancelled":
                return True
            metrics, result_meta = _metrics_from_result(result)
            meta = {**exp.get("meta", {}), **result_meta}
            self._store.update_experiment(
                exp_id,
                status="completed",
                metrics=metrics,
                meta=meta,
                pid=None,
                error=None,
                status_msg=None,  # normal success
                finished_at=now,
            )
            if result_idx is not None:
                self._strip_result_line(log_path, result_idx)
            self._cleanup_worktree(exp)
            self._allocator.release(_label)
            self.wake_scheduler()
            from . import ws as ws_mod
            ws_mod.broadcaster.broadcast_soon({"type": "experiment_finished", "label": _label,
                                               "experiment_id": exp_id,
                                               "idea_id": _idea_id, "status": "completed",
                                               "status_msg": None, "metrics": metrics})
            return True

        if progress_path.exists():
            try:
                progress = json.loads(progress_path.read_text())
            except json.JSONDecodeError:
                progress = {}
            if progress.get("pipeline_status") == "done" or progress.get("status") == "done":
                # M3 cancel-bug fix: re-read status before clobbering with a
                # terminal write, so a concurrent cancel wins.
                _cur = self._store.get_experiment(exp_id)
                if _cur and _cur.get("status") == "cancelled":
                    return True
                self._store.update_experiment(
                    exp_id,
                    status="completed",
                    metrics=exp.get("metrics"),
                    pid=None,
                    error=None,
                    status_msg=None,  # normal success
                    finished_at=now,
                )
                self._cleanup_worktree(exp)
                self._allocator.release(_label)
                self.wake_scheduler()
                from . import ws as ws_mod
                ws_mod.broadcaster.broadcast_soon({"type": "experiment_finished", "label": _label,
                                                   "experiment_id": exp_id,
                                                   "idea_id": _idea_id, "status": "completed",
                                                   "status_msg": None,
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
        output = await self._read_text_off_loop(log_path)

        # M3 cancel-bug fix: re-read status before the terminal write so a
        # cancel that landed while we polled the PID isn't clobbered.
        _cur = self._store.get_experiment(exp_id)
        if _cur and _cur.get("status") == "cancelled":
            self._tasks.pop(exp_id, None)
            self._finished_queue.put_nowait(exp_id)
            return

        result, result_idx = self._extract_json(output)
        if result is not None:
            metrics, result_meta = _metrics_from_result(result)
            meta = {**exp.get("meta", {}), **result_meta}
            (await run_blocking(self._store.update_experiment,
                exp_id, status="completed", metrics=metrics, meta=meta,
                pid=None, status_msg=None, finished_at=now,  # normal success
            ))
            if result_idx is not None:
                await asyncio.get_running_loop().run_in_executor(
                    None, self._strip_result_line, log_path, result_idx)
        else:
            # Metrics-optional: assume success if process was running normally
            (await run_blocking(self._store.update_experiment,
                exp_id, status="completed", metrics=None,
                pid=None, status_msg=None, finished_at=now,  # normal success
            ))

        # Clean up worktree
        wt = (exp.get("meta") or {}).get("worktree")
        if wt and (await run_blocking(Path(wt).exists)):
            try:
                (await run_blocking(remove_worktree, wt, cwd=self._store.repo_dir))
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
        if self._read_only:
            return {"status": "error", "reason": "runner is read-only"}
        exp = self._store.get_experiment(exp_id)
        if not exp:
            return {"status": "error", "reason": f"experiment {exp_id} not found"}
        if exp["status"] not in ("queued", "pending", "failed", "cancelled"):
            return {"status": "error", "reason": f"experiment is {exp['status']}, expected queued, failed, or cancelled"}

        # Claim the state before the first await.  A cancellation can now
        # change starting→cancelled, and every later launch/terminal write
        # checks this CAS rather than resurrecting it.
        claimed = await run_blocking(
            self._store.transition_experiment, exp_id,
            {"queued", "pending", "failed", "cancelled"}, status="starting",
        )
        if not claimed:
            return {"status": "error", "reason": "experiment state changed before start"}
        exp = claimed
        worktree_path = None

        async def _abort_launch(reason: str, *, token: str = "", log_file=None) -> None:
            """Leave a claimed launch terminal without blocking the event loop."""
            if log_file is not None:
                try:
                    await run_blocking(log_file.close)
                except Exception:
                    pass
            if token:
                token_registry.unregister(token)
            if worktree_path:
                await run_blocking(self._cleanup_worktree,
                                   {"meta": {"worktree": str(worktree_path)}})
            await run_blocking(
                self._store.transition_experiment, exp_id, {"starting"},
                status="failed", error=reason, status_msg=f"error: {reason}",
                pid=None, finished_at=datetime.now(timezone.utc).isoformat(),
            )

        script_path = self._store.repo_dir / exp["script"]
        if not (await run_blocking(script_path.exists)):
            msg = (
                f"script not found at {exp['script']!r}. "
                f"Provide script_content when creating the experiment, or use "
                f"POST /experiments/<source>/rerun on an experiment that has a script."
            )
            await _abort_launch(msg)
            return {"status": "error", "reason": msg}

        try:
            await run_blocking(lambda: os.chmod(script_path, os.stat(script_path).st_mode | 0o755))
        except Exception as exc:
            await _abort_launch(f"unable to prepare experiment script: {exc}")
            return {"status": "error", "reason": str(exc)}

        loop = asyncio.get_running_loop()

        def _auto_commit_sync():
            desc = exp.get("description", "")
            lbl = exp.get("label", str(exp_id))
            auto_commit(cwd=self._store.repo_dir, message=f"exp {lbl}: {desc}")

        try:
            # Git subprocesses block — run off-loop so a slow NFS index scan
            # can't stall the scheduler, WS pings, and every in-flight request.
            await loop.run_in_executor(None, _auto_commit_sync)
        except Exception:
            pass

        # Resolve the idea's branch to a commit, then create a worktree so
        # this experiment runs in isolation (concurrent experiments don't
        # interfere with each other or the main checkout).
        idea = self._store.get_idea(exp["idea_id"])
        worktree_commit = None  # the actual commit the experiment runs on
        run_cwd = str(self._store.repo_dir)  # fallback

        def _prepare_worktree_sync(branch: str):
            """Blocking git + symlink work; runs on the threadpool."""
            commit = resolve_branch_commit(branch, cwd=self._store.repo_dir)
            wt_path = self._worktree_dir / str(exp_id).replace(".", "_")
            if wt_path.exists():
                remove_worktree(wt_path, cwd=self._store.repo_dir)
            create_worktree(wt_path, commit, cwd=self._store.repo_dir)
            # Symlink .venv directories from the main repo into the worktree.
            # Worktrees only contain git-tracked files, but experiments often
            # need virtual environments which are in .gitignore.
            self._symlink_venvs(wt_path)
            self._symlink_the_lab_files(wt_path)
            return wt_path, commit

        try:
            branch = idea["branch"] if idea else await loop.run_in_executor(
                None, lambda: get_current_branch(cwd=self._store.repo_dir))
            if self._git_lock:
                await self._git_lock.acquire()
            try:
                worktree_path, worktree_commit = await loop.run_in_executor(
                    None, _prepare_worktree_sync, branch)
            finally:
                if self._git_lock:
                    self._git_lock.release()
            run_cwd = str(worktree_path)
        except Exception:
            worktree_path = None  # fall back to main repo
            if idea:
                branch = idea["branch"]
            else:
                try:
                    branch = await loop.run_in_executor(
                        None, lambda: get_current_branch(cwd=self._store.repo_dir))
                except Exception:
                    branch = "main"

        try:
            # Log the commit the experiment actually runs on (the worktree's commit),
            # not the main checkout's HEAD which may differ.
            actual_commit = worktree_commit if worktree_commit else await loop.run_in_executor(
                None, lambda: get_head_commit(cwd=self._store.repo_dir))
            meta = {
                **exp.get("meta", {}),
                "git_branch": branch,
                "git_commit": actual_commit,
            }
            if worktree_path:
                meta["worktree"] = str(worktree_path)
            exp = (await run_blocking(self._store.update_experiment, exp_id, meta=meta)) or exp
        except Exception:
            pass

        base = script_path.with_suffix("")
        log_path = base.with_suffix(".log")
        err_path = base.with_suffix(".err")
        progress_path = base.with_suffix(".progress")
        metrics_path = base.with_suffix(".metrics.jsonl")

        for p in (log_path, err_path, progress_path, metrics_path):
            try:
                if (await run_blocking(p.exists)):
                    (await run_blocking(p.unlink))
            except Exception as exc:
                await _abort_launch(f"unable to reset experiment sidecar {p.name}: {exc}")
                return {"status": "error", "reason": str(exc)}

        # Check before allocating external credentials or starting a child.
        if (self._store.get_experiment(exp_id) or {}).get("status") != "starting":
            if worktree_path:
                await run_blocking(self._cleanup_worktree,
                                   {"meta": {"worktree": str(worktree_path)}})
            return {"status": "error", "reason": "experiment cancelled during start setup"}

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
        command_script_path = script_path
        if worktree_path:
            command_script_path = worktree_path / ".the_lab" / "current_experiment" / "script.sh"
            try:
                (await run_blocking(command_script_path.parent.mkdir, parents=True, exist_ok=True))
                (await run_blocking(shutil.copy2, script_path, command_script_path))
                await run_blocking(os.chmod, command_script_path, 0o755)
            except Exception as exc:
                await _abort_launch(f"unable to prepare worktree script: {exc}", token=run_token)
                return {"status": "error", "reason": str(exc)}

        command = ["bash", str(command_script_path)]
        try:
            uses_inner_arc_sandbox = "_sandboxed" in await self._read_text_off_loop(script_path)
        except Exception:
            uses_inner_arc_sandbox = False

        try:
            sandbox_config = (await run_blocking(load_sandbox_config, self._store.repo_dir))
        except Exception as exc:
            await _abort_launch(f"unable to load sandbox configuration: {exc}", token=run_token)
            return {"status": "error", "reason": str(exc)}
        if (
            sandbox_config.get("enabled", False)
            and not os.environ.get("THE_LAB_NO_SANDBOX")
            and not uses_inner_arc_sandbox
        ):
            try:
                capabilities = (await run_blocking(sandbox_capabilities))
            except Exception as exc:
                await _abort_launch(f"unable to inspect sandbox: {exc}", token=run_token)
                return {"status": "error", "reason": str(exc)}
            if not capabilities.get("available"):
                # Keep the stored reason to a few lines (it shows up in the
                # experiment record), but still say what to install and how.
                summary = capabilities.get("summary") or "The sandbox runtime is unavailable."
                install = capabilities.get("install_command")
                parts = [f"Experiment not started — the sandbox is enabled but unavailable. {summary}"]
                if install:
                    parts.append(f"Install the missing programs with: {install}")
                parts.append(
                    "Full explanation in the dashboard's Sandbox tab. To run without "
                    "isolation, disable the sandbox there."
                )
                # Newline-joined so the install command is not run together with
                # the following sentence.
                reason = "\n".join(parts)
                await _abort_launch(reason, token=run_token)
                return {"status": "error", "reason": reason}
            env["THE_LAB_SANDBOX_TARGET_UID"] = str(os.getuid())
            env["THE_LAB_SANDBOX_TARGET_GID"] = str(os.getgid())
            try:
                command = (await run_blocking(build_sandbox_command,
                    self._store.repo_dir, "experiment", f"exp-{exp_id}", command,
                    config=sandbox_config, cwd=run_cwd,
                ))
            except Exception as exc:
                await _abort_launch(f"unable to build sandbox command: {exc}", token=run_token)
                return {"status": "error", "reason": str(exc)}

        # Write stdout/stderr directly to the log file so it survives server restarts.
        try:
            log_file = (await run_blocking(open, log_path, "w"))
        except Exception as exc:
            await _abort_launch(f"unable to open experiment log: {exc}", token=run_token)
            return {"status": "error", "reason": str(exc)}
        # If the driver is loaded but this container has no GPU device nodes, CUDA
        # will fail with something opaque ("Failed to infer device type"). Say so
        # at the top of the log — this holds sandboxed or not, and cannot be fixed
        # from inside the container.
        try:
            _gpu_warning = (await run_blocking(gpu_host_state)).get("warning")
        except Exception as exc:
            await _abort_launch(f"unable to inspect GPU host state: {exc}",
                                token=run_token, log_file=log_file)
            return {"status": "error", "reason": str(exc)}
        if _gpu_warning:
            try:
                (await run_blocking(log_file.write, f"[the-lab] WARNING: {_gpu_warning}\n\n"))
                (await run_blocking(log_file.flush))
            except Exception as exc:
                await _abort_launch(f"unable to write experiment log: {exc}",
                                    token=run_token, log_file=log_file)
                return {"status": "error", "reason": str(exc)}
        # Cancellation may have happened while we prepared git/worktree state.
        current = self._store.get_experiment(exp_id)
        if not current or current.get("status") != "starting":
            try:
                await run_blocking(log_file.close)
            finally:
                token_registry.unregister(run_token)
            if worktree_path:
                await run_blocking(self._cleanup_worktree,
                                   {"meta": {"worktree": str(worktree_path)}})
            return {"status": "error", "reason": "experiment cancelled during start setup"}
        try:
            process = await asyncio.create_subprocess_exec(
                *command, stdout=log_file, stderr=asyncio.subprocess.STDOUT,
                cwd=run_cwd, env=env, start_new_session=True,
            )
        except Exception as exc:
            await _abort_launch(f"unable to launch experiment: {exc}",
                                token=run_token, log_file=log_file)
            return {"status": "error", "reason": str(exc)}
        (await run_blocking(log_file.close))  # the child process has inherited the fd

        launch_meta = {**(exp.get("meta") or {}), **(self._process_identity(process.pid) or {})}

        exp = (await run_blocking(self._store.transition_experiment,
            exp_id,
            {"starting"},
            status="running",
            pid=process.pid,
            meta=launch_meta,
            error=None,
            started_at=datetime.now(timezone.utc).isoformat(),
            finished_at=None,
        ))

        if exp is None:
            # A cancel won after the last check but before publication.  Do not
            # leave an untracked child running.
            self._signal_experiment(process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), timeout=10)
            except asyncio.TimeoutError:
                self._signal_experiment(process.pid, signal.SIGKILL)
                await process.wait()
            token_registry.unregister(run_token)
            if worktree_path:
                await run_blocking(self._cleanup_worktree,
                                   {"meta": {"worktree": str(worktree_path)}})
            return {"status": "error", "reason": "experiment cancelled during launch"}

        # M3 feedback: lightweight started broadcast on the running transition.
        # We're post-await here, so use broadcast (not broadcast_soon).
        from . import ws as ws_mod
        ws_mod.broadcaster.broadcast({
            "type": "experiment_started",
            "experiment_id": exp_id,
            "label": exp.get("label") if exp else str(exp_id),
            "idea_id": exp.get("idea_id") if exp else None,
        })

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
        output = await self._read_text_off_loop(log_path)
        now = datetime.now(timezone.utc).isoformat()

        from . import ws as ws_mod
        _label = exp.get("label") or str(exp_id)
        _idea_id = exp.get("idea_id")

        # M3 cancel-bug fix: a user cancel may have landed between the guard
        # above and the terminal write below (we awaited a log read + did work).
        # Re-read right before writing; if cancelled, leave it alone. Extends
        # the same pattern already used at the top of this method and in the
        # slurm monitor (~1118).
        def _cancelled_now() -> bool:
            _cur = self._store.get_experiment(exp_id)
            return bool(_cur and _cur.get("status") == "cancelled")

        if timed_out:
            error = f"killed: exceeded timeout of {timeout}s"
            # M3 feedback: distinguish a hard SIGTERM/timeout kill from the case
            # where the run cleanly abandoned after exhausting its budget/turns.
            # Best-effort from the live progress snapshot the script maintains.
            _msg = f"timeout: exceeded timeout of {timeout}s"
            try:
                _prog = exp.get("progress") or {}
                _budget = _prog.get("budget_remaining_s")
                if _budget is not None and float(_budget) <= 0:
                    _msg = "cap_abandoned: budget_remaining_s exhausted"
            except (TypeError, ValueError):
                pass
            if not _cancelled_now():
                (await run_blocking(self._store.transition_experiment,
                    exp_id, {"running"}, status="failed", error=error,
                    status_msg=_msg, pid=None, finished_at=now,
                ))
                (await run_blocking(err_path.write_text, f"{error}\n\n{output[-2000:]}"))
                ws_mod.broadcaster.broadcast({"type": "experiment_finished", "label": _label,
                                              "experiment_id": exp_id, "idea_id": _idea_id,
                                              "status": "failed", "status_msg": _msg,
                                              "metrics": None})
        elif process.returncode == 0:
            result, result_idx = self._extract_json(output)
            if result is not None:
                metrics, result_meta = _metrics_from_result(result)
                meta = {**exp.get("meta", {}), **result_meta}
                if not _cancelled_now():
                    (await run_blocking(self._store.transition_experiment,
                        exp_id, {"running"}, status="completed", metrics=metrics, meta=meta,
                        pid=None, status_msg=None, finished_at=now,  # normal success
                    ))
                    if result_idx is not None:
                        await asyncio.get_running_loop().run_in_executor(
                            None, self._strip_result_line, log_path, result_idx)
                    ws_mod.broadcaster.broadcast({"type": "experiment_finished", "label": _label,
                                                  "experiment_id": exp_id, "idea_id": _idea_id,
                                                  "status": "completed", "status_msg": None,
                                                  "metrics": metrics})
            else:
                if not _cancelled_now():
                    (await run_blocking(self._store.transition_experiment,
                        exp_id, {"running"}, status="completed", metrics=None,
                        pid=None, status_msg=None, finished_at=now,  # normal success
                    ))
                    ws_mod.broadcaster.broadcast({"type": "experiment_finished", "label": _label,
                                                  "experiment_id": exp_id, "idea_id": _idea_id,
                                                  "status": "completed", "status_msg": None,
                                                  "metrics": None})
        else:
            error = f"exit code {process.returncode}"
            # M3 feedback: best-effort reason line from the tail of the log.
            _tail = next((ln.strip() for ln in reversed(output.splitlines()) if ln.strip()), "")
            _msg = f"error: {_tail}" if _tail else f"error: {error}"
            if not _cancelled_now():
                (await run_blocking(self._store.transition_experiment,
                    exp_id, {"running"}, status="failed", error=error,
                    status_msg=_msg, pid=None, finished_at=now,
                ))
                (await run_blocking(err_path.write_text, f"{error}\n\n{output[-2000:]}"))
                ws_mod.broadcaster.broadcast({"type": "experiment_finished", "label": _label,
                                              "experiment_id": exp_id, "idea_id": _idea_id,
                                              "status": "failed", "status_msg": _msg,
                                              "metrics": None})

        # Clean up worktree
        wt = (exp.get("meta") or {}).get("worktree")
        if wt and (await run_blocking(Path(wt).exists)):
            try:
                (await run_blocking(remove_worktree, wt, cwd=self._store.repo_dir))
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
        from .executors.slurm import SlurmExecutor

        # Atomic claim prevents two dispatch passes (or a manual start racing
        # one) from submitting the same queued record twice.
        claimed = await run_blocking(
            self._store.transition_experiment, label, {"queued", "pending"},
            status="starting",
        )
        if not claimed:
            return {"status": "error", "reason": "experiment state changed before Slurm submit"}
        exp = claimed

        ssh_host = resource.executor_config.get("ssh_host", "slurm")
        executor = SlurmExecutor(ssh_host, resource.executor_config, instance_id=self._store.instance_id,
                                 repo_dir=self._store.repo_dir)

        # Build env dict that the wrapper script will expose to the experiment
        # The admin Basic credential is deliberately NOT computed or embedded
        # here. The generated wrapper runs on a remote compute node and curls
        # back to callback_url — which comes from caller-supplied
        # executor_config — so anything the wrapper carries can be aimed at an
        # attacker's listener. Review demonstrated exactly that: the real
        # admin credential arriving at a controlled endpoint. Every callback in
        # the wrapper now authenticates with the per-run bearer token instead,
        # which is scoped, unregistered when the run ends, and useless for the
        # admin-only routes (see app._ADMIN_ONLY_ROUTES).

        # The callback URL must be reachable from the *compute node*, not just
        # localhost.  Priority order:
        #   1. THE_LAB_API_URL env var (explicit override)
        #   2. callback_url in the resource executor_config (per-resource config)
        #   3. Auto-detect: server's primary network IP + THE_LAB_PORT
        port = os.environ.get("THE_LAB_PORT", "8000")
        callback_scheme = os.environ.get("THE_LAB_API_SCHEME", "http")
        _callback_default = resource.executor_config.get("callback_url")
        if not _callback_default:
            try:
                import socket as _socket
                _s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
                _s.connect(("8.8.8.8", 80))
                _ip = _s.getsockname()[0]
                _s.close()
                _callback_default = f"{callback_scheme}://{_ip}:{port}/api/v1"
            except Exception:
                _callback_default = f"{callback_scheme}://localhost:{port}/api/v1"
        lab_api_url = os.environ.get("THE_LAB_API_URL", _callback_default)

        script_path = self._store.repo_dir / exp["script"]
        local_exp_dir = script_path.parent
        if not (await run_blocking(script_path.exists)):
            msg = (
                f"script file missing: {exp['script']!r}. "
                f"The experiment was created without script_content — "
                f"provide script_content when creating, or use "
                f"POST /experiments/<source>/rerun on an experiment that has a script."
            )
            (await run_blocking(self._store.transition_experiment,
                label, {"starting"}, status="failed", error=msg,
                # M3 feedback: terminal-state reason line.
                status_msg="error: script file missing",
                finished_at=datetime.now(timezone.utc).isoformat(),
            ))
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
                git_branch = idea["branch"] if idea else (await run_blocking(get_current_branch, cwd=self._store.repo_dir))
                git_commit = (await run_blocking(resolve_branch_commit, git_branch, cwd=self._store.repo_dir))
                # Commit to auto-save any local changes on the idea branch.
                try:
                    (await run_blocking(auto_commit, cwd=self._store.repo_dir,
                                message=f"exp {label}: pre-slurm submit"))
                    git_commit = (await run_blocking(resolve_branch_commit, git_branch, cwd=self._store.repo_dir))
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
            token_registry.unregister(run_token)
            await run_blocking(
                self._store.transition_experiment, exp["id"], {"starting"},
                status="failed", error=f"Slurm submit failed: {e}",
                status_msg="error: Slurm submit failed", pid=None,
                finished_at=datetime.now(timezone.utc).isoformat(),
            )
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
        published = (await run_blocking(self._store.transition_experiment,
            exp_id,
            {"starting"},
            status="running",
            started_at=now,
            meta=meta,
            pid=None,
            error=None,
            finished_at=None,
        ))
        if published is None:
            # Cancellation happened during submit.  Best-effort scancel and
            # token cleanup; never turn a cancelled record back to running.
            try:
                await run_blocking(executor.cancel, job_id)
            finally:
                token_registry.unregister(run_token)
            return {"status": "error", "reason": "experiment cancelled during Slurm submit"}

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
        # ``squeue`` is deliberately not completion authority: jobs vanish
        # between squeue and sacct updates, and ssh outages used to turn into
        # empty strings and false successes.  A successful script needs a
        # matching durable exit-status record (or accounting exit code).
        poll_interval = SLURM_LIVE_SYNC_SECONDS
        terminal_states = {"COMPLETED", "FAILED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL", "CANCELLED"}
        while True:
            current = self._store.get_experiment(exp_id)
            if not current or (current.get("meta") or {}).get("slurm_job_id") != job_id:
                # A requeue/new attempt owns this label now.  In particular do
                # not pull its remote directory or release its allocation.
                return
            if current.get("status") == "cancelled":
                self._finish_own_slurm_monitor(exp_id)
                return
            await asyncio.sleep(poll_interval)
            try:
                state = str(await run_blocking(executor.poll_status, job_id) or "").strip().upper()
            except Exception as exc:
                logger.warning("poll_status for job %s failed; retaining monitor: %s", job_id, exc)
                await self._slurm_sync_note(exp_id, job_id, error=f"status poll failed: {exc}")
                poll_interval = min(60, max(SLURM_LIVE_SYNC_SECONDS, poll_interval * 1.5))
                continue

            current = self._store.get_experiment(exp_id)
            if not current or (current.get("meta") or {}).get("slurm_job_id") != job_id:
                return
            if current.get("status") == "cancelled":
                self._finish_own_slurm_monitor(exp_id)
                return

            if state in {"RUNNING", "PENDING", "CONFIGURING"}:
                poll_interval = 30 if state == "PENDING" else SLURM_LIVE_SYNC_SECONDS
                if await self._pull_slurm_results(exp_id, executor, job_id, label, local_exp_dir):
                    await self._publish_live_slurm_update(exp_id, label, local_exp_dir, ws_mod)
                continue
            if state == "COMPLETING":
                poll_interval = 5
                continue
            if state == "PREEMPTED":
                await self._requeue_preempted_slurm(exp_id, job_id, label)
                return

            evidence = await self._slurm_exit_evidence(executor, label, job_id)
            current = self._store.get_experiment(exp_id)
            if not current or (current.get("meta") or {}).get("slurm_job_id") != job_id:
                return
            if current.get("status") == "cancelled":
                self._finish_own_slurm_monitor(exp_id)
                return
            accounting_state = str(evidence.get("state") or "").upper()
            exit_code = evidence.get("exit_code")
            effective_state = accounting_state or state
            if effective_state == "PREEMPTED":
                await self._requeue_preempted_slurm(exp_id, job_id, label)
                return
            if effective_state not in terminal_states:
                await self._slurm_sync_note(exp_id, job_id, error="waiting for Slurm accounting/exit status")
                poll_interval = 15
                continue
            # A failed scheduler state is enough to report failure.  Completion
            # (including a disappeared squeue row) additionally requires an
            # explicit zero exit code tied to this attempt.
            if effective_state == "COMPLETED" and exit_code is None:
                await self._slurm_sync_note(exp_id, job_id, error="waiting for durable exit status")
                poll_interval = 15
                continue
            if not await self._pull_slurm_results(exp_id, executor, job_id, label, local_exp_dir):
                # Keep both monitor and bearer token alive so the pull is
                # retryable; remote artifacts are intentionally untouched.
                poll_interval = min(60, max(10, poll_interval))
                continue
            await self._finalize_slurm_terminal(
                exp_id, executor, job_id, label, local_exp_dir, ws_mod,
                effective_state, exit_code,
            )
            return

    async def _slurm_sync_note(self, exp_id: int, job_id: str, *, error: str | None = None) -> None:
        """Persist dashboard-visible sync state without touching terminal state."""
        exp = self._store.get_experiment(exp_id)
        if not exp or (exp.get("meta") or {}).get("slurm_job_id") != job_id:
            return
        at = datetime.now(timezone.utc).isoformat()
        meta_updates = {"slurm_last_sync_at": at}
        meta_remove: tuple[str, ...] = ()
        if error:
            meta_updates["slurm_sync_error"] = error
        else:
            meta_remove = ("slurm_sync_error",)
            meta_updates["slurm_last_successful_sync_at"] = at
        fields = {}
        if exp.get("status") == "running":
            if error:
                fields["status_msg"] = f"sync: {error}"
            elif str(exp.get("status_msg") or "").startswith("sync:"):
                fields["status_msg"] = None
        await run_blocking(self._store.transition_experiment, exp_id, {"running"},
                           expected_meta={"slurm_job_id": job_id},
                           meta_updates=meta_updates, meta_remove=meta_remove,
                           **fields)

    async def _pull_slurm_results(self, exp_id: int, executor, job_id: str,
                                  label: str, local_exp_dir: Path) -> bool:
        lock = self._slurm_pull_locks.setdefault(f"{label}:{job_id}", asyncio.Lock())
        async with lock:
            current = self._store.get_experiment(exp_id)
            if not current or (current.get("meta") or {}).get("slurm_job_id") != job_id:
                return False
            try:
                await run_blocking(executor.pull_results, label, local_exp_dir)
            except Exception as exc:
                logger.warning("pull_results for Slurm job %s failed: %s", job_id, exc)
                await self._slurm_sync_note(exp_id, job_id, error=f"artifact sync failed (retrying): {exc}")
                return False
        await self._slurm_sync_note(exp_id, job_id)
        return True

    async def _publish_live_slurm_update(self, exp_id: int, label: str,
                                         local_exp_dir: Path, ws_mod) -> None:
        """Publish only after a successful pull; preserve script-owned progress."""
        exp = self._store.get_experiment(exp_id)
        if not exp:
            return
        log_path, progress_path = local_exp_dir / "script.log", local_exp_dir / "script.progress"
        # A structured progress file sent by the script is more authoritative
        # than a best-effort log parser.  Only synthesize when it is absent.
        if not await run_blocking(progress_path.exists) and await run_blocking(log_path.exists):
            try:
                snapshot = _parse_log_progress((await self._read_text_off_loop(log_path)).splitlines())
                if snapshot:
                    await run_blocking(jsonio.write_json, progress_path, snapshot, indent=0)
            except Exception:
                pass
        ws_mod.broadcaster.broadcast_soon({
            "type": "experiment_log_updated", "label": label,
            "idea_id": exp.get("idea_id"), "experiment_id": exp_id,
        })
        ws_mod.broadcaster.broadcast_soon({
            "type": "experiment_progress_updated", "label": label,
            "idea_id": exp.get("idea_id"), "experiment_id": exp_id,
        })

    async def _slurm_exit_evidence(self, executor, label: str, job_id: str) -> dict:
        """Read only attempt-matching durable exit/accounting information.

        New executors provide ``get_exit_status`` and ``accounting_status``;
        tolerate older executors during upgrade, but never treat their absence
        as a zero exit code.
        """
        evidence: dict = {}
        reader = getattr(executor, "get_exit_status", None)
        if callable(reader):
            try:
                item = await run_blocking(reader, label, job_id)
                if isinstance(item, dict) and str(item.get("job_id", "")) == str(job_id):
                    if isinstance(item.get("exit_code"), int):
                        evidence["exit_code"] = item["exit_code"]
                    if item.get("preempted"):
                        evidence["state"] = "PREEMPTED"
            except Exception as exc:
                logger.info("exit status for Slurm job %s not ready: %s", job_id, exc)
        accounting = getattr(executor, "accounting_status", None)
        if callable(accounting):
            try:
                item = await run_blocking(accounting, job_id)
                if isinstance(item, dict):
                    # The executor deliberately selects the root/batch row,
                    # never a successful extern/step row.
                    raw_state = str(item.get("state") or "").strip().upper()
                    # sacct emits e.g. "CANCELLED by 1008" and may append
                    # modifiers; preserve only the scheduler state token.
                    if raw_state:
                        evidence["state"] = raw_state.split()[0].split("+")[0]
                    # The wrapper's atomically-written attempt status wins
                    # over a transient/incorrect accounting row (notably an
                    # extern step reporting 0 after a root exit 127).
                    if "exit_code" not in evidence and isinstance(item.get("exit_code"), int):
                        evidence["exit_code"] = item["exit_code"]
                    if item.get("signal"):
                        evidence["signal"] = item["signal"]
            except Exception as exc:
                logger.info("accounting for Slurm job %s not ready: %s", job_id, exc)
        # Accounting can lag or be unavailable.  A matching durable wrapper
        # status is still authoritative and is never interpreted as implicit 0.
        if evidence.get("state") != "PREEMPTED" and isinstance(evidence.get("exit_code"), int):
            if evidence["exit_code"] != 0 or evidence.get("signal"):
                evidence["state"] = "FAILED"
            elif not evidence.get("state"):
                evidence["state"] = "COMPLETED"
        return evidence

    async def _requeue_preempted_slurm(self, exp_id: int, job_id: str, label: str) -> None:
        exp = self._store.get_experiment(exp_id)
        if exp and exp.get("status") == "running" and (exp.get("meta") or {}).get("slurm_job_id") == job_id:
            now = datetime.now(timezone.utc).isoformat()
            meta = dict(exp.get("meta") or {})
            meta["slurm_attempts"] = [*(meta.get("slurm_attempts") or []),
                                      {"job_id": job_id, "reason": "preempted", "at": now}]
            meta.pop("slurm_job_id", None)
            requeued = await run_blocking(self._store.transition_experiment, exp_id, {"running"},
                                          expected_meta={"slurm_job_id": job_id},
                                          status="queued", queued_at=now, meta=meta, error=None)
            if requeued:
                self._allocator.release(label)
                self.wake_scheduler()
        self._finish_own_slurm_monitor(exp_id)

    def _finish_own_slurm_monitor(self, exp_id: int) -> None:
        """Do not let an old attempt remove a newly scheduled monitor."""
        current = asyncio.current_task()
        owner = self._tasks.get(exp_id)
        if owner is not None and owner is not current:
            return
        self._tasks.pop(exp_id, None)
        self._finished_queue.put_nowait(exp_id)

    async def _finalize_slurm_terminal(self, exp_id: int, executor, job_id: str,
                                       label: str, local_exp_dir: Path, ws_mod,
                                       state: str, exit_code: int | None) -> None:
        exp = self._store.get_experiment(exp_id)
        # Late results must not resurrect cancellation/requeue/a newer attempt.
        if not exp or (exp.get("meta") or {}).get("slurm_job_id") != job_id:
            # A requeue already owns this record/allocator; an old monitor is
            # not allowed to release its resources or remove its new task.
            return
        if exp.get("status") != "running":
            # A validated /slurm_done may have already published a non-zero
            # failure.  This monitor just completed a successful artifact pull,
            # so remote cleanup is now safe only after a fresh fenced read.
            if exp.get("status") == "failed":
                confirmed = await run_blocking(self._store.transition_experiment,
                                               exp_id, {"failed"},
                                               expected_meta={"slurm_job_id": job_id}, pid=None)
                if confirmed:
                    await run_blocking(executor.cleanup_remote, label,
                                       abs_bare_path=getattr(executor, "_resolved_bare_path", None))
                    self._allocator.release(label)
                    self.wake_scheduler()
                    self._finish_own_slurm_monitor(exp_id)
            return
        now = datetime.now(timezone.utc).isoformat()
        output = await self._read_text_off_loop(local_exp_dir / "script.log")
        result, result_idx = self._extract_json(output)
        success = state == "COMPLETED" and exit_code == 0
        fatal_patterns = ("FATAL", "\nError:", "Traceback (most recent", "error: command",
                          "command not found", "No such file or directory")
        log_fatal = any(pattern in output for pattern in fatal_patterns)
        # A verified successful process with structured metrics wins over a
        # warning-looking log line.  The heuristic is only a last resort for
        # legacy/no-result scripts.
        if success and result is None and log_fatal:
            success = False
        if success:
            metrics, result_meta = _metrics_from_result(result) if result is not None else (None, {})
            updated = await run_blocking(self._store.transition_experiment, exp_id, {"running"},
                                         expected_meta={"slurm_job_id": job_id},
                                         status="completed", metrics=metrics, meta_updates=result_meta,
                                         pid=None, status_msg=None, finished_at=now)
            if updated:
                if result_idx is not None:
                    await run_blocking(self._strip_result_line, local_exp_dir / "script.log", result_idx)
                try:
                    await run_blocking(jsonio.write_json, local_exp_dir / "script.progress",
                                       {"_final": True, "pct_complete": 100, **(metrics or {})}, indent=0)
                except OSError:
                    pass
                ws_mod.broadcaster.broadcast_soon({"type": "experiment_finished", "label": label,
                    "experiment_id": exp_id, "idea_id": exp.get("idea_id"), "status": "completed",
                    "status_msg": None, "metrics": metrics})
        else:
            detail = f"slurm job {job_id} ended with state {state}"
            if exit_code is not None:
                detail += f" (exit code {exit_code})"
            if log_fatal:
                detail += "; fatal error detected in log"
            tail = "\n".join(output.splitlines()[-30:])
            error = f"{detail}\n\n--- last log lines ---\n{tail}" if tail else detail
            updated = await run_blocking(self._store.transition_experiment, exp_id, {"running"},
                                         expected_meta={"slurm_job_id": job_id},
                                         status="failed", error=error, pid=None, finished_at=now,
                                         status_msg=f"error: {detail}")
            if updated:
                ws_mod.broadcaster.broadcast_soon({"type": "experiment_finished", "label": label,
                    "experiment_id": exp_id, "idea_id": exp.get("idea_id"), "status": "failed",
                    "status_msg": f"error: {detail}", "metrics": None})
        if not updated:
            # A cancel/requeue/new attempt won after the read above.  This
            # old attempt must not clean up or release the label it no longer
            # owns.
            return
        # Pull succeeded above and this exact attempt terminalized, so remote
        # cleanup is now safe.  Await it so no background cleanup survives a
        # shutdown and races a retry.
        await run_blocking(executor.cleanup_remote, label,
                           abs_bare_path=getattr(executor, "_resolved_bare_path", None))
        self._allocator.release(label)
        self.wake_scheduler()
        self._finish_own_slurm_monitor(exp_id)

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
        if exp["status"] in ("queued", "pending", "starting"):
            self.wake_scheduler()
            result = (await run_blocking(self._store.transition_experiment,
                exp_id,
                {"queued", "pending", "starting"},
                status="cancelled",
                status_msg="cancelled",  # M3: user cancelled before it ran
                finished_at=datetime.now(timezone.utc).isoformat(),
            ))
            ws_mod.broadcaster.broadcast({"type": "experiment_cancelled",
                                          "label": label, "idea_id": idea_id})
            return result
        if exp["status"] != "running":
            return exp

        # Win the terminal state before awaiting an external cancel/signal.
        # The monitor's running-only CAS can no longer publish completion.
        pid_to_signal = exp.get("pid")
        result = (await run_blocking(self._store.transition_experiment,
            exp_id, {"running"}, status="cancelled", status_msg="sigterm",
            pid=None, finished_at=datetime.now(timezone.utc).isoformat(),
        ))
        if result is None:
            return self._store.get_experiment(exp_id)

        # Handle Slurm jobs
        slurm_job_id = (exp.get("meta") or {}).get("slurm_job_id")
        if slurm_job_id:
            try:
                from .executors.slurm import SlurmExecutor
                from .queue import load_config
                resources, _ = (await run_blocking(load_config, self._store.repo_dir))
                resource_name = (exp.get("meta") or {}).get("assigned_resource")
                resource = next((r for r in resources if r.name == resource_name), None)
                if resource and resource.kind == "slurm":
                    ssh_host = resource.executor_config.get("ssh_host", "slurm")
                    executor = SlurmExecutor(ssh_host, resource.executor_config, instance_id=self._store.instance_id,
                                             repo_dir=self._store.repo_dir)
                    (await run_blocking(executor.cancel, slurm_job_id))
            except Exception as e:
                print(f"[the-lab] scancel for {label} failed: {e}")
        elif pid_to_signal and self._pid_matches_record(pid_to_signal, exp):
            self._signal_experiment(pid_to_signal, signal.SIGTERM)
            await asyncio.sleep(10)
            # A PID can be recycled while waiting; verify the durable launch
            # identity again before escalating to SIGKILL.
            if self._pid_matches_record(pid_to_signal, exp):
                self._signal_experiment(pid_to_signal, signal.SIGKILL)

        # Clean up worktree
        wt = (exp.get("meta") or {}).get("worktree")
        if wt and (await run_blocking(Path(wt).exists)):
            try:
                (await run_blocking(remove_worktree, wt, cwd=self._store.repo_dir))
            except Exception:
                pass

        ws_mod.broadcaster.broadcast({"type": "experiment_cancelled",
                                      "label": label, "idea_id": idea_id})
        return result

    @staticmethod
    def _read_tail(path: Path, n_lines: int, block: int = 65536) -> str:
        """Read only the last *n_lines* lines of *path* by seeking backwards
        in blocks — a ?tail=25 poll on a multi-MB NFS log costs one or two
        64kB reads instead of the whole file. Capped at 8MB as a guard
        against pathological single-line files."""
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            pos = fh.tell()
            data = b""
            while pos > 0 and data.count(b"\n") <= n_lines and len(data) < 8 * 1024 * 1024:
                step = min(block, pos)
                pos -= step
                fh.seek(pos)
                data = fh.read(step) + data
        text = data.decode("utf-8", errors="replace")
        lines = text.split("\n")
        return "\n".join(lines[-n_lines:]) if len(lines) > n_lines else text

    def get_log(self, exp_id: int, tail: int | None = None) -> str | None:
        """Read the .log file for an experiment, optionally tail N lines.

        Tail reads use a backwards seek (see _read_tail) and never load the
        whole file. Trailing JSON-object lines (the script's final result
        dump) are filtered out — the parsed metrics already live on the
        experiment record and the raw blob clutters the "Show log" view.
        Newly-completed runs have the line stripped from disk; this filter
        is defense-in-depth for legacy logs.
        """
        exp = self._store.get_experiment(exp_id)
        if not exp:
            return None
        log_path = self._store.repo_dir / exp["script"].replace(".sh", ".log")
        if not log_path.exists():
            return ""
        if tail is not None:
            # Over-read a few lines so stripping the JSON result line still
            # leaves a full *tail* lines (parity with the full-read path).
            raw = self._read_tail(log_path, tail + 3)
            content = self._strip_trailing_json_line(raw)
            lines = content.split("\n")
            return "\n".join(lines[-tail:])
        return self._strip_trailing_json_line(log_path.read_text())

    async def pull_results_for_label(self, label: str) -> bool:
        """Trigger an immediate rsync pull for a slurm experiment. Used by
        the compute-node wrapper to confirm data is safe before cleanup."""
        from .executors.slurm import SlurmExecutor
        exp = self._store.get_experiment(label)
        if not exp:
            return False
        meta = exp.get("meta") or {}
        job_id = str(meta.get("slurm_job_id") or "")
        resource_name = meta.get("assigned_resource")
        if not resource_name:
            return False
        from .queue import load_config as _load_config
        _resources, _ = (await run_blocking(_load_config, self._store.repo_dir))
        resource = next(
            (r for r in _resources if r.name == resource_name),
            None,
        )
        if not resource or resource.kind != "slurm":
            return False
        ssh_host = resource.executor_config.get("ssh_host", "slurm")
        executor = SlurmExecutor(
            ssh_host, resource.executor_config,
            instance_id=self._store.instance_id, repo_dir=self._store.repo_dir,
        )
        script_path = self._store.repo_dir / exp["script"]
        local_exp_dir = script_path.parent
        # Reuse the monitor's per-attempt lock so a wrapper-triggered pull
        # cannot race the periodic live sync into the same local directory.
        if job_id:
            return await self._pull_slurm_results(exp["id"], executor, job_id, label, local_exp_dir)
        try:
            await run_blocking(executor.pull_results, label, local_exp_dir)
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
                    # Sync helper (also used at startup); its log read can be
                    # multi-MB on NFS — run it off-loop.
                    await asyncio.get_event_loop().run_in_executor(
                        None, self._reconcile_stale_running, exp)

            # Waiting on ONE named experiment asks "what happened to this run?",
            # so answer from its current state and bypass _seen entirely.
            #
            # _seen is process-global and pre-populated at startup with every
            # already-finished experiment, and it is consumed destructively (the
            # first waiter to observe a completion removes it from every other
            # waiter's view). Consulting it here meant `the-lab wait <label>`
            # blocked until timeout — with no output — whenever the experiment
            # was already terminal: finished before this server started, or
            # finished while the caller was away and claimed by another waiter.
            # It also silently never reported "cancelled", which the scan below
            # omits while startup marks it seen.
            if experiment_id is not None:
                exp = self._store.get_experiment(experiment_id)
                if exp and exp.get("status") in ("completed", "failed", "cancelled"):
                    # Deliberately not added to _seen: this response is scoped to
                    # this caller, and claiming it would starve a global waiter.
                    return {
                        "event": exp["status"],
                        "experiment": exp,
                    }
            else:
                # Unfiltered / idea-scoped wait keeps "wake me on the next
                # completion" semantics, where _seen is what stops the same
                # result being returned over and over.
                for status in ("completed", "failed", "cancelled"):
                    for exp in self._store.list_experiments_by_status(status):
                        if exp["id"] in self._seen:
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
                # CLAIM on delivery rather than peek. "Unread" is a latch, not
                # an edge: unread_for() is a pure read, so it stayed true until
                # the agent explicitly read elsewhere and this wake re-fired on
                # every call. A client that re-waits the moment /wait returns
                # then spins as fast as the network allows — measured ~36 req/s
                # from three pollers against one agent with 10 stale unread
                # messages, which also floods the dashboard's activity feed.
                #
                # claim_unread_for() selects and marks read under one lock, the
                # same contract the notifications middleware and the WS listener
                # already use (N1 cross-channel dedup) — so a message delivered
                # here is not re-delivered there, and vice versa.
                # Role-only callers can't claim (no id for read_by); the /wait
                # route only sets agent_role when agent_id resolved, so in
                # practice this is always the claiming path.
                unread = (
                    (await run_blocking(messages_mod.claim_unread_for,
                        repo_dir, agent_id=agent_id, role=agent_role, limit=20,
                    ))
                    if agent_id
                    else (await run_blocking(messages_mod.unread_for,
                        repo_dir, agent_id=agent_id, role=agent_role, limit=20,
                    ))
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
