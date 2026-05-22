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
"""
from __future__ import annotations

import logging
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger("the-lab.slurm")


class SlurmExecutor:
    DEFAULT_SLURM_CONF = "/data/slurm/etc/slurm.conf"

    def __init__(self, ssh_host: str, config: dict):
        self.ssh_host    = ssh_host
        self.partition   = config.get("partition",  "lowprio")
        self.account     = config.get("account")
        self.ntasks      = int(config.get("ntasks",  1))
        self.gpus        = int(config.get("gpus",    1))
        self.mem         = config.get("mem")
        self.time_limit  = config.get("time")
        self.slurm_conf  = config.get("slurm_conf", self.DEFAULT_SLURM_CONF)
        self.qos         = config.get("qos",        self.partition)
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

    def _ssh_plain(self, cmd: str, check: bool = True) -> subprocess.CompletedProcess:
        """Run cmd via SSH without the SLURM_CONF prefix (for git commands)."""
        result = subprocess.run(
            ["ssh", self.ssh_host, cmd],
            capture_output=True, text=True,
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
        """Expand $HOME in remote_base to the real absolute path via SSH."""
        if "$HOME" not in self.remote_base:
            return self.remote_base
        home = self._ssh_plain("echo $HOME").stdout.strip()
        return self.remote_base.replace("$HOME", home)

    def _resolve_git_repo_path(self) -> str:
        """Expand ~ in git_repo_path to the real absolute path via SSH."""
        if not self.git_repo_path.startswith("~"):
            return self.git_repo_path
        home = self._ssh_plain("echo $HOME").stdout.strip()
        return self.git_repo_path.replace("~", home, 1)

    @property
    def _rsync_base(self) -> str:
        """remote_base with $HOME → ~ for rsync (rsync expands ~, not $HOME)."""
        return self.remote_base.replace("$HOME", "~")

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
        """Create an isolated worktree at *worktree_path* for *commit_sha*."""
        self._ssh_plain(
            f"git -C {abs_bare_path} worktree add --detach -q "
            f"{worktree_path} {commit_sha}"
        )

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
                       worktree_dir: str, unit_kind: str = "gpu") -> str:
        gpu_line     = f"#SBATCH --gres=gpu:{self.gpus}" if unit_kind == "gpu" else ""
        mem_line     = f"#SBATCH --mem={self.mem}"        if self.mem        else ""
        time_line    = f"#SBATCH --time={self.time_limit}" if self.time_limit else ""
        account_line = f"#SBATCH --account={self.account}" if self.account   else ""
        qos_line     = f"#SBATCH --qos={self.qos}"         if self.qos       else ""

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

# Run the experiment script from the worktree so relative paths
# (python, hooks, prompts, ...) resolve against the committed code.
cd "{worktree_dir}"
bash "{remote_job_dir}/script.sh" &
_SCRIPT_PID=$!
wait $_SCRIPT_PID
_EXIT=$?

if [ $_PREEMPTED -eq 0 ]; then
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

        # 3. Build and upload wrapper
        wrapper_content = self._build_wrapper(
            label, remote_job_dir, worktree_dir, unit_kind=unit_kind,
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
            ["rsync", "-az", "--timeout=30",
             # Exclude the worktree — it can be hundreds of MB and is already
             # in the local repo; we only want the generated artefacts.
             "--exclude=worktree/",
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
