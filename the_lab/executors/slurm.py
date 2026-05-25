"""Slurm executor: SSH-submit experiments to a Slurm cluster.

Uses only stdlib (subprocess, tempfile) — no paramiko or other extras.

Design
------
Each experiment gets an isolated worktree on the remote machine, just like
the local executor creates one under .the_lab/worktrees/.  The full flow:

  1. Ensure a bare git repo exists on the slurm machine
     (~/.thelab/repo.git by default).
  2. Push the idea branch from the local repo to that bare remote via SSH.
  3. SSH: git worktree add --detach <job_dir>/worktree <commit_sha>
     so the job runs against the exact commit the scheduler resolved.
  4. Copy the experiment script into the worktree's .the_lab/experiments/
     path so relative paths (e.g. `python gemma_loop.py`) just work.
  5. sbatch the wrapper; job cd's into the worktree.
  6. On completion: rsync the whole job dir back to the lab server.
  7. Cleanup: git worktree remove + rm -rf the job dir.

executor_config keys
--------------------
  ssh_host        : SSH host alias or user@host  (default: "slurm")
  partition       : Slurm partition               (default: "lowprio")
  qos             : QOS name                      (default: partition)
  account         : Slurm account                 (optional)
  ntasks          : --ntasks                      (default: 1)
  gpus            : --gres=gpu:<n>                (default: 1)
  mem             : --mem=<value>                 (optional)
  time            : --time=<value>                (optional)
  git_repo_path   : path of bare repo on remote  (default: "~/.thelab/repo.git")
  remote_base     : parent dir for job dirs       (default: "$HOME/.thelab/jobs")
  slurm_conf      : path to slurm.conf on remote  (default: /data/slurm/etc/slurm.conf)
  base_venv_path  : shared venv with heavy packages (vllm, torch, triton) that
                    should NOT change between experiments.  When set, the wrapper
                    creates a lightweight per-job venv with --system-site-packages
                    so each experiment can install its own version of packages/
                    without racing against parallel jobs.  The per-job venv lives
                    inside the job dir and is removed by cleanup_remote().
                    Leave unset to skip venv management entirely (default: None)
"""
from __future__ import annotations

import logging
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger("the-lab.slurm")


