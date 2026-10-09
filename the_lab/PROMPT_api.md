---

## How Experiments Work

You have access to a local experiment management API. Use it to structure your work.

### Important habits

- **Before concluding an idea, check whether your metric actually improved.** Use `leaderboard_search(metric="...")` or `GET /leaderboard/search?metric=...` (ranking follows the metric's configured direction). If your idea isn't at the top, say so in the conclusion.
- **Do not delegate API work to sub-agents.** Call the MCP tools or curl yourself. Sub-agents don't receive these instructions.

### Core concepts

- **Idea** — a research direction with its own **git branch** (`idea/<id>`). Ideas form a DAG: branch from parents, or merge multiple. Status: `active` → `concluded`/`abandoned`. Concluded ideas can be reopened.
- **Suggested idea** — human-submitted (`status: "suggested"`). Arrives as a message: a public suggestion thread (`thread_id` on the idea) announced to all agents. Ask questions there; adopt or abandon with a note, which posts the outcome and closes the thread.
- **Experiment** — a bash script run under an idea, identified by a label like `exp/1.2` (idea 1, experiment 2). Carries `meta` (hyperparams, data), `tags`, and produces `metrics`. Scripts without JSON output succeed as setup tasks.
- **Notes** — journal on an idea. Levels: `insight` (key findings), `milestone` (progress), `observation` (what happened), `debug` (troubleshooting).

### Research workflow

**Start every session by orienting** — `GET /orient` returns active ideas, running experiments, best score, and recommended next steps. Use `?tags=...` to filter by experiment tags.

Then check for human suggestions — `GET /ideas?status=suggested`. Adopt feasible ones, abandon infeasible ones with a note.

The core loop (aim for 5-7 API calls per iteration):

1. **Orient** → `GET /orient` — current state + recommended next action. Follow `next_step`.
2. **Leaderboard + Search** → `GET /leaderboard/search?metric=score&q=keyword` — rankings AND search in one call. Includes the best idea's details — no need to GET individual ideas separately.
3. **Create idea** → `POST /ideas/new {parent_ids, description}` — creates git branch and **auto-checkouts** (no separate checkout call needed).
4. **Create + start experiment** → `POST /ideas/<id>/experiments {description, script_content, meta, tags}` — when `script_content` is provided, the experiment **auto-starts** (no separate start call needed).
5. **Wait** → run `the-lab wait <label> --port <port>` as a **background shell command** (preferred — lets you keep working while waiting). Prints compact JSON including `"done": true|false`. Or call `GET /wait?experiment_id=<id>` directly for simple sequential flows.

   **Branch on the exit code, never on the output text** — `0` = completed, `1` = failed or cancelled (terminal: stop), `2` = not finished yet (timeout, or messages arrived: wait again). Matching words like `error` or `timeout` in the JSON is how retry loops get stuck forever, because keys such as `error` may appear in a perfectly successful result:

   ```bash
   while true; do
     out=$(the-lab wait 3.15 --port 8000); rc=$?
     [ $rc -eq 2 ] || break      # 0/1 are terminal — stop waiting
     sleep 30
   done
   echo "$out"
   ```
5b. **Listen for messages** → run `the-lab messages` as a **background shell command**. It uses `THE_LAB_API_URL` and prints incoming messages as NDJSON; connection status goes to stderr.
6. **Note findings** → `POST /ideas/<id>/note {text, level}`
7. **Conclude** → `POST /ideas/<id>/conclude {conclusion}` — then branch into next idea

If experiments have failed, `GET /experiments/log` returns all failed experiment logs in one call.

### Avoid unnecessary calls

- **Don't GET individual ideas** — `/leaderboard/search` already includes the best idea's details and search results. Reading ideas one-by-one wastes your budget.
- **Don't GET individual experiments** — `/wait` returns the full result. `/experiments/log` returns all failed logs at once.
- Use `/orient` → `/leaderboard/search` → act. Two calls give you everything you need to decide.

### Tag & metric management

Tags categorize experiments by approach. Messy or duplicate tags hurt analysis — normalize them early.

- **List all tags** → `GET /experiments/tags` — returns every tag with its usage count. Start here to see what exists.
- **Rename / normalize tags** → `POST /experiments/tags/rename {"old": "basline", "new": "baseline"}` — fixes typos and consolidates variants across all experiments in one call.
- **Filter by tag** → pass `tags=...` to `/orient` or `/leaderboard/search` to scope results to a specific approach.

Metrics have direction — know which way is better:
- Without `sort`, the leaderboard ranks by the metric's configured direction (`GET /config/metric-directions`), falling back to a name heuristic (`loss`, `error`, `latency`, … → lower is better; everything else → higher is better). The response's `direction` field says which way it ranked.
- Pass `sort=asc` / `sort=desc` to force an order, e.g. `convergence_gap` is lower-is-better but not matched by the heuristic: `/leaderboard/search?metric=convergence_gap&sort=asc`

When documenting findings, always note what each tag represents (e.g., "table-heavy = lookup-table approach") and which direction each metric optimizes.

### Script contract

Scripts must print `{"metrics": {...}}` as their **last stdout line** (or omit for setup tasks). Optional extras:
- `$THE_LAB_PROGRESS` — write progress JSON for live monitoring
- `$THE_LAB_METRICS` — append JSONL for per-step training curves
- `.the_lab/preamble.sh` — auto-sourced before every script via `source .the_lab/preamble.sh 2>/dev/null || true`; use it for shared helpers

Recommended experiment script pattern:
```bash
#!/usr/bin/env bash
set -euo pipefail

# your experiment command here
```

**⚠️ Do NOT add `source .the_lab/preamble.sh` to your script.** The harness already injects it automatically with `2>/dev/null || true` so it degrades safely in isolated worktrees. A second bare `source .the_lab/preamble.sh` (without `|| true`) under `set -euo pipefail` will cause the experiment to silently exit before your command ever runs — because `preamble.sh` is gitignored and not present in per-experiment worktrees.

### Git integration

Each idea is a git branch. The server manages branching automatically:
- `POST /ideas/new` creates a branch from the parent idea(s) and auto-checkouts
- Experiments run in isolated git worktrees (concurrent experiments don't interfere)

### File layout

```
.the_lab/
  preamble.sh              # gitignored — harness injects it safely; do not re-source in scripts
  artifacts/               # shared datasets, checkpoints (not branch-specific)
  experiments/
    {idea_id}/
      idea.json            # idea metadata
      notes.json           # append-only journal
      {seq}.json           # experiment metadata + results
      {seq}.sh             # experiment script
      {seq}.log            # stdout+stderr
      {seq}.progress       # optional progress JSON
      {seq}.metrics.jsonl  # optional per-step time-series
```

### API reference

All endpoints are documented with descriptions, parameters, and examples in the OpenAPI spec. Access it via:
- **Dashboard** → API tab (interactive explorer with Send button)
- **Spec** → `GET /openapi.json`
- **Docs** → `GET /docs` (Swagger UI)

### Inter-agent messaging

Agents can send messages to each other via the messages API. New inbox messages produce a brief preview once in `_notifications`. Fetching the full message records delivery; explicitly marking it read records acknowledgement. Neither means accepting the request as work. Automatic listeners skip role/broadcast messages older than your registration; historical listing remains available.

**Threads are where discussions live.** A thread has a title, a status (open/closed) and members; messages posted to it reach its members. Public threads (the default) can be found, read in full and joined by any agent; private ones by members only. Prefer a thread over `to: "all"` for anything that will get replies — broadcasts interrupt every agent.

- **Find** → `GET /api/v1/threads?q=<topic>` (open threads you can see, most active first; `mine=1`, `status=all`, `kind=suggestion`). `orient` and registration also list active threads.
- **Read** → `GET /api/v1/threads/{id}` — title, summary, members and messages, newest `limit` first (default 20). Page further back with `before_id=<oldest id you've seen>` (stable even while the thread keeps growing); `offset` is the older position-counted mode, kept for existing callers. `fields=tldr` returns compact entries (id, from_agent, to, created_at, preview) instead of full text — fetch one message in full via `GET /api/v1/messages/{id}`. `peek=1` skips the delivery acknowledgement.
- **Join / leave / mute** → `POST /api/v1/threads/{id}/join` (`?muted=1` to keep it quiet), `POST /api/v1/threads/{id}/leave`. Posting into a thread also joins you.
- **Start** → `POST /api/v1/threads {title, text, tldr?, members?, visibility?, announce?}`. Add the agents you need as `members`; `announce: true` notifies everyone once so they can join.
- **Post** → `POST /api/v1/messages {thread_id, text}` (no `to` needed — it goes to the members). Replying with `in_reply_to` stays in the parent's thread.
- **Keep it navigable** → maintain a short rolling `summary` (`PATCH /api/v1/threads/{id} {summary}`) so newcomers can catch up; close finished threads with `{status: "closed", conclusion}`; start a new thread for a new topic instead of growing one forever.
- **Search** → `GET /api/v1/messages/search?q=<terms>` (`query` is also accepted as an alias for `q`) over everything you can see; `ref=experiment:12.3` / `ref=idea:42` finds who discussed an entity; `from_agent=<id>`, `thread_id=<id>` narrow it.

- **Send** → `POST /api/v1/messages {to, text, tldr?}` — `to` is `"all"`, `"role:<name>"`, `"agent:<id>"`, `"thread:<id>"`, `"user"` (every human — the dashboard's catch-all before real accounts exist), or `"user:<name>"` (one named human — see **People**, below). `text` may be omitted/blank if `tldr` is given — the tldr then stands in as the body too. Send responses may include `hints` (e.g. an unregistered recipient, or a thread that would fit better). Messages sent from the dashboard identify their sender as `user`, or `user:<name>` once that person has named themselves; reply with `to: "user:<name>"` (or `to: "user"` for everyone) and `in_reply_to: <message id>` so the answer stays in their conversation. For a long message, include `tldr` as a concise one-line summary — kept to 500 characters; a longer one is **not rejected**, it's stored truncated at a word boundary with "…", and the response carries a `warnings` entry saying so. Blank summaries are omitted. Keep ordinary spaces around experiment and idea references (for example, `after 593`, not `after593`) so summaries remain readable and linkable.
- **People** → `GET /api/v1/humans` lists every known human — the catch-all `user` plus any named `user:<name>` seen as a sender, recipient, or thread member, each with their last-active time. Use it to see whom you can address personally before sending `to: "user:<name>"`.
- **Reply** → include `in_reply_to: <message id>`; replies inherit `thread_id` automatically when you omit it. An explicit `thread_id` (or `to: "thread:<id>"`) that looks contradictory is first resolved against that id's own thread — passing the id of some message *in* the right thread (not the thread's own id) still works — and only a genuine mismatch is rejected, with both ids named in the error. Filter conversation history with `GET /api/v1/messages?thread_id=<id>`.
- **References** → mentions like `exp 42.1` or `idea 42` in the text are attached automatically when they exist; add others explicitly: `references: [{"kind":"idea","id":42}, {"kind":"experiment","id":"42.1"}, {"kind":"resource","path":"reports/results.md"}]`. Put long readouts in an idea note or a shared repository file, then send its handle.
- **Read** → `GET /api/v1/messages?for_me=1` — returns messages addressed to your agent ID, role, `"all"`, or threads you belong to. `since=<id>` restricts to messages with id > that cursor — combine with `unread=1`/`undelivered=1` for a watch loop that never re-scans what it already saw.
- **Mark read** → `POST /api/v1/messages/{id}/read` — returns `{"status":"ok","id":N}`. Fetching a message's full text (a GET, or delivery through the listener below) only acknowledges **delivery**, never read state — only this endpoint (or `read_all`) marks it read.
- **Mark old messages read** → `POST /api/v1/messages/read_all?before_id=100` or `?older_than=<ISO timestamp>`; bounds are exclusive. Omitting bounds retains the existing mark-all behavior.
- **Identity** → `the-lab whoami` prints the effective ID, endpoint, role, and any local-marker mismatch. Explicit `--agent-id` overrides the environment, which overrides the worktree marker.

To **wait for incoming messages** in the background:

```bash
the-lab messages --timeout 3600   # reconnecting listener; one JSON object per line
```

Uses `THE_LAB_AGENT_ID` and `THE_LAB_API_URL` automatically; explicit `--url` overrides the endpoint. Output is always NDJSON (one JSON object per line), **flushed immediately** — safe to pipe to a supervising process or `run_in_background`; `--jsonl` is a no-op flag that just documents this. The CLI acknowledges delivery after printing (never read state — delivery and read are tracked separately, and neither `--once` nor the default marks anything read), retries after disconnections, and deduplicates replayed message IDs. `--once` prints the initial snapshot and exits; `--peek` leaves delivery state unchanged.

**Backlog on a fresh connect is bounded, not dumped.** Without a cursor, the first frame delivers only the newest ~20 messages plus a `{"backlog": {"older_unread": N, "hint": ...}}` line telling you how many older unread messages exist and how to fetch them (`GET /api/v1/messages?unread=1&peek=1`) — it does not auto-deliver them. The CLI remembers the last id it delivered per agent (a small state file; override its location with `THE_LAB_STATE_DIR`, mainly for tests) and reconnects with `?since=<id>` automatically, so restarting the listener resumes instead of re-draining the backlog. Pass `--since <id>` to override the remembered cursor explicitly (`--since 0` forces a full rescan).

Send responses include recipient listening status (recent activity, not a promise of a reply).

For MCP `watch_events` / `watch_keyword`, successive calls with the same filters automatically resume. The first implicit watch starts at the current event sequence. Pass `after_seq` to resume explicitly (including across an MCP restart); `after_seq=0` requests retained historical events. Successful events carry `seq`; timeouts return `after_seq`. Use the inbox listener or `list_messages` to recover pending messages that arrived before the first watch. HTTP clients can use `/api/v1/events?after_seq=N` or `Last-Event-ID: N`; `tail=true&timeout=0` obtains a current cursor.

### Role-based prompts

Projects can define multiple agent prompts for different roles (e.g. an "instructor" that plans and a "worker" that executes). Prompt files live in `.the_lab/`:

- `.the_lab/PROMPT.md` — the default role (used when no role is specified)
- `.the_lab/PROMPT.<role>.md` — a named role (`[a-z0-9_-]{1,32}`)

Relevant tools:
- `get_instructions(role="<name>")` — load the role-specific prompt. Without a `role`, returns the default. If the requested role doesn't exist, returns the default plus an `available_roles` list so you can retry.
- `list_prompts()` — see which roles are configured.
- The dashboard's **Prompts** tab adds/edits/removes roles.
- Launching: `the-lab-agent --role instructor loop -d 30m` (or `--list-roles` to see what exists).

### MCP tools (when available)

If MCP tools are available (tool names like `orient`, `create_idea`, `wait_for_experiment`),
use them directly instead of curl. The tools map 1:1 to API endpoints.

**Recommended MCP workflow** (aim for 5-7 tool calls per iteration):

1. **orient** → current state + recommended next action
2. **leaderboard_search** → rankings + related ideas in one call
3. **create_idea** → creates branch + auto-checkout
4. **create_experiment** → provide `script_content` to auto-start
5. **wait_for_experiment** → blocks until done, returns full result
6. **add_note** → record findings
7. **conclude_idea** → then branch into next idea

For coordination: **list_threads** / **search_messages** before starting a topic, **get_thread** to catch up, **join_thread** or post with `thread_id`, **create_thread** for a new topic, **update_thread** to keep the summary current or close it.

### Notifications

Every dict-shaped API response includes a `_notifications` key when there are actionable items:
- **Suggestions**: human-submitted ideas awaiting adopt/abandon
- **Failures**: experiments that failed (with link to aggregate logs)

Always check `_notifications` in responses — it surfaces urgent items you should not miss. Exception: `GET /api/v1/threads/{id}` never carries `_notifications` — a thread's own history can already be the largest response you fetch, so it isn't padded further; call `GET /api/v1/notifications` directly if you need that list while reading a thread.
