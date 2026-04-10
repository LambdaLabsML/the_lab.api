<h3 align="center">
    <br/>
    <img src="https://github.com/user-attachments/assets/186c8033-25d2-4f98-a18c-e8d5d19c0dcd" width="300" alt="The Lab"/><br/><br/>
    Autonomous experiment management for AI research agents.<br/>
    Let agents <a href="#writing-experiment-scripts">run experiments</a>, <a href="#defining-your-research-problem">branch ideas</a>, and <a href="#5-watch-it-work">track metrics</a> — hands-free.
</h3>

<p align="center">
    Jump to: <a href="#quick-start">Quick Start</a> | <a href="#defining-your-research-problem">Problem Setup</a> | <a href="#writing-experiment-scripts">Experiments</a> | <a href="#dashboard">Dashboard</a> | <a href="#architecture">Architecture</a>
</p>

<p align="center">
The Lab is a lightweight API + dockable dashboard that lets AI agents run structured research:
create ideas, branch, run experiments, compare metrics, and iterate — all managed through
a REST API with automatic git integration. No database, no setup beyond <code>pip install</code>.
</p>

---

## Quick Start

### 1. Install

```bash
pip install -e .
```

### 2. Define your research problem

Create `PROMPT_problem.md` in your target repository:

```markdown
# Swarm Math Problem Solving

## Goal
Improve multi-agent accuracy on competition math problems. Beat the solo baseline of 10%.

## Background
We have a framework that spawns N agents to solve math problems collaboratively.
Prior work: solo agent gets 10% on our heldout set.

## Setup
- Hardware: 8xH100 node
- Data: problems in /data/heldout/*.json
- Run: python run_swarm.py --config <path>
```

That's it — just describe the problem. The Lab ships with `PROMPT_api.md` which documents the API workflow. When the agent launches, both files are concatenated automatically into `PROMPT_generated.md`.

If you only have a `PROMPT.md` (legacy), it works as-is.

### 3. Start the API server

```bash
# From your target repo (must be a git repository)
the-lab /path/to/your/repo

# Custom port
the-lab /path/to/your/repo --port 8001

# With hot-reload during development
the-lab /path/to/your/repo --dev
```

Open the dashboard at `http://localhost:8001`.

### 4. Launch an agent

**Recommended — `the-lab-agent`:**
```bash
the-lab-agent PROMPT_problem.md                     # Claude, 15m loop
the-lab-agent PROMPT_problem.md --model opus        # specific model
the-lab-agent PROMPT_problem.md --agent codex       # use Codex instead
the-lab-agent PROMPT_problem.md -d 5m               # 5 minute loop interval
the-lab-agent PROMPT_problem.md --no-sandbox        # skip network sandbox
```

This automatically concatenates `PROMPT_problem.md` + `PROMPT_api.md`, launches the agent in a loop, and manages sandboxing.

**Manual launch** (if you prefer):
```bash
cd /path/to/your/repo
claude --dangerously-skip-permissions PROMPT_generated.md
```

### 5. Watch it work

Open the dashboard. The dockable panel layout shows:
- **Graph** — idea branches as a subway map, colored by status + metric improvement
- **Metrics** — experiment results over time with Improvements Only, Idea Mean, and Hide Outliers toggles
- **Scatter** — 2D metric-A vs metric-B scatter plot
- **Detail** — selected idea with experiments, notes, JSON viewer
- **Timeline** — temporal view of when ideas were active
- **Log** — unified chronological feed of ideas, experiments, and notes

All panels are draggable, resizable, floatable. Maximize any panel fullscreen and add floating overlays on top. Layouts persist and can be saved/loaded by name.

---

## Defining Your Research Problem

### File structure

| File | Purpose | Who writes it |
|---|---|---|
| `PROMPT_problem.md` | Your research goal, background, setup | You |
| `PROMPT_api.md` | API workflow, endpoints, script contract | Ships with The Lab |
| `PROMPT_generated.md` | Concatenation of both (auto-generated) | `the-lab-agent` |

### What goes in PROMPT_problem.md

Three sections:

1. **Goal** — what you're optimizing and the success criteria
2. **Background** — prior work, constraints, relevant context
3. **Setup** — hardware, software, data locations, how to run things

Keep it concise. The agent reads this plus the API docs and starts working.

### What NOT to include

Don't describe how to use The Lab API — that's in `PROMPT_api.md`. Don't list API endpoints or the experiment workflow. Just describe your research problem.

---

## Writing Experiment Scripts

Experiment scripts are bash scripts that the agent writes and the API executes. The only contract is how results are reported.

### Final metrics (optional)

The **last line of stdout** should be JSON with a `metrics` key:

```bash
#!/bin/bash
python train.py --lr 1e-4 --epochs 10
echo '{"metrics": {"accuracy": 0.84, "loss": 0.21}}'
```

For setup tasks (data download, env install), metrics can be omitted — exit code 0 = success.

### Auto-start

