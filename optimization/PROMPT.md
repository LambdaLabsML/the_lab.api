# Autoresearch Optimization

You are optimizing `karpathy/autoresearch`.

## Objective

Minimize `val_bpb`, also reported as `loss`. Lower is better.

The evaluation command is:

```bash
python .the_lab/artifacts/run_eval.py
```

That wrapper runs:

```bash
uv run train.py
```

and parses the final `val_bpb:` line from `train.py`.

## Scope

Only edit:

- `train.py`

Do not edit:

- `prepare.py`
- `pyproject.toml`
- `uv.lock`
- the evaluation wrapper
- data files, tokenizer files, or cache contents

This follows the upstream autoresearch design: `prepare.py` fixes data prep, dataloading, evaluation, and timing constants; `train.py` is the optimization surface.

## Experiment Loop

1. Establish or inspect the current best `val_bpb`.
2. Create one focused idea branch.
3. Modify `train.py`.
4. Run an experiment with `python .the_lab/artifacts/run_eval.py`.
5. Compare by `val_bpb` ascending.
6. Keep changes only if loss improves or the simplification is clearly worth a negligible loss change.
7. Record conclusions with the exact `val_bpb`, VRAM, and what changed.

Use leaderboard and search with `metric=val_bpb&sort=asc` or `metric=loss&sort=asc`.

## Constraints

- Lower `val_bpb` is the primary goal.
- The code must run to completion and print the normal final summary.
- VRAM is a secondary concern; avoid large memory increases unless they buy clear loss improvement.
- Prefer simple, reviewable changes over fragile tricks.
- Do not optimize by changing the metric, suppressing evaluation, changing data, or editing `prepare.py`.
