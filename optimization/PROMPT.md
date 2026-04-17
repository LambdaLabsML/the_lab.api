# Optimizing an ARC-AGI-3 Solver via Prompt Engineering

## Goal

Maximize **`best_score`** — the highest ARC scorecard score achieved by the inner agent across all its experiments.

Secondary: maximize **`experiments_completed`** (more successful runs = more data) and minimize **`wall_seconds`**.

## Background

We have a nested Lab setup:
- **You (outer agent, Opus)**: optimize what the inner agent sees — its PROMPT.md, conventions, strategy hints, experiment patterns
- **Inner agent (Gemma 4 31B via Claude Code)**: reads the PROMPT.md you write, uses The Lab to create ideas, writes Python solver code in `agent/arc_agent.py`, runs `evaluate_agent.py` to score it

The inner agent solves ARC-AGI-3 puzzles. Each puzzle is a grid-manipulation game with levels. The agent class `ARCAgent.pick_action(frame)` receives the game state and returns an action. The current baseline is random — your job is to guide the inner agent toward writing an effective solver.

**The inner agent writes code, not prompts.** It edits `agent/arc_agent.py` with pure Python logic (heuristics, search, pattern recognition). No LLM calls at runtime.

## What you can edit

All files in `arc3_autosolver/` are on your idea branches:

| File | Purpose | Impact |
|---|---|---|
| `PROMPT.md` | Problem description, strategies, conventions | What the inner agent reads at the start |
| `agent/arc_agent.py` | Baseline solver code | Starting point the inner agent iterates on |
| `evaluate_agent.py` | Evaluation harness | How scores are collected (usually leave as-is) |

### Primary lever: PROMPT.md

The inner agent's effectiveness depends heavily on how well PROMPT.md guides it:
- **Clear problem framing**: what the grid looks like, what actions do, what "solved" means
- **Strategy suggestions**: concrete algorithmic approaches to try (not vague ideas)
- **Code patterns**: example snippets showing how to parse frames, analyze grids, pick actions
- **Anti-patterns**: what NOT to do (no LLM calls, no hardcoded solutions, respect step budget)
- **Evaluation guidance**: how to interpret scorecard results, which games to focus on

### Secondary lever: baseline agent code

You can also improve the starting `agent/arc_agent.py`:
- Seed it with a smarter baseline (not random) so the inner agent has something to build on
- Add utility functions the inner agent can use (grid parsing, pattern detection, etc.)
- Structure the code so it's easy for the inner agent to modify specific parts

## Setup

**Running an evaluation:**
```bash
python .the_lab/artifacts/run_eval_arc.py --model gemma-4-31b --budget 10 --timeout 7200
```

This copies `arc3_autosolver/` to a temp dir, starts an inner Lab, launches the inner agent (Gemma), waits for it to complete experiments, and reports the best score.

**Experiment script pattern:**
```bash
#!/usr/bin/env bash
set -euo pipefail
python .the_lab/artifacts/run_eval_arc.py --model gemma-4-31b --budget 10 --timeout 7200
```

## Key metrics

| Metric | Meaning |
|---|---|
| `best_score` | **PRIMARY** — highest ARC score from inner agent's experiments |
| `mean_score` | Average score across all inner experiments |
| `experiments_completed` | How many experiments the inner agent finished |
| `experiments_failed` | How many experiments crashed (indicates code issues) |
| `wall_seconds` | Total time for the eval run |
| `best_levels_completed` | Levels cleared by the best experiment |
| `best_envs_completed` | Games fully solved by the best experiment |

## Strategies for the outer agent (you)

### Iteration 1: establish a baseline
- Run the eval with the current random agent to get baseline scores
- Study the scorecard to understand which games are easy vs hard
- Note the per-game breakdown in the experiment meta

### Iteration 2: improve the PROMPT.md
- Add concrete strategy descriptions with code examples
- Describe the ARC game frame format in detail (what fields exist, what they mean)
- Suggest a first approach (e.g., grid analysis → action heuristic)
- Include the baseline scorecard so the inner agent knows what to beat

### Iteration 3+: iterate on strategy
- Analyze which games the inner agent solves vs fails
- Adjust PROMPT.md strategies based on failure patterns
- Consider seeding `agent/arc_agent.py` with utility code
- Try different levels of guidance (more prescriptive vs more exploratory)

## Important notes

- **One change per idea**: isolate prompt changes so you can measure their effect
- **Non-determinism**: Gemma's outputs vary — run 2-3 times for confidence
- **Read the scorecard**: the meta field has per-game results — use them to guide your next idea
- **Don't over-prescribe**: the inner agent is good at code — give it strategies, not implementations
- **Budget awareness**: the inner agent has limited experiments — make each PROMPT.md iteration count
