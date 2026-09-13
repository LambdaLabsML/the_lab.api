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
  ssh_host        : SSH host alias or user@host, or "local" when the API
                    process itself can run Slurm commands (default: "slurm")
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

import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger("the-lab.slurm")


class SlurmExecutor:
    DEFAULT_SLURM_CONF = "/data/slurm/etc/slurm.conf"

    def __init__(self, ssh_host: str, config: dict, instance_id: str | None = None,
                 repo_dir: Path | None = None):
        # Re-validate at construction, not just at the API boundary. Values that
        # reach here from a hand-edited .the_lab/queue.json, or a resource
        # written before ingress validation existed, get spliced into remote
        # command strings the same way — and ssh hands those to the remote
        # user's shell. Fail loudly at dispatch rather than executing them.
        from ..queue import _validate_executor_config
        _validate_executor_config({**(config or {}), "ssh_host": ssh_host})

        self.ssh_host    = ssh_host
        self._is_local    = ssh_host == "local"
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
        # Optional, fixed launcher for a model server which lives for exactly
        # the lifetime of this batch allocation.  It is intentionally not an
        # arbitrary command; validation only permits known launchers.
        self.model_server: dict | None = config.get("model_server") or None
        # $HOME in remote_base is embedded into sbatch directives; Slurm's
        # sbatch parser doesn't expand shell variables so we resolve it to an
        # absolute path at submit time via _resolve_remote_base().
        self.remote_base  = config.get("remote_base",  "$HOME/.thelab/jobs")
        # Path to the bare git repo on the slurm machine.
        self.git_repo_path = config.get("git_repo_path", "~/.thelab/repo.git")
        # Used solely to validate canonical dashboard-output symlink targets.
        # It is operator-owned runner context, never resource configuration.
        self.repo_dir = Path(repo_dir).resolve() if repo_dir else None

    # ------------------------------------------------------------------
    # SSH / SCP helpers
    # ------------------------------------------------------------------

    def _ssh(self, cmd: str, check: bool = True) -> subprocess.CompletedProcess:
        """Run cmd on the remote host via SSH, with SLURM_CONF prefixed."""
        # shlex.quote the config-derived value: ssh runs this string through
        # the remote user's shell, so an unquoted "…conf; <anything>" would
        # execute <anything> there. Validated at ingress too
        # (queue._validate_executor_config) — quoted here so a resource written
        # by an older version, or loaded straight off disk, is still safe.
        full_cmd = f"SLURM_CONF={shlex.quote(self.slurm_conf)} {cmd}"
        argv = ["bash", "-lc", full_cmd] if self._is_local else ["ssh", self.ssh_host, full_cmd]
        try:
            result = subprocess.run(
                argv, capture_output=True, text=True, timeout=30,
            )
        except subprocess.TimeoutExpired as e:
            result = subprocess.CompletedProcess(
                args=argv, returncode=124, stdout="", stderr="timeout after 30s",
            )
            if check:
                raise RuntimeError(f"SSH command timed out after 30s: {cmd!r}") from e
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
            argv = (["bash", "-lc", cmd] if self._is_local else ["ssh",
                 # Kill remote process when the connection dies (prevents orphaned
                 # git processes that leave index.lock files on the remote).
                 "-o", "ServerAliveInterval=10",
                 "-o", "ServerAliveCountMax=3",
                 self.ssh_host, cmd])
            result = subprocess.run(
                argv,
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
        if self._is_local:
            target = Path(remote_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(local_path, target)
            return
        result = subprocess.run(
            ["scp", str(local_path), f"{self.ssh_host}:{remote_path}"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"SCP failed: {local_path!r} -> {remote_path!r}\n"
                f"stderr: {result.stderr.strip()}"
            )

    def _copy_remote_to_local(self, remote_path: str, local_path: Path) -> None:
        if self._is_local:
            shutil.copyfile(remote_path, local_path)
            return
        result = subprocess.run(
            ["scp", f"{self.ssh_host}:{remote_path}", str(local_path)],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(f"SCP pull failed for {remote_path!r}: {result.stderr.strip()}")

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

        Sets ``receive.denyCurrentBranch = ignore`` unconditionally — this is
        a no-op on proper bare repos (which have no checked-out branch) and a
        safety net when the remote is a non-bare repo with a branch checked out.
        """
        abs_path = self._resolve_git_repo_path()
        q_path = shlex.quote(abs_path)
        self._ssh_plain(
            f"git init --bare {q_path} -q 2>/dev/null || true && "
            f"git -C {q_path} config receive.denyCurrentBranch ignore"
        )
        self._resolved_bare_path: str = abs_path  # type: ignore[attr-defined]
        return abs_path

    def push_branch(self, local_repo_dir: Path, branch: str) -> None:
        """Push *branch* from the local repo to the remote repo via SSH.

        Uses an SSH URL so no extra git remote needs to be configured.
        The push is force-pushed because ideas are re-committed frequently.

        If the push fails because the branch is currently checked out on the
        remote (non-bare repo), switch the remote off that branch first
        (checkout main, or detach HEAD) so the push can proceed cleanly.
        """
        abs_bare = self._resolve_git_repo_path()
        remote_url = abs_bare if self._is_local else f"ssh://{self.ssh_host}{abs_bare}"

        def _do_push() -> subprocess.CompletedProcess:
            return subprocess.run(
                ["git", "-C", str(local_repo_dir),
                 "push", "--force", "--quiet", remote_url,
                 f"{branch}:{branch}"],
                capture_output=True, text=True,
            )

        result = _do_push()
        if result.returncode != 0 and "branch is currently checked out" in result.stderr:
            # Remote is a non-bare repo with this branch checked out.
            # Force-checkout a different ref so the push is accepted, then retry.
            # --force is required in case there are uncommitted changes on the remote.
            self._ssh_plain(
                f"git -C {abs_bare} checkout --force main -q 2>/dev/null || "
                f"git -C {abs_bare} checkout --force --detach HEAD -q 2>/dev/null || true"
            )
            result = _do_push()
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
    # Narrow runtime staging (gitignored, but required by some scripts)
    # ------------------------------------------------------------------

    @staticmethod
    def _script_runtime_requirements(script_path: Path) -> tuple[bool, bool]:
        """Return ``(needs_preamble, needs_bin)`` for an experiment script.

        The injected preamble line is optional. A bare source, an explicit bin
        path, or ``_sandboxed`` is not: otherwise an omitted runtime becomes a
        misleading exit 127 after the allocation has begun.
        """
        content = script_path.read_text(errors="replace")
        needs_bin = bool(re.search(r"(?:^|[\s;&|])(?:_sandboxed|\.the_lab/bin/)", content))
        bare_preamble = re.search(
            r"(?m)^\s*(?:source|\.)\s+['\"]?\.the_lab/preamble\.sh['\"]?\s*$",
            content,
        )
        return bool(bare_preamble or needs_bin), needs_bin

    def _stage_runtime_assets(self, local_repo_dir: Path, worktree_dir: str,
                              script_path: Path) -> bool:
        """Stage only trusted preamble/bin assets omitted from git worktrees.

        Never recursively copy ``.the_lab``: it can contain tokens, queue
        state and experiment records. Do this before ``sbatch`` so a required
        preamble failure cannot waste a model allocation.
        """
        needs_preamble, needs_bin = self._script_runtime_requirements(script_path)
        runtime_dir = local_repo_dir / ".the_lab"
        preamble = runtime_dir / "preamble.sh"
        bin_dir = runtime_dir / "bin"

        if needs_preamble and not preamble.exists():
            raise RuntimeError(
                "experiment requires .the_lab/preamble.sh (or _sandboxed), "
                f"but no trusted preamble exists at {preamble}"
            )
        script_text = script_path.read_text(errors="replace")
        if needs_bin and ".the_lab/bin/" in script_text and not bin_dir.is_dir():
            raise RuntimeError(
                "experiment references .the_lab/bin but no trusted bin directory exists "
                f"at {bin_dir}"
            )

        self._ssh_plain(f"mkdir -p {shlex.quote(worktree_dir + '/.the_lab')}")
        if preamble.exists():
            remote_preamble = f"{worktree_dir}/.the_lab/preamble.sh"
            if preamble.is_symlink():
                # Copying a symlinked project preamble changes BASH_SOURCE and
                # can make it resolve to an unrelated worktree. Retain its
                # target, but only when that trusted target is readable there.
                target = str(preamble.resolve(strict=True))
                q_target, q_dest = shlex.quote(target), shlex.quote(remote_preamble)
                check = self._ssh_plain(f"test -r {q_target}", check=False)
                if check.returncode != 0:
                    raise RuntimeError(
                        "experiment requires a symlinked .the_lab/preamble.sh, "
                        f"but its trusted target is not readable on the Slurm host: {target}"
                    )
                self._ssh_plain(f"rm -f {q_dest} && ln -s {q_target} {q_dest}")
            elif preamble.is_file():
                self._scp_to_remote(preamble, remote_preamble)

        # ``bin`` is an explicit allowlist. Stage files individually rather
        # than copying .the_lab, and refuse symlinks that could escape it.
        if bin_dir.is_dir():
            for asset in sorted(bin_dir.rglob("*")):
                if asset.is_symlink() or not asset.is_file():
                    continue
                rel = asset.relative_to(bin_dir)
                remote_asset = f"{worktree_dir}/.the_lab/bin/{rel.as_posix()}"
                self._ssh_plain(f"mkdir -p {shlex.quote(str(Path(remote_asset).parent))}")
                self._scp_to_remote(asset, remote_asset)
                if asset.stat().st_mode & 0o111:
                    self._ssh_plain(f"chmod +x {shlex.quote(remote_asset)}")
        return needs_preamble

    # ------------------------------------------------------------------
    # Wrapper script builder
    # ------------------------------------------------------------------

    def _build_wrapper(self, label: str, remote_job_dir: str,
                       worktree_dir: str, unit_kind: str = "gpu",
                       env_vars: dict | None = None,
                       requires_preamble: bool = False) -> str:
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

        model_setup = ""
        if self.model_server:
            model_root = self.model_server.get(
                "model_inference_root", "/home/davidh/architects/model_inference"
            )
            api_key_file = self.model_server.get("api_key_file", f"{model_root}/secret.key")
            port = self.model_server.get("port", 30000)
            readiness_timeout = self.model_server.get("readiness_timeout_s", 2700)
            model_setup = f'''\
# ── allocation-local Qwen 3.8 Flash server ──────────────────────────────────
# The launcher uses the shared model_inference HF cache; it never copies model
# weights into this per-experiment job directory.
export MODEL_INFERENCE_ROOT="{model_root}"
export MODEL_API_KEY_FILE="{api_key_file}"
export SGLANG_PORT="$(({port} + ${{THE_LAB_ASSIGNED_UNITS%%,*}}))"
export SGLANG_READY_TIMEOUT_S="{readiness_timeout}"
export THE_LAB_MODEL_READY_FILE="{remote_job_dir}/model.ready.json"
export VLLM_BASE="http://127.0.0.1:${{SGLANG_PORT}}/v1"
export VLLM_BASE_URL="$VLLM_BASE"
export VLLM_API_KEY="$(tr -d '\\r\\n' < "$MODEL_API_KEY_FILE")"
if [ -z "$VLLM_API_KEY" ]; then
    echo "model API key is empty" >&2
    exit 1
fi
printf '{{"status":"starting_model","model":"qwen3.8-flash-next","pct_complete":0}}\\n' > "$THE_LAB_PROGRESS"
bash "{remote_job_dir}/model_server.sh" &
_MODEL_PID=$!
_model_deadline=$((SECONDS + SGLANG_READY_TIMEOUT_S + 30))
until [ -s "$THE_LAB_MODEL_READY_FILE" ]; do
    if ! kill -0 "$_MODEL_PID" 2>/dev/null; then
        wait "$_MODEL_PID"
        exit 1
    fi
    if [ "$SECONDS" -ge "$_model_deadline" ]; then
        echo "timed out waiting for model readiness file" >&2
        exit 1
    fi
    sleep 2
done
printf '{{"status":"model_ready","model":"qwen3.8-flash-next","pct_complete":0}}\\n' > "$THE_LAB_PROGRESS"
_cleanup_model() {{
    if [ -n "${{_MODEL_PID:-}}" ] && kill -0 "$_MODEL_PID" 2>/dev/null; then
        kill "$_MODEL_PID" 2>/dev/null || true
        for _cleanup_tick in $(seq 1 50); do
            kill -0 "$_MODEL_PID" 2>/dev/null || break
            sleep 0.1
        done
        kill -9 "$_MODEL_PID" 2>/dev/null || true
        wait "$_MODEL_PID" 2>/dev/null || true
    fi
}}
# ─────────────────────────────────────────────────────────────────────────────
'''

        preamble_preflight = f'''\
if [ ! -r "{worktree_dir}/.the_lab/preamble.sh" ]; then
    echo "the-lab: experiment requires .the_lab/preamble.sh but it is unavailable in the Slurm worktree" >&2
    exit 127
fi
''' if requires_preamble else ""

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

# Progress/metrics land in the job dir (not the worktree — it's read-only).
# This must precede model startup: preambles and launchers use these paths.
export THE_LAB_PROGRESS="{remote_job_dir}/script.progress"
export THE_LAB_METRICS="{remote_job_dir}/script.metrics.jsonl"

_PREEMPTED=0
_STATUS_WRITTEN=0
_lab_callback() {{
    [ -n "${{THE_LAB_API_URL:-}}" ] && [ -n "${{THE_LAB_TOKEN:-}}" ] || return 2
    curl -fsS --connect-timeout 5 --max-time 30 -X POST "$THE_LAB_API_URL$1" \
        -H "Authorization: Bearer $THE_LAB_TOKEN" \
        -H "Content-Type: application/json" "${{@:2}}"
}}
_write_exit_status() {{
    [ "$_STATUS_WRITTEN" -eq 0 ] || return 0
    _status_exit="$1"
    _status_job_id="${{SLURM_JOB_ID:-}}"
    case "$_status_job_id" in *[!0-9_.-]*) _status_job_id="";; esac
    if [ "$_PREEMPTED" -eq 0 ]; then _status_preempted=false; else _status_preempted=true; fi
    _status_tmp="{remote_job_dir}/.exit-status.$$.tmp"
    printf '{{"exit_code":%d,"preempted":%s,"job_id":"%s"}}\\n' \
        "$_status_exit" "$_status_preempted" "$_status_job_id" > "$_status_tmp"
    mv -f "$_status_tmp" "{remote_job_dir}/exit-status.json"
    _STATUS_WRITTEN=1
}}
_lab_finish() {{
    _exit=$?
    trap - EXIT
    # Persist the script/preflight outcome first, before callback or cleanup.
    _write_exit_status "$_exit" || true
    if [ -n "${{_WATCHER_PID:-}}" ]; then
        kill "$_WATCHER_PID" 2>/dev/null || true
        for _watcher_tick in $(seq 1 50); do
            kill -0 "$_WATCHER_PID" 2>/dev/null || break
            sleep 0.1
        done
        kill -9 "$_WATCHER_PID" 2>/dev/null || true
        wait "$_WATCHER_PID" 2>/dev/null || true
    fi
    if declare -F _cleanup_model >/dev/null 2>&1; then
        _cleanup_model || true
    fi
    if [ "$_PREEMPTED" -eq 0 ]; then
        _payload=$(printf '{{"exit_code":%d,"job_id":"%s"}}' "$_exit" "$_status_job_id")
        _lab_callback "/experiments/{label}/slurm_done" -d "$_payload" || true
    fi
    exit "$_exit"
}}
_lab_requeue() {{
    _PREEMPTED=1
    _lab_callback "/experiments/{label}/requeue" -d '{{"reason":"preempted"}}' || true
    exit 143
}}
trap _lab_finish EXIT
trap _lab_requeue SIGTERM SIGUSR1

# Fail before per-job setup or a costly model launch when the worktree/runtime
# that the script will use is missing.
if ! cd "{worktree_dir}"; then
    echo "the-lab: required experiment worktree is unavailable: {worktree_dir}" >&2
    exit 1
fi
{preamble_preflight}

{venv_setup}

# Background watcher: push progress updates to the API whenever the progress
# file changes, so the dashboard gets live updates without rsync polling lag.
_push_progress() {{
    local _last_mtime="" _cur_mtime
    while true; do
        if [ -f "$THE_LAB_PROGRESS" ]; then
            _cur_mtime=$(stat -c %Y "$THE_LAB_PROGRESS" 2>/dev/null || echo "")
            if [ "$_cur_mtime" != "$_last_mtime" ] && [ -n "$_cur_mtime" ]; then
                _last_mtime="$_cur_mtime"
                _lab_callback "/experiments/{label}/progress" \\
                    --data-binary "@$THE_LAB_PROGRESS" || true
            fi
        fi
        sleep 2
    done
}}
_push_progress &
_WATCHER_PID=$!

{model_setup}

# Run the experiment script from the worktree so relative paths
# (python, hooks, prompts, ...) resolve against the committed code.
bash "{remote_job_dir}/script.sh" &
_SCRIPT_PID=$!
wait $_SCRIPT_PID
_EXIT=$?

# Persist the computation outcome before a potentially slow pull/callback.
_write_exit_status "$_EXIT" || true

# One final bounded progress push; EXIT cleanup stops the watcher/model and
# persists the authoritative status even if this callback cannot reach the API.
if [ -f "$THE_LAB_PROGRESS" ]; then
    _lab_callback "/experiments/{label}/progress" --data-binary "@$THE_LAB_PROGRESS" || true
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
    if _lab_callback "/experiments/{label}/pull" --max-time 1200; then
        # The wrapper itself is still cwd=worktree; leave it before removing
        # the worktree so shell cleanup and the EXIT trap retain a valid cwd.
        cd "{remote_job_dir}" || true
        rm -rf "{remote_job_dir}/worktree" "{remote_job_dir}/venv" 2>/dev/null || true
    fi
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
        requires_preamble = False
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
            requires_preamble = self._stage_runtime_assets(
                Path(local_repo_dir), worktree_dir, local_script_path
            )
            # Also copy script to job dir root for the wrapper to reference
            self._scp_to_remote(local_script_path, f"{remote_job_dir}/script.sh")
        else:
            # No git context — copy script directly (simple self-contained mode)
            requires_preamble, _ = self._script_runtime_requirements(Path(local_script_path))
            if requires_preamble:
                raise RuntimeError(
                    "experiment requires .the_lab/preamble.sh (or _sandboxed), "
                    "but Slurm runtime staging requires git worktree context"
                )
            self._scp_to_remote(local_script_path, f"{remote_job_dir}/script.sh")
            worktree_dir = remote_job_dir  # fall back to job dir as cwd

        # 3. Build and upload wrapper.
        # Merge resource-level env_vars (e.g. HF_HOME) with per-experiment
        # env_extra; experiment values take precedence.
        merged_env = {**self.env_vars, **(env_extra or {})}
        if self.model_server:
            launcher = Path(__file__).parent.parent / "artifacts" / "model_inference" / "launch_qwen38_flash_sglang.sh"
            if not launcher.is_file():
                raise RuntimeError(f"bundled model launcher is missing: {launcher}")
            self._scp_to_remote(launcher, f"{remote_job_dir}/model_server.sh")
        wrapper_content = self._build_wrapper(
            label, remote_job_dir, worktree_dir, unit_kind=unit_kind,
            env_vars=merged_env, requires_preamble=requires_preamble,
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
            raise RuntimeError(
                f"squeue failed for {job_id} (rc={result.returncode}): "
                f"{result.stderr.strip()}"
            )
        return result.stdout.strip()

    def get_exit_status(self, label: str, job_id: str | None = None) -> dict | None:
        """Read the wrapper's durable job-root status, if it has been written.

        ``job_id`` prevents a stale status from a previous run with the same
        label being treated as the current attempt. Invalid or mismatched data
        is deliberately indistinguishable from absent data to callers.
        """
        path = f"{self._resolve_remote_base()}/{label}/exit-status.json"
        result = self._ssh_plain(f"cat {shlex.quote(path)}", check=False)
        if result.returncode != 0:
            return None
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return None
        if (not isinstance(data, dict) or isinstance(data.get("exit_code"), bool)
                or not isinstance(data.get("exit_code"), int)
                or not isinstance(data.get("preempted"), bool)):
            return None
        recorded_id = data.get("job_id", "")
        if not isinstance(recorded_id, str) or (job_id and recorded_id != str(job_id)):
            return None
        return data

    def accounting_status(self, job_id: str) -> dict | None:
        """Return Slurm accounting's root-job result, excluding job steps.

        ``squeue`` drops a completed job before accounting is always ready.
        Prefer the exact root record; a ``.batch`` record is only a fallback
        for clusters that delay the root row.  Never mistake ``.extern`` or a
        task step for the experiment's result.
        """
        if not re.fullmatch(r"[0-9]+(?:_[0-9]+)?", str(job_id)):
            return None
        command = (
            f"SLURM_CONF={shlex.quote(self.slurm_conf)} sacct --noheader --parsable2 "
            f"--jobs={shlex.quote(str(job_id))} --format=JobIDRaw,State,ExitCode"
        )
        result = self._ssh_plain(command, check=False, timeout=30)
        if result.returncode != 0:
            return None
        root: dict | None = None
        batch: dict | None = None
        for line in result.stdout.splitlines():
            fields = line.split("|")
            if len(fields) < 3:
                continue
            raw, state, exit_code = (item.strip() for item in fields[:3])
            if raw not in {str(job_id), f"{job_id}.batch"}:
                continue
            match = re.fullmatch(r"([0-9]+):([0-9]+)", exit_code)
            if not match:
                continue
            record = {
                "job_id": str(job_id), "state": state,
                "exit_code": int(match.group(1)), "signal": int(match.group(2)),
            }
            if raw == str(job_id):
                root = record
            else:
                batch = record
        return root or batch

    @staticmethod
    def _is_under(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    def _trusted_output_roots(self) -> list[Path]:
        if not self.repo_dir:
            return []
        roots = [self.repo_dir]
        preamble = self.repo_dir / ".the_lab" / "preamble.sh"
        if preamble.exists():
            roots.append(preamble.resolve().parent)
        return roots

    def _remote_realpath(self, path: str) -> Path | None:
        if self._is_local:
            candidate = Path(path)
            if not candidate.exists() and not candidate.is_symlink():
                return None
            try:
                return candidate.resolve(strict=True)
            except OSError:
                raise RuntimeError(f"canonical Slurm output is a dangling or unreadable link: {path}")
        exists = self._ssh_plain(f"test -e {shlex.quote(path)}", check=False, timeout=30)
        if exists.returncode == 1:
            linked = self._ssh_plain(f"test -L {shlex.quote(path)}", check=False, timeout=30)
            if linked.returncode == 0:
                raise RuntimeError(f"canonical Slurm output is a dangling link: {path}")
            return None
        if exists.returncode != 0:
            raise RuntimeError(f"cannot inspect canonical Slurm output: {path}")
        result = self._ssh_plain(f"realpath -e {shlex.quote(path)}", check=False, timeout=30)
        if result.returncode != 0 or not result.stdout.strip():
            raise RuntimeError(f"cannot resolve canonical Slurm output: {path}")
        return Path(result.stdout.strip())

    def _sync_canonical_outputs(self, label: str, local_dir: Path) -> None:
        """Atomically publish only validated canonical dashboard outputs."""
        remote_job_dir = f"{self._resolve_remote_base()}/{label}"
        allowed = [Path(remote_job_dir).resolve(), *self._trusted_output_roots()]
        for name in ("script.output.html", "script.output.md"):
            destination = local_dir / name
            target = self._remote_realpath(f"{remote_job_dir}/{name}")
            if target is None:
                # A missing new output does not destroy an existing good one.
                continue
            if not any(self._is_under(target, root) for root in allowed):
                raise RuntimeError(f"refusing untrusted Slurm output target for {name}")
            with tempfile.NamedTemporaryFile(dir=local_dir, prefix=f".{name}.", delete=False) as tmp:
                temporary = Path(tmp.name)
            try:
                self._copy_remote_to_local(str(target), temporary)
                os.replace(temporary, destination)
            except Exception:
                temporary.unlink(missing_ok=True)
                raise

    def pull_results(self, label: str, local_exp_dir: str | Path) -> None:
        """rsync the job dir back to the local experiment directory."""
        local_path = Path(local_exp_dir)
        local_dir = str(local_path).rstrip("/") + "/"
        source = (f"{self._rsync_base}/{label}/" if self._is_local
                  else f"{self.ssh_host}:{self._rsync_base}/{label}/")
        result = subprocess.run(
            ["rsync", "-az",
             # The worktree is git-tracked code the local machine already has —
             # no need to sync it back. Only pull the job-root output files:
             # script.log, script.progress, script.metrics.jsonl, *.gpu_stats,
             # and any artifacts the script copies to the job root.
             "--exclude=worktree/",
             "--exclude=venv/",
             # Canonical dashboard outputs are copied separately only after
             # their remote target has passed the narrow allowlist below.
             "--exclude=script.output.html",
             "--exclude=script.output.md",
             source,
            local_dir],
            capture_output=True, text=True,
            timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"rsync pull for {label} failed (rc={result.returncode}): "
                f"{result.stderr.strip()}"
            )
        self._sync_canonical_outputs(label, local_path)

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
