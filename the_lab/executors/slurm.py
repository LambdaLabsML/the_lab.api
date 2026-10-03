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

    def __init__(self, ssh_host: str, config: dict, instance_id: str | None = None):
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

    # ------------------------------------------------------------------
    # SSH / SCP helpers
    # ------------------------------------------------------------------

    def _ssh(self, cmd: str, check: bool = True, timeout: int = 120) -> subprocess.CompletedProcess:
        """Run cmd on the remote host via SSH, with SLURM_CONF prefixed."""
        # shlex.quote the config-derived value: ssh runs this string through
        # the remote user's shell, so an unquoted "…conf; <anything>" would
        # execute <anything> there. Validated at ingress too
        # (queue._validate_executor_config) — quoted here so a resource written
        # by an older version, or loaded straight off disk, is still safe.
        full_cmd = f"SLURM_CONF={shlex.quote(self.slurm_conf)} {cmd}"
        argv = ["bash", "-lc", full_cmd] if self._is_local else ["ssh", self.ssh_host, full_cmd]
        result = subprocess.run(
            argv,
            capture_output=True, text=True,
            timeout=timeout,
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
    # Wrapper script builder
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Narrow runtime staging (gitignored but required by experiment scripts)
    # ------------------------------------------------------------------

    @staticmethod
    def _script_runtime_requirements(script_path: Path) -> tuple[bool, bool]:
        content = script_path.read_text(errors="replace")
        needs_bin = bool(re.search(r"(?:^|[\s;&|])(?:_sandboxed|\.the_lab/bin/)", content))
        bare_preamble = re.search(
            r"(?m)^\s*(?:source|\.)\s+['\"]?\.the_lab/preamble\.sh['\"]?\s*$", content,
        )
        return bool(bare_preamble or needs_bin), needs_bin

    def _stage_runtime_assets(self, local_repo_dir: Path, worktree_dir: str,
                              script_path: Path) -> bool:
        """Stage the explicit trusted runtime allowlist before sbatch."""
        needs_preamble, needs_bin = self._script_runtime_requirements(script_path)
        runtime_dir = local_repo_dir / ".the_lab"
        preamble, bin_dir = runtime_dir / "preamble.sh", runtime_dir / "bin"
        if needs_preamble and not preamble.exists():
            raise RuntimeError(f"experiment requires .the_lab/preamble.sh (or _sandboxed), but no trusted preamble exists at {preamble}")
        if needs_bin and ".the_lab/bin/" in script_path.read_text(errors="replace") and not bin_dir.is_dir():
            raise RuntimeError(f"experiment references .the_lab/bin but no trusted bin directory exists at {bin_dir}")
        self._ssh_plain(f"mkdir -p {shlex.quote(worktree_dir + '/.the_lab')}")
        if preamble.exists():
            destination = f"{worktree_dir}/.the_lab/preamble.sh"
            if preamble.is_symlink():
                target = str(preamble.resolve(strict=True))
                if self._ssh_plain(f"test -r {shlex.quote(target)}", check=False).returncode != 0:
                    raise RuntimeError(f"symlinked trusted preamble is not readable on the Slurm host: {target}")
                self._ssh_plain(f"rm -f {shlex.quote(destination)} && ln -s {shlex.quote(target)} {shlex.quote(destination)}")
            elif preamble.is_file():
                self._scp_to_remote(preamble, destination)
        if bin_dir.is_dir():
            for asset in sorted(bin_dir.rglob("*")):
                if asset.is_symlink() or not asset.is_file():
                    continue
                destination = f"{worktree_dir}/.the_lab/bin/{asset.relative_to(bin_dir).as_posix()}"
                self._ssh_plain(f"mkdir -p {shlex.quote(str(Path(destination).parent))}")
                self._scp_to_remote(asset, destination)
                if asset.stat().st_mode & 0o111:
                    self._ssh_plain(f"chmod +x {shlex.quote(destination)}")
        return needs_preamble

    def _stage_experiment_snapshot(self, remote_job_dir: str, record: dict) -> None:
        """Write one immutable dispatch record for preamble/model configuration."""
        if not isinstance(record, dict) or ("meta" in record and record["meta"] is not None
                                            and not isinstance(record["meta"], dict)):
            raise RuntimeError("experiment snapshot must be an object with meta as an object or null")
        try:
            frozen = json.loads(json.dumps(record, ensure_ascii=False, allow_nan=False))
            meta = frozen.get("meta")
            if isinstance(meta, dict):
                meta.pop("slurm_run_token", None)
            payload = json.dumps(frozen, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("experiment snapshot is not valid JSON; refusing Slurm submission") from exc
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", prefix="thelab_experiment_",
                                         encoding="utf-8", delete=False) as tmp:
            tmp.write(payload)
            tmp_path = Path(tmp.name)
        try:
            os.chmod(tmp_path, 0o600)
            self._scp_to_remote(tmp_path, f"{remote_job_dir}/experiment.json")
            self._ssh_plain(f"chmod 600 {shlex.quote(remote_job_dir + '/experiment.json')}")
        finally:
            tmp_path.unlink(missing_ok=True)

    def _build_wrapper(self, label: str, remote_job_dir: str,
                       worktree_dir: str, unit_kind: str = "gpu",
                       env_vars: dict | None = None,
                       requires_preamble: bool = False,
                       requires_experiment_json: bool = False) -> str:
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
# Logical allocator slots are unique within this resource.  Derive a loopback
# port from that slot so two experiments placed on the same node can each own
# their server without a bind collision.
export SGLANG_PORT="$(({port} + ${{THE_LAB_ASSIGNED_UNITS%%,*}}))"
export SGLANG_READY_TIMEOUT_S="{readiness_timeout}"
export THE_LAB_MODEL_READY_FILE="{remote_job_dir}/model.ready.json"
export THE_LAB_PROGRESS="{remote_job_dir}/script.progress"
export THE_LAB_METRICS="{remote_job_dir}/script.metrics.jsonl"
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
        wait "$_MODEL_PID" 2>/dev/null || true
    fi
}}
# ─────────────────────────────────────────────────────────────────────────────
'''

        preamble_preflight = f'''if [ ! -r "{worktree_dir}/.the_lab/preamble.sh" ]; then
    echo "the-lab: required experiment preamble is unavailable in the Slurm worktree" >&2
    exit 127