class SlurmExecutor:
    DEFAULT_SLURM_CONF = "/data/slurm/etc/slurm.conf"

    def __init__(self, ssh_host: str, config: dict, instance_id: str | None = None):
        self.ssh_host    = ssh_host
        self.instance_id = instance_id or ""
        self.partition   = config.get("partition",  "lowprio")
        self.account     = config.get("account")
        self.ntasks      = int(config.get("ntasks",  1))
        self.gpus        = int(config.get("gpus",    1))
        self.mem         = config.get("mem")
        self.time_limit  = config.get("time")
        self.slurm_conf  = config.get("slurm_conf", self.DEFAULT_SLURM_CONF)
        self.qos         = config.get("qos",        self.partition)
        # Two-tier venv: base holds heavy read-only packages (vllm/torch/triton);
        # each job gets a lightweight per-job venv that inherits from the base
        # via --system-site-packages and installs experiment-specific packages.
        self.base_venv_path = config.get("base_venv_path")  # None → skip
        # Static env vars injected into every job wrapper from this resource.
        # Useful for cluster-wide settings like HF_HOME, CUDA_VISIBLE_DEVICES
        # overrides, proxy URLs, etc.  Merged before per-experiment env_extra
        # so experiment-level values always win.
        self.env_vars: dict = config.get("env_vars") or {}
        # $HOME in remote_base is embedded into sbatch directives; Slurm's
        # sbatch parser doesn't expand shell variables so we resolve it to an
        # absolute path at submit time via _resolve_remote_base().
        self.remote_base  = config.get("remote_base",  "$HOME/.thelab/jobs")
        # Path to the bare git repo on the slurm machine.
        self.git_repo_path = config.get("git_repo_path", "~/.thelab/repo.git")

    # ------------------------------------------------------------------
    # SSH / SCP helpers
    # ------------------------------------------------------------------

    def _ssh(self, cmd: str, check: bool = True) -> subprocess.CompletedProcess:
        """Run cmd on the remote host via SSH, with SLURM_CONF prefixed."""
        full_cmd = f"SLURM_CONF={self.slurm_conf} {cmd}"
        result = subprocess.run(
            ["ssh", self.ssh_host, full_cmd],
            capture_output=True, text=True,
        )
        if check and result.returncode != 0:
            raise RuntimeError(
                f"SSH command failed (rc={result.returncode}): {cmd!r}\n"
                f"stderr: {result.stderr.strip()}"
            )
        return result

    def _ssh_plain(self, cmd: str, check: bool = True,
                   timeout: int = 120) -> subprocess.CompletedProcess:
        """Run cmd via SSH without the SLURM_CONF prefix (for git commands).

        Uses a timeout to prevent hanging indefinitely on git lock contention.
        The remote process is killed if the local timeout fires.
        """
        try:
            result = subprocess.run(
                ["ssh",
                 # Kill remote process when the connection dies (prevents orphaned
                 # git processes that leave index.lock files on the remote).
                 "-o", "ServerAliveInterval=10",
                 "-o", "ServerAliveCountMax=3",
                 self.ssh_host, cmd],
                capture_output=True, text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as e:
            if check:
                raise RuntimeError(
                    f"SSH command timed out after {timeout}s: {cmd!r}"
                ) from e
            # Return a fake failure result on timeout
            return subprocess.CompletedProcess(
                args=[], returncode=1,
                stdout="", stderr=f"timeout after {timeout}s",
            )
        if check and result.returncode != 0:
            raise RuntimeError(
                f"SSH command failed (rc={result.returncode}): {cmd!r}\n"
                f"stderr: {result.stderr.strip()}"
            )
        return result

    def _scp_to_remote(self, local_path: str | Path, remote_path: str) -> None:
        result = subprocess.run(
            ["scp", str(local_path), f"{self.ssh_host}:{remote_path}"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"SCP failed: {local_path!r} -> {remote_path!r}\n"
                f"stderr: {result.stderr.strip()}"
            )

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    def _resolve_remote_base(self) -> str:
        """Expand $HOME in remote_base and append instance_id subdirectory."""
        base = self.remote_base
        if "$HOME" in base:
            home = self._ssh_plain("echo $HOME").stdout.strip()
            base = base.replace("$HOME", home)
        return f"{base}/{self.instance_id}" if self.instance_id else base

    def _resolve_git_repo_path(self) -> str:
        """Expand ~ in git_repo_path and namespace by instance_id."""
        path = self.git_repo_path
        if path.startswith("~"):
            home = self._ssh_plain("echo $HOME").stdout.strip()
            path = path.replace("~", home, 1)
        # Keep bare repo path as-is when no instance_id (backwards compat)
        return path

    @property
    def _rsync_base(self) -> str:
        """remote_base (with $HOME→~) plus instance_id for rsync."""
        base = self.remote_base.replace("$HOME", "~")
        return f"{base}/{self.instance_id}" if self.instance_id else base

    # ------------------------------------------------------------------
    # Git helpers
    # ------------------------------------------------------------------

    def ensure_bare_repo(self) -> str:
        """Ensure a bare git repo exists on the remote. Returns absolute path.

        Also caches the result as ``_resolved_bare_path`` so cleanup_remote
        can pass it without needing to SSH again.
        """
        abs_path = self._resolve_git_repo_path()
        self._ssh_plain(
            f"git init --bare {abs_path} -q 2>/dev/null || true"
        )
        self._resolved_bare_path: str = abs_path  # type: ignore[attr-defined]
        return abs_path

    def push_branch(self, local_repo_dir: Path, branch: str) -> None:
        """Push *branch* from the local repo to the remote bare repo via SSH.

        Uses an SSH URL so no extra git remote needs to be configured.
        The push is force-pushed because ideas are re-committed frequently.
        """
        abs_bare = self._resolve_git_repo_path()
        # SSH URL: ssh://alias/absolute/path  (no user@ needed if alias handles it)
        remote_url = f"ssh://{self.ssh_host}{abs_bare}"
        result = subprocess.run(
            ["git", "-C", str(local_repo_dir),
             "push", "--force", "--quiet", remote_url,
             f"{branch}:{branch}"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"git push to slurm failed for branch {branch!r}:\n"
                f"{result.stderr.strip()}"
            )

    def create_worktree(self, abs_bare_path: str, worktree_path: str,
                        commit_sha: str) -> None:
        """Create an isolated worktree at *worktree_path* for *commit_sha*.

        Prunes stale worktree registrations first so retries after a failed
        job don't hit 'already exists' errors.
        """
        # Clear any stale index.lock files left by interrupted git commands
        # before running any git worktree operations (locks cause hangs).
        try:
            self._ssh_plain(
                f"find {abs_bare_path}/worktrees -name 'index.lock' "
                f"-delete 2>/dev/null || true"
            )
        except Exception:
            pass
        # Unlock the worktree if it was locked by a previous interrupted add.
        try:
            self._ssh_plain(
                f"git -C {abs_bare_path} worktree unlock {worktree_path} "
                f"2>/dev/null || true"
            )
        except Exception:
            pass
        # Prune stale worktree registrations, then force-remove any live
        # worktree at the same path (happens when a rerun uses the same label).
        try:
            self._ssh_plain(
                f"git -C {abs_bare_path} worktree prune 2>/dev/null || true"
            )
        except Exception:
            pass
        try:
            self._ssh_plain(
                f"git -C {abs_bare_path} worktree remove --force {worktree_path} "
                f"2>/dev/null || true"
            )
        except Exception:
            pass
        # Also remove the directory if it still exists (defensive)
        try:
            self._ssh_plain(f"rm -rf {worktree_path} 2>/dev/null || true")
        except Exception:
            pass
        result = self._ssh_plain(
            f"git -C {abs_bare_path} worktree add --detach -q "
            f"{worktree_path} {commit_sha}",
            check=False,
        )
        if result.returncode != 0:
            # Worktree may still exist at the right commit (partial retry).
            # Check with rev-parse HEAD inside the worktree.
            check = self._ssh_plain(
                f"git -C {worktree_path} rev-parse HEAD 2>/dev/null "
                f"| grep -q {commit_sha}",
                check=False,
            )
            if check.returncode != 0:
                raise RuntimeError(
                    f"git worktree add failed (rc={result.returncode}): "
                    f"{result.stderr.strip()}"
                )
            # Worktree exists and is at the correct commit — reuse it.

    def remove_worktree(self, abs_bare_path: str, worktree_path: str) -> None:
        """Remove the worktree from git's tracking. Best-effort."""
        try:
            self._ssh_plain(
                f"git -C {abs_bare_path} worktree remove --force {worktree_path} 2>/dev/null || true"
            )
        except Exception as e:
            logger.warning("worktree remove failed: %s", e)

    # ------------------------------------------------------------------
    # Wrapper script builder
    # ------------------------------------------------------------------

    def _build_wrapper(self, label: str, remote_job_dir: str,
                       worktree_dir: str, unit_kind: str = "gpu",
                       env_vars: dict | None = None) -> str:
        gpu_line     = f"#SBATCH --gres=gpu:{self.gpus}" if unit_kind == "gpu" else ""
        mem_line     = f"#SBATCH --mem={self.mem}"        if self.mem        else ""
        time_line    = f"#SBATCH --time={self.time_limit}" if self.time_limit else ""
        account_line = f"#SBATCH --account={self.account}" if self.account   else ""
        qos_line     = f"#SBATCH --qos={self.qos}"         if self.qos       else ""

        # Build explicit export lines so env vars survive the sbatch→compute hop.
        # --export=ALL only carries the submitter's shell env; vars set later
        # (like THE_LAB_TOKEN) never make it to the compute node otherwise.
        env_exports = "\n".join(
            f"export {k}={v!r}" for k, v in (env_vars or {}).items()
        )

        # Per-job venv setup — only emitted when base_venv_path is configured.
        # Creates a lightweight venv that inherits heavy packages (vllm/torch)
        # from the shared base via --system-site-packages, then exports VENV_DIR
        # and UV so the experiment script installs its own packages/  into the
        # isolated per-job venv.  The per-job venv is deleted by cleanup_remote.
        if self.base_venv_path:
            venv_setup = f"""\
# ── per-job isolated venv ────────────────────────────────────────────────────
# --system-site-packages only inherits from the *system* Python, not from a
# parent venv.  We instead create a plain venv and drop a .pth file that adds
# the base venv's site-packages to sys.path — the standard "layered venvs"
# pattern.  Heavy packages (vllm/torch/triton) come from the base; the
# experiment installs its own packages/ here without touching the base.
export _PJVENV="{remote_job_dir}/venv"
python3 -m venv "$_PJVENV"
# Add base venv's site-packages to child venv via .pth so heavy packages
# (vllm, torch, triton) are importable from the per-job Python.
_BASE_SP=$("{self.base_venv_path}/bin/python" -c "import site; print(site.getsitepackages()[0])")
_PJ_SP=$("$_PJVENV/bin/python"               -c "import site; print(site.getsitepackages()[0])")
echo "$_BASE_SP" > "$_PJ_SP/base-venv.pth"
# Symlink the base venv's bin/ executables (vllm, etc.) into the per-job venv
# so scripts that call .venv/bin/vllm find it.  The per-job Python and uv are
# already present from the venv create above.
for _bin in "{self.base_venv_path}/bin/"*; do
  _name="$(basename "$_bin")"
  [ -e "$_PJVENV/bin/$_name" ] || ln -sf "$_bin" "$_PJVENV/bin/$_name"
done
export VENV_DIR="$_PJVENV"
export UV="{self.base_venv_path}/bin/uv"
# tokenizers: base venv 0.22.2 is compatible with the relaxed >=0.21 constraint
# ─────────────────────────────────────────────────────────────────────────────
"""
        else:
            venv_setup = ""

        return f"""#!/bin/bash
#SBATCH --partition={self.partition}
#SBATCH --job-name=thelab-{label}
#SBATCH --output={remote_job_dir}/script.log
#SBATCH --error={remote_job_dir}/script.log
#SBATCH --ntasks={self.ntasks}
{gpu_line}
{mem_line}
{time_line}
{account_line}
{qos_line}
#SBATCH --export=ALL
#SBATCH --requeue

# Inject env vars that must survive the sbatch → compute node hop.
{env_exports}

{venv_setup}
# Preemption handler — Slurm sends SIGTERM before killing the job
_PREEMPTED=0
_lab_requeue() {{
    _PREEMPTED=1
    curl -s -X POST "$THE_LAB_API_URL/experiments/{label}/requeue" \\
         -H "Authorization: Basic $THE_LAB_AUTH" \\
         -H "Content-Type: application/json" \\
         -d '{{"reason":"preempted"}}' || true
}}
trap _lab_requeue SIGTERM SIGUSR1

# Progress/metrics land in the job dir (not the worktree — it's read-only)
export THE_LAB_PROGRESS="{remote_job_dir}/script.progress"
export THE_LAB_METRICS="{remote_job_dir}/script.metrics.jsonl"

# Background watcher: push progress updates to the API whenever the progress
# file changes, so the dashboard gets live updates without rsync polling lag.
_push_progress() {{
    local _last_mtime="" _cur_mtime
    while true; do
        if [ -f "$THE_LAB_PROGRESS" ]; then
            _cur_mtime=$(stat -c %Y "$THE_LAB_PROGRESS" 2>/dev/null || echo "")
            if [ "$_cur_mtime" != "$_last_mtime" ] && [ -n "$_cur_mtime" ]; then
                _last_mtime="$_cur_mtime"
                curl -s -X POST "$THE_LAB_API_URL/experiments/{label}/progress" \\
                     -H "Authorization: Bearer $THE_LAB_TOKEN" \\
                     -H "Content-Type: application/json" \\
                     --data-binary "@$THE_LAB_PROGRESS" || true
            fi
        fi
        sleep 2
    done
}}
_push_progress &
_WATCHER_PID=$!

# Run the experiment script from the worktree so relative paths
# (python, hooks, prompts, ...) resolve against the committed code.
cd "{worktree_dir}"
bash "{remote_job_dir}/script.sh" &
_SCRIPT_PID=$!
wait $_SCRIPT_PID
_EXIT=$?

# Stop watcher and do one final push
kill $_WATCHER_PID 2>/dev/null || true
if [ -f "$THE_LAB_PROGRESS" ]; then
    curl -s -X POST "$THE_LAB_API_URL/experiments/{label}/progress" \\
         -H "Authorization: Bearer $THE_LAB_TOKEN" \\
         -H "Content-Type: application/json" \\
         --data-binary "@$THE_LAB_PROGRESS" || true
fi

if [ $_PREEMPTED -eq 0 ]; then
    # Wait for artifact files (e.g. GIFs) to finish writing — poll until sizes
    # are stable so we don't rsync a partially-written file.
    _wait_stable() {{
        local dir="$1" timeout="${{2:-120}}"
        local deadline=$(( $(date +%s) + timeout ))
        local prev="" cur
        while [ "$(date +%s)" -lt "$deadline" ]; do
            cur=$(find "$dir" -maxdepth 1 \( -name "*.gif" -o -name "*.json" \) \\
                  -exec stat -c '%n:%s' {{}} \; 2>/dev/null | sort | tr '\n' '|')
            if [ -n "$cur" ] && [ "$cur" = "$prev" ]; then
                break
            fi
            prev="$cur"
            sleep 3
        done
    }}
    _wait_stable "{remote_job_dir}" 120

    # Trigger rsync on the lab server and wait for it to complete.
    # The /pull endpoint blocks until rsync finishes, so when we get 200 back
    # all artifacts are safely on the lab server.
    # Only delete local files if the pull succeeded — otherwise keep them for recovery.
    if curl -sf --max-time 120 \\
         -X POST "$THE_LAB_API_URL/experiments/{label}/pull" \\
         -H "Authorization: Bearer $THE_LAB_TOKEN" \\
         -H "Content-Type: application/json"; then
        rm -rf "{remote_job_dir}/worktree" "{remote_job_dir}/venv" 2>/dev/null || true
    fi

    curl -s -X POST "$THE_LAB_API_URL/experiments/{label}/slurm_done" \\
         -H "Authorization: Basic $THE_LAB_AUTH" \\
         -H "Content-Type: application/json" \\
         -d "{{\\"exit_code\\":$_EXIT}}" || true
fi

exit $_EXIT
"""

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def submit(
        self,
        label: str,
        local_script_path: str | Path,
        env_extra: dict,
        local_exp_dir: str | Path,
        unit_kind: str = "gpu",
        # Git context — required for worktree isolation
        local_repo_dir: Path | None = None,
        git_branch: str | None = None,
        git_commit: str | None = None,
    ) -> str:
        """Submit an experiment to Slurm. Returns the job_id string.

        When *git_branch* and *git_commit* are provided the method:
          1. Ensures a bare repo exists on the remote.
          2. Pushes the branch from *local_repo_dir*.
          3. Creates a per-job worktree at <job_dir>/worktree.
        The job then runs with cwd=worktree so all repo-relative paths work.
        Without git context the job runs from the bare job dir (useful for
        simple self-contained scripts that don't need the full repo).
        """
        resolved_base  = self._resolve_remote_base()
        remote_job_dir = f"{resolved_base}/{label}"
        worktree_dir   = f"{remote_job_dir}/worktree"

        # 1. Create remote job directory
        self._ssh_plain(f"mkdir -p {remote_job_dir}")

        # 2. Git: push branch and create per-job worktree
        use_worktree = bool(local_repo_dir and git_branch and git_commit)
        abs_bare_path = None
        if use_worktree:
            abs_bare_path = self.ensure_bare_repo()
            self.push_branch(local_repo_dir, git_branch)           # type: ignore[arg-type]
            self.create_worktree(abs_bare_path, worktree_dir, git_commit) # type: ignore[arg-type]
            # Copy the script into the worktree's experiment path so it exists
            # alongside the repo code (mirroring the local runner's layout).
            local_script_path = Path(local_script_path)
            rel_script = local_script_path.relative_to(
                local_repo_dir                                       # type: ignore[arg-type]
            ) if local_script_path.is_relative_to(
                local_repo_dir                                       # type: ignore[arg-type]
            ) else local_script_path
            remote_script_in_wt = f"{worktree_dir}/{rel_script}"
            self._ssh_plain(f"mkdir -p $(dirname {remote_script_in_wt})")
            self._scp_to_remote(local_script_path, remote_script_in_wt)
            # Also copy script to job dir root for the wrapper to reference
            self._scp_to_remote(local_script_path, f"{remote_job_dir}/script.sh")
        else:
            # No git context — copy script directly (simple self-contained mode)
            self._scp_to_remote(local_script_path, f"{remote_job_dir}/script.sh")
            worktree_dir = remote_job_dir  # fall back to job dir as cwd

        # 3. Build and upload wrapper.
        # Merge resource-level env_vars (e.g. HF_HOME) with per-experiment
        # env_extra; experiment values take precedence.
        merged_env = {**self.env_vars, **(env_extra or {})}
        wrapper_content = self._build_wrapper(
            label, remote_job_dir, worktree_dir, unit_kind=unit_kind,
            env_vars=merged_env,
        )
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".sh", prefix="thelab_wrapper_", delete=False,
        ) as tmp:
            tmp.write(wrapper_content)
            tmp_path = tmp.name
        try:
            self._scp_to_remote(tmp_path, f"{remote_job_dir}/wrapper.sh")
        finally:
            Path(tmp_path).unlink(missing_ok=True)

        self._ssh_plain(
            f"chmod +x {remote_job_dir}/script.sh {remote_job_dir}/wrapper.sh"
        )

        # 4. Submit
        result = self._ssh(f"sbatch --parsable {remote_job_dir}/wrapper.sh")
        raw = result.stdout.strip().split(";")[0].strip()
        if not raw.isdigit():
            raise RuntimeError(f"sbatch returned unexpected output: {result.stdout!r}")
        return raw

    def cancel(self, job_id: str) -> None:
        try:
            # Disable requeue before cancelling so the job doesn't restart
            # (slurm's --requeue flag causes scancelled jobs to be resubmitted).
            self._ssh(f"scontrol update JobId={job_id} Requeue=0 2>/dev/null || true",
                      check=False)
            self._ssh(f"scancel {job_id}")
        except Exception as e:
            logger.warning("scancel %s failed: %s", job_id, e)

    def poll_status(self, job_id: str) -> str:
        """Poll squeue. Returns state string or '' when job is gone."""
        result = self._ssh(
            f"squeue --noheader -j {job_id} -o %T", check=False,
        )
        if result.returncode != 0:
            return ""
        return result.stdout.strip()

    def pull_results(self, label: str, local_exp_dir: str | Path) -> None:
        """rsync the job dir back to the local experiment directory."""
        local_dir = str(local_exp_dir).rstrip("/") + "/"
        result = subprocess.run(
            ["rsync", "-az", "--timeout=1200",
             # The worktree is git-tracked code the local machine already has —
             # no need to sync it back. Only pull the job-root output files:
             # script.log, script.progress, script.metrics.jsonl, *.gpu_stats,
             # and any artifacts the script copies to the job root.
             "--exclude=worktree/",
             "--exclude=venv/",
             f"{self.ssh_host}:{self._rsync_base}/{label}/",
             local_dir],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            logger.error("rsync pull for %s failed (rc=%d): %s",
                         label, result.returncode, result.stderr.strip())

    def cleanup_remote(self, label: str,
                       abs_bare_path: str | None = None) -> None:
        """Remove worktree tracking entry and job directory. Best-effort."""
        if abs_bare_path:
            self.remove_worktree(
                abs_bare_path,
                f"{self._resolve_remote_base()}/{label}/worktree",
            )
        try:
            self._ssh_plain(f"rm -rf {self._resolve_remote_base()}/{label}")
        except Exception as e:
            logger.warning("cleanup_remote %s failed: %s", label, e)
