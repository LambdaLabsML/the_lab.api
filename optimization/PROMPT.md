# Optimizing The Lab API for Agent Comprehension

## Goal

Maximize **`api_effectiveness`** — a composite metric measuring how well agents understand and use The Lab API across 4 tasks. Higher is better.

```
api_effectiveness = geometric_mean(t1_score, t2_score, t3_score, t4_score)
```

The 4 tasks test different aspects of API comprehension:
- **T1 Branching**: Can the agent find the best idea and branch from it?
- **T2 Experiment Management**: Can the agent iterate efficiently (auto_start, /wait)?
- **T3 Error Recovery**: Can the agent read logs, diagnose failures, and fix them?
- **T4 Leaderboard & Search**: Can the agent navigate efficiently using /leaderboard and /search?

Each task scores 0-1 based on correctness checks (did the right API calls happen?) with a waste penalty if API calls exceed budget.

## Background

Agents struggle with several aspects of the Lab API:
- **Branching**: Agents don't understand that branching creates a new idea with `parent_ids`, often calling wrong endpoints
- **Experiment management**: Agents don't know about `auto_start`, poll instead of using `/wait`
- **Error recovery**: Agents miss the `/log` endpoint and don't diagnose failures
- **Navigation**: Agents read every idea individually instead of using `/leaderboard` or `/search`

The optimization target is both the API code (`the_lab/`) AND the API documentation (`PROMPT_api.md`). Better endpoints + better docs = better agent comprehension.

## Setup

The API code lives in `the_lab/` and API docs in `PROMPT_api.md` — modify both on idea branches.

**Running an evaluation:**
```bash
python .the_lab/artifacts/run_eval.py --model haiku --budget 4 --tests t1,t2,t3,t4
```

Runs 4 tests concurrently, each starting a Lab instance from the current branch's code + PROMPT_api.md, launching an inner agent against a pre-seeded math kernel project, and scoring API usage patterns.

**Experiment script pattern:**
```bash
#!/usr/bin/env bash
set -euo pipefail
python "$(dirname "$0")/../../artifacts/run_eval.py" --model haiku --budget 4 --tests t1,t2,t3,t4
```

## What to optimize

Two levers per idea:

### 1. API code (`the_lab/routes/`, `the_lab/deps.py`, `the_lab/schemas.py`)
- Better error messages that tell the agent what to do next
- Richer responses that include context (e.g., branch diff in /wait)
- New convenience endpoints (e.g., /orient with recommendations)
- Default behaviors that reduce call count (auto_start, auto_checkout)

### 2. API documentation (`PROMPT_api.md`)
- Clearer workflow descriptions
- Better examples showing the correct call sequence
- Explicit mention of features agents miss (branching, /search, /log)
- Shorter, more scannable format

## Optimization objective

**Minimize `total_cost`** (token cost across all 4 tests) while maintaining `api_effectiveness` ≥ 0.78 (idea/35 baseline median: 0.7979).

Cost baseline (idea/35): median $69.3, range [$62-$110], median 544 turns across 4 tests.

Cost is dominated by context tokens (59%) which grow with each agent turn. Reducing turns or shrinking per-turn context are the main levers. API response sizes accumulate in the agent's conversation context.

## Key metrics

| Metric | Meaning |
|---|---|
| `total_cost` | **PRIMARY** — token cost across all 4 tests (lower = better) |
| `api_effectiveness` | **CONSTRAINT** — must stay ≥ 0.78 (geometric mean of T1-T4) |
| `t1_score` | Branching: correct parent, used /orient or /leaderboard |
| `t2_score` | Experiment mgmt: used /wait, auto_start, documented findings |
| `t3_score` | Error recovery: read logs, created fixes |
| `t4_score` | Navigation: used /leaderboard, /search, chose best direction |
| `total_api_calls` | Total calls across all 4 tests |

## Important notes

- **Non-determinism**: Inner agent behavior varies. Run 2-3 times, report median.
- **One change per idea**: Don't bundle API changes + doc changes. Test each independently.
- **Two levers**: Always consider whether the improvement should be in the code or the docs.
- **Don't modify fixtures**: Test fixtures in `optimization/tests/` are static.
- **Key files**: `the_lab/routes/*.py` (endpoints), `the_lab/deps.py` (helpers), `the_lab/schemas.py` (models), `PROMPT_api.md` (agent-facing docs)
- **Cost reduction strategies**: Smaller API responses (less context per turn), fewer agent turns (more decisive guidance), removing redundant data from responses

---

## How Experiments Work

You have access to a local experiment management API at `http://localhost:9000/api/v1`. Use it to structure your work.

### Core concepts

- **Idea** — a research direction with its own **git branch**. Ideas form a DAG: branch from parents.
- **Experiment** — a bash script run under an idea, labeled as `exp/1.2` (idea 1, experiment 2). Produces `metrics` as JSON.
- **Notes** — journal on an idea: `insight`, `milestone`, `observation`, `debug`.

### Research workflow

1. **Orient** → `GET /orient` — current state + recommended next action
2. **Search** → `GET /ideas/search?q=...` — find related ideas
3. **Create idea** → `POST /ideas/new {parent_ids, description}` — auto-checkouts
4. **Modify** code in `the_lab/` and/or `PROMPT_api.md`
5. **Create experiment** → `POST /ideas/<id>/experiments {description, script_content}` — auto-starts when script_content provided
6. **Wait** → `GET /wait?experiment_id=<label>` — blocks until done, returns full result
7. **Note** → `POST /ideas/<id>/note {text, level}`
8. **Conclude** → `POST /ideas/<id>/conclude {conclusion}`

### Script contract

Scripts must print `{"metrics": {...}}` as their **last stdout line**.

### API reference

`GET /openapi.json` or `GET /docs`