fi
''' if requires_preamble else ""
        snapshot_preflight = f'''if [ ! -r "{remote_job_dir}/experiment.json" ] || ! python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); raise SystemExit(0 if isinstance(d, dict) and (d.get("meta") is None or isinstance(d.get("meta"), dict)) else 1)' "{remote_job_dir}/experiment.json"; then
    echo "the-lab: required experiment.json is unavailable or malformed" >&2
    exit 126
fi
''' if requires_experiment_json else ""

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

# Native Slurm requeue starts this wrapper again with the same job id.  Archive
# mutable attempt outputs before startup; truncate the scheduler-opened log
# rather than renaming it so its file descriptor stays with this generation.
_THE_LAB_GENERATION="${{SLURM_RESTART_COUNT:-0}}"
case "$_THE_LAB_GENERATION" in ''|*[!0-9]*) _THE_LAB_GENERATION=0;; esac
export THE_LAB_RESTART_COUNT="$_THE_LAB_GENERATION"
_THE_LAB_PREVIOUS_GENERATION="$(cat {remote_job_dir}/.the_lab_generation 2>/dev/null || true)"
case "$_THE_LAB_PREVIOUS_GENERATION" in ''|*[!0-9]*) _THE_LAB_PREVIOUS_GENERATION="unknown";; esac
if [ "$_THE_LAB_PREVIOUS_GENERATION" != "$_THE_LAB_GENERATION" ]; then
    _THE_LAB_ARCHIVE="{remote_job_dir}/attempts/${{_THE_LAB_PREVIOUS_GENERATION:-unknown}}"
    mkdir -p "$_THE_LAB_ARCHIVE"
    for _the_lab_path in "{remote_job_dir}"/model.ready*.json "{remote_job_dir}"/script.progress "{remote_job_dir}"/script.metrics.jsonl "{remote_job_dir}"/exit-status.json "{remote_job_dir}"/script.err "{remote_job_dir}"/script.output.md "{remote_job_dir}"/script.output.html "{remote_job_dir}"/script.heartbeats; do
        # mv moves a symlink itself, never its target.  Do not follow a stale
        # script-produced link while resetting a native attempt.
        [ -e "$_the_lab_path" ] || [ -L "$_the_lab_path" ] || continue
        mv -f -- "$_the_lab_path" "$_THE_LAB_ARCHIVE/" || true
    done
    : > "{remote_job_dir}/script.log"
    printf '%s\\n' "$_THE_LAB_GENERATION" > "{remote_job_dir}/.the_lab_generation"
fi

# Slurm owns native requeue. SIGTERM is also used by user cancellation, so it
# must never manufacture a Lab retry.
_PREEMPTED=0
_lab_interrupted() {{
    _PREEMPTED=1
    [ -n "${{_SCRIPT_PID:-}}" ] && kill "$_SCRIPT_PID" 2>/dev/null || true
    [ -n "${{_WATCHER_PID:-}}" ] && kill "$_WATCHER_PID" 2>/dev/null || true
    [ -n "${{_MODEL_PID:-}}" ] && kill "$_MODEL_PID" 2>/dev/null || true
    if declare -F _cleanup_model >/dev/null 2>&1; then _cleanup_model || true; fi
    exit 143
}}
trap _lab_interrupted SIGTERM SIGUSR1

# Validate runtime before a venv/model allocation is started.
if ! cd "{worktree_dir}"; then
    echo "the-lab: required experiment worktree is unavailable: {worktree_dir}" >&2
    exit 1
fi
{preamble_preflight}
{snapshot_preflight}

{venv_setup}
{model_setup}

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
                     -H "X-The-Lab-Slurm-Job-ID: ${{SLURM_JOB_ID:-}}" \\
                     -H "X-The-Lab-Restart-Count: $_THE_LAB_GENERATION" \\
                     --data-binary "@$THE_LAB_PROGRESS" || true
            fi
        fi
        sleep 2
    done
}}
_push_progress &
_WATCHER_PID=$!

# Run the experiment script from the already-validated worktree.
bash "{remote_job_dir}/script.sh" &
_SCRIPT_PID=$!
wait $_SCRIPT_PID
_EXIT=$?

if [ "${{_MODEL_PID:-}}" ]; then
    _cleanup_model || true
fi

# Stop watcher and do one final push
kill $_WATCHER_PID 2>/dev/null || true
if [ -f "$THE_LAB_PROGRESS" ]; then
    curl -s -X POST "$THE_LAB_API_URL/experiments/{label}/progress" \\
        -H "Authorization: Bearer $THE_LAB_TOKEN" \\
        -H "Content-Type: application/json" \\
        -H "X-The-Lab-Slurm-Job-ID: ${{SLURM_JOB_ID:-}}" \\
        -H "X-The-Lab-Restart-Count: $_THE_LAB_GENERATION" \\
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

    # Best-effort early sync only. The monitor owns cleanup after authoritative
    # terminal evidence; a late preemption must retain restart prerequisites.
    curl -sf --max-time 1200 \\
         -X POST "$THE_LAB_API_URL/experiments/{label}/pull" \\
         -H "Authorization: Bearer $THE_LAB_TOKEN" \\
         -H "Content-Type: application/json" || true

    curl -s -X POST "$THE_LAB_API_URL/experiments/{label}/slurm_done" \\
         -H "Authorization: Bearer $THE_LAB_TOKEN" \\
         -H "Content-Type: application/json" \\
         -d "{{\\"exit_code\\":$_EXIT,\\"job_id\\":\\"${{SLURM_JOB_ID:-}}\\",\\"restart_count\\":$_THE_LAB_GENERATION}}" || true
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
        experiment_record: dict | None = None,
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

        # The preamble reads this job-root file before model startup.  Lab
        # dispatches must pass an explicit frozen record; the fallback exists
        # only for direct backwards-compatible executor callers.
        if experiment_record is None:
            fallback = Path(local_exp_dir) / "experiment.json"
            try:
                experiment_record = json.loads(fallback.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError("experiment snapshot is required for Slurm submission") from exc
        if not isinstance(experiment_record, dict):
            raise RuntimeError("experiment snapshot must be a JSON object")
        self._stage_experiment_snapshot(remote_job_dir, experiment_record)

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
                raise RuntimeError("experiment requires .the_lab/preamble.sh (or _sandboxed), but Slurm runtime staging requires git worktree context")
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
            requires_experiment_json=True,
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
            # Normal scancel is terminal; disable requeue defensively so an
            # unusual site policy cannot turn an operator cancellation into a
            # restart.
            self._ssh(f"scontrol update JobId={job_id} Requeue=0 2>/dev/null || true",
                      check=False)
            self._ssh(f"scancel {job_id}")
        except Exception as e:
            logger.warning("scancel %s failed: %s", job_id, e)

    def poll_status(self, job_id: str) -> str:
        """Poll squeue. Returns state string or '' when job is gone."""
        result = self._ssh(f"squeue --noheader -j {job_id} -o %T", check=False, timeout=30)
        # Once a finished job ages out of slurmctld (MinJobAge), squeue
        # rejects its id instead of printing nothing.  That is "gone", and the
        # monitor still requires sacct evidence before treating it as terminal.
        if result.returncode != 0 and "invalid job id" in result.stderr.lower():
            return ""
        if result.returncode != 0:
            raise RuntimeError(f"squeue failed for {job_id} (rc={result.returncode}): {result.stderr.strip()}")
        return result.stdout.strip()

    def accounting_status(self, job_id: str) -> dict | None:
        """Return root-job accounting state, never an ``extern`` step.

        squeue has a normal empty window between a preemption and its requeue;
        sacct is consulted only to distinguish that window from a real terminal
        job.  Prefer the root row over ``.batch``.
        """
        if not re.fullmatch(r"[0-9]+(?:_[0-9]+)?", str(job_id)):
            return None
        command = (f"SLURM_CONF={shlex.quote(self.slurm_conf)} sacct --noheader --parsable2 "
                   f"--jobs={shlex.quote(str(job_id))} --format=JobIDRaw,State,ExitCode,Restarts")
        result = self._ssh_plain(command, check=False, timeout=30)
        if result.returncode != 0:
            return None
        root = batch = None
        for line in result.stdout.splitlines():
            fields = line.split("|")
            if len(fields) < 3:
                continue
            raw, state, code = (x.strip() for x in fields[:3])
            if raw not in (str(job_id), f"{job_id}.batch"):
                continue
            match = re.fullmatch(r"([0-9]+):([0-9]+)", code)
            if not match:
                continue
            item = {"job_id": str(job_id), "state": state,
                    "exit_code": int(match.group(1)), "signal": int(match.group(2))}
            if len(fields) > 3 and fields[3].strip().isdigit():
                item["restart_count"] = int(fields[3].strip())
            if raw == str(job_id):
                root = item
            else:
                batch = item
        # A failing batch only invalidates a *matching completed* root.  A
        # preempted/running root normally has an old batch exit 143, and sacct
        # can retain a prior restart's batch row after the new root starts.
        same_restart = (isinstance(root.get("restart_count"), int)
                        and root.get("restart_count") == batch.get("restart_count")) if root and batch else False
        root_state = str(root.get("state") or "").upper().split("+")[0] if root else ""
        if (root and batch and root_state == "COMPLETED" and same_restart
                and (batch["exit_code"] != 0 or batch["signal"])):
            return {**root, "state": "FAILED", "exit_code": batch["exit_code"], "signal": batch["signal"]}
        return root or batch

    def restart_count(self, label: str) -> int | None:
        """Read the wrapper generation, including during model warm-up."""
        path = f"{self._resolve_remote_base()}/{label}/.the_lab_generation"
        result = self._ssh_plain(f"cat {shlex.quote(path)}", check=False, timeout=15)
        value = result.stdout.strip()
        return int(value) if result.returncode == 0 and value.isdigit() else None

    def pull_results(self, label: str, local_exp_dir: str | Path) -> int | None:
        """Publish one verified generation of job-root results.

        A native requeue can replace files while rsync runs.  Pull into a
        sibling staging directory, verify the generation before/after, then
        publish files.  Lifecycle-owned source files are never touched.
        """
        destination = Path(local_exp_dir)
        destination.mkdir(parents=True, exist_ok=True)
        source = (f"{self._rsync_base}/{label}/" if self._is_local
                  else f"{self.ssh_host}:{self._rsync_base}/{label}/")
        before = self.restart_count(label)
        with tempfile.TemporaryDirectory(prefix=".thelab-pull-", dir=destination.parent) as staging_name:
            staging = Path(staging_name)
            result = subprocess.run(
                ["rsync", "-az", "--exclude=worktree/", "--exclude=venv/",
                 "--exclude=experiment.json", "--exclude=script.sh", source, str(staging) + "/"],
                capture_output=True, text=True, timeout=1200,
            )
            if result.returncode != 0:
                raise RuntimeError("rsync pull for %s failed (rc=%d): %s" %
                                   (label, result.returncode, result.stderr.strip()))
            staged_marker = staging / ".the_lab_generation"
            staged_generation = (int(staged_marker.read_text().strip())
                                 if staged_marker.is_file() and staged_marker.read_text().strip().isdigit() else None)
            after = self.restart_count(label)
            # A present staged marker must be corroborated by both remote
            # reads. ``None`` is only tolerated for old wrappers which have
            # no marker at all; a transport failure cannot bless generation 1.
            if (staged_generation is not None and
                    (before != staged_generation or after != staged_generation)) or \
               (staged_generation is None and (before != after or before is not None)):
                raise RuntimeError("Slurm generation changed while pulling results; retrying")

            protected = {"experiment.json", "script.sh"}
            managed = {"model.ready.json", "model.ready.json.smoke.json", "script.progress",
                       "script.metrics.jsonl", "exit-status.json", "script.err", "script.output.md",
                       "script.output.html", "script.heartbeats"}
            # Reset only explicit current-attempt files that disappeared in the
            # stable remote snapshot; never broadly delete a local experiment.
            for name in managed:
                if not (staging / name).exists() and not (staging / name).is_symlink():
                    old = destination / name
                    if old.is_file() or old.is_symlink():
                        old.unlink()
            for item in staging.iterdir():
                if item.name in protected:
                    continue
                target = destination / item.name
                if item.is_symlink():
                    # Publish the link itself; never dereference an untrusted
                    # experiment-produced target during a dashboard sync.
                    fd, temporary_name = tempfile.mkstemp(prefix=f".{item.name}.", suffix=".pull-link", dir=destination)
                    os.close(fd)
                    temporary = Path(temporary_name)
                    temporary.unlink()
                    os.symlink(os.readlink(item), temporary)
                    os.replace(temporary, target)
                elif item.is_file():
                    fd, temporary_name = tempfile.mkstemp(prefix=f".{item.name}.", suffix=".pull", dir=destination)
                    os.close(fd)
                    temporary = Path(temporary_name)
                    shutil.copy2(item, temporary, follow_symlinks=False)
                    os.replace(temporary, target)
                elif item.is_dir():
                    # Artifact directories are replaced from the verified
                    # snapshot without following symlinks inside them.
                    if target.is_symlink() or target.is_file():
                        target.unlink()
                    elif target.exists():
                        shutil.rmtree(target)
                    shutil.copytree(item, target, symlinks=True)
            return before

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
