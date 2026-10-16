# Opt-in CPU profile

The `paired_24` resource profile requests 48 Slurm CPUs and two threads per core, then verifies an actual mask of 24 complete SMT pairs. It is off unless explicitly configured on a resource. Existing resources and generated wrappers remain unchanged when the profile is absent.

The complete resource configuration is:

```json
{
  "cpu_profile": "paired_24",
  "cpus_per_task": 48,
  "threads_per_core": 2,
  "cpu_bind": "cores",
  "ntasks": 1
}
```

Counts must be positive integers, booleans are rejected, and this version supports only that exact profile. CPU fields without the profile are rejected instead of silently ignored. This is resource configuration, not experiment metadata or a global environment change.

The batch wrapper emits `--nodes=1 --ntasks=1 --cpus-per-task=48 --threads-per-core=2`. Before a venv, model launcher or experiment script starts, a stdlib probe checks its inherited affinity, sysfs physical/core IDs and complete thread-sibling lists. The CPU cgroup must identify the current Slurm job. The server verifies job membership from the host namespace through the bounded handshake described below.

The model launcher uses `--cpu-bind=verbose,cores` and repeats the same CPU request. Its actual container command runs the probe before SGLang imports. It must match the successful batch receipt's exact allowed CPUs, core pairs, job ID and native restart generation. The probe is mounted read-only and verified by SHA256; only the receipt directory is writable. No thread counts or sampling parameters are changed.

Receipts are written atomically under the job's `cpu_profile/` directory. Failed checks retain an error receipt and stop before model startup. The wrapper archives this directory on native restart, and every receipt carries the restart count. `cpu_profile_probe.py` is a launcher-owned stable artifact; it is not supplied by model output.

This does not guarantee exclusive cores when the partition permits sharing, nor does it infer historical CPU utilization. Default-off byte parity and fake topology tests do not establish that a particular future Slurm allocation will pass the gates. The requested 48-CPU count is larger than the historical implicit 20-CPU reservation.

Configure the profile only on the resource intended for paired24 jobs, with a distinct positive routing tag. Resources without this profile retain their existing CPU allocation and launcher behavior.

The paired24 server gate uses a host attestation handshake because production
Pyxis gives the container a cgroup namespace whose self path is `/`. The
container probe remains alive before model imports while a host probe verifies
its actual PID, process start ticks, shared host PID namespace, numeric server
step cgroup, and exact successful batch CPU/core/job/restart tuple. A fresh
256-bit launch nonce and canonical challenge digest bind the atomic host ACK.
Both sides have 90-second bounded waits; stale/missing evidence, a vanished or
zombie srun launcher, or a rejected host observation prevents model startup.
Failure receipts retain the available namespace/topology observations. No bare
container-root path or environment job ID is accepted as membership proof.

Production topology is fixed at48 logical CPUs and24 complete SMT pairs. A
separate CPU-only diagnostic adapter may exercise the same handshake with one
pair; it is never copied by the backend or called by the production launcher.
The cpu_1 partition currently caps a node at42 consumable CPUs, so it cannot
validate the full48 allocation. Full-size acceptance remains the actual approved
trial's fail-closed gate. Default-off resource behavior remains unchanged.

The handshake is a consistency gate for trusted launcher/probe code, not a security boundary against another process controlled by the same Unix user. A fresh nonce is passed only into this launch, the probe declares its own PID, and the host verifies that live process in the expected job and a numeric server step. It does not independently map the local srun PID to a remote step identifier.
