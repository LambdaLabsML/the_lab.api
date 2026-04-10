# Optimizing The Lab API for Agent Comprehension

## Goal

Maximize **`api_effectiveness`** while minimizing **`total_cost`**.

```
api_effectiveness = geometric_mean(t1, t2, t3, t4, t5, t6, t7)
```

Primary objective: `api_effectiveness` ≥ 0.78 (higher is better).
Secondary objective: minimize `total_cost` (token cost across all 7 tests).

## Background

The Lab is an experiment management API used by AI agents (Claude, Codex) to run research. Agents struggle with branching, experiment management, error recovery, navigation, API discovery, multi-branch workflows, and tag management.

The optimization target is both:
1. **API code** (`the_lab/routes/`, `the_lab/deps.py`, `the_lab/schemas.py`) — better endpoints, smarter defaults, richer responses
2. **API documentation** (`PROMPT_api.md`) — clearer workflow descriptions, better examples

## Test Suite

7 tests run concurrently. Each launches a Lab instance from the current branch's code, runs an inner agent against a pre-seeded project, and scores API usage patterns post-hoc.

| Test | What it measures | Checks | Budget |
|---|---|---|---|
| **T1 Branching** | Find best idea, branch from it | created_idea, branched_from_best, ran_experiment, score_improved, used_orient_or_leaderboard | 15 calls |
| **T2 Experiment Mgmt** | Iterate with /wait, auto_start | experiments_created, used_wait, efficient_start, score_improved, documented_findings | 25 calls |
| **T3 Error Recovery** | Read logs, diagnose, fix failures | read_logs, checked_status, experiments_fixed, score_improved, documented_errors | 20 calls |
| **T4 Leaderboard & Search** | Navigate using /leaderboard, /search | used_leaderboard, used_search, used_orient, chose_best_direction, branched_from_best, score_improved, searched_related, navigation_efficiency | 15 calls |
| **T5 API Discovery** | Find undocumented endpoints via /openapi.json | explored_openapi, discovered_tags, discovered_metric_direction, discovered_leaderboard_search, discovered_failed_logs, used_discovered_feature, score_improved | 20 calls |
| **T6 Multi-Branch** | Work across multiple idea branches | checked_out_multiple, experiments_on_different_ideas, correct_branch_context, no_cross_branch_confusion, used_orient_or_backlog, compared_results, score_improved | 30 calls |
| **T7 Analytics & Tags** | Normalize tags, filter, compute means | listed_tags, renamed_tags, tags_normalized, used_tag_filter, understood_metric_direction, documented_analysis, score_improved, wait_efficiency | 25 calls |

**Scoring per test**: `task_score = avg(checks) × min(1.0, budget / actual_calls)`

Under budget = no penalty. Over budget = proportional penalty.

## Setup

API code lives in `the_lab/` and docs in `PROMPT_api.md` — modify both on idea branches.

**Running an evaluation:**
```bash
python .the_lab/artifacts/run_eval.py --model haiku --budget 4 --tests t1,t2,t3,t4,t5,t6,t7
```

**Experiment script pattern:**
```bash
#!/usr/bin/env bash
set -euo pipefail
python "$(dirname "$0")/../../artifacts/run_eval.py" --model haiku --budget 4 --tests t1,t2,t3,t4,t5,t6,t7
```

## What to optimize

### API code levers
- Better error messages that tell the agent what to do next
- Richer responses that include context (branch diff, score comparisons)
- Convenience endpoints that bundle multiple calls into one
- Default behaviors that reduce call count (auto_start, auto_checkout)
- Smaller response payloads (less context overhead per agent turn)

### Documentation levers (PROMPT_api.md)
- Clearer workflow descriptions with concrete examples
- Explicit mention of features agents miss (branching semantics, /search, /log)
- Anti-patterns section ("don't poll, use /wait")
- Shorter, more scannable format

## Key metrics

| Metric | Meaning |
|---|---|
| `api_effectiveness` | **PRIMARY** — geometric mean of T1-T7 (higher = better) |
| `total_cost` | **SECONDARY** — token cost across all 7 tests (lower = better) |
| `t1_score` .. `t7_score` | Per-test scores (0-1) |
| `total_api_calls` | Total API calls across all 7 tests |

## Important notes

- **Non-determinism**: Inner agent behavior varies. Run 2-3 times, report median.
- **One change per idea**: Don't bundle multiple optimizations. Measure each independently.
- **Two levers**: Always consider whether the improvement should be in the code or the docs.
- **Don't modify fixtures**: Test fixtures in `optimization/tests/` are static.
- **Key files**: `the_lab/routes/*.py`, `the_lab/deps.py`, `the_lab/schemas.py`, `PROMPT_api.md`
- **Cost reduction**: Smaller API responses = less context per turn. Fewer agent turns = less cost. More decisive guidance = faster convergence.
