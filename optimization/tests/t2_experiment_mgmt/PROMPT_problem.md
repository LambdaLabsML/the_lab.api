# Experiment Management: Iterate to Maximize Score

## Goal

Maximize the score by iterating: edit `solver.py`, run an experiment, read the results, adjust. The current best score is 0.52 — push it as high as possible.

## Background

`solver.py` has a `guess()` function returning a float. `eval_harness.py` scores it — closer to a hidden target means higher score (0.0 to 1.0).

Two ideas exist with 3 experiments total. The best guess so far is 20.0 (score 0.52). Use the experiment results to guide your next guess — look at the score and error values to determine which direction to go.

## Setup

```bash
python eval_harness.py
```

Create experiments through the API, wait for results, then create follow-up experiments informed by the metrics.
