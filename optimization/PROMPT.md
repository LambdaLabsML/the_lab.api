# Optimizing The Lab API for Agent Comprehension

## Goal

Maximize **`score_sum`** while minimizing **`total_cost`**.

```
score_sum = t1 + t2 + t3 + t4 + t5 + t6 + t7 + t8
```

Primary objective: maximize `score_sum` (higher is better; max 8.0).
Secondary objective: minimize `total_cost` (token cost across all 8 tests).

## Background

The Lab is an experiment management API used by AI agents (Claude, Codex) to run research. Agents struggle with branching, experiment management, error recovery, navigation, API discovery, multi-branch workflows, and tag management.

The optimization target is both:
1. **API code** (`the_lab/routes/`, `the_lab/deps.py`, `the_lab/schemas.py`) — better endpoints, smarter defaults, richer responses
2. **API documentation** (`PROMPT_api.md`) — clearer workflow descriptions, better examples

## Test Suite

8 tests run concurrently. Each launches a Lab instance from the current branch's code, runs an inner agent against a pre-seeded project, and scores API usage patterns post-hoc.

| Test | What it measures | Checks | Budget |
|---|---|---|---|
| **T1 Sequential + Priority** | Create 4 ideas, adapt to suggestions | created_4_ideas, ran_experiments_sequentially, checked_suggestions, adopted_high_priority (2×), adopted_high_priority_quickly (2×), deferred_low_priority (2×), score_improved, used_orient_or_leaderboard | 40 calls |
| **T2 Experiment Mgmt** | Iterate with /wait, auto_start | experiments_created, used_wait, efficient_start, score_improved, documented_findings | 25 calls |
| **T3 Error Recovery** | Read logs, diagnose, fix failures | read_logs, checked_status, experiments_fixed, score_improved, documented_errors | 20 calls |
| **T4 Leaderboard & Search** | Navigate using /leaderboard, /search | used_leaderboard, used_search, used_orient, chose_best_direction, branched_from_best, score_improved, searched_related, navigation_efficiency | 15 calls |
| **T5 API Discovery** | Find undocumented endpoints via /openapi.json | explored_openapi, discovered_tags, discovered_leaderboard_search, discovered_failed_logs, used_discovered_feature, score_improved | 20 calls |
| **T6 Multi-Branch** | Work across multiple idea branches | checked_out_multiple, experiments_on_different_ideas, correct_branch_context, no_cross_branch_confusion, used_orient_or_backlog, compared_results, score_improved | 30 calls |
| **T7 Analytics** | Answer data questions efficiently (grouping, filtering, hierarchical means, fluke vs reliable) | approach_means_documented, hierarchical_mean_correct, identified_over_budget, chose_reliable_over_fluke, recognized_variance, understood_convergence_gap, query_efficiency, used_tag_filter, score_improved, documented_analysis | 25 calls |
| **T8 Metadata Comprehension** | Understand tag/metric semantics, normalize tags, document new tags | listed_tags, renamed_tags, tags_normalized, understood_metric_direction, described_tag_purposes, mapped_tags_to_experiments, described_metric_semantics, documented_new_tag, score_improved | 25 calls |

**Scoring per test**: `task_score = avg(checks) × min(1.0, budget / actual_calls)`

Under budget = no penalty. Over budget = proportional penalty.

## Setup

API code lives in `the_lab/` and docs in `PROMPT_api.md` — modify both on idea branches.

**Running an evaluation:**
```bash
python .the_lab/artifacts/run_eval.py --model haiku --budget 4 --tests t1,t2,t3,t4,t5,t6,t7,t8
```

**Experiment script pattern:**
```bash
#!/usr/bin/env bash
set -euo pipefail
python "$(dirname "$0")/../../artifacts/run_eval.py" --model haiku --budget 4 --tests t1,t2,t3,t4,t5,t6,t7,t8
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
| `score_sum` | **PRIMARY** — sum of T1-T8 scores (higher = better, max 8.0) |
| `total_cost` | **SECONDARY** — token cost across all 8 tests (lower = better) |
| `t1_score` .. `t8_score` | Per-test scores (0-1) |
| `total_api_calls` | Total API calls across all 8 tests |

## Important notes

- **Non-determinism**: Inner agent behavior varies. Run 2-3 times, report median.
- **One change per idea**: Don't bundle multiple optimizations. Measure each independently.
- **Two levers**: Always consider whether the improvement should be in the code or the docs.
- **Don't modify fixtures**: Test fixtures in `optimization/tests/` are static.
- **Key files**: `the_lab/routes/*.py`, `the_lab/deps.py`, `the_lab/schemas.py`, `PROMPT_api.md`
- **Cost reduction**: Smaller API responses = less context per turn. Fewer agent turns = less cost. More decisive guidance = faster convergence.
