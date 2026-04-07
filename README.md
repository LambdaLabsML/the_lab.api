<h3 align="center">
    <br/>
    <img src="https://github.com/user-attachments/assets/186c8033-25d2-4f98-a18c-e8d5d19c0dcd" width="300" alt="The Lab"/><br/><br/>
    Autonomous experiment management for AI research agents.<br/>
    Let agents <a href="#writing-experiment-scripts">run experiments</a>, <a href="#understanding-loopmd">branch ideas</a>, and <a href="#5-watch-it-work">track metrics</a> — hands-free.
</h3>

<p align="center">
    Jump to: <a href="#quick-start">Quick Start</a> | <a href="#writing-experiment-scripts">Experiments</a> | <a href="#environment-variables">Env Vars</a> | <a href="#understanding-loopmd">Agent Loop</a> | <a href="#research-chatbot">Chatbot</a> | <a href="#architecture">Architecture</a>
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

**Claude Code** (recommended — use `/loop` for continuous operation):
```bash
cd /path/to/your/repo
claude --dangerously-skip-permissions
```
Then in the Claude session:
```
/loop 5m Start by reading the API reference /openapi.json. Then, review finished experiments using the tools in .the_lab/artifacts, conclude and combine ideas that worked well and tag all experiments that YOU start as claude-4.6. Don't abandon ideas unless you are EXTREMELY certain. <paste LOOP.md content here>
```

Or use `the-lab-agent` for a one-liner:
```bash
the-lab-agent LOOP.md              # defaults to 15m loop interval
the-lab-agent LOOP.md -d 5m        # custom interval
the-lab-agent LOOP.md --model opus # pick a model
the-lab-agent LOOP.md --agent codex # launch Codex with --yolo instead
the-lab-agent LOOP.md --no-sandbox # opt out of sandboxing for this launch
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

### Final metrics (optional)

For research experiments, the **last line of stdout** should be a JSON object with a `metrics` key. This can come from anywhere — a bash `echo`, a Python `print()`, or any other program. For setup tasks (data download, env install), metrics can be omitted entirely — the experiment is marked completed if exit code is 0:

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

### Time-series metrics (optional)

For training experiments, append per-step metrics to `$THE_LAB_METRICS` for training curve tracking. This is an append-only JSONL file — each line is one data point:

**From Python:**
```python
import json, os
metrics_path = os.environ.get("THE_LAB_METRICS")
if metrics_path:
    with open(metrics_path, "a") as f:
        f.write(json.dumps({"step": step, "train_loss": loss, "lr": lr}) + "\n")
```

The dashboard shows training curves in the experiment detail panel. Compare curves across experiments with `GET /experiments/compare-curves?ids=7,12&key=train_loss`.

### Experiment preamble

If `.the_lab/preamble.sh` exists, it is automatically sourced at the start of every experiment script (after the guard, before user content). Use it for common setup:

```bash
# .the_lab/preamble.sh
source venv/bin/activate
export PYTHONPATH=.
export CUDA_VISIBLE_DEVICES=0
```

### Experiment tags

Experiments can have optional `tags` (list of strings) for categorization:

```
POST /ideas/1/experiments  {description: "...", tags: ["ablation", "synthetic"]}
```

---

## Environment Variables

Every experiment script receives these environment variables:

| Variable | Description |
|----------|-------------|
| `THE_LAB_TOKEN` | Auth token proving the script was launched by the API |
| `THE_LAB_EXP_ID` | This experiment's ID (e.g. `"23"`) |
| `THE_LAB_IDEA_ID` | The parent idea's ID (e.g. `"5"`) |
| `THE_LAB_PROGRESS` | Path to write progress JSON (e.g. `.the_lab/experiments/5/23.progress`) |
| `THE_LAB_METRICS` | Path to append time-series JSONL (e.g. `.the_lab/experiments/5/23.metrics.jsonl`) |

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

## Human Idea Submission

Humans can inject ideas into the agent's research loop via the dashboard or the API. Ideas from humans start as `suggested` — the agent decides whether to adopt or reject them.

### Via the dashboard

Open `http://localhost:8000` and use the "Suggest idea" panel to submit a description, optional parent idea, priority (normal/high), and resource links (papers, repos, datasets).

