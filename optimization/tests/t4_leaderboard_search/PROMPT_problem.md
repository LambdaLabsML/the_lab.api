# Leaderboard Navigation: Find the Best Direction

## Goal

12 ideas span 3 research directions (tagged: `binary-search`, `gradient`, `random`). Your job:

1. **Survey the landscape** — use the leaderboard and search to understand what's been tried
2. **Identify the best direction** — which approach is closest to the optimum?
3. **Continue the best work** — branch from the top-scoring idea and push further
4. **Beat the best score** — current best is 0.95. Get closer to 1.0.

## Background

`solver.py` has a `guess()` function. `eval_harness.py` scores it — closer to a hidden target means higher score (0.0 to 1.0).

Three teams explored different strategies:
- **Binary search**: systematic halving of the search range
- **Gradient**: incremental steps in the improving direction
- **Random**: exploratory guesses

Use `/leaderboard?metric=score` and `/ideas/search?q=...` to navigate efficiently rather than reading every idea individually.

## Setup

```bash
python eval_harness.py
```
