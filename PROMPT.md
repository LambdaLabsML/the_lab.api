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

- **Idea** — a research direction. Ideas form a DAG: you branch from previous ideas, or merge multiple ideas together. Each idea has a description, a status (active → concluded/abandoned), and a conclusion you write when you're done with it. **Each idea maps to a git branch** (`idea/<id>`). Concluded ideas can be reopened if new evidence warrants it.
- **Experiment** — a script you run under an idea. You can run multiple experiments per idea (iterate until you've learned what you need). Each experiment carries a freeform `meta` dict (data used, hyperparams, hardware, anything you want to track/filter by) and produces a JSON result with `metrics`. Experiments under the same idea run sequentially (they share one branch). Experiments across different ideas can run concurrently.
- **Notes** — a running journal on the idea. Each note has a `level`:

  | Level | Use for | Shown in |
  |-------|---------|----------|
  | `insight` | Key findings, reusable knowledge | idea listings, detail view, tree |
  | `milestone` | Significant progress markers | idea listings, detail view, tree |
  | `observation` | What happened in an experiment, intermediate findings | detail view |
  | `debug` | Troubleshooting, dead ends, technical details | only with `?notes=all` |

### Git integration

Ideas are isolated via git branches. The server manages this automatically:

- **Creating an idea** with one parent → `git branch idea/<id>` from `idea/<parent_id>`
- **Creating an idea** with multiple parents → merges the parent branches into `idea/<id>`. If there are merge conflicts, the creation fails and returns the conflicts for you to resolve.
- **Creating a root idea** (no parents) → branches from the current `main`/`HEAD`
- **Running an experiment** → the script executes in the idea's branch (via worktree)
- **Concluding/abandoning** → the branch stays as a record

This means you can freely modify code as part of your experiments — each idea has its own isolated copy. Make commits on the idea branch as you iterate.

### File layout

All experiment data lives under `.the_lab/experiments/` at the repo root — a fixed location that is **not** affected by git branch switches or worktree operations. Scripts run with the idea's worktree as their working directory, so they have access to the idea's code.

```
.the_lab/experiments/1/idea.json       # idea metadata: {description, status, conclusion, parent_ids, created_at}
.the_lab/experiments/1/notes.json      # [{text, level, created_at}, ...] — append-only log
.the_lab/experiments/1/1.sh            # your experiment script
.the_lab/experiments/1/1.json          # {description, meta, metrics, status, created_at, started_at, finished_at}
.the_lab/experiments/1/1.log           # full stdout+stderr log (auto-generated, streams in real-time)
.the_lab/experiments/1/1.err           # error details if failed (auto-generated)
```

All timestamps are ISO 8601. You can reconstruct the full timeline of an idea by merging notes and experiments sorted by their timestamps.

### Running an experiment

1. **Create an idea** (or work within an existing one):
   ```
   POST /ideas/new  {parent_ids: [1], description: "what you're testing"}
   → returns {id: 3, branch: "idea/3", ...}
   → merge conflict: {status: "conflict", conflicts: ["path/to/file.py", ...]}
   ```

2. **Create an experiment** under the idea. You can pass the script inline or write it yourself:
   ```
   # Option A: inline script (preferred — server writes it for you)
   POST /ideas/<idea_id>/experiments  {
     description: "what this run tests",
     meta: {dataset: "train_v3", split: "val", lr: 1e-4},
     script_content: "#!/bin/bash\npython train.py --lr 1e-4\n..."
   }

   # Option B: just create the experiment, write the script yourself at the returned path
   POST /ideas/<idea_id>/experiments  {description: "...", meta: {...}}
   → returns {id: "<exp_id>", script: "./experiments/<idea_id>/<exp_id>.sh", ...}
   ```

   The script must print a JSON object as its **last stdout line**:
   ```json
   {"metrics": {"accuracy": 0.84, ...}, "meta?": {"gpu_hours": 1.2, "actual_samples": 4832}}
   ```
   `metrics` is required. `meta` is optional — the server merges it with the experiment's existing meta dict (script values override).

3. **Start it:**
   ```
   POST /experiments/<exp_id>/start
   → success: {status: "running", pid: 12345, experiment: {...}}
   → error:   {status: "error", reason: "script not found..."}
   ```
   The server runs the script, streams stdout/stderr into `.log` in real-time, extracts the final JSON line on completion, and writes errors to `.err`.

4. **Wait for any experiment to finish:**
   ```
   GET /wait?timeout=3600
   → completed: {event: "completed", experiment: {id, idea_id, metrics, meta, ...}}
   → failed:    {event: "failed", experiment: {id, idea_id, error, ...}}
   → timeout:   {event: "timeout", running: [list of still-running experiment ids]}
   ```

5. **Take notes on the idea** (at any time — before, during, or after experiments):
   ```
   POST /ideas/<id>/note  {text: "accuracy 84% is +2% over baseline", level: "insight"}
   POST /ideas/<id>/note  {text: "OOM at batch_size=64, dropped to 32", level: "debug"}
   ```
   Default level is `observation` if omitted.

6. **Conclude the idea** when done, then branch:
   ```
   POST /ideas/<id>/conclude  {conclusion: "what you learned"}
   ```

### Other useful endpoints

| Endpoint | What |
|----------|------|
| `GET /ideas` | List all ideas (filter: `?status=active`). Shows `insight` + `milestone` notes. |
| `GET /ideas/<id>` | Get idea with experiments and notes (`insight` + `milestone` + `observation`). |
| `GET /ideas/<id>?notes=all` | Same but includes `debug` notes too. |
| `GET /ideas/<id>/tree` | See ancestors and descendants with `insight` + `milestone` notes. |
| `POST /ideas/<id>/abandon` | Abandon an idea (with `{reason}`). |
| `POST /ideas/<id>/reopen` | Reopen a concluded/abandoned idea (with `{reason}`). Old conclusion preserved as an `insight` note. |
| `GET /backlog` | Overview of all active work. |
| `GET /graph` | Full idea DAG. |
| `POST /experiments/<id>/restart` | Re-run a failed/cancelled experiment (same script). |
| `POST /experiments/<id>/cancel` | Kill a running experiment. |
| `GET /experiments/<id>/log?tail=50` | Read experiment log (streams in real-time while running). |
