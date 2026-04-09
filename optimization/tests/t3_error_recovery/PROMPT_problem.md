# Error Recovery: Fix Failing Experiments

## Goal

Several experiments have failed with different errors. Your job:

1. **Find the failed experiments** — check experiment statuses across all ideas
2. **Read the error logs** — understand what went wrong in each case
3. **Fix the issues** — create new experiments that succeed
4. **Improve the score** — the working baseline scores 0.50. Beat it.

## Background

`solver.py` has a `guess()` function. `eval_harness.py` scores it. The baseline works (score 0.50), but 4 experiments have failed with different errors:
- Import errors (missing modules)
- Type errors (wrong data types)
- Timeout errors (infinite loops)
- Script errors (typos in filenames)

Each error has a clear fix. Read the logs to understand what happened.

## Setup

```bash
python eval_harness.py
```

Check experiment logs with the API to diagnose failures before creating fixes.
