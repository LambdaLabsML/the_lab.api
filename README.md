# The Lab — Autonomous Experiment Management

A lightweight API + dashboard that lets AI agents run structured research loops: create ideas, branch, run experiments, compare metrics, and iterate — all managed through a REST API with git integration.

## Quick Start

### 1. Install

```bash
pip install -e .
```

### 2. Configure your research goal

Edit `PROMPT.md` in your target repository. Fill in the three sections:

```markdown
# Swarm Math Problem Solving

## Goal
Improve multi-agent accuracy on competition math problems. Beat the solo baseline of 10% agent_accuracy.

## Background
We have a framework that spawns N agents to solve math problems collaboratively.
Prior work: solo agent gets 10% on our heldout set (4 problems, 25 rollouts each).

## Setup
- Hardware: 8xH100 node
- Data: problems in /data/heldout/*.json
- Run: python run_swarm.py --config <path> --problems /data/heldout/
- Python 3.11, deps in requirements.txt
```

The rest of `PROMPT.md` (below the `---`) documents the API and is already filled in.

### 3. Start the API server

```bash
# From your target repo (must be a git repository)
the-lab /path/to/your/repo

# Or with hot-reload during development
the-lab /path/to/your/repo --dev
```

The server starts at `http://localhost:8000`. Open it in a browser to see the dashboard.

### 4. Launch an agent

Pick your agent and paste the contents of `LOOP.md` as the initial prompt.

**Claude Code** (recommended — use `/loop` for continuous operation):
```bash
cd /path/to/your/repo
claude --dangerously-skip-permissions
```
Then in the Claude session:
```
/loop 10m <paste LOOP.md content here>
```

**Codex:**
```bash
cd /path/to/your/repo
codex --yolo
```
Then paste `LOOP.md` as the first message.

### 5. Watch it work

Open `http://localhost:8000` in your browser. The dashboard shows:
- **Subway graph**: idea branches, colored by status + metric improvement
- **Metrics chart**: experiment results over time with multiple color modes
- **Timeline**: temporal view of when ideas were active
- **Log**: unified chronological feed of ideas, experiments, and notes

## Writing Experiment Scripts

Experiment scripts are bash scripts that the agent writes and the API executes. They must follow two rules:

### Rule 1: Print metrics as the last stdout line

The last line of stdout must be a JSON object with a `metrics` key:

```bash
#!/bin/bash
python train.py --lr 1e-4 --epochs 10

# Last line must be JSON with metrics
echo '{"metrics": {"accuracy": 0.84, "loss": 0.21, "f1": 0.79}}'
```

`metrics` is required. You can optionally include `meta` to record extra info (the server merges it with the experiment's existing meta):

```json
{"metrics": {"accuracy": 0.84}, "meta": {"gpu_hours": 1.2, "actual_samples": 4832}}
```

### Rule 2 (optional): Report progress

For long-running experiments, write intermediate progress to `$THE_LAB_PROGRESS`:

```bash
#!/bin/bash
for epoch in $(seq 1 100); do
    python train.py --epoch $epoch
    echo "{\"epoch\": $epoch, \"loss\": 0.5, \"pct\": $epoch}" > "$THE_LAB_PROGRESS"
done
echo '{"metrics": {"accuracy": 0.91}}'
```

The dashboard polls this file and shows live progress in the detail panel, including a progress bar if a percentage field is detected.

## Environment Variables

Every experiment script receives these environment variables:

| Variable | Description |
|----------|-------------|
| `THE_LAB_TOKEN` | Auth token proving the script was launched by the API |
| `THE_LAB_EXP_ID` | This experiment's ID (e.g. `"23"`) |
| `THE_LAB_IDEA_ID` | The parent idea's ID (e.g. `"5"`) |
| `THE_LAB_PROGRESS` | Path to write progress JSON (e.g. `.the_lab/experiments/5/23.progress`) |

Scripts also inherit the full server environment, and run in the repository root directory on the currently checked-out git branch.

**Tip**: use `THE_LAB_EXP_ID` to create unique output directories for artifacts:

```bash
OUTDIR="outputs/exp_${THE_LAB_EXP_ID}"
mkdir -p "$OUTDIR"
python train.py --output-dir "$OUTDIR"
```

## Understanding LOOP.md

`LOOP.md` is the behavioral prompt for the agent. It defines a four-step cycle:

1. **Never Stop** — the agent should always be exploring, even when the backlog is empty
2. **Orient** — read `PROMPT.md`, check the backlog and existing ideas
3. **Work** — create ideas, checkout branches, write and run experiments, take notes
4. **Repeat** — wait for results, analyze, decide what to try next

The key API endpoints the agent uses:

```
POST /ideas/new              → create a new research direction (auto-creates git branch)
POST /ideas/<id>/checkout    → switch to an idea's branch (auto-commits current work)
POST /ideas/<id>/experiments → create an experiment with an inline script
POST /experiments/<id>/start → run the experiment (auto-commits first)
GET  /wait?experiment_id=N   → block until the experiment finishes
POST /ideas/<id>/note        → record observations, insights, milestones
POST /ideas/<id>/conclude    → wrap up an idea with a conclusion
GET  /experiments/compare?ids=1,2,3 → side-by-side metric comparison
```

## Data Storage

All experiment data lives in `.the_lab/experiments/` at the repo root. This directory is git-ignored (via `.git/info/exclude`) so it persists across branch switches:

```
.the_lab/experiments/
  1/
    idea.json          # idea metadata
    notes.json         # running journal
    1.json             # experiment metadata + results
    1.sh               # experiment script
    1.log              # stdout+stderr (streams in real-time)
    1.progress         # optional progress JSON
    1.err              # error details if failed
```

## Architecture

```
[Agent] → REST API (FastAPI :8000) → File storage (.the_lab/)
                                   → Git operations (branch per idea)
                                   → Subprocess runner (experiments)
              ↕
         [Dashboard] ← Polls API for updates
```

- **No database** — everything is JSON files on disk
- **No worktrees** — standard git branches, auto-commit on checkout/experiment start
- **Survives restarts** — running experiments continue as orphaned processes; the server re-attaches on startup
