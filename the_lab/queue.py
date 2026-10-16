"""Resource model + scheduler state for the experiment queue.

Resources are declared in ``.the_lab/queue.json``. Each resource has a
``capacity`` (total integer units — usually GPU IDs 0..N-1) and a
``jobs_per_unit`` ratio that controls how the resource is shared:

  jobs_per_unit = 1.0  → 1 job per unit, capacity parallel jobs
  jobs_per_unit = 0.25 → each job needs 4 units, capacity * 0.25 parallel jobs
  jobs_per_unit > 1.0  → reserved (GPU sharing — not in v1)

Derived per resource (read-only, computed in to_dict):

  default_units_per_job = ceil(1 / jobs_per_unit)
  max_parallel_jobs     = floor(capacity * jobs_per_unit)

The runtime allocator (``_allocations``) tracks which units a running
experiment holds so they can be released when it finishes. Only the
config (resources + queue knobs) is persisted; allocations are rebuilt
at startup from each running experiment's stored ``meta``.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import threading
from urllib.parse import urlparse

from . import jsonio
from dataclasses import dataclass, field, asdict
from pathlib import Path


@dataclass
class Resource:
    name: str
    kind: str = "local"            # local | (future: ssh, slurm, k8s, ...)
    unit_kind: str = "gpu"         # gpu | cpu | none (queue-only)
    capacity: int = 1
    jobs_per_unit: float = 1.0
    tags: list[str] = field(default_factory=list)
    executor_config: dict = field(default_factory=dict)

    @property
    def default_units_per_job(self) -> int:
        if self.jobs_per_unit <= 0:
            return 1
        return max(1, math.ceil(1 / self.jobs_per_unit))

    @property
    def max_parallel_jobs(self) -> int:
        return max(1, math.floor(self.capacity * self.jobs_per_unit))

    def to_dict(self) -> dict:
        d = asdict(self)
        d["default_units_per_job"] = self.default_units_per_job
        d["max_parallel_jobs"] = self.max_parallel_jobs
        return d


@dataclass
class QueueConfig:
    paused: bool = False
    dispatch_interval_s: float = 2.0


# ---------------------------------------------------------------------------
# Persisted config: resources + queue knobs
# ---------------------------------------------------------------------------

def _config_path(repo_dir: Path) -> Path:
    return repo_dir / ".the_lab" / "queue.json"


def _detect_local_gpus() -> int:
    """Return GPU count from nvidia-smi, or 0 if absent / errors."""
    if not shutil.which("nvidia-smi"):
        return 0
    try:
        out = subprocess.run(
            ["nvidia-smi", "-L"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if out.returncode != 0:
            return 0
        return sum(1 for line in out.stdout.splitlines() if line.startswith("GPU "))
    except Exception:
        return 0


def _default_local_resource() -> Resource:
    n = _detect_local_gpus()
    if n > 0:
        return Resource(name="local-gpu", kind="local", unit_kind="gpu", capacity=n)
    # No GPUs detected — degenerate to a single-slot CPU pool so the queue
    # still works on dev machines.
    return Resource(name="local", kind="local", unit_kind="none", capacity=1)


# The scheduler calls load_config every tick (default 2s) from the event
# loop — cache the parsed config keyed on the file's mtime so the NFS read
# + parse happens only when queue.json actually changed.
_config_cache: tuple[int, list, QueueConfig] | None = None
_config_cache_lock = threading.Lock()


def load_config(repo_dir: Path) -> tuple[list[Resource], QueueConfig]:
    """Load resources + queue config; auto-create on first run.

    mtime-cached: re-reads only when queue.json changed on disk.
    """
    global _config_cache
    path = _config_path(repo_dir)
    if not path.exists():
        # First run: write a default with a single auto-detected resource.
        # (In read-only demo mode, serve the default without persisting it.)
        default = _default_local_resource()
        if os.environ.get("THE_LAB_DEMO", "").strip().lower() not in ("1", "true", "yes"):
            save_config(repo_dir, [default], QueueConfig())
        return [default], QueueConfig()
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        mtime = -1
    with _config_cache_lock:
        if _config_cache is not None and _config_cache[0] == mtime:
            _, resources, qc = _config_cache
            return list(resources), qc
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return [], QueueConfig()
    resources = [Resource(**r) for r in data.get("resources", [])]
    qc = QueueConfig(**data.get("queue", {}))
    with _config_cache_lock:
        _config_cache = (mtime, resources, qc)
    return list(resources), qc


def save_config(repo_dir: Path, resources: list[Resource], qc: QueueConfig) -> None:
    path = _config_path(repo_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "resources": [asdict(r) for r in resources],
        "queue": asdict(qc),
    }
    jsonio.write_json(path, payload)


# ---------------------------------------------------------------------------
# Runtime allocator
# ---------------------------------------------------------------------------

class Allocator:
    """In-memory record of which experiment holds which units on which resource.

    Thread-safe; the scheduler runs in the asyncio loop but cancellation /
    rebuild paths can be invoked from other contexts.
    """

    def __init__(self) -> None:
        # exp_label -> {"resource": str, "units": [int]}
        self._allocations: dict[str, dict] = {}
        self._lock = threading.Lock()

    def reserve(self, exp_label: str, resource: Resource, count: int) -> list[int] | None:
        """Try to reserve *count* units on *resource* for *exp_label*.

        Returns the chosen unit ids (e.g., [4,5,6,7]) on success, or None if
        not enough free units exist. Honors the resource's max_parallel_jobs
        (refuses if the resource already has that many holders).
        """
        with self._lock:
            held_on_resource = [
                a for a in self._allocations.values() if a["resource"] == resource.name
            ]
            if len(held_on_resource) >= resource.max_parallel_jobs:
                return None
            taken: set[int] = set()
            for a in held_on_resource:
                taken.update(a["units"])
            free = [i for i in range(resource.capacity) if i not in taken]
            if len(free) < count:
                return None
            chosen = free[:count]
            self._allocations[exp_label] = {"resource": resource.name, "units": chosen}
            return chosen

    def release(self, exp_label: str) -> dict | None:
        with self._lock:
            return self._allocations.pop(exp_label, None)

    def get(self, exp_label: str) -> dict | None:
        with self._lock:
            return self._allocations.get(exp_label)

    def all(self) -> dict[str, dict]:
        with self._lock:
            return dict(self._allocations)

    def utilization(self, resource: Resource) -> dict:
        """Return free / in-use unit counts and holder labels for a resource."""
        with self._lock:
            held = [
                (label, a) for label, a in self._allocations.items()
                if a["resource"] == resource.name
            ]
        in_use_units = sum(len(a["units"]) for _, a in held)
        return {
            "name": resource.name,
            "capacity": resource.capacity,
            "in_use_units": in_use_units,
            "free_units": resource.capacity - in_use_units,
            "running_jobs": len(held),
            "max_parallel_jobs": resource.max_parallel_jobs,
            "default_units_per_job": resource.default_units_per_job,
            "holders": [{"experiment_label": label, "units": a["units"]} for label, a in held],
        }

    def restore_from_running(self, running_exps: list[dict]) -> None:
        """On startup, rebuild allocations from each running exp's stored meta.

        Each running exp may have ``meta.assigned_resource`` and
        ``meta.assigned_units`` from when it was scheduled. Skips entries
        without that data (older experiments started before the queue
        existed, etc.) — those run "outside" the allocator.
        """
        with self._lock:
            self._allocations.clear()
            for exp in running_exps:
                meta = exp.get("meta") or {}
                resource = meta.get("assigned_resource")
                units = meta.get("assigned_units")
                label = exp.get("label") or str(exp.get("id"))
                if resource and isinstance(units, list):
                    self._allocations[label] = {"resource": resource, "units": list(units)}


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def match_resource(
    resources: list[Resource],
    requirements: dict,
) -> Resource | None:
    """Pick the most-idle resource that satisfies *requirements*.

    requirements:
      kind:  "gpu" | "cpu" | "none" | "any" (matches Resource.unit_kind; when
             omitted or "any", any unit_kind is accepted — common for vLLM
             client experiments where the GPU is held by the server, not the
             experiment process)
      tags:  list of required tags (must all be present in resource.tags)
      units: int                     (caller-specified; if 0 or missing, uses
                                       the resource's default_units_per_job)

    Returns None if no resource satisfies the requirement.
    """
    want_kind = requirements.get("kind")
    if want_kind == "any":
        want_kind = None
    want_tags = set(requirements.get("tags") or [])
    candidates = [
        r for r in resources
        if (want_kind is None or r.unit_kind == want_kind)
        and want_tags.issubset(set(r.tags))
    ]
    if not candidates:
        return None
    # Prefer the resource whose default sizing fits the request best, then
    # the one with most absolute capacity. The allocator does the actual
    # free-unit check at reservation time.
    requested = requirements.get("units")
    if requested:
        candidates = [r for r in candidates if r.capacity >= requested] or candidates
    candidates.sort(key=lambda r: r.capacity, reverse=True)
    return candidates[0]


# ---------------------------------------------------------------------------
# Resource-list CRUD helpers (used by the routes)
# ---------------------------------------------------------------------------

# An ssh destination: [user@]host, where host is a hostname/IPv4/IPv6-in-brackets.
# Deliberately strict — a leading "-" would be parsed by ssh as an OPTION
# (e.g. -oProxyCommand=...) rather than a destination, and shell metacharacters
# would break out of the remote command string the executor builds.
_SSH_HOST_RE = re.compile(
    r"^(?:[A-Za-z0-9._-]+@)?"                      # optional user@
    r"(?:"
    r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?"  # hostname / IPv4
    r"|\[[0-9A-Fa-f:.]+\]"                        # [IPv6]
    r")$"
)

# Config values that end up inside a remote command string or an sbatch
# directive. Even with shlex.quote() at the splice points, reject the obviously
# hostile shapes at ingress so a bad resource definition fails loudly at
# definition time rather than silently at dispatch time.
_SHELL_UNSAFE = set(";|&$`\n\r<>()!*?[]{}'\"\\")
_PATHY_CONFIG_KEYS = ("slurm_conf", "remote_base", "git_repo_path", "base_venv_path")


def _validate_model_server_config(value: object) -> None:
    """Validate the optional model server launched inside a Slurm allocation.

    This deliberately supports a small, versioned surface rather than an
    arbitrary shell command.  A resource definition is editable from the API,
    so allowing a command here would turn a scheduling setting into remote code
    execution on the cluster login host.
    """
    if value is None:
        return
    if not isinstance(value, dict):
        raise ValueError("executor_config.model_server must be an object")
    if value.get("kind") != "qwen38_flash_sglang":
        raise ValueError(
            "executor_config.model_server.kind must be 'qwen38_flash_sglang'"
        )
    for key in ("model_inference_root", "api_key_file"):
        item = value.get(key)
        if item is None:
            continue
        if not isinstance(item, str) or not item:
            raise ValueError(f"executor_config.model_server.{key} must be a non-empty string")
        if _SHELL_UNSAFE.intersection(item):
            raise ValueError(
                f"executor_config.model_server.{key} contains shell metacharacters"
            )
    for key, minimum, maximum in (("port", 1024, 65535), ("readiness_timeout_s", 1, 3600)):
        item = value.get(key)
        if item is None:
            continue
        if isinstance(item, bool) or not isinstance(item, int) or not minimum <= item <= maximum:
            raise ValueError(
                f"executor_config.model_server.{key} must be an integer in [{minimum}, {maximum}]"
            )


def _validate_executor_config(cfg: dict) -> None:
    """Reject executor_config values that would escape into the remote shell.

    ``executor_config`` used to be accepted with no validation at all, while
    ``ssh_host``/``slurm_conf``/``remote_base``/``git_repo_path`` were spliced
    unescaped into strings handed to ``ssh`` — which runs them through the
    remote user's shell. Anyone able to define a resource could therefore run
    arbitrary commands on the configured host.
    """
    if not isinstance(cfg, dict):
        raise ValueError("executor_config must be an object")

    host = cfg.get("ssh_host")
    if host is not None:
        if not isinstance(host, str) or not host.strip():
            raise ValueError("executor_config.ssh_host must be a non-empty string")
        if host.strip() != "local" and not _SSH_HOST_RE.match(host.strip()):
            raise ValueError(
                f"executor_config.ssh_host '{host}' is not a valid [user@]host "
                "(or 'local'; letters, digits, dot, dash, underscore; no leading '-', no "
                "shell metacharacters)"
            )

    for key in _PATHY_CONFIG_KEYS:
        value = cfg.get(key)
        if value is None:
            continue
        if not isinstance(value, str):
            raise ValueError(f"executor_config.{key} must be a string")
        # "$HOME" is a supported, documented placeholder here (the executor
        # expands it via _resolve_remote_base), and it is the built-in default
        # for remote_base — so allow exactly that token while still rejecting
        # any other use of "$".
        probe = value.replace("$HOME", "")
        bad = _SHELL_UNSAFE.intersection(probe)
        if bad:
            raise ValueError(
                f"executor_config.{key} contains disallowed character(s) "
                f"{''.join(sorted(bad))!r} — paths must not contain shell metacharacters"
            )

    callback = cfg.get("callback_url")
    if callback is not None:
        if not isinstance(callback, str):
            raise ValueError("executor_config.callback_url must be a string")
        parsed = urlparse(callback)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(
                "executor_config.callback_url must be an http(s) URL"
            )
        if _SHELL_UNSAFE.intersection(callback):
            raise ValueError(
                "executor_config.callback_url contains shell metacharacters"
            )

    _validate_model_server_config(cfg.get("model_server"))
    from .executors.cpu_profile import validate_cpu_profile
    validate_cpu_profile(cfg)


# Upper bound on a resource's declared unit count. Unbounded capacity let one
# API call schedule an arbitrary number of parallel jobs on the host.
try:
    MAX_RESOURCE_CAPACITY = int(os.environ.get("THE_LAB_MAX_RESOURCE_CAPACITY", "") or 1024)
except ValueError:
    MAX_RESOURCE_CAPACITY = 1024


def _validate_resource(r: Resource) -> None:
    if not r.name or "/" in r.name or " " in r.name:
        raise ValueError(f"invalid resource name '{r.name}'")
    if r.capacity < 1:
        raise ValueError("capacity must be >= 1")
    if r.capacity > MAX_RESOURCE_CAPACITY:
        raise ValueError(
            f"capacity {r.capacity} exceeds the maximum of {MAX_RESOURCE_CAPACITY} "
            "(raise THE_LAB_MAX_RESOURCE_CAPACITY if this machine really has "
            "that many units)"
        )
    if r.jobs_per_unit <= 0:
        raise ValueError("jobs_per_unit must be > 0")
    if r.jobs_per_unit > 1.0:
        raise ValueError(
            "jobs_per_unit > 1.0 (unit sharing) is not supported in v1"
        )
    if r.kind not in ("local", "slurm"):
        raise ValueError(f"resource kind '{r.kind}' is not supported (use 'local' or 'slurm')")
    if r.unit_kind not in ("gpu", "cpu", "none"):
        raise ValueError(f"unit_kind must be one of: gpu, cpu, none")
    _validate_executor_config(r.executor_config or {})


def upsert_resource(repo_dir: Path, resource: Resource) -> Resource:
    _validate_resource(resource)
    resources, qc = load_config(repo_dir)
    out = [r for r in resources if r.name != resource.name]
    out.append(resource)
    save_config(repo_dir, out, qc)
    return resource


def remove_resource(repo_dir: Path, name: str) -> bool:
    resources, qc = load_config(repo_dir)
    new = [r for r in resources if r.name != name]
    if len(new) == len(resources):
        return False
    save_config(repo_dir, new, qc)
    return True
