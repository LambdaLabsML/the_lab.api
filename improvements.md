# API / Workflow Improvements

## Idea Branch Provenance

- When creating or updating an idea, optionally require a real git commit on the idea branch.
- Show a warning in the API/UI if an idea branch points to the same commit as its parent, so "script-only" ideas are obvious.
- Add an endpoint to "materialize experiment logic into branch" for cases where generated experiment scripts encode the novelty but the branch itself does not.

## Experiment Commit Fidelity

- `GET /experiments/{id}` should expose both:
  - the commit recorded at experiment creation time
  - the actual checked-out commit in the detached worktree at start time
- If the start-time worktree commit differs from the recorded commit, surface that explicitly instead of showing stale metadata.
- Consider pinning experiments to an immutable commit at creation time unless the caller explicitly requests "start from latest branch tip".

## Queue Reliability

- Add a first-class server-side queue primitive instead of relying on ad hoc Python wait/start wrappers.
- Add durable queue state, resume-on-restart, and automatic advance on terminal experiment status.
- Emit a heartbeat or "still waiting on exp X" event so long-running waits do not look stalled.
- Provide a queue endpoint that skips already-terminal experiments without requiring client-side polling logic.

## Detached Worktree Environment

- Detached experiment worktrees should inherit required local support paths automatically:
  - vendored training dependencies
  - shared reward function files
  - shared artifact files such as dataset subsets
- Add a validated experiment preflight step that checks:
  - referenced files exist inside the worktree or via approved shared absolute paths
  - reward function paths resolve
  - required vendor directories are available

## Experiment Script Safety

- Generated experiment scripts should default to unique run names, ideally suffixing `exp{id}`, to prevent accidental output-directory reuse across reruns.
- Add a pre-start validator for common collision risks:
  - reused output directories
  - reused run names
  - missing dataset subset files
- Surface script lint / shell syntax validation as part of experiment creation.

## Result Integrity

- Add an API-side way to invalidate an experiment without deleting all history, so bad runs can be excluded from leaderboards while preserving auditability.
- Mark experiments that terminated before meaningful eval as `invalid` rather than overloading `failed`.
- Add provenance fields for:
  - training data roots
  - eval subset file
  - reward function path
  - adapter init path

## Analysis Support

- Add first-class leaderboard columns for:
  - `oracle_accuracy`
  - `vote_efficiency`
  - `missed_oracle_given_oracle_rate`
  - `group_accuracy_per_mtoken`
- Add built-in "compare experiments" endpoints that decompose `group_accuracy` into oracle and conversion terms.
