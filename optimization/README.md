# The Lab: Self-Optimization

Use The Lab to optimize The Lab's own API for agent efficiency.

## Quick Start

```bash
# 0. Generate test fixtures (once, or after changes to scoring/seeds)
python3 optimization/tests/seed_fixture.py

# 1. Reset optimization proj (regenerates fixtures + clean slate)
./optimization/lab-optimize.sh reset

# 2. Start the outer Lab dashboard
./optimization/lab-optimize.sh start [port]

# 3. Establish baseline — creates idea #1
./optimization/lab-optimize.sh baseline [eval-model] [budget]

# 4. Launch the optimization agent
./optimization/lab-optimize.sh agent [outer-model] [eval-model] [budget]

# 5. Cherry-pick winners back to main
./optimization/lab-optimize.sh cherry-pick <commit>
```

Run `./optimization/lab-optimize.sh` without arguments for full help.

## Architecture

### What's fixed (not modifiable by the outer agent)

| Component | Location | Purpose |
|---|---|---|
| Evaluation harness | `optimization/run_eval.py` | Starts Lab instances, launches inner agents, collects scores. Symlinked into `proj/.the_lab/artifacts/`. |
| Test fixtures | `optimization/tests/t{1-8}_*/` | Pre-seeded git repos with ideas/experiments. Scoring is post-hoc pattern matching on API call logs. |
| Test project | `optimization/test_project/` | The research problem (kernel optimization) that inner agents solve. |
| CLI wrapper | `optimization/lab-optimize.sh` | Manages the outer Lab, baseline, and agent launch. |

### What the outer agent can edit (inside `proj/`)

| Component | Location in `proj/` | Purpose |
|---|---|---|
| API code | `the_lab/routes/`, `the_lab/deps.py`, `the_lab/schemas.py`, `the_lab/app.py` | Endpoints, responses, defaults, middleware |
| API docs | `the_lab/PROMPT_api.md` | Workflow docs appended to inner agent prompts |
| MCP bridge | `the_lab/agent_skills/skills/lab_api_mcp.py` | OpenAPI-to-MCP tool bridge |
| MCP config | `the_lab/agent_skills/mcp.json`, `the_lab/agent_skills/settings.json` | MCP server registration + permissions |
| Package config | `pyproject.toml` | Package metadata |

The outer agent creates idea branches in `proj/`, modifies these files, then runs experiments to evaluate the changes.

## How an experiment runs

When the outer agent creates an experiment, it runs:

```bash
python .the_lab/artifacts/run_eval.py --model haiku --budget 6 --tests t1,t2,t3,t4,t5,t6,t7,t8
```

`run_eval.py` runs **T1-T8 concurrently**, each in an isolated temp directory:

### Per-test steps