### Via the API

```bash
curl -X POST http://localhost:8000/api/v1/ideas/suggest \
  -H 'Content-Type: application/json' \
  -d '{
    "description": "Try complex-valued state from Mamba-3 paper",
    "priority": "high",
    "resources": [
      {"url": "https://arxiv.org/abs/2503.xxxxx", "label": "Mamba-3 paper, Section 3.2"}
    ]
  }'
```

The agent checks for suggestions during its Orient phase and either adopts them (`POST /ideas/<id>/adopt`) or abandons with a note explaining why.

---

## Research Digest

The digest endpoint provides a compact summary of all research for agent context efficiency:

```
GET /api/v1/digest
```

Returns: total ideas/experiments, global best metrics (with source idea/experiment), concluded ideas with conclusions and key insights, abandoned ideas with reasons, plus the current open ideas and running experiments. Designed to fit in a manageable context window even after 50+ ideas.

---

## Research Chatbot

The dashboard includes an AI-powered chat panel that lets you ask questions about your research project — experiment comparisons, best results, key insights, or trends. It uses Claude with the full project state as context.

### Setup

Create a `.env` file in your repository root with your Anthropic API key:

```bash
ANTHROPIC_API_KEY=sk-ant-...your-key-here...
```

The server loads this file automatically on startup. You can also set the environment variable directly instead of using a `.env` file.

### Usage

Once an API key is configured, a green chat button appears in the bottom-right corner of the dashboard. Click it to open the chat panel. Example questions:

- "What is the best result so far?"
- "Compare experiments #12 and #15 — what changed and how did metrics differ?"
- "Which ideas have been abandoned and why?"
- "Summarize the key insights from concluded ideas"

The chatbot streams responses in real time and cites specific idea/experiment IDs with actual metric values.

---

## Experiment Timeout

Prevent runaway experiments by specifying a timeout (in seconds) when starting:

```
POST /experiments/<id>/start  {"timeout": 1200}
```

If the experiment exceeds the timeout, it is killed (SIGTERM → SIGKILL) and marked as failed.

---

## Network Sandbox

Outbound network sandboxing applies to:
- experiments started through the API while the Sandbox tab toggle is enabled
- Claude or Codex launched through `the-lab-agent` (unless you pass `--no-sandbox`)

The sandbox is **network-only** — local file reads/writes still work normally with the process's usual OS permissions. Experiments still run from their worktree directory, and `.the_lab/` remains writable on disk.

### Default behavior

- **Default deny** for outbound internet access
- **Built-in allowlist** for package installation hosts used by `uv`, `pip`, and `apt-get` (derived from common defaults plus your local pip/apt config where possible)
- **User allowlist / denylist** editable live from the dashboard's **Sandbox** tab
- **Observed access log** ("gray list") that records requested destinations and whether they were allowed or blocked

Changes in the Sandbox tab apply automatically to new outbound connections from already-running sandboxed processes.

### Notes

- Manual `claude` / `codex` launches are not intercepted; use `the-lab-agent` if you want the agent session sandboxed by The Lab.
- The Sandbox tab toggle controls experiment runs. `the-lab-agent` always launches inside the sandbox unless you pass `--no-sandbox`.
- The sandbox configuration and observed access log live under `.the_lab/sandbox/`.

---

## Data Storage

All experiment data lives in `.the_lab/` at the repo root. This directory is git-ignored (via `.git/info/exclude`) so it persists across branch switches:

```
.the_lab/
  preamble.sh             # optional: sourced at the start of every experiment script
  artifacts/              # shared datasets, checkpoints (not branch-specific)
  experiments/
    1/
      idea.json           # idea metadata (description, status, source, priority, resources)
      notes.json          # running journal (text, level, resources)
      1.json              # experiment metadata + results (description, meta, metrics, tags)
      1.sh                # experiment script
      1.log               # stdout+stderr (streams in real-time)
      1.progress          # optional progress JSON
      1.metrics.jsonl     # optional per-step time-series metrics
      1.err               # error details if failed
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
