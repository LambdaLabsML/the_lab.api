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

# Launch an agent (reads .the_lab/PROMPT.md by default)
the-lab-agent
```

That's it. Open the dashboard URL printed by the server and watch the agent work.

## How It Works

1. You describe your research problem in `.the_lab/PROMPT.md` (Goal / Background / Setup)
2. The agent calls the `get_instructions()` MCP tool at startup (or every loop iteration) to fetch the current prompt + API reference
3. An agent (Claude or Codex) creates ideas as git branches, runs experiments, and iterates
4. The dashboard shows progress in real-time: metrics, graphs, tables, experiment logs, rendered output viewer

Ideas form a DAG — each idea is a git branch that can fork from parents. Experiments run in isolated worktrees so concurrent work never interferes.

**Multiple roles per project** — define `.the_lab/PROMPT.<role>.md` for different agent personas (e.g. `instructor`, `worker`) and select one with `the-lab-agent --role <name>`. Manage roles from the dashboard's Prompts pane.

## Project Setup

`the-lab init` walks you through everything interactively:

| Step | What it does |
|---|---|
| Git check | Initializes a git repo if needed |
| `.the_lab/PROMPT.md` | Creates a template for your research problem (offers to migrate `<repo>/PROMPT.md` if it already exists at the legacy location) |
| MCP bridge | Installs tools so agents can call the API directly (no curl) |
| `.gitignore` | Adds `.the_lab/`, `.claude/`, `.mcp.json` |
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
| Rendered output | Write `<script>.output.md` (rendered as markdown — GFM tables, `<details>`, mermaid, nested lists) or `<script>.output.html` (injected raw — interactive widgets, `<script>` runs, relative `<img>` paths resolved) and click **Show output** in the dashboard |

## Dashboard

Dockable panel layout with drag, resize, float, maximize, and save/load.

**Default layout:**

```
Filters (full width)
Metrics (70%)  |  Scatter (30%)
Table/Graph/Timeline/Log (50%)  |  Detail (50%)
[API] [Stats] [Sandbox] [Prompts] [Task] [Suggest]  ← tray
```

**Key features:**
- **Table** — sortable multi-metric comparison, same filters as the chart
- **Metrics** — line chart with Improvements Only, Idea Mean, Hide Outliers, Log Scale
- **Output viewer** — click *Show output* to render `<script>.output.md` (or `.output.html`) inline: GFM tables, mermaid diagrams, nested lists, inline HTML widgets with live `<script>` execution, cross-file `.md` link navigation with back/anchor support
- **Progress rings** — running experiments show completion percentage everywhere
- **ETA** — estimated time remaining in the detail panel
- **Sandbox pane** — manage network allow/deny + per-path file rules (rw / ro / hidden)
- **Prompts pane** — add / edit / delete role-specific `PROMPT.<role>.md` files; copy `the-lab-agent --role <name>` launch command
- **Shareable URLs** — every lightbox sets a hash (`#idea=5&exp=5.3&view=output`); send the link, the recipient lands on the same view
- **Tray** — bottom bar for hidden panels; click to float, drag tabs to hide
- **Navigation** — click anywhere to cross-link between graph, chart, table, and detail

## Launching Agents

```bash
the-lab-agent                            # single run (reads .the_lab/PROMPT.md)
the-lab-agent "try a lower learning rate" # single run with inline prompt
the-lab-agent loop                       # loop every 15m
the-lab-agent loop -d 5m                 # loop every 5m
the-lab-agent --model opus               # specific model
the-lab-agent --agent codex              # Codex instead
the-lab-agent --role instructor          # use .the_lab/PROMPT.instructor.md
the-lab-agent --list-roles               # show configured roles + sizes
the-lab-agent --sandbox                  # enable network + file sandbox
```

The agent gets MCP tools automatically — typed tool calls instead of curl. `get_instructions(role=…)` re-fetches the current prompt + API reference each loop iteration so edits to `PROMPT.md` apply without restarting.

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
  PROMPT.md               # default prompt (replaces the old <repo>/PROMPT.md)
  PROMPT.<role>.md        # one per named role, optional
  preamble.sh             # sourced before every experiment (optional)
  artifacts/              # shared datasets, checkpoints
  experiments/{idea_id}/  # idea metadata, experiment results, scripts, logs
  worktrees/              # isolated git worktrees for concurrent experiments
  sandbox/                # network + file rules + access log (when sandbox enabled)
  api_perf.csv            # per-request timing log (when launched with --perf)
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
- **Git integration** — idea = branch, auto-commit on checkout, uncommitted changes carry to the new branch
- **Survives restarts** — running experiments re-attach
- **MCP + curl** — agents use typed tools or HTTP, both tracked
- **Sandbox v2** — `rootlesskit` + `bubblewrap` + transparent proxy. Default-deny outbound network with configurable allowlist *and* per-path file rules (rw / ro / invisible)
- **Performance** — version-keyed response cache with inflight coalescing, GZip on the wire, opt-in `--perf` CSV log of every request