1. **Copy fixture** — `optimization/tests/t1_branching/fixture/` → `/tmp/lab_eval_t1_*/`
2. **Build prompt** — `tests/t1/PROMPT_problem.md` + `proj/the_lab/PROMPT_api.md` (from the branch being tested) → `PROMPT_generated.md`
3. **Inject agent skills** — copies `proj/the_lab/agent_skills/` into the temp dir (CLAUDE.md, `.claude/skills/`, hooks, `.mcp.json`)
4. **Start inner Lab** — launches uvicorn with `the_lab.app` **from `proj/the_lab/`** (the branch's code), pointed at the temp fixture
5. **Launch inner agent** — `claude --dangerously-skip-permissions --mcp-config '{"mcpServers":{"labapi":{...}}}' -p "Read PROMPT_generated.md..."`. The MCP bridge runs from the branch's `the_lab/agent_skills/`
6. **Wait for budget** — monitors API call count via `/stats`. Kills the agent when budget is reached or timeout expires
7. **Score** — runs `tests/t1/score.py` against the Lab API. Checks patterns like "did the agent use /wait?", "did it adopt the high-priority suggestion?"
8. **Cleanup** — kills Lab instance, removes temp dir

### What this means for optimization

The inner Lab runs the **branch's code**, reads the **branch's PROMPT_api.md**, and uses the **branch's MCP bridge**. So the outer agent's edits to any of these directly affect how inner agents behave.

## Diagram

```
lab-optimize.sh start            → outer Lab on :9000 (manages proj/)
lab-optimize.sh baseline         → creates idea #1, runs run_eval.py

the-lab-agent PROMPT_problem.md  → outer agent in proj/ (the optimizer)
  ├─ creates idea branch
  ├─ edits the_lab/*.py, PROMPT_api.md, agent_skills/
  ├─ creates experiment → run_eval.py
  │   ├─ T1: temp fixture + Lab from branch + inner agent + score
  │   ├─ T2: ...
  │   └─ T8: ...
  ├─ reads results, concludes/branches
  └─ repeats

┌─────────────────────────────────────────────────────────────────────┐
│  the_lab.api/  (parent repo — committed, stable)                    │
│  ├── the_lab/              ← stable API code (outer Lab uses this)  │
│  │   └── agent_skills/     ← MCP bridge, settings, skills           │
│  └── optimization/         ← static fixtures + test suite            │
│       ├── tests/t{1-8}_*   ← pre-seeded fixtures (generated)        │
│       ├── test_project/    ← research problem for inner agents       │
│       └── proj/            ← gitignored from parent                  │
│                                                                      │
│  ┌───────────────────────────────────────────────────────────────┐   │
│  │  proj/  (own git repo — optim branch, idea branches)          │   │
│  │  ├── the_lab/       ← copied API code, modified on branches   │   │
│  │  │   └── agent_skills/  ← MCP bridge, editable per branch    │   │
│  │  └── .the_lab/      ← Lab data (experiments, ideas, notes)    │   │
│  │       └── artifacts/ → symlinks to optimization/ statics      │   │
│  │                                                                │   │
│  │  OUTER LAB  (port 9000, runs parent's the_lab/ code)          │   │
│  │  Tracks optimization ideas as branches in proj/.git            │   │
│  │                                                                │   │
│  │  Each experiment runs run_eval.py --tests t1,...,t8:           │   │
│  │  ┌──────────────── × 8 tests concurrently ───────────────┐    │   │
│  │  │  /tmp/lab_eval_t{N}_*/  (copy of pre-seeded fixture)   │    │   │
│  │  │                                                         │    │   │
│  │  │  INNER LAB  (random port, runs branch's the_lab/ code)  │    │   │
│  │  │  INNER AGENT  (haiku, with branch's MCP + PROMPT_api)   │    │   │
│  │  │  SCORING  (post-hoc: checks API usage + Lab state)      │    │   │
│  │  └─────────────────────────────────────────────────────────┘    │   │
│  └───────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
```

## Test Suite

8 tests run concurrently. Each launches a Lab instance from the branch's code, runs an inner agent against a pre-seeded project, and scores API usage patterns post-hoc.

| Test | What it measures | Budget |
|---|---|---|
| **T1 Sequential + Priority** | Create ideas, adapt to suggestions, handle priority | 40 calls |
| **T2 Experiment Mgmt** | Iterate with /wait, auto_start, document findings | 40 calls |
| **T3 Error Recovery** | Read logs, diagnose, fix failures | 30 calls |
| **T4 Leaderboard & Search** | Navigate using /leaderboard, /search, branch from best | 35 calls |
| **T5 API Discovery** | Find undocumented endpoints (via /openapi.json or MCP) | 30 calls |
| **T6 Multi-Branch** | Work across multiple idea branches without confusion | 30 calls |
| **T7 Analytics** | Answer data questions: grouping, filtering, variance | 35 calls |
| **T8 Metadata** | Understand tag/metric semantics, normalize tags | 35 calls |

Generate fixtures: `python3 optimization/tests/seed_fixture.py`

## Scoring

```
task_score = avg(checks) × efficiency_factor × speed_mult × confusion_mult

score_sum = t1 + t2 + ... + t8  (max 8.0, primary metric)
```

- **checks**: 0-1 per behavior (e.g. `used_wait`, `score_improved`, `adopted_high_priority`)
- **efficiency_factor**: under budget = up to 10% bonus, over budget = proportional penalty
- **speed_mult**: up to 15% bonus for finishing well under timeout
- **confusion_mult**: up to 10% penalty for confused API usage

Secondary metric: `total_cost` (token cost across all 8 tests, lower is better).

## Optimization levers

### API code
- Better error messages that tell the agent what to do next
- Richer responses that include context (branch diff, score comparisons)
- Convenience endpoints that bundle multiple calls into one
- Default behaviors that reduce call count (auto_start, auto_checkout)
- Smaller response payloads (less context overhead per agent turn)
- Notifications middleware (`_notifications` key in responses)

### Documentation (PROMPT_api.md)
- Clearer workflow descriptions with concrete examples
- Explicit mention of features agents miss (branching semantics, /search, /log)
- Anti-patterns section ("don't poll, use /wait")
- Shorter, more scannable format

### MCP bridge (agent_skills/)
- Tool naming and descriptions that reduce confusion
- Grouping related operations
- Environment configuration for API discovery

## Structure

```
optimization/
├── lab-optimize.sh           # CLI: reset, start, baseline, agent, cherry-pick
├── PROMPT_problem.md         # Optimization meta-instructions (outer agent)
├── README.md                 # This file
├── run_eval.py               # Multi-test eval harness (8 concurrent tests)
├── test_project/             # Research problem for inner agents
│   └── PROMPT_problem.md    # Kernel optimization problem description
└── tests/
    ├── seed_fixture.py       # Generate all test fixtures
    ├── score_common.py       # Shared scoring utilities
    ├── shared_project/       # Math kernel project template
    └── t{1-8}_{name}/
        ├── PROMPT_problem.md # Per-test task instruction
        ├── score.py          # Post-hoc scoring (checks API usage + Lab state)
        └── fixture/          # Generated git repo (15 ideas, gitignored)
```

## Dual Model Support

```bash
./optimization/lab-optimize.sh agent opus haiku 10
#                                    ^^^^  ^^^^^
#                                    outer  inner (eval)
```

The outer model (optimizer) and inner model (evaluator) can differ. Use a strong model (opus/sonnet) for the outer agent and a cheaper model (haiku) for inner evaluation.

## Cost

| Inner model | Per test | Full suite (8 tests) |
|---|---|---|
| Haiku | ~$0.50-1 | ~$4-8 |
| Sonnet | ~$1-3 | ~$8-24 |
