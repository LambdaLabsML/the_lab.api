# Slurm checkout isolation

The executor serializes the shared bare-repository lifecycle across processes
and hosts with an atomic `mkdir` ownership lock beside the repository. The lock
covers repository initialization, branch push, worktree creation, runtime asset
staging, source attestation and `sbatch`; cleanup takes the same lock. It does
not remain held during queue residence, model startup or gameplay. It has a
bounded acquisition timeout and never steals a stale lock. A crashed owner
requires operator investigation; do not delete a lock while its owner runs.
The target topology uses shared NFSv3, so an in-process mutex alone is insufficient.

New worktrees have Git's persistent `--lock` protection. Creation neither
prunes registrations nor removes another operation's index locks. An existing
job directory or worktree is refused, not reset/reused. Use a new experiment
label after a failed submission unless an operator has resolved the preserved
artifacts. Native Slurm requeue reuses its existing allocation and emits new
per-generation attestations; it does not re-enter submit.

Attestation verifies the forward `.git` pointer, reciprocal administrative
`gitdir`, expected common repository, persistent registration lock, detached
HEAD and every tracked blob and executable/symlink mode against the requested
full commit. It hashes bytes directly rather than trusting Git index flags.
Gitlinks and transformed checkout bytes are unsupported and fail closed.
Untracked runtime assets are not certified as solver source. This is an
observed source snapshot, not protection against arbitrary external writes
between checks. Persistent registration locking and serialized cooperating
lifecycle operations prevent ordinary registration reuse; external tools must
honor that contract.

The helper is staged outside the solver tree and hash-checked. Checks run after
staging, before expensive venv/model/preamble startup, and again before script
execution. Receipts are exclusive-create JSON files. The frozen experiment
payload is unchanged. Runtime source errors exit125 before running the script.
Cleanup re-attests and refuses nonreciprocal/changed checkouts; it preserves job
logs, source receipts and failed directories. Successful cleanup removes only
the checkout and per-job virtualenv, retaining other artifacts. Disk retention
therefore changes and requires an explicit later archival policy.

## Adoption boundary

This patch is prepared in an isolated checkout. The serving installation is an
editable import of `/home/davidh/the_lab.api/the_lab` via the workspace virtualenv;
changing a different checkout does not activate it. Imported executor classes
remain cached in an existing Python service. Adoption must stop dispatch and
cleanup in *all* old executor processes sharing the bare repository, then restart
or otherwise deliberately reload the reviewed service code. A new mkdir lock
cannot coordinate with legacy processes that ignore it and delete index locks.
Do not restart or modify live solver allocations solely to install this patch.
Their already-generated wrappers do not gain the new attestation checks.

Existing queued/running worktrees created by the old executor lack persistent
locks and submit attestations. New cleanup will preserve them for explicit
operator handling, rather than treating them as certified. Never bulk-prune,
reset or repair the current collision set. Preserve forward/reverse pointer,
HEAD and byte evidence before any individually authorized recovery. This patch
does not retroactively qualify a legacy run or recover missing failure evidence.
