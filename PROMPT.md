# {Title}

## Goal

{What you're trying to achieve. One paragraph. Be specific about the success criteria.}

## Background

{Why this matters. Prior work, context, constraints, relevant papers/links. As much or as little as needed.}

## Setup

{Environment details: hardware, software, data locations, how to run things. Anything the agent needs to get started.}

---

## How Experiments Work

You have access to a local experiment management API at `http://localhost:8000/api/v1`. Use it to structure your work.

### Concepts

All IDs are sequential integers (1, 2, 3, ...).

- **Idea** — a research direction. Ideas form a DAG: you branch from previous ideas, or merge multiple ideas together. Each idea has a description, a status (active → concluded/abandoned), and a conclusion you write when you're done with it. **Each idea maps to a git branch** (`idea/<id>`), created automatically when you create the idea. Concluded ideas can be reopened if new evidence warrants it.
- **Experiment** — a script you run under an idea. You can run multiple experiments per idea (iterate until you've learned what you need). Each experiment carries a freeform `meta` dict (data used, hyperparams, hardware, anything you want to track/filter by) and produces a JSON result with `metrics`.
- **Notes** — a running journal on the idea. Each note has a `level`:

  | Level | Use for | Shown in |
  |-------|---------|----------|
  | `insight` | Key findings, reusable knowledge | idea listings, detail view, tree |
  | `milestone` | Significant progress markers | idea listings, detail view, tree |
  | `observation` | What happened in an experiment, intermediate findings | detail view |
  | `debug` | Troubleshooting, dead ends, technical details | only with `?notes=all` |

### Git integration

Each idea is a git branch. The server manages branching automatically — you switch between ideas with checkout:

- **`POST /ideas/new`** creates a new git branch (`idea/<id>`) from the parent idea's branch (or from `main`/`HEAD` for root ideas). Does **not** check it out.
- **`POST /ideas/<id>/checkout`** saves all work on the current branch (auto-commit tracked changes, then stash anything remaining if checkout would conflict), then checks out the idea's branch. The response includes `auto_committed` and `stashed` flags so you know what happened. This is how you switch between ideas.
- **`POST /ideas/new` with multiple parents** merges the parent branches. If there are merge conflicts, the creation fails and returns the conflicts for you to resolve.

This means you can freely modify code as part of your experiments — each idea has its own branch. Always checkout an idea before running experiments on it.

### File layout

All experiment data lives under `.the_lab/experiments/` at the repo root — a fixed location that is **not** affected by git branch switches.

```
.the_lab/experiments/1/idea.json       # idea metadata: {description, status, conclusion, parent_ids, created_at}
.the_lab/experiments/1/notes.json      # [{text, level, created_at}, ...] — append-only log
.the_lab/experiments/1/1.sh            # your experiment script
.the_lab/experiments/1/1.json          # {description, meta, metrics, status, created_at, started_at, finished_at}
.the_lab/experiments/1/1.log           # full stdout+stderr log (auto-generated, streams in real-time)
.the_lab/experiments/1/1.progress      # optional progress JSON written by the script (see $THE_LAB_PROGRESS)
.the_lab/experiments/1/1.err           # error details if failed (auto-generated)
```

All timestamps are ISO 8601. You can reconstruct the full timeline of an idea by merging notes and experiments sorted by their timestamps.

### Running an experiment

1. **Create an idea** (creates a new branch, does not checkout):
   ```
   POST /ideas/new  {parent_ids: [1], description: "what you're testing"}
   → returns {id: 3, branch: "idea/3", ...}
   → merge conflict: {status: "conflict", conflicts: ["path/to/file.py", ...]}
   ```

2. **Checkout the idea** (saves current work, switches branch):
   ```
   POST /ideas/3/checkout
   → {status: "checked_out", branch: "idea/3", idea_description: "...", previous_branch: "idea/1", auto_committed: true}
   ```
   The server first tries to commit all changes. If checkout still conflicts (untracked files differ across branches, commit hook failure, etc.), it stashes remaining changes and retries. When `stashed: true`, the stash is preserved on the previous branch.

3. **Create an experiment** under the idea. You can pass the script inline or write it yourself:
   ```
   # Option A: inline script (preferred — server writes it for you)
   POST /ideas/<idea_id>/experiments  {
     description: "what this run tests",
     meta: {dataset: "train_v3", split: "val", lr: 1e-4},
     script_content: "#!/bin/bash\npython train.py --lr 1e-4\n..."
   }

   # Option B: just create the experiment, write the script yourself at the returned path
   POST /ideas/<idea_id>/experiments  {description: "...", meta: {...}}
   → returns {id: "<exp_id>", script: ".the_lab/experiments/<idea_id>/<exp_id>.sh", ...}
   ```

   The script must print a JSON object as its **last stdout line**:
   ```json
   {"metrics": {"accuracy": 0.84, ...}, "meta?": {"gpu_hours": 1.2, "actual_samples": 4832}}
   ```
   `metrics` is required. `meta` is optional — the server merges it with the experiment's existing meta dict (script values override).

   **Progress reporting** (optional): scripts can write intermediate progress to `$THE_LAB_PROGRESS` (a JSON file path set by the server). Write any JSON you like — epoch number, current loss, ETA, etc.:
   ```bash
   echo '{"epoch": 5, "loss": 0.31, "eta_minutes": 12}' > "$THE_LAB_PROGRESS"
   ```
   Poll it with `GET /experiments/<exp_id>/progress`.

4. **Start it:**
   ```
   POST /experiments/<exp_id>/start
   → success: {status: "running", current_branch: "idea/1", idea_description: "...", pid: 12345, experiment: {...}}
   → error:   {status: "error", reason: "script not found..."}
   ```
   The server auto-commits any uncommitted changes, then creates an **isolated git worktree** from the idea's branch so the experiment runs on the correct code — even if you checkout a different branch while it's running. Multiple experiments can run concurrently on different branches without interfering. Stdout/stderr stream into `.log` in real-time; the final JSON line is extracted on completion; errors go to `.err`. Worktrees are cleaned up automatically when experiments finish.

5. **Wait for an experiment to finish:**
   ```
   GET /wait?experiment_id=7            ← wait for a specific experiment (recommended)
   GET /wait?idea_id=3                  ← wait for any experiment in this idea
   GET /wait?timeout=3600               ← wait for any experiment (default timeout 3600s)
   → completed: {event: "completed", current_branch: "idea/1", idea_description: "...", experiment: {id, idea_id, metrics, meta, ...}}
   → failed:    {event: "failed", current_branch: "idea/1", idea_description: "...", experiment: {id, idea_id, error, ...}}
   → timeout:   {event: "timeout", current_branch: "idea/1", running: [list of still-running experiment ids]}
   ```
   **Tip:** always pass `experiment_id` to avoid getting stale results from unrelated experiments.

6. **Take notes on the idea** (at any time — before, during, or after experiments):
   ```
   POST /ideas/<id>/note  {text: "accuracy 84% is +2% over baseline", level: "insight"}
   POST /ideas/<id>/note  {text: "OOM at batch_size=64, dropped to 32", level: "debug"}
   ```
   Default level is `observation` if omitted.

7. **Conclude the idea** when done, then branch into a new one:
   ```
   POST /ideas/<id>/conclude  {conclusion: "what you learned"}
   ```

### Other useful endpoints

| Endpoint | What |
|----------|------|
| `GET /ideas` | List all ideas (filter: `?status=active`). Includes `insight`/`milestone` notes + `experiment_summary` with counts and `latest_metrics`. |
| `GET /ideas/<id>` | Get idea with experiments and notes (`insight` + `milestone` + `observation`). |
| `GET /ideas/<id>?notes=all` | Same but includes `debug` notes too. |
| `GET /ideas/<id>/tree` | See ancestors and descendants with `insight` + `milestone` notes. |
| `POST /ideas/<id>/checkout` | Save work (commit + stash if needed) and switch to this idea's branch. |
| `POST /ideas/<id>/abandon` | Abandon an idea (with `{reason}`). |
| `POST /ideas/<id>/reopen` | Reopen a concluded/abandoned idea (with `{reason}`). Old conclusion preserved as an `insight` note. |
| `GET /backlog` | Overview of all active work + current branch. |
| `GET /graph` | Full idea DAG. |
| `POST /experiments/<id>/restart` | Re-run a failed/cancelled experiment (same script). |
| `POST /experiments/<id>/cancel` | Kill a running experiment. |
| `GET /experiments/compare?ids=9,13,15` | Side-by-side comparison: pivoted `metrics` and `meta` tables, plus full experiment objects. Optional `&metrics=accuracy,loss` to filter metric keys. |
| `GET /experiments/<id>/progress` | Read script-reported progress JSON (falls back to `{status}` if no progress file). |
| `GET /experiments/<id>/log?tail=50` | Read experiment log (streams in real-time while running). |
