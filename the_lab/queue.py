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
import shutil
import subprocess
import threading
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


def load_config(repo_dir: Path) -> tuple[list[Resource], QueueConfig]:
    """Load resources + queue config; auto-create on first run."""
    path = _config_path(repo_dir)
    if not path.exists():
        # First run: write a default with a single auto-detected resource.
        default = _default_local_resource()
        save_config(repo_dir, [default], QueueConfig())
        return [default], QueueConfig()
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return [], QueueConfig()
    resources = [Resource(**r) for r in data.get("resources", [])]
    qc = QueueConfig(**data.get("queue", {}))
    return resources, qc


def save_config(repo_dir: Path, resources: list[Resource], qc: QueueConfig) -> None:
    path = _config_path(repo_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "resources": [asdict(r) for r in resources],
        "queue": asdict(qc),
    }
    path.write_text(json.dumps(payload, indent=2) + "\n")


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

def _validate_resource(r: Resource) -> None:
    if not r.name or "/" in r.name or " " in r.name:
        raise ValueError(f"invalid resource name '{r.name}'")
    if r.capacity < 1:
        raise ValueError("capacity must be >= 1")
    if r.jobs_per_unit <= 0:
        raise ValueError("jobs_per_unit must be > 0")
    if r.jobs_per_unit > 1.0:
        raise ValueError(
            "jobs_per_unit > 1.0 (unit sharing) is not supported in v1"
        )
    if r.kind != "local":
        raise ValueError(f"resource kind '{r.kind}' is not supported in v1 (only 'local')")
    if r.unit_kind not in ("gpu", "cpu", "none"):
        raise ValueError(f"unit_kind must be one of: gpu, cpu, none")


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
