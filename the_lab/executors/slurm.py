"""Slurm executor: SSH-submit experiments to a Slurm cluster.

Uses only stdlib (subprocess, tempfile) — no paramiko or other extras.
All SSH calls use ``subprocess.run(["ssh", ssh_host, cmd], ...)``.
Results are pulled back via rsync.
"""
from __future__ import annotations

import logging
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger("the-lab.slurm")


class SlurmExecutor:
    # Default SLURM_CONF path discovered on the ml-16 cluster head node.
    # Can be overridden via executor_config["slurm_conf"].
    DEFAULT_SLURM_CONF = "/data/slurm/etc/slurm.conf"

    def __init__(self, ssh_host: str, config: dict):
        self.ssh_host = ssh_host
        self.partition = config.get("partition", "lowprio")
        self.account = config.get("account")         # optional
        self.ntasks = int(config.get("ntasks", 1))
        self.gpus = int(config.get("gpus", 1))
        self.mem = config.get("mem")                  # optional, e.g. "32G"
        self.time_limit = config.get("time")          # optional, e.g. "08:00:00"
        # remote_base is embedded verbatim inside the sbatch wrapper script, so
        # use $HOME (which bash expands at runtime) rather than ~ (which bash
        # does NOT expand inside double-quoted strings — causes exit 127).
        # rsync uses _rsync_base which converts $HOME → ~ (rsync expands ~ but
        # not shell variables in remote paths).
        self.remote_base = config.get("remote_base", "$HOME/.thelab/jobs")
        # SLURM_CONF must be set so sbatch/squeue/scancel can find the controller.
        self.slurm_conf = config.get("slurm_conf", self.DEFAULT_SLURM_CONF)
        # QOS to request — defaults to the partition name (lowprio → qos=lowprio).
        # Set explicitly via executor_config["qos"] to override.
        self.qos = config.get("qos", self.partition)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ssh(self, cmd: str, check: bool = True) -> subprocess.CompletedProcess:
        """Run a command on the remote host via SSH with SLURM_CONF set."""
        # Prefix every remote command with SLURM_CONF so sbatch/squeue/scancel
        # can locate the controller without a DNS SRV record.
        full_cmd = f"SLURM_CONF={self.slurm_conf} {cmd}"
        result = subprocess.run(
            ["ssh", self.ssh_host, full_cmd],
            capture_output=True,
            text=True,
        )
        if check and result.returncode != 0:
            raise RuntimeError(
                f"SSH command failed (rc={result.returncode}): {cmd!r}\n"
                f"stderr: {result.stderr.strip()}"
            )
        return result

    def _scp_to_remote(self, local_path: str | Path, remote_path: str) -> None:
        """Copy a local file to the remote host via SCP."""
        result = subprocess.run(
            ["scp", str(local_path), f"{self.ssh_host}:{remote_path}"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"SCP failed (rc={result.returncode}): {local_path!r} -> {remote_path!r}\n"
                f"stderr: {result.stderr.strip()}"
            )

    def _build_wrapper(self, label: str, remote_job_dir: str, unit_kind: str = "gpu") -> str:
        """Build the sbatch wrapper script content."""
        gpu_line = f"#SBATCH --gres=gpu:{self.gpus}" if unit_kind == "gpu" else ""
        mem_line = f"#SBATCH --mem={self.mem}" if self.mem else ""
        time_line = f"#SBATCH --time={self.time_limit}" if self.time_limit else ""
        account_line = f"#SBATCH --account={self.account}" if self.account else ""
        qos_line = f"#SBATCH --qos={self.qos}" if self.qos else ""

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

# Preemption handler — fires on SIGTERM (Slurm sends this before kill)
_PREEMPTED=0
_lab_requeue() {{
    _PREEMPTED=1
    curl -s -X POST "$THE_LAB_API_URL/experiments/{label}/requeue" \\
         -H "Authorization: Basic $THE_LAB_AUTH" \\
         -H "Content-Type: application/json" \\
         -d '{{"reason":"preempted"}}' || true
}}
trap _lab_requeue SIGTERM SIGUSR1

# Point progress/metrics env vars to local files in the job dir
export THE_LAB_PROGRESS="{remote_job_dir}/script.progress"
export THE_LAB_METRICS="{remote_job_dir}/script.metrics.jsonl"

# Run the actual experiment script
bash "{remote_job_dir}/script.sh" &
_SCRIPT_PID=$!
wait $_SCRIPT_PID
_EXIT=$?

if [ $_PREEMPTED -eq 0 ]; then
    # Signal normal completion to lab (lab will rsync results)
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
    ) -> str:
        """Submit an experiment to Slurm. Returns the job_id string."""
        # Resolve $HOME to an absolute path — Slurm's sbatch parser does not
        # expand shell variables in #SBATCH directives (only %j/%u/etc. work).
        resolved_base = self._resolve_remote_base()
        remote_job_dir = f"{resolved_base}/{label}"

        # 1. Create remote directory
        self._ssh(f"mkdir -p {remote_job_dir}")

        # 2. SCP the experiment script
        self._scp_to_remote(local_script_path, f"{remote_job_dir}/script.sh")

        # 3. Generate and upload wrapper script
        wrapper_content = self._build_wrapper(label, remote_job_dir, unit_kind=unit_kind)
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".sh", prefix="thelab_wrapper_", delete=False
        ) as tmp:
            tmp.write(wrapper_content)
            tmp_path = tmp.name

        try:
            self._scp_to_remote(tmp_path, f"{remote_job_dir}/wrapper.sh")
        finally:
            Path(tmp_path).unlink(missing_ok=True)

        # Make scripts executable
        self._ssh(f"chmod +x {remote_job_dir}/script.sh {remote_job_dir}/wrapper.sh")

        # 4. Submit via sbatch
        sbatch_cmd = f"sbatch --parsable {remote_job_dir}/wrapper.sh"
        result = self._ssh(sbatch_cmd)
        raw = result.stdout.strip().split(";")[0].strip()
        if not raw.isdigit():
            raise RuntimeError(f"sbatch returned unexpected output: {result.stdout!r}")
        return raw

    def cancel(self, job_id: str) -> None:
        """Cancel a Slurm job. Best-effort — errors are logged."""
        try:
            self._ssh(f"scancel {job_id}")
        except Exception as e:
            logger.warning("scancel %s failed: %s", job_id, e)

    def poll_status(self, job_id: str) -> str:
        """Poll squeue for a job's state string.

        Returns one of: RUNNING, PENDING, COMPLETING, COMPLETED, FAILED,
        PREEMPTED, TIMEOUT, OUT_OF_MEMORY, NODE_FAIL, CANCELLED, or "" when
        the job has disappeared from the queue.
        """
        result = self._ssh(
            f"squeue --noheader -j {job_id} -o %T",
            check=False,
        )
        if result.returncode != 0:
            # Job gone or squeue error — treat as gone
            return ""
        state = result.stdout.strip()
        return state

    def _resolve_remote_base(self) -> str:
        """Return remote_base with $HOME expanded to the actual remote home path.

        Slurm's sbatch parser does NOT expand $HOME in #SBATCH directives —
        only its own %-tokens (like %j, %u) work there. We SSH-resolve the
        real path once at submit time so every path in the wrapper is absolute.
        """
        if "$HOME" not in self.remote_base:
            return self.remote_base
        home = self._ssh("echo $HOME", check=True).stdout.strip()
        return self.remote_base.replace("$HOME", home)

    @property
    def _rsync_base(self) -> str:
        """remote_base with $HOME replaced by ~ for use in rsync remote paths.

        rsync expands ~ in remote paths but does not evaluate shell variables
        like $HOME — the inverse of bash's double-quote behaviour.
        """
        return self.remote_base.replace("$HOME", "~")

    def _remote_job_dir(self, label: str) -> str:
        """Absolute path to the job dir on the remote host (resolved $HOME)."""
        return f"{self._resolve_remote_base()}/{label}"

    def pull_results(self, label: str, local_exp_dir: str | Path) -> None:
        """rsync results from remote job dir to local experiment dir. Best-effort."""
        local_dir = str(local_exp_dir).rstrip("/") + "/"
        result = subprocess.run(
            [
                "rsync", "-az", "--timeout=30",
                f"{self.ssh_host}:{self._rsync_base}/{label}/",
                local_dir,
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logger.error(
                "rsync pull for %s failed (rc=%d): %s",
                label, result.returncode, result.stderr.strip(),
            )

    def cleanup_remote(self, label: str) -> None:
        """Remove remote job directory. Best-effort."""
        try:
            self._ssh(f"rm -rf {self.remote_base}/{label}")
        except Exception as e:
            logger.warning("cleanup_remote for %s failed: %s", label, e)
