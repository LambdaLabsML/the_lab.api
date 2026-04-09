# Branching Test: Find the Best Path and Continue It

## Goal

This project has 15 existing ideas forming a research tree. Your job:

1. **Examine the existing ideas** — use the API to find which idea has the best score
2. **Branch from the best idea** — create a new idea with the best-scoring idea as parent
3. **Improve the score** — edit `solver.py` on your new branch and run an experiment
4. **Conclude** — note your findings and conclude if you improved the score

## Background

`solver.py` contains a `guess()` function that returns a float. `eval_harness.py` scores it — the closer to a hidden target, the higher the score (0.0 to 1.0).

Previous researchers explored different ranges and left 15 ideas with varying scores. Some branches are dead ends, others are promising. The best score so far is 0.95 — can you beat it?

## Setup

```bash
python eval_harness.py
```

Returns `{"metrics": {"score": ..., "guess": ..., "error": ...}}`.
