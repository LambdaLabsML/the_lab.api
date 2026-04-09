# The Lab v4: Self-Optimization

Use The Lab to optimize The Lab's own API for agent efficiency.

## Quick Start

```bash
# 0. Generate test fixtures (once, or after changes to seed_fixture.py)
python3 optimization/tests/seed_fixture.py

# 1. Reset optimization proj (clean slate)
./optimization/lab-optimize.sh reset

# 2. Start the Lab dashboard
./optimization/lab-optimize.sh start [port]

# 3. Establish baseline — creates idea #1 in the Lab
./optimization/lab-optimize.sh baseline [eval-model] [budget]

# 4. Launch an optimization agent
./optimization/lab-optimize.sh agent [outer-model] [eval-model] [budget]

# 5. Cherry-pick winners back to main
./optimization/lab-optimize.sh cherry-pick <commit>
```

Run `./optimization/lab-optimize.sh` without arguments for help.

## How It Works

```
┌─────────────────────────────────────────────────────────────────────┐
│  the_lab.api/  (parent git repo — committed, stable)               │
│  ├── the_lab/          ← stable API code (outer Lab imports this)  │
│  └── optimization/     ← static fixtures + test suite              │
│       ├── tests/       ← T1-T4 test fixtures (generated)           │
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
│  │  Each experiment runs run_eval.py --tests t1,t2,t3,t4:       │   │
│  │  ┌──────────────── × 4 tests concurrently ───────────────┐   │   │
│  │  │  /tmp/lab_eval_t{N}_*/  (copy of pre-seeded fixture)   │   │   │
│  │  │  ├── benchmark/          ← math kernel optimization     │   │   │
│  │  │  └── .the_lab/           ← 15 pre-seeded ideas          │   │   │
│  │  │                                                         │   │   │
│  │  │  INNER LAB  (random port, runs branch's the_lab/ code)  │   │   │
│  │  │                                                         │   │   │
│  │  │  INNER AGENT  (haiku)                                   │   │   │
│  │  │  Tests API comprehension: branching, experiment mgmt,   │   │   │
│  │  │  error recovery, leaderboard navigation                 │   │   │
│  │  │                                                         │   │   │
│  │  │  SCORING  (post-hoc: checks API usage + Lab state)      │   │   │
│  │  └─────────────────────────────────────────────────────────┘   │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                                                                     │
│  Git repos:                                                         │
│    the_lab.api/.git      ← parent (main branch, commits fixtures)  │
│    proj/.git             ← shared history, optim branch             │
│    /tmp/.../fixture/.git ← ephemeral (per test, destroyed after)   │
└─────────────────────────────────────────────────────────────────────┘
```

## Test Suite

4 tests run concurrently, each with a pre-seeded project (15 ideas, 3 strategy
families, real kernel implementations):

| Test | What it measures | Key checks |
|---|---|---|
| T1 Branching | Find best idea, branch from it | correct parent, used /orient or /leaderboard |
| T2 Experiment Mgmt | Iterate, use /wait, auto_start | experiments created, used /wait, documented findings |
| T3 Error Recovery | Read logs, diagnose, fix failures | read logs, new successful experiments |
| T4 Leaderboard Search | Navigate efficiently, find best direction | used /leaderboard, /search, chose best direction |

Generate fixtures: `python3 optimization/tests/seed_fixture.py`

## Scoring

```
api_effectiveness = geometric_mean(t1_score, t2_score, t3_score, t4_score)
task_score = avg(checks) × waste_penalty
waste_penalty = min(1.0, max_calls / actual_calls)
```

| Test | Min calls | Budget | Penalty at |
|---|---|---|---|
| T1 | ~5 | 15 | 16+ calls |
| T2 | ~10 | 25 | 26+ calls |
| T3 | ~9 | 20 | 21+ calls |
| T4 | ~6 | 15 | 16+ calls |

Under budget = no penalty. Over budget = proportional penalty.

## Structure

```
optimization/
├── lab-optimize.sh           # CLI: baseline, start, agent, reset, cherry-pick
├── PROMPT.md                 # Optimization agent instructions
├── run_eval.py               # Multi-test eval harness
├── test_project/             # Legacy single-test fixture
└── tests/
    ├── seed_fixture.py       # Generate all test fixtures
    ├── score_common.py       # Shared scoring utilities
    ├── shared_project/       # Math kernel project template
    └── t{1-4}_{name}/
        ├── PROMPT_problem.md # Task instruction (problem only, no API docs)
        ├── score.py          # Post-hoc scoring (checks API usage + Lab state)
        └── fixture/          # Generated git repo (15 ideas, gitignored)
```

## Dual Model Support

```bash
./optimization/lab-optimize.sh agent opus haiku 5
#                                    ^^^^  ^^^^^
#                                    outer  inner (eval)
```

Outer model (optimizer) and inner model (evaluator) can differ.

## Cost

| Inner model | Per test | Full suite (4 tests) |
|---|---|---|
| Haiku | ~$0.50-1 | ~$2-4 |
| Sonnet | ~$1-3 | ~$4-12 |
