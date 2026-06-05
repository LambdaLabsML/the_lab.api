<h3 align="center">the_lab.api</h3>

<p align="center">
  <img width="900" height="474" alt="the_lab.api Window" src="https://github.com/user-attachments/assets/5a7e8505-9f20-4958-86fa-3fe04b59cf7b" /><br/><br/>
</p>

<p align="center">
    Autonomous experiment management for AI research agents.<br/>
    Let agents run experiments, branch ideas, and track metrics — hands-free.
</p>

<p align="center">
The Lab is a lightweight API + dashboard that lets AI agents run structured research.
No database, no setup beyond installing the CLI and running <code>the-lab init</code>.
</p>

<p align="center">
Jump To: <a href="#quick-start">Quick Start</a>, <a href="#how-it-works">How It Works</a>, <a href="#under-the-hood">Under the Hood</a>
</p>

<br/>

---

<br/>

## Quick Start

```bash
# User install
pipx install git+https://github.com/LambdaLabsML/the_lab.api.git

# Or install from source while developing
git clone https://github.com/LambdaLabsML/the_lab.api.git
cd the_lab.api
pip install -e .

# Initialize your project (interactive — sets up prompts, MCP, gitignore)
cd /path/to/your/repo
the-lab init

# Start the API + dashboard
the-lab .

# Launch one agent turn, or keep it looping
the-lab-agent
the-lab-agent loop -d 15m
```

That's it. Open `http://localhost:8000` and watch the agent work.

See [LambdaLabsML/the_lab.api.demo](https://github.com/LambdaLabsML/the_lab.api.demo) for a working example project.

<br/>

```bash
the-lab . --port 9009           # bind a different port
the-lab . --host 127.0.0.1      # bind a different host
the-lab . --https               # self-signed HTTPS
the-lab . --perf                # write .the_lab/api_perf.csv
the-lab-agent --role instructor # use .the_lab/PROMPT.instructor.md
the-lab-agent --sandbox on      # enable network + file sandbox
the-lab-agent --agent codex     # Codex instead of Claude
```

To enable HTTP Basic Auth, set credentials before starting the server — the agent and MCP bridge pick them up automatically from the same env vars:

```bash
export THE_LAB_USER=alice
export THE_LAB_PASSWORD=secret
the-lab .                  # server enforces auth
the-lab-agent              # agent authenticates automatically
```

<br/>

---

<br/>

## How It Works

1. `the-lab init` scaffolds `.the_lab/PROMPT.md` and optionally has Claude analyze your repo and fill it in — describe your research goal, background, and setup there
2. `the-lab-agent` registers an isolated per-agent worktree, then launches Claude or Codex with the MCP bridge attached
3. The agent calls `get_instructions()` to fetch the current prompt + API reference, creates ideas as git branches, runs experiments, and iterates
4. The dashboard shows progress in real time: metrics, graphs, tables, experiment logs, rendered output, agent state, prompts, sandbox config, and suggestions

Ideas form a DAG — each idea is a git branch that can fork from parents. Experiments and agents each run in isolated worktrees, so concurrent work does not collide.

<br/>

**Panes** — the dashboard is a dockable layout; every pane can be floated, resized, or hidden to the tray:

| Pane | What it shows / does |
|---|---|
| Filters | Global filter bar — by tag, status, experiment mode; affects all charts and table |
| Metrics | Line chart of any logged metric over time — log scale, outlier clipping, best-line overlay, colorblind palette |
| Scatter | Cross-metric scatter — plot any two metrics against each other |
| Table | Sortable multi-metric comparison across all experiments |
| Graph | Idea DAG — relationships, status, and improvement at a glance |
| Timeline | Experiments and notes interleaved chronologically |
| Log | Live experiment log viewer with full-text search |
| Detail | Per-idea drill-down: experiments, notes, log, output, script, diff |
| Agents | Active and past agent sessions — output log, token cost, resume command |
| Queue | Running and queued experiments; priority and resource assignment |
| Messages | Inter-agent messages and notifications |
| Sandbox | Network allow/deny lists + per-path file rules (rw / ro / hidden) |
| Prompts | Edit role-specific `PROMPT.<role>.md` files; copy agent launch command |
| Task | Current task description and status |
| Stats | API request timing and sizes |
| Suggest | Submit a new idea directly from the dashboard |
| API | Inline API reference |

<br/>

---

<br/>

## Under the Hood

- **No database** — all state in `.the_lab/` as JSON files (gitignored, created automatically)
- **Ideas are git branches** — checkout, uncommitted changes carry to the new branch automatically
- **Isolated worktrees** — each experiment and each agent gets its own git worktree; concurrent runs never collide
- **Survives restarts** — running experiments re-attach on server startup
- **MCP + curl** — agents use typed MCP tools or plain HTTP, both work
- **Sandbox** — `rootlesskit` + `bubblewrap` + transparent proxy; default-deny network with per-path file rules (rw / ro / hidden)
- **Auth** — optional Basic Auth; scoped bearer tokens for experiment callbacks
