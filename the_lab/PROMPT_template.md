# [Your Research Goal]

## Goal
Describe what you're optimizing and the success criteria.
Example: Maximize accuracy on held-out evaluation set while minimizing compute cost.

## Background
Prior work, constraints, relevant context.
Example: We have a baseline model achieving 10% accuracy. The framework supports N-agent collaboration.

## Setup
- Hardware: [describe your setup, e.g. 8xH100 node]
- Data: [where is your data, e.g. /data/heldout/*.json]
- Run: [how to run experiments, e.g. python run.py --config <path>]

## Conventions

### Tagging

Every experiment tag must be namespaced with a prefix:

| Prefix | Use for | Examples |
|---|---|---|
| `method-*` | Algorithmic approach | `method-ppo`, `method-dpo`, `method-ensemble` |
| `model-*` | Model variant | `model-7b`, `model-70b`, `model-distilled` |
| `data-*` | Dataset or data split | `data-full`, `data-subset-1k`, `data-augmented` |
| `sweep-*` | Hyperparameter sweep | `sweep-lr`, `sweep-batch-size` |

This makes it easy to compare apples-to-apples (filter by `method-*` to compare approaches, by `model-*` to compare scales, etc.).

### When to create a new idea (branch)

Create a new idea when:
- Trying a **new approach** or method variant
- Changing **model architecture** or data pipeline
- Any change that is NOT a bugfix of the current idea

Do NOT create a new idea for:
- Fixing a bug in the current experiment script (just rerun on the same idea)
- Re-running for variance (add another experiment to the same idea)
- Sweeping hyperparameters (add experiments with `sweep-*` tags to the same idea)

This ensures every idea is reproducible via its git branch and experiments map cleanly back to the idea that motivated them.

### When to add experiments to an existing idea

Add more experiments to an existing idea for:
- **Reruns** — to measure variance (run the same config 2-3 times)
- **Bugfixes** — after fixing a script error, rerun on the same idea
- **Hyperparameter sweeps** — tag with `sweep-*` to distinguish from the baseline run
- **Ablations** — testing sub-components of the idea's approach