When creating an experiment with `script_content`, it auto-starts — no separate POST /start needed:

```
POST /ideas/1/experiments
{"description": "test lr=1e-4", "script_content": "#!/bin/bash\npython train.py --lr 1e-4"}
```

### Live progress (optional)

Write JSON to `$THE_LAB_PROGRESS` for live dashboard updates:

```python
import json, os
progress_path = os.environ.get("THE_LAB_PROGRESS")
if progress_path:
    with open(progress_path, "w") as f:
        json.dump({"epoch": epoch, "loss": loss, "pct": round(100 * epoch / total)}, f)
```

### Time-series metrics (optional)

Append per-step metrics to `$THE_LAB_METRICS` for training curves:

```python
metrics_path = os.environ.get("THE_LAB_METRICS")
if metrics_path:
    with open(metrics_path, "a") as f:
        f.write(json.dumps({"step": step, "train_loss": loss}) + "\n")
```

### Experiment preamble

If `.the_lab/preamble.sh` exists, it's sourced before every experiment script:

```bash
# .the_lab/preamble.sh
source venv/bin/activate
export CUDA_VISIBLE_DEVICES=0
```

### Experiment IDs

Experiments are identified by per-idea labels: `exp/5.3` = idea 5, experiment 3. API endpoints accept these labels: `GET /experiments/5.3`.

---

## Environment Variables

Every experiment script receives:

| Variable | Description |
|---|---|
| `THE_LAB_TOKEN` | Auth token proving the script was launched by the API |
| `THE_LAB_EXP_ID` | Experiment label (e.g. `"5.3"`) |
| `THE_LAB_IDEA_ID` | Parent idea ID (e.g. `"5"`) |
| `THE_LAB_PROGRESS` | Path to write progress JSON |
| `THE_LAB_METRICS` | Path to append time-series JSONL |

---

## Dashboard

The dashboard uses **dockview-core** for a fully dockable, resizable panel layout.

### Panels

12 dockable panels: Graph, Timeline, Log, Metrics, Scatter, Detail, API, Stats, Sandbox, Suggest, Filters, Task.

### Key features

- **Drag tabs** to rearrange, split, or merge panels
- **Float** any panel (⬚ button or long-press tab → Float)
- **Maximize** any panel fullscreen (⤢ button)
- **Fullscreen workspaces**: maximize a panel, add floating overlays, exit — arrangement remembered per-panel
- **Save/load named layouts** (Layouts dropdown in topbar)
- **Long-press / right-click** on tabs for move menu (touch-friendly)
- **Bidirectional navigation**: click graph node → detail panel, click experiment → graph highlights
- **JSON viewer**: syntax-highlighted, collapsible JSON for experiment metrics and meta

### Metrics chart

- Metric selector, color modes, Improvements Only, Idea Mean (μ), Hide Outliers
- 2D scatter chart for metric-A vs metric-B comparisons

---

## Human Idea Submission

Humans can inject ideas via the Suggest panel or API:

```bash
curl -X POST http://localhost:8001/api/v1/ideas/suggest \
  -H 'Content-Type: application/json' \
  -d '{"description": "Try complex-valued state from Mamba-3 paper", "priority": "high"}'
```

The agent checks for suggestions during its Orient phase and either adopts or abandons with a note.

---

## Network Sandbox

`the-lab-agent` launches agents inside a network sandbox by default:
- Default deny for outbound internet
- Built-in allowlist for package managers (pip, apt, uv)
- User-editable allowlist/denylist via the Sandbox panel

Pass `--no-sandbox` to opt out. Manual `claude` / `codex` launches are not sandboxed.

---

## Data Storage

All data lives in `.the_lab/` (git-ignored via `.git/info/exclude`):

```
.the_lab/
  preamble.sh             # sourced before every experiment
  artifacts/              # shared datasets, checkpoints
  experiments/
    5/
      idea.json           # idea metadata
      notes.json          # journal (text, level)
      3.json              # experiment: exp/5.3
      3.sh                # script
      3.log               # stdout+stderr
      3.progress          # live progress JSON
      3.metrics.jsonl     # training curves
```

---

## Architecture

```
the_lab/
  app.py              — FastAPI app (129 lines), router registration
  deps.py             — shared state + helpers
  schemas.py          — Pydantic request models
  routes/
    ideas.py          — 15 idea endpoints
    experiments.py    — 15 experiment endpoints + timeseries
    overview.py       — leaderboard, orient, wait, graph, chart-data
    operational.py    — task, config, sandbox, stats, chat
  PROMPT_api.md       — API workflow docs (shipped with package)
  store.py            — file-backed store, per-idea experiment IDs

dashboard/
  dockview-core       — vanilla TS docking framework
  Preact + Vite       — 12 panel components
```

- **No database** — JSON files on disk
- **Git integration** — idea = branch, auto-commit on checkout
- **Survives restarts** — running experiments re-attach on startup
- **Multi-agent** — supports Claude and Codex
