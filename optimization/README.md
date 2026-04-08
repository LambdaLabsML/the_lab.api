# The Lab v4: Self-Optimization

Use The Lab to optimize The Lab's own API for agent efficiency.

## Quick Start

```bash
# 1. Establish baseline — creates idea #1 in the Lab (~5 min, ~$1-3)
./optimization/lab-optimize.sh baseline [eval-model] [budget]

# 2. Start the Lab dashboard
./optimization/lab-optimize.sh start [port]

# 3. Launch an optimization agent
./optimization/lab-optimize.sh agent [outer-model] [eval-model] [budget]

# 4. Reset proj/ to a clean state
./optimization/lab-optimize.sh reset

# 5. Cherry-pick winners back to main
./optimization/lab-optimize.sh cherry-pick <commit>
```

Run `./optimization/lab-optimize.sh` without arguments for help.

## How It Works

```
┌─────────────────────────────────────────────────────────────────────┐
│  the_lab.api/  (parent git repo — committed, stable)               │
│  ├── the_lab/          ← stable API code (outer Lab imports this)  │
│  └── optimization/     ← static fixtures (test_project, run_eval)  │
│       └── proj/        ← gitignored from parent                    │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │  proj/  (own git repo — optim branch, shared history)        │   │
│  │  ├── the_lab/       ← copied API code, modified on branches  │   │
│  │  └── .the_lab/      ← Lab data (experiments, ideas, notes)   │   │
│  │       └── artifacts/ → symlinks to optimization/ statics     │   │
│  │                                                               │   │
│  │  OUTER LAB  (port 9000, runs parent's the_lab/ code)         │   │
│  │  Tracks optimization ideas as branches in proj/.git           │   │
│  │                                                               │   │
│  │  Each experiment runs run_eval.py, which creates:             │   │
│  │  ┌────────────────────────────────────────────────────────┐   │   │
│  │  │  /tmp/lab_eval_*/  (temp copy of test_project)          │   │   │
│  │  │  ├── benchmark/kernels.py  ← inner agent modifies this  │   │   │
│  │  │  └── .the_lab/             ← inner Lab data              │   │   │
│  │  │                                                          │   │   │
│  │  │  INNER LAB  (random port, runs branch's the_lab/ code)  │   │   │
│  │  │  The thing being tested — uses the modified API          │   │   │
│  │  │                                                          │   │   │
│  │  │  ┌──────────────────────────────────────────────────┐    │   │   │
│  │  │  │  INNER AGENT  (haiku by default)                  │    │   │   │
│  │  │  │  Optimizes math kernels via the inner Lab API     │    │   │   │
│  │  │  │  Creates ideas, runs eval_harness.py, iterates    │    │   │   │
│  │  │  └──────────────────────────────────────────────────┘    │   │   │
│  │  │                                                          │   │   │
│  │  │  Metrics collected: api_calls, confusion, tokens, DAG   │   │   │
│  │  └────────────────────────────────────────────────────────┘   │   │
│  └──────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘

Git repos:
  the_lab.api/.git     ← parent repo (main branch, commits static fixtures)
  proj/.git            ← shared history, optim branch (idea branches with API changes)
  /tmp/.../project/.git ← ephemeral (created fresh per eval, destroyed after)

Lab instances:
  Outer (port 9000)    ← stable code, manages optimization ideas
  Inner (random port)  ← experimental code from idea branch, tested by inner agent
```

## Scoring

```
api_score = norm_quality × (1 - failure_rate) × (1 - confusion) / norm_cost
```

Fixed budget of experiments. Score = how good did the agent get?

| Component | What |
|---|---|
| `quality = -log10(1 - score)` | 0.9→1, 0.99→2, 0.999→3 |
| `confusion_score` | Retries, errors, corrections, oscillations |
| `norm_cost` | Token cost relative to baseline |

Baseline = 1.0. Higher is better.

## Structure

```
optimization/
├── lab-optimize.sh      # All commands: baseline, start, agent, reset, cherry-pick
├── PROMPT.md            # Agent instructions (committed)
├── run_eval.py          # Eval harness (committed)
├── test_project/        # Fast math fixture (committed)
└── proj/                # Lab project (gitignored, own .git)
    ├── the_lab/         # API code — what gets optimized on branches
    └── .the_lab/artifacts/ → symlinks to above
```

Static files versioned in the parent repo. `proj/` has its own git
(shared history) so cherry-picks to main just work.

## Dual Model Support

The outer agent (who decides what API changes to try) and the inner agent
(who tests the API by optimizing math kernels) can use different models:

```bash
./optimization/lab-optimize.sh agent opus haiku 10
#                                    ^^^^  ^^^^^
#                                    outer  inner (eval)
```

Use a stronger model (opus/sonnet) for the outer loop and a cheaper model
(haiku) for evaluation.

## Cost

| Model | Per eval run | 3 runs |
|---|---|---|
| Haiku | ~$1-3 | ~$3-9 |
| Sonnet | ~$3-8 | ~$9-24 |
