# Slurm experiment diagnostics and output

## Incident: experiments 445.3 and 445.4

Read-only inspection on 2026-09-13 established:

- The filesystem records labelled both experiments `completed`, with no metrics.
- Their corresponding Slurm jobs, 1828 and 1829, are recorded by `sacct` as
  `FAILED`, with exit status `127:0` (the batch steps agree).
- Both experiment logs report `.the_lab/preamble.sh: No such file or directory`
  followed by `_sandboxed: command not found`. The experiment body never ran,
  so these attempts did not produce the expected markdown/HTML output.
- Slurm submission staged git-tracked code and the experiment script but did
  not stage the gitignored runtime preamble that the local runner supplies.
- Both generated wrappers used port `9000` for API callbacks, although the lab
  server listened on `30033`. The CLI did not propagate its selected port to
  the runner's callback-URL generation. Consequently progress and failure
  callbacks were aimed at the wrong endpoint.
- The fallback monitor treated an empty `squeue` result as successful
  completion. Its old log heuristic did not recognise these two shell errors.
  A successful `extern` accounting step is not evidence that the batch script
  succeeded: the root/batch job records are authoritative for its exit status.

The saved wrapper's callback JSON was correctly escaped. There is no evidence
that a dashboard cache hid an output file produced by these two attempts.

These observations do not rewrite the historical records, submit replacement
jobs, change cluster configuration, or restart the live API server.
The operator explicitly chose to leave both historical records unchanged.

## Runtime and output contract

Experiments depend on more than their git commit when they source trusted,
gitignored runtime infrastructure. In this lab, the preamble resolves its own
symlink to locate the trusted `arc3_lab` checkout. Copying its contents into an
unrelated directory is not equivalent to preserving that runtime location.
Missing runtime dependencies must be reported before costly model startup;
they must never be worked around by disabling the sandbox.

The output API reads `script.output.html` (preferred) or `script.output.md`
beside the canonical experiment script in the new directory layout. Slurm
syncs the job directory, excluding its code worktree and virtual environment.
Output intended for the dashboard must reach that synced directory, including
the content of viewer links and the files they reference. A link that worked
on a compute node is not necessarily valid after relocation to the API host.

## Implemented recovery behavior

The CLI propagates its actual listening port and scheme to callback generation;
explicit callback URL overrides retain precedence. The wrapper checks its
working directory and required runtime before model startup, starts the
progress watcher before warmup, and writes an atomic, attempt-tagged
`exit-status.json`. Failure to notify the API cannot erase that exit evidence.

The monitor distinguishes an unavailable scheduler from a finished job. It
uses the matching durable status and root/batch accounting, retries unavailable
exit evidence or artifact transfers, and fences updates by Slurm job ID.
The `/pull` endpoint reports failure with HTTP 503 rather than acknowledging
an unsuccessful transfer. Transfer errors do not turn a successful script's
exit code into a computation failure.

Live sync defaults to 10 seconds (`THE_LAB_SLURM_SYNC_SECONDS`, clamped to
2–120 seconds); pending jobs are polled less frequently. Metadata records the
last successful sync and any sync error. Existing script-written progress is
preserved rather than replaced by a log-derived guess.

Canonical HTML/markdown outputs are published through temporary local files
and atomic replacement. Output links are materialized only within the job,
repository, or trusted preamble-runtime roots; arbitrary outside links are not
followed. Worktrees and virtual environments are not copied back wholesale.

The dashboard offers output before the first file arrives, distinguishes an
expected missing file from an API outage, reacts to live sync events, and keeps
previous content visible on a transient error. Opening another experiment or
closing the viewer cancels obsolete requests. Failed runs with no output offer
log/sync guidance instead of an empty view.

## Operator checks

- Configure an explicit resource `callback_url` or `THE_LAB_API_URL` when a
  reverse proxy, TLS terminator, or network routing means compute nodes cannot
  reach the API's automatically selected host and port.
- Treat a disappearing queue entry as unknown until exit evidence is
  available. Distinguish scheduler/transport failure from experiment failure.
- Preserve remote results when a pull fails. A failed transfer is not an
  acknowledgement that outputs are safe to delete.
- Inspect the experiment log when output has not appeared: model startup,
  runtime preparation, and application execution are different phases.
- Do not automatically rerun an experiment whose outcome is unknown; verify
  scheduler and artifact state first to avoid duplicate computation.
