<h3 align="center">
    <br/>
    <img src="https://github.com/user-attachments/assets/186c8033-25d2-4f98-a18c-e8d5d19c0dcd" width="300" alt="The Lab"/><br/><br/>
    Autonomous experiment management for AI research agents.<br/>
    Let agents <a href="#writing-experiment-scripts">run experiments</a>, <a href="#understanding-loopmd">branch ideas</a>, and <a href="#5-watch-it-work">track metrics</a> — hands-free.
</h3>

<p align="center">
    Jump to: <a href="#quick-start">Quick Start</a> | <a href="#writing-experiment-scripts">Experiments</a> | <a href="#environment-variables">Env Vars</a> | <a href="#understanding-loopmd">Agent Loop</a> | <a href="#architecture">Architecture</a>
</p>

<p align="center">
The Lab is a lightweight API + dashboard that lets AI agents run structured research loops:
create ideas, branch, run experiments, compare metrics, and iterate — all managed through
a REST API with automatic git integration. No database, no setup beyond <code>pip install</code>.
</p>

<p align="center">
    <img src="https://github.com/user-attachments/assets/d8971a7e-a157-46bc-a0da-fd6091fea49e" alt="Dashboard screenshot"/>
</p>

---

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
the-lab-agent LOOP.md              # defaults to 15m loop interval
the-lab-agent LOOP.md -d 10m       # custom interval
the-lab-agent LOOP.md --model opus # pick a model
```

**Codex:**
```bash
cd /path/to/your/repo
codex --yolo
```
Then paste `LOOP.md` as the first message.

### 5. Watch it work

Open `http://localhost:8000` in your browser. The dashboard shows:
- **Subway graph** — idea branches as a subway map, colored by status + metric improvement (purple = new global best)
- **Metrics chart** — experiment results over time, scrollable, with multiple color modes
- **Timeline** — temporal view of when ideas were active
- **Log** — unified chronological feed of ideas, experiments, and notes

Hover over a graph node to highlight its ancestor path and corresponding chart points. Click a chart point to highlight the idea in the graph.

---

## Writing Experiment Scripts

Experiment scripts are bash scripts that the agent writes and the API executes. The scripts can call anything — Python, Rust, Node, other bash scripts — the only contract is how results are reported back.

### Final metrics (required)

The **last line of stdout** must be a JSON object with a `metrics` key. This can come from anywhere — a bash `echo`, a Python `print()`, or any other program:

```bash
#!/bin/bash
# Option A: bash echo
python train.py --lr 1e-4 --epochs 10
echo '{"metrics": {"accuracy": 0.84, "loss": 0.21}}'
```

```bash
#!/bin/bash
# Option B: let the Python script print it directly
python train.py --lr 1e-4 --epochs 10 --print-metrics
# where train.py ends with: print(json.dumps({"metrics": {"accuracy": acc}}))
```

You can optionally include `meta` to record extra info (the server merges it with the experiment's existing meta):

```json
{"metrics": {"accuracy": 0.84}, "meta": {"gpu_hours": 1.2, "actual_samples": 4832}}
```

### Live progress (optional)

For long-running experiments, write intermediate progress to `$THE_LAB_PROGRESS`. Any process can write this file — the bash wrapper, the Python training script, or a helper. Just write JSON to the path:

**From bash:**
```bash
echo '{"epoch": 5, "loss": 0.31, "pct": 50}' > "$THE_LAB_PROGRESS"
```

**From Python** (using the env var):
```python
import json, os
progress_path = os.environ.get("THE_LAB_PROGRESS")
if progress_path:
    with open(progress_path, "w") as f:
        json.dump({"epoch": epoch, "loss": loss, "pct": round(100 * epoch / total)}, f)
```

The dashboard polls this file and shows live progress in the detail panel, including a progress bar if a `pct`, `percent`, or `progress` field is present.

---

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

---

## Customizing LOOP.md

`LOOP.md` is the behavioral prompt you paste into the agent. It defines a four-step cycle (Orient → Work → Wait → Repeat) with the API endpoints baked in. You need to customize three parts — everything else (the API reference, the repeat logic) stays as-is.

### What to change

**1. Title and role** (top of the file) — describe what the agent is researching:

```markdown
# Research: Collaborative Agents
You are an autonomous researcher.
You design tools that enable useful communication between agents while solving
math problems. The goal is to improve the accuracy of agent swarms.
```

**2. Orient section** — tell the agent how to assess the current state of your specific research. What artifacts should it inspect? What signals matter?

```markdown
Check the most recent experiment, explore saved rollouts to make a conclusion —
this work is about agent collaboration, so we need to check how agents collaborate.
Explore the most promising ideas (those with the best improvements over their parents)
by checking the reasoning traces from their experiments.
```

**3. Work section** — add domain-specific guidance about how to approach the work:

```markdown
Our research system is backed by git. You can create new branches by adding a new
idea, merge feature branches by providing multiple parent_ids, checkout ideas and
run experiments in these branches.
Inspect what has been already tried before concluding which direction to go next.
```

### What NOT to change

The API endpoint blocks (the `` ``` `` code fences), the "Never Stop" section, and the "Repeat" section should stay as-is — they contain the protocol the agent needs to interact with The Lab.

---

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

---

## Architecture

```
[Agent] → REST API (FastAPI :8000) → File storage (.the_lab/)
                                   → Git operations (branch per idea)
                                   → Subprocess runner (experiments)
              ↕
         [Dashboard] ← Polls API for updates
```

- **No database** — everything is JSON files on disk
- **No worktrees** — standard git branches, auto-commit on checkout and experiment start
- **Survives restarts** — running experiments continue as orphaned processes; the server re-attaches on startup
