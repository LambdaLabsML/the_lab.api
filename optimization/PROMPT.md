# Optimizing The Lab API for Agent Efficiency

## Goal

Maximize **`api_score`** — a composite metric that measures how efficiently agents can use The Lab API to complete research tasks. The score is relative to a baseline (unmodified API = 1.0). Higher is better.

```
api_score = norm_quality × (1 - failure_rate) × (1 - confusion) / norm_cost
```

The inner agent gets a **fixed budget** of experiments (default 15). The score measures how good it got within that budget:
- `quality = -log10(1 - final_score)` — rewards pushing past plateaus (0.9→1, 0.99→2, 0.999→3)
- `norm_quality`: quality relative to baseline
- `failure_rate`: fraction of experiments that failed (broken workflows)
- `confusion_score`: retries, errors, corrections, oscillation (agent couldn't figure out the API)
- `norm_cost`: token cost relative to baseline

## Background

Analysis of 914 agent API calls across 29 Claude + 13 Codex sessions ($5,253 total) revealed:
- **80% of cost is context management** (cache read/write), not generation
- **50% of total cost ($2,636) is Bash tool calls** — agents shell out to curl
- **Each API call via curl costs ~$0.39** in context overhead
- The canonical workflow is 8+ calls per idea: new → checkout → experiments → start → wait → get → note → conclude
- Many calls are always paired: create→start (49×), new→checkout (21×), wait→get (40×)

## Setup

The API code lives in `the_lab/` — modify it directly on idea branches.

**Running an evaluation:**
```bash
python .the_lab/artifacts/run_eval.py --model haiku --budget 10
```

This starts a Lab instance from the current branch's `the_lab/` code, runs an inner agent against the fast math test project, and collects metrics.

**Establishing baseline** (do this first, on unmodified code):
```bash
python .the_lab/artifacts/run_eval.py --model haiku --budget 10 2>/dev/null | \
  python3 -c "import sys,json; json.dump(json.load(sys.stdin)['metrics'], open('.the_lab/artifacts/baseline.json','w'), indent=2)"
```

**Experiment script pattern** (use absolute path since experiments run in worktrees):
```bash
#!/usr/bin/env bash
set -euo pipefail
# $THE_LAB_REPO points to the worktree with the branch's code
python "$(dirname "$0")/../../artifacts/run_eval.py" --model haiku --budget 10
```

## What to optimize

Ranked by expected impact:
1. **Eliminate redundant calls** — `auto_start=true`, `auto_checkout=true`, `/wait` returning full results
2. **Bundle orientation** — single `/orient` replacing digest + suggested + search
3. **Trim response verbosity** — `?compact=true` to strip unused fields
4. **Reduce confusion** — better error messages, more intuitive endpoint naming

## Key metrics

| Metric | Meaning |
|---|---|
| `api_score` | Composite (higher = better API) |
| `quality_log` | -log10(1 - final_score) — quality achieved within budget |
| `calls_per_idea` | API calls per research cycle |
| `confusion_score` | Agent confusion (retries, errors, corrections, oscillation) |
| `cost_total` | Token cost breakdown (bash, context, reasoning, etc.) |
| `dag_max_depth` | Did the agent build a research tree? |
| `dag_branching_ratio` | Fraction of ideas that branched from parents |

## Important notes

- **Non-determinism**: Inner agent behavior varies. Run 2-3 times, report median.
- **Baseline first**: Establish `baseline.json` before making changes.
- **One change per idea**: Don't bundle multiple optimizations.
- **Don't modify fixtures**: `run_eval.py`, `test_project/`, and `baseline.json` are in `.the_lab/artifacts/` (symlinked from `optimization/`). Don't change them.
- **Key files**: `the_lab/app.py` (endpoints), `the_lab/store.py` (data), `the_lab/runner.py` (execution), `the_lab/stats.py` (tracking)

---

## How Experiments Work

You have access to a local experiment management API at `http://localhost:9000/api/v1`. Use it to structure your work.

### Core concepts

- **Idea** — a research direction with its own **git branch** (`idea/<id>`). Ideas form a DAG: branch from parents, or merge multiple. Status: `active` → `concluded`/`abandoned`.
- **Experiment** — a bash script run under an idea. Produces `metrics` as JSON.
- **Notes** — journal on an idea: `insight`, `milestone`, `observation`, `debug`.

### Research workflow

**Start every session by checking the leaderboard** — `GET /leaderboard?metric=api_score`.

The core loop:

1. **Search** → `GET /ideas/search?q=...`
2. **Create idea** → `POST /ideas/new {parent_ids, description}`
3. **Checkout** → `POST /ideas/<id>/checkout`
4. **Modify** `the_lab/app.py` (or store.py, runner.py, etc.)
5. **Create experiment** → `POST /ideas/<id>/experiments {description, script_content, meta, tags}`
6. **Start** → `POST /experiments/<id>/start`
7. **Wait** → `GET /wait?experiment_id=<id>`
8. **Compare** → `GET /leaderboard?metric=api_score&include_details=true`
9. **Note** → `POST /ideas/<id>/note {text, level}`
10. **Conclude** → `POST /ideas/<id>/conclude {conclusion}`

### Script contract

Scripts must print `{"metrics": {...}}` as their **last stdout line**.

### API reference

`GET /openapi.json` or `GET /docs`
