<h3 align="center">
    <br/>
    <img src="https://github.com/user-attachments/assets/186c8033-25d2-4f98-a18c-e8d5d19c0dcd" width="300" alt="The Lab"/><br/><br/>
    Autonomous experiment management for AI research agents.<br/>
    Let agents run experiments, branch ideas, and track metrics — hands-free.
</h3>

<p align="center">
The Lab is a lightweight API + dashboard that lets AI agents run structured research.
No database, no setup beyond <code>pip install</code> and <code>the-lab init</code>.
</p>

---

## Quick Start

```bash
# Install
pip install -e .

# Initialize your project (interactive — sets up prompts, MCP, gitignore)
cd /path/to/your/repo
the-lab init

# Start the server + dashboard
the-lab .

# Launch an agent
the-lab-agent PROMPT_problem.md
```

That's it. Open the dashboard URL printed by the server and watch the agent work.

## How It Works

1. You describe your research problem in `PROMPT_problem.md` (Goal / Background / Setup)
2. The Lab concatenates it with API workflow docs into `PROMPT_generated.md`
3. An agent (Claude or Codex) reads the prompt, creates ideas as git branches, runs experiments, and iterates
4. The dashboard shows progress in real-time: metrics, graphs, tables, experiment logs

Ideas form a DAG — each idea is a git branch that can fork from parents. Experiments run in isolated worktrees so concurrent work never interferes.

## Project Setup

`the-lab init` walks you through everything interactively:

| Step | What it does |
|---|---|
| Git check | Initializes a git repo if needed |
| PROMPT_problem.md | Creates a template for your research problem |
| MCP bridge | Installs tools so agents can call the API directly (no curl) |
| .gitignore | Adds `.the_lab/`, `.claude/`, `.mcp.json`, prompt files |
| Claude pre-fill | Optionally has Claude analyze the repo and fill in the prompt |

Run it again anytime to update MCP tools or fix missing config.

## Writing Experiments

Experiment scripts are bash scripts. The only contract:

```bash
#!/bin/bash
python train.py --lr 1e-4 --epochs 10
# Last line of stdout = metrics (optional)
echo '{"metrics": {"accuracy": 0.84, "loss": 0.21}}'
```

Optional features for richer tracking:

| Feature | How |
|---|---|
| Live progress | Write JSON to `$THE_LAB_PROGRESS` (include `pct_complete` for progress rings) |
| Training curves | Append JSONL to `$THE_LAB_METRICS` |
| Shared setup | Put common env setup in `.the_lab/preamble.sh` |

## Dashboard

Dockable panel layout with drag, resize, float, maximize, and save/load.

**Default layout:**

```
Filters (full width)
Metrics (70%)  |  Scatter (30%)
Table/Graph/Timeline/Log (50%)  |  Detail (50%)
[API] [Stats] [Sandbox] [Task] [Suggest]  ← tray
```

**Key features:**
- **Table** — sortable multi-metric comparison, same filters as the chart
- **Metrics** — line chart with Improvements Only, Idea Mean, Hide Outliers, Log Scale
- **Progress rings** — running experiments show completion percentage everywhere
- **ETA** — estimated time remaining in the detail panel
- **Tray** — bottom bar for hidden panels; click to float, drag tabs to hide
- **Navigation** — click anywhere to cross-link between graph, chart, table, and detail

## Launching Agents

```bash
the-lab-agent PROMPT_problem.md                  # Claude, 15m loop
the-lab-agent PROMPT_problem.md --model opus     # specific model
the-lab-agent PROMPT_problem.md --agent codex    # Codex instead
the-lab-agent PROMPT_problem.md -d 5m            # 5 minute loop
the-lab-agent --sandbox                          # enable network sandbox
```

The agent gets MCP tools automatically — typed tool calls instead of curl.

## Human Ideas

Inject ideas via the Suggest panel in the tray, or via API:

```bash
curl -X POST http://localhost:8000/api/v1/ideas/suggest \
  -H 'Content-Type: application/json' \
  -d '{"description": "Try approach X", "priority": "high"}'
```

The agent sees suggestions via `_notifications` in every API response.

## Data & Config

All state lives in `.the_lab/` (gitignored, created automatically):

```
.the_lab/
  preamble.sh             # sourced before every experiment (optional)
  artifacts/              # shared datasets, checkpoints
  experiments/{idea_id}/  # idea metadata, experiment results, scripts, logs
```

**Preamble** — if `.the_lab/preamble.sh` exists, it's sourced before every experiment script. Use it for shared setup:

```bash
# .the_lab/preamble.sh
source venv/bin/activate
export CUDA_VISIBLE_DEVICES=0
```

**Environment variables** available in every experiment script:

| Variable | Example | Purpose |
|---|---|---|
| `THE_LAB_EXP_ID` | `"5.3"` | Experiment label |
| `THE_LAB_IDEA_ID` | `"5"` | Parent idea ID |
| `THE_LAB_PROGRESS` | path | Write progress JSON here |
| `THE_LAB_METRICS` | path | Append per-step JSONL here |

## Architecture

```
the_lab/
  app.py              — FastAPI app, notifications middleware
  routes/             — ideas, experiments, overview, operational
  store.py            — JSON-on-disk, in-memory cache
  runner.py           — subprocess + worktree isolation
  agent_skills/       — MCP bridge, settings, permissions
  PROMPT_api.md       — API workflow docs (shipped with package)

dashboard/
  Preact + Vite + dockview-core
```

- **No database** — JSON files in `.the_lab/`
- **Git integration** — idea = branch, auto-commit on checkout
- **Survives restarts** — running experiments re-attach
- **MCP + curl** — agents use typed tools or HTTP, both tracked
- **Network sandbox** — default-deny outbound, configurable allowlist
