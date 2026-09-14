# Slurm preemption and native restarts

Slurm owns requeueing. The Lab submits a batch job once with `--requeue`;
preemption must not cause the Lab scheduler to submit a second job.

For QoS-based preemption, the cluster must enable `PreemptType=preempt/qos`
and configure the victim QoS for `REQUEUE`. The preempting QoS must explicitly
be allowed to preempt the victim; priority alone is not sufficient.

The checked cluster has `JobRequeue=1`, `PreemptMode=REQUEUE`, and `lowprio`
configured for requeue. Its `dev`, `short`, `mid`, and `long` QoS entries can
preempt `lowprio`. These are observations, not configuration changes made by
the Lab.

## Lifecycle

Slurm restarts the batch script from the beginning using the same job ID.
`SLURM_RESTART_COUNT` identifies successive attempts. No checkpoint/resume
protocol is required: the experiment and any allocation-local model server
start again.

The reset concerns Lab-managed attempt files, not arbitrary application state
or shared model caches. Experiment scripts should initialize their own mutable
state on entry and should not automatically resume an application checkpoint
when a clean restart is desired.

While a native job is waiting to restart, the Lab retains the experiment's
`running` status and its allocator reservation. Here `running` means managed
by Slurm, not necessarily currently executing on a GPU. Returning it to the
Lab's dispatch queue would create a duplicate submission. The dashboard
shows the Slurm state and restart count separately.

SIGTERM is not itself evidence of preemption. Cancellation, timeout, and
failure must be distinguished using scheduler state, and missing queue
output or a failed scheduler query must not be interpreted as success.

Attempt-specific progress, metrics, readiness, and output must not carry over
as results of the next attempt. Callbacks from earlier attempts must not
overwrite the current attempt. Worktree cleanup belongs after authoritative
terminal confirmation, never inside a wrapper that Slurm may restart.

For live progress, writing JSON to `$THE_LAB_PROGRESS` remains the simplest
integration: the wrapper sends attempt-aware callbacks. Custom Slurm clients
that post progress directly must include `X-The-Lab-Slurm-Job-ID` and
`X-The-Lab-Restart-Count`, populated from `SLURM_JOB_ID` and
`${SLURM_RESTART_COUNT:-0}`. An unfenced callback cannot safely identify the
current attempt. Local, non-Slurm progress is unaffected.

## Deployment and compatibility

Each new Slurm submission stages a dispatch-time `experiment.json` snapshot
in the job directory, beside the metrics/progress files. Project preambles
can read configuration from this snapshot just as they do for local runs;
settings must not depend on duplicate `export` lines in the experiment script.
Snapshot staging failures prevent submission. Native requeues reuse the same
snapshot, and result synchronization never overwrites the authoritative local
experiment record with the remote copy.

`THE_LAB_EXPERIMENT_JSON_REQUIRED=1` identifies wrappers requiring the snapshot.
The ARC preamble fails before model startup if the required snapshot is missing
or malformed. It imports valid uppercase configuration names, excluding
reserved `THE_LAB_*` and `SLURM_*` control variables, after workspace defaults.
Old submitted wrappers are not retroactively upgraded by this change.

Already submitted jobs contain their original batch script. Updating the API
does not rewrite those scripts. The complete restart behavior applies to jobs
submitted with the updated wrapper; do not assume existing jobs have acquired
the new cleanup or attempt-isolation behavior.

This change does not rerun or repair historical experiment records and does
not change cluster QoS settings. Restarting the API or publishing dashboard
assets is a separate deployment step.

## Validation

Run the offline regression suite with:

```bash
python -m unittest discover -s tests -p 'test_slurm*.py' -v
```

The tests use temporary experiment directories, simulated scheduler responses,
and local wrapper execution. They do not submit jobs or force preemption on a
cluster. A live QoS-preemption smoke test remains a separate deployment check.

See the official [sbatch documentation](https://slurm.schedmd.com/sbatch.html)
for `--requeue` and `SLURM_RESTART_COUNT`, and the
[preemption documentation](https://slurm.schedmd.com/preempt.html) for cluster
configuration.
