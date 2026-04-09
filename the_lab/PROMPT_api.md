---

## How Experiments Work

You have access to a local experiment management API. Use it to structure your work.

### Core concepts

- **Idea** — a research direction with its own **git branch** (`idea/<id>`). Ideas form a DAG: branch from parents, or merge multiple. Status: `active` → `concluded`/`abandoned`. Concluded ideas can be reopened.
- **Suggested idea** — human-submitted (`status: "suggested"`). Adopt or abandon with a note.
- **Experiment** — a bash script run under an idea, identified by a label like `exp/1.2` (idea 1, experiment 2). Carries `meta` (hyperparams, data), `tags`, and produces `metrics`. Scripts without JSON output succeed as setup tasks.
- **Notes** — journal on an idea. Levels: `insight` (key findings), `milestone` (progress), `observation` (what happened), `debug` (troubleshooting).

### Research workflow

**Start every session by orienting** — `GET /orient` returns active ideas, running experiments, best score, and recommended next steps. Use `?tags=...` to filter by experiment tags.

Then check for human suggestions — `GET /ideas?status=suggested`. Adopt feasible ones, abandon infeasible ones with a note.

The core loop:

1. **Orient** → `GET /orient` — single call with current state and recommended next action.
2. **Search existing ideas** → `GET /ideas/search?q=keyword1,keyword2,...` — search by keywords with optional `status=`, `metric=`, `min_metric=` filters.
3. **Create idea** → `POST /ideas/new {parent_ids, description}` — creates git branch and **auto-checkouts** (no separate checkout call needed).
4. **Create + start experiment** → `POST /ideas/<id>/experiments {description, script_content, meta, tags}` — when `script_content` is provided, the experiment **auto-starts** (no separate start call needed).
5. **Wait** → `GET /wait?experiment_id=<id>` — blocks until finished, returns the **full experiment result** with metrics and branch diff summary. Accepts experiment labels (e.g. `1.2`).
6. **Note findings** → `POST /ideas/<id>/note {text, level}`
7. **Conclude** → `POST /ideas/<id>/conclude {conclusion}` — then branch into next idea

### Script contract

Scripts must print `{"metrics": {...}}` as their **last stdout line** (or omit for setup tasks). Optional extras:
- `$THE_LAB_PROGRESS` — write progress JSON for live monitoring
- `$THE_LAB_METRICS` — append JSONL for per-step training curves
- `.the_lab/preamble.sh` — auto-sourced before every script; use it for shared helpers

Recommended experiment script pattern:
```bash
#!/usr/bin/env bash
set -euo pipefail

source .the_lab/preamble.sh
# your experiment command here
```

### Git integration

Each idea is a git branch. The server manages branching automatically:
- `POST /ideas/new` creates a branch from the parent idea(s) and auto-checkouts
- Experiments run in isolated git worktrees (concurrent experiments don't interfere)

### File layout

```
.the_lab/
  preamble.sh              # optional: sourced before every experiment
  artifacts/               # shared datasets, checkpoints (not branch-specific)
  experiments/
    {idea_id}/
      idea.json            # idea metadata
      notes.json           # append-only journal
      {seq}.json           # experiment metadata + results
      {seq}.sh             # experiment script
      {seq}.log            # stdout+stderr
      {seq}.progress       # optional progress JSON
      {seq}.metrics.jsonl  # optional per-step time-series
```

### API reference

All endpoints are documented with descriptions, parameters, and examples in the OpenAPI spec. Access it via:
- **Dashboard** → API tab (interactive explorer with Send button)
- **Spec** → `GET /openapi.json`
- **Docs** → `GET /docs` (Swagger UI)
