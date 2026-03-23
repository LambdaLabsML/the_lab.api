# {Title}

## Goal

{What you're trying to achieve. One paragraph. Be specific about the success criteria.}

## Background

{Why this matters. Prior work, context, constraints, relevant papers/links. As much or as little as needed.}

## Setup

{Environment details: hardware, software, data locations, how to run things. Anything the agent needs to get started.}

---

## How Experiments Work

You have access to a local experiment management API at `http://localhost:8000/api/v1`. Use it to structure your work. Every endpoint is fully documented — run `GET /openapi.json` or open the **API tab** in the dashboard for descriptions, parameters, and examples.

### Core concepts

- **Idea** — a research direction with its own **git branch** (`idea/<id>`). Ideas form a DAG: branch from parents, or merge multiple. Status: `active` → `concluded`/`abandoned`. Concluded ideas can be reopened.
- **Suggested idea** — human-submitted (`status: "suggested"`). Adopt or abandon with a note.
- **Experiment** — a bash script run under an idea. Carries `meta` (hyperparams, data), `tags`, and produces `metrics`. Scripts without JSON output succeed as setup tasks.
- **Notes** — journal on an idea. Levels: `insight` (key findings), `milestone` (progress), `observation` (what happened), `debug` (troubleshooting).

### Research workflow

**Start every session by checking the leaderboard** — `GET /leaderboard?metric=<key>` returns a compact summary of top experiments, open ideas, running experiments, key insights, and global best metrics. Add `&include_details=true` to see full per-problem metrics and experiment settings.

Then check for human suggestions — `GET /ideas?status=suggested`. Adopt feasible ones, abandon infeasible ones with a note.

The core loop:

1. **Search existing ideas** → `GET /ideas/search?q=keyword1,keyword2,...` — before creating an idea, search for similar ones by keywords. Filter with `&status=concluded&metric=accuracy_per_mtoken&min_metric=4.0` to find what actually worked. Review their experiment metrics to avoid duplicating work and to build on prior results.
2. **Create idea** → `POST /ideas/new {parent_ids, description}` — creates git branch
3. **Checkout** → `POST /ideas/<id>/checkout` — auto-commits current work, switches branch
4. **Create experiment** → `POST /ideas/<id>/experiments {description, script_content, meta, tags}`
5. **Start** → `POST /experiments/<id>/start {timeout?}` — runs script in isolated worktree
6. **Monitor** → `GET /experiments/<id>/progress`, `GET /experiments/<id>/log?tail=50`
7. **Wait** → `GET /wait?experiment_id=<id>` — blocks until finished
8. **Compare with best** → `GET /leaderboard?metric=<key>&include_details=true` — compare your experiment's metrics against the current leaderboard and best-known results. Note whether you improved, matched, or regressed relative to the previous best. Use `GET /experiments/compare?ids=<yours>,<best>` to see exactly what config changed.
9. **Note findings** → `POST /ideas/<id>/note {text, level}` — include how results compare to the previous best
10. **Conclude** → `POST /ideas/<id>/conclude {conclusion}` — then branch into next idea

### Script contract

Scripts must print `{"metrics": {...}}` as their **last stdout line** (or omit for setup tasks). Optional extras:
- `$THE_LAB_PROGRESS` — write progress JSON for live monitoring
- `$THE_LAB_METRICS` — append JSONL for per-step training curves
- `.the_lab/preamble.sh` — auto-sourced before every script (venv activation, etc.)

### Git integration

Each idea is a git branch. The server manages branching automatically:
- `POST /ideas/new` creates a branch from the parent idea(s)
- `POST /ideas/<id>/checkout` auto-commits + stashes, then switches
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
      {exp_id}.json        # experiment metadata + results
      {exp_id}.sh          # experiment script
      {exp_id}.log         # stdout+stderr
      {exp_id}.progress    # optional progress JSON
      {exp_id}.metrics.jsonl  # optional per-step time-series
      {exp_id}.err         # error details
```

### API reference

All endpoints are documented with descriptions, parameters, and examples in the OpenAPI spec. Access it via:
- **Dashboard** → API tab (interactive explorer with Send button)
- **Spec** → `GET /openapi.json`
- **Docs** → `GET /docs` (Swagger UI)
