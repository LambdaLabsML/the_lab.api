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

- **Idea** — a research direction. Ideas form a DAG: you branch from previous ideas, or merge multiple ideas together. Each idea has a description, a status (active → concluded/abandoned), and a conclusion you write when you're done with it. **Each idea maps to a git branch** (`idea/<id>`), created automatically when you create the idea. Concluded ideas can be reopened if new evidence warrants it. Ideas may have a `source` ("agent" or "human") and attached `resources` (URLs with labels).
- **Suggested idea** — a human-submitted idea with status `suggested`. These appear in the backlog and take priority over agent-generated ideas. You adopt them (promoting to `active` and creating a git branch) or abandon them with a note explaining why.
- **Experiment** — a script you run under an idea. You can run multiple experiments per idea (iterate until you've learned what you need). Each experiment carries a freeform `meta` dict (data used, hyperparams, hardware, anything you want to track/filter by), optional `tags` (list of strings for categorization), and produces a JSON result with `metrics`. Experiments can succeed without metrics (for setup tasks like data download).
- **Notes** — a running journal on the idea. Each note has a `level` and optional `resources`:

  | Level | Use for | Shown in |
  |-------|---------|----------|
  | `insight` | Key findings, reusable knowledge | idea listings, detail view, tree, digest |
  | `milestone` | Significant progress markers | idea listings, detail view, tree |
  | `observation` | What happened in an experiment, intermediate findings | detail view |
  | `debug` | Troubleshooting, dead ends, technical details | only with `?notes=all` |

### Git integration

Each idea is a git branch. The server manages branching automatically — you switch between ideas with checkout:

- **`POST /ideas/new`** creates a new git branch (`idea/<id>`) from the parent idea's branch (or from `main`/`HEAD` for root ideas). Does **not** check it out. Returns `similar_ideas` if existing ideas have overlapping descriptions.
- **`POST /ideas/<id>/checkout`** saves all work on the current branch (auto-commit tracked changes, then stash anything remaining if checkout would conflict), then checks out the idea's branch. The response includes `auto_committed` and `stashed` flags so you know what happened. This is how you switch between ideas.
- **`POST /ideas/new` with multiple parents** merges the parent branches. If there are merge conflicts, the creation fails and returns the conflicts for you to resolve.

This means you can freely modify code as part of your experiments — each idea has its own branch. Always checkout an idea before running experiments on it.

### File layout

All experiment data lives under `.the_lab/experiments/` at the repo root — a fixed location that is **not** affected by git branch switches. Shared artifacts (datasets, checkpoints) go in `.the_lab/artifacts/`.

```
.the_lab/experiments/1/idea.json       # idea metadata: {description, status, conclusion, parent_ids, source, priority, resources, created_at}
.the_lab/experiments/1/notes.json      # [{text, level, resources?, created_at}, ...] — append-only log
.the_lab/experiments/1/1.sh            # your experiment script
.the_lab/experiments/1/1.json          # {description, meta, metrics, status, tags, created_at, started_at, finished_at}
.the_lab/experiments/1/1.log           # full stdout+stderr log (auto-generated, streams in real-time)
.the_lab/experiments/1/1.progress      # optional progress JSON written by the script (see $THE_LAB_PROGRESS)
.the_lab/experiments/1/1.metrics.jsonl # optional per-step time-series metrics (see $THE_LAB_METRICS)
.the_lab/experiments/1/1.err           # error details if failed (auto-generated)
.the_lab/artifacts/                    # shared datasets, checkpoints, etc. (not branch-specific)
.the_lab/preamble.sh                   # optional: sourced at the start of every experiment script
```

All timestamps are ISO 8601. You can reconstruct the full timeline of an idea by merging notes and experiments sorted by their timestamps.

### Running an experiment

1. **Create an idea** (creates a new branch, does not checkout):
   ```
   POST /ideas/new  {parent_ids: [1], description: "what you're testing"}
   → returns {id: 3, branch: "idea/3", similar_ideas?: [...], ...}
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
     tags: ["training", "ablation"],
     script_content: "#!/bin/bash\npython train.py --lr 1e-4\n..."
   }

   # Option B: just create the experiment, write the script yourself at the returned path
   POST /ideas/<idea_id>/experiments  {description: "...", meta: {...}}
   → returns {id: "<exp_id>", script: ".the_lab/experiments/<idea_id>/<exp_id>.sh", ...}
   ```

   The script must print a JSON object as its **last stdout line** (or omit it for setup tasks):
   ```json
   {"metrics": {"accuracy": 0.84, ...}, "meta?": {"gpu_hours": 1.2, "actual_samples": 4832}}
   ```
   `metrics` is required for research experiments. `meta` is optional — the server merges it with the experiment's existing meta dict (script values override). If the script exits 0 with no JSON, the experiment is marked completed with `metrics: null` (useful for setup tasks).

   **Progress reporting** (optional): scripts can write intermediate progress to `$THE_LAB_PROGRESS` (a JSON file path set by the server). Write any JSON you like — epoch number, current loss, ETA, etc.:
   ```bash
   echo '{"epoch": 5, "loss": 0.31, "eta_minutes": 12}' > "$THE_LAB_PROGRESS"
   ```
   Poll it with `GET /experiments/<exp_id>/progress`.

   **Time-series logging** (optional): scripts can append per-step metrics to `$THE_LAB_METRICS` (a JSONL file path). Each line is one data point:
   ```python
   import json, os
   metrics_path = os.environ.get("THE_LAB_METRICS")
   if metrics_path:
       with open(metrics_path, "a") as f:
           f.write(json.dumps({"step": step, "train_loss": loss, "lr": lr}) + "\n")
   ```
   Read with `GET /experiments/<exp_id>/timeseries`. Compare curves across experiments with `GET /experiments/compare-curves?ids=7,12&key=train_loss`.

4. **Start it:**
   ```
   POST /experiments/<exp_id>/start  {timeout?: 1200}
   → success: {status: "running", current_branch: "idea/1", idea_description: "...", pid: 12345, experiment: {...}}
   → error:   {status: "error", reason: "script not found..."}
   ```
   The server auto-commits any uncommitted changes before launching (so `git_commit` in the experiment meta reflects exactly the code being tested), then runs the script in the repo directory on the currently checked-out branch. Stdout/stderr stream into `.log` in real-time; the final JSON line is extracted on completion; errors go to `.err`. Optional `timeout` (seconds) kills the experiment if it exceeds the budget.

5. **Monitor progress** (optional, while the experiment runs):
   ```
   GET /experiments/<exp_id>/progress   → script-reported progress (epoch, loss, ETA, etc.)
   GET /experiments/<exp_id>/log?tail=50 → live stdout/stderr tail
   GET /experiments/<exp_id>/timeseries  → per-step training metrics (if the script logs to $THE_LAB_METRICS)
   ```

6. **Wait for the experiment to finish:**
   ```
   GET /wait?experiment_id=<exp_id>&timeout=3600
   → completed: {event: "completed", current_branch: "idea/1", idea_description: "...", experiment: {id, idea_id, metrics, meta, ...}}
   → failed:    {event: "failed", current_branch: "idea/1", idea_description: "...", experiment: {id, idea_id, error, ...}}
   → timeout:   {event: "timeout", current_branch: "idea/1", running: [list of still-running experiment ids]}
   ```
   Always pass `experiment_id` to avoid getting stale results from unrelated experiments. You can also wait by idea (`?idea_id=3`) or for any experiment (omit both filters).

7. **Take notes on the idea** (at any time — before, during, or after experiments):
   ```
   POST /ideas/<id>/note  {text: "accuracy 84% is +2% over baseline", level: "insight"}
   POST /ideas/<id>/note  {text: "OOM at batch_size=64, dropped to 32", level: "debug"}
   POST /ideas/<id>/note  {text: "Found a better approach", level: "insight", resources: [{url: "https://...", label: "Paper"}]}
   ```
   Default level is `observation` if omitted.

8. **Conclude the idea** when done, then branch into a new one:
   ```
   POST /ideas/<id>/conclude  {conclusion: "what you learned"}
   ```

### Human-suggested ideas

Humans can suggest ideas via the dashboard or API. These appear with `status: "suggested"`:

```
GET /ideas?status=suggested    → list pending suggestions
POST /ideas/<id>/adopt         → promote to active, create git branch
POST /ideas/<id>/abandon       → reject with reason
```

### Research digest

**Start every session by checking the digest** — it gives you a compact summary of everything that's been tried and learned so far, without having to read through every idea individually:
```
GET /digest  → {total_ideas, best_metrics, concluded_ideas (with conclusions + insights), key_insights, ...}
```

### Other endpoints

#### Orientation — understand the current state

| Endpoint | What |
|----------|------|
| `GET /digest` | **Start here.** Compact research summary: concluded ideas with conclusions + insights, global best metrics, abandoned ideas with reasons. |
| `GET /backlog` | Active work overview: running/pending counts per idea, suggested ideas, current branch. |
| `GET /ideas` | All ideas with `insight`/`milestone` notes + `experiment_summary`. Filter: `?status=active`, `?source=human`. |
| `GET /ideas/<id>` | Full idea detail: experiments, notes (`insight` + `milestone` + `observation`). Add `?notes=all` for `debug` too. |
| `GET /ideas/<id>/tree` | Ancestors and descendants with key notes. |
| `GET /graph` | Full idea DAG (for the dashboard). |

#### Idea management

| Endpoint | What |
|----------|------|
| `POST /ideas/<id>/checkout` | Save work (commit + stash if needed) and switch to this idea's branch. |
| `POST /ideas/<id>/abandon` | Abandon an idea (with `{reason}`). Works on `active` and `suggested` ideas. |
| `POST /ideas/<id>/reopen` | Reopen a concluded/abandoned idea (with `{reason}`). Old conclusion preserved as an `insight` note. |

#### Monitoring — track running experiments

| Endpoint | What |
|----------|------|
| `GET /experiments/<id>/progress` | Script-reported progress JSON (epoch, loss, ETA). Falls back to `{status}` if no progress file. |
| `GET /experiments/<id>/log?tail=50` | Live stdout/stderr (streams in real-time while running). |
| `GET /experiments/<id>/timeseries` | Per-step training metrics. Optional `?keys=loss,lr` to filter, `?last=100` for recent only. |

#### Comparison — analyze results across experiments

| Endpoint | What |
|----------|------|
| `GET /experiments/compare?ids=9,13,15` | Side-by-side: pivoted `metrics` and `meta` tables, plus full experiment objects. Optional `&metrics=accuracy,loss`. |
| `GET /experiments/compare-curves?ids=9,13&key=train_loss` | Overlay training curves across experiments. |

#### Experiment lifecycle

| Endpoint | What |
|----------|------|
| `POST /experiments/<id>/restart` | Re-run a failed/cancelled experiment (same script). |
| `POST /experiments/<id>/cancel` | Kill a running experiment. |
