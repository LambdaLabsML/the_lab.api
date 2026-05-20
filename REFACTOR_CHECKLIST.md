# Refactor checklist — the_lab.api

Working document for the API + dashboard codebase. We walk top-down
(entry points first, leaves last). Per-file notes flag dependencies,
gotchas, and known technical debt. Tick boxes as files get reviewed /
refactored / deleted.

Legend:
- 🟢 stable / minor changes only
- 🟡 needs structural attention
- 🔴 god-object / major refactor target
- ⚫ likely dead or superseded, candidate for deletion

Tasks:
- initiated by `- task:`. Work on the task and provide an answer as a
  nested list entry below. When done, change the top-level item to
  `- [.]` (to be reviewed by the human).
- If the top-level item is `- [x]` it has been checked by the human;
  the description sublist can be removed.

---

## Level 1 — Entry points & application factory

- [ ] 🔴 **`the_lab/app.py`** *(~430 LOC)*
  - FastAPI application factory. Creates `Store`, `ExperimentRunner`,
    `ApiStats`; calls `deps.init()`; registers all 8 routers; mounts the
    Vite SPA bundle.
  - Hosts four `@app.middleware("http")` functions, the
    `@app.websocket("/api/v1/ws")` endpoint, and the
    `@app.get("/{path:path}")` SPA fallback.
  - **Recent**: added `from . import ws as _ws_mod` at startup so the
    broadcaster singleton is initialised before any route handler runs.
  - **Recent**: WebSocket endpoint with token auth (`?token=<base64>`),
    missed-event replay via `?since=N`, 30s ping, and per-subscriber
    queue. Token auth is separate from the HTTP Basic Auth middleware
    (browsers can't set headers on WebSocket connections).
  - **Gotcha**: `build_notifications()` and `GET /api/v1/notifications`
    live here rather than in a dedicated router — they ended up
    here to avoid import cycles but belong elsewhere.
  - **Gotcha**: `_SPA_HTML` is read once at import time; the file path is
    resolved relative to `__file__`, so the server must be started from
    a known directory.
  - The GZip ordering note in the comment is critical — don't rearrange
    `add_middleware` vs. `@app.middleware` without re-reading it.

- [ ] 🟡 **`the_lab/cli.py`** *(504 LOC)*
  - `the-lab` entrypoint. Subcommands: `init`, `run`, `dev`, `agent`.
  - `_build_dashboard()` does an mtime-based Vite rebuild; the node
    detection path for NVM is fragile and has caused silent stale-bundle
    issues in the past (see session history May 5).
  - `_write_gitignore()` appends to `.gitignore` idempotently — be
    careful if the file has been manually modified.

- [ ] 🟡 **`the_lab/agent_cli.py`** *(409 LOC)*
  - `the-lab-agent` entrypoint. Registers a per-agent worktree, builds
    MCP config, launches `claude`/`codex` in isolated mode via `Popen`.
  - **Recent**: fixed to prefer the packaged `lab_api_mcp.py` bridge over
    any workspace-local copy (`THE_LAB_LOCAL_MCP=1` to opt back in).
    Stale local copies caused X-Agent-Id to be missing → git ops leaked
    to main repo (May 8 incident).
  - Signal-forwarding in the wrapper loop is correct; don't replace with
    `os.execvpe` or you'll lose the cleanup path.
  - `mcp_config` is written to a temp file because `--mcp-config` consumes
    subsequent args; the `"--"` terminator before the prompt is required.

---

## Level 2 — Storage / state layer

- [ ] 🔴 **`the_lab/store.py`** *(587 LOC)*
  - Single source of truth. Holds every idea and experiment in two
    in-memory dicts loaded from `.the_lab/experiments/<idea_id>/…` at
    startup. All writes go through `_write_idea` / `_write_experiment`
    (disk + cache update in one call).
  - `_version` counter is the cache invalidation key; only bump it for
    changes that affect rendered responses (see commit `14cafb1`).
  - `Store.LISTING_LEVELS` controls which note levels ship with
    `GET /ideas` — currently `{"insight", "milestone", "observation"}`.
  - `resolve_experiment(ref)` accepts both the dotted label `"1.2"` and
    legacy integer IDs; all new call sites should use labels.
  - **Gotcha**: `list_all_experiments()` returns from the in-memory dict
    only; if the experiments directory is wiped without restarting the
    server, the cache diverges silently.

- [ ] 🟢 **`the_lab/git_ops.py`** *(326 LOC)*
  - All git subprocess calls live here. `checkout_idea` / `create_branch_from` /
    `remove_worktree` / `auto_commit` / `branch_diff` / etc.
  - `create_branch_from` is "create only, no checkout" — this burned us:
    `adopt_idea` used to call it and leave the agent on `agent_init_<id>`.
    Now fixed to call `checkout_idea` after.
  - `_run()` raises `GitError` (subclass of `RuntimeError`) on non-zero
    exit; callers should catch it, not the generic `Exception`.

---

## Level 2.5 — WebSocket push layer

- [ ] 🟢 **`the_lab/ws.py`** *(~110 LOC)*
  - `Broadcaster` singleton. Ring buffer (deque maxlen=500), list of
    per-subscriber `asyncio.Queue`s, monotonic seq counter.
  - `broadcast(event)` — async-context callers (routes, runner tasks).
    Stamps seq, appends to ring, fans out to queues; drops oldest item
    if a queue exceeds 100 items (prevents slow clients back-pressuring).
  - `broadcast_soon(event)` — sync-context callers (store, messages).
    Uses `loop.call_soon_threadsafe`; silently drops if no loop is
    captured yet (safe: no subscribers can exist at that point either).
  - `subscribe() / unsubscribe(q)` — register/deregister a Queue for the
    lifetime of a WebSocket connection.
  - `replay_since(seq) → list[dict]` — returns buffered events with
    seq > given value; used to catch up reconnecting clients.
  - **Gotcha**: `_loop` is captured lazily on the first `broadcast()` /
    `subscribe()` call from inside the event loop. If `broadcast_soon`
    is called before the loop is running, it drops silently. Routes that
    need guaranteed delivery (like `send_message`) should be `async def`
    so they call `broadcast()` directly.

---

## Level 3 — Execution engine

- [ ] 🔴 **`the_lab/runner.py`** *(~900 LOC — largest file)*
  - `ExperimentRunner` owns the scheduler asyncio task, the allocator
    reference, subprocess monitoring, PID recovery, and worktree cleanup.
  - Three concerns that would each make a clean separate class:
    1. **Scheduler** (`_scheduler_loop`, `_dispatch_once`, `wake_scheduler`):
       ticks every `dispatch_interval_s`, walks queued exps, reserves
       resource units, calls `start()`.
    2. **Monitor** (`_monitor`, `_monitor_pid`, `_reconcile_stale_running`):
       polls running PIDs; extracts JSON result from log; strips result
       line from disk on completion.
    3. **Lifecycle** (`start`, `cancel`, `get_log`, `wait_any`): public
       API consumed by routes.
  - **Gotcha**: `_extract_json` now returns `(dict | None, int | None)` —
    the int is the line index to strip. Every call site must unpack.
  - **Recent**: emits WebSocket events at key lifecycle points:
    `experiment_queued` (in routes/experiments.py), `experiment_started`
    (after scheduler dispatch), `experiment_finished`/`experiment_cancelled`
    (in monitor/cancel paths), `queue_changed` throttled to 1/s from
    `wake_scheduler()`.
  - **Gotcha**: `wait_any` races `_finished_queue.get()` vs.
    `messages._wake_event().wait()`. The wake event is cleared inside the
    loop after reading it; if you add another listener you must handle the
    clear/reset cycle carefully.
  - Import guard in `wait_any` (`from . import messages`) avoids the
    circular-import at module load time.

- [ ] 🟡 **`the_lab/queue.py`** *(293 LOC)*
  - `Resource` / `QueueConfig` dataclasses + `Allocator` (thread-safe unit
    reservation) + `match_resource()` + config CRUD helpers.
  - `Allocator` is in-memory only; rebuilt from running experiments'
    `meta.assigned_resource` / `meta.assigned_units` at startup via
    `restore_from_running()`. If the server dies mid-run the allocator
    is empty on restart, but the scheduler re-loads correctly because
    running experiments are re-attached.
  - `match_resource(resources, requirements)` treats `kind=None` and
    `kind="any"` as match-any — important for vLLM-side experiments where
    the GPU is held by the server, not the experiment process.
  - `jobs_per_unit > 1.0` (unit sharing) is refused in v1.

---

## Level 4 — API Routes

All routers are registered in `app.py` under `/api/v1`.

- [ ] 🔴 **`the_lab/routes/overview.py`** *(1,057 LOC — largest route)*
  - Leaderboard, digest, backlog, orient, graph, chart-data, wait,
    aggregate, compare, field-projection.
  - `_build_leaderboard_response()` (~200 LOC) is called by three
    endpoints; hard to test in isolation.
  - `get_graph()` and `get_backlog()` are `@cached_response` decorated;
    `get_chart_data()` is deliberately uncached (reads `.progress` files).
  - `/wait` is async; all others are sync (run in FastAPI's threadpool).
    The `build_notifications` helper in `app.py` is called from here
    indirectly via middleware.
  - `resolve_metric()` from `deps` now handles dot-notation paths into
    nested metric dicts (e.g. `subagent.cache_hits`).

- [ ] 🔴 **`the_lab/routes/experiments.py`** *(1,035 LOC)*
  - Everything experiment-related: CRUD, execution control, log/output/
    script/progress endpoints, timeseries, tag management, analysis runner.
  - `_wrap_script` was moved to `deps.py`; some inline logic still remains.
  - `/start` now queues instead of bypassing the scheduler (fixed May 17).
    `/rerun` does the same.
  - **Gotcha**: `store.update_experiment` clears `error` and `finished_at`
    when re-queuing a failed run via `/start` — this is deliberate but
    easy to forget.
  - `GET /experiments/{ref}/log` strips trailing JSON result lines via
    `runner._strip_trailing_json_line()`.

- [ ] 🟡 **`the_lab/routes/ideas.py`** *(707 LOC)*
  - Idea CRUD, suggest, adopt, checkout, conclude, abandon, reopen, notes.
  - `adopt_idea` now calls `checkout_idea()` after creating the branch —
    agent worktrees actually move to `idea/<N>` (fixed May 14).
  - Parent-ids semantics: `None` = infer from current branch; `[]` = root
    idea off main.
  - `GET /ideas` has `?fields=` projection and short-circuits
    note + experiment_summary enrichment when those fields aren't requested.

- [ ] 🟡 **`the_lab/routes/queue.py`** *(205 LOC)*
  - Queue state, pause/resume, resource CRUD, per-experiment priority.
  - `GET /queue` now includes `recent` (last N finished experiments,
    newest first) — useful for queue history view in the dashboard.
  - **Name collision**: `from .. import queue as queue_mod` because the
    file is in `routes/queue.py` but imports from `the_lab/queue.py`.

- [ ] 🟢 **`the_lab/routes/agents.py`** *(111 LOC)*
  - Agent worktree CRUD. `GET /agents` enriches each entry with
    `current_branch`, `current_idea`, and `unread_messages` count via
    `_enrich()` (reads worktree HEAD + store + messages.py).

- [ ] 🟢 **`the_lab/routes/messages.py`** *(126 LOC)*
  - Inter-agent messaging endpoints. Sender resolved from `X-Agent-Id`.
  - `POST /messages/read_all` marks all unread messages for the caller.
  - `DELETE /messages/{id}` removes a message entirely (for cleanup).

- [ ] 🟢 **`the_lab/routes/prompts.py`** *(106 LOC)*
  - Role-specific prompt CRUD. Backed by `.the_lab/PROMPT.<role>.md`.
  - `GET /instructions` serves the combined prompt (used by MCP bridge).

- [ ] 🟡 **`the_lab/routes/operational.py`** *(396 LOC)*
  - `GET /orient`, `GET /backlog`, `POST /task`, `GET /config`,
    `GET/PUT /sandbox`, `GET /stats`, `POST /chat`, `GET /debug`.
  - Chat endpoint streams Claude responses via SSE.
  - `GET /orient` is the recommended first call for any agent; it returns
    running experiments, active ideas, the recommended next step, and the
    current task if set.

---

## Level 5 — Support modules

- [ ] 🟡 **`the_lab/agents.py`** *(244 LOC)*
  - Registry backed by `.the_lab/agents/registry.json`. `register_agent`
    creates the worktree branch + symlinks `.claude` / `.mcp.json`.
  - `prune_dead_agents()` is called at startup to remove entries whose
    PID is gone. Runs best-effort.
  - `find_branch_holder()` scans `git worktree list --porcelain` to detect
    concurrency conflicts; returns the registry entry or `None`.

- [ ] 🟡 **`the_lab/messages.py`** *(~270 LOC)*
  - Persistent inbox at `.the_lab/messages.json`. Lock is
    `threading.Lock` (sync routes run in threadpool).
  - `notify_wake()` is thread-safe via `loop.call_soon_threadsafe`.
    `_loop` is captured lazily by `_wake_event()` on the first `/wait`
    call — must be after uvicorn starts the event loop.
  - `unread_for()` excludes the sender from their own inbox.
  - `clear_all()` is called on first agent registration in a new session
    (clears stale session messages).
  - **Gotcha**: `broadcast_soon` in `add_message` is best-effort for
    sync callers. The route (`routes/messages.py`) is `async def` and
    calls `broadcaster.broadcast()` directly for guaranteed delivery —
    don't remove that `async` annotation.

- [ ] 🟢 **`the_lab/prompts.py`** *(145 LOC)*
  - Reads/writes role-specific prompt files. `list_roles()` scans the
    `.the_lab/` directory for `PROMPT.<role>.md` files.
  - `read_prompt(repo_dir, role)` falls back to the legacy `PROMPT.md`
    in the repo root when no role file exists.

- [ ] 🟢 **`the_lab/schemas.py`** *(132 LOC)*
  - 15+ Pydantic request models shared across route modules.
  - `ResourceRequirements.kind` is `str | None = None` — `None`/`"any"`
    both mean "match any unit_kind" (vLLM use-case).
  - `NewIdeaRequest.parent_ids` is `list[int] | None = None` (not `[]`);
    `None` means "infer from current branch", `[]` means root idea.

---

## Level 6 — Infrastructure

- [ ] 🟡 **`the_lab/sandbox.py`** *(619 LOC)*
  - Two concerns: (1) sandbox *configuration* (load/save `config.json`,
    allow/denylist, file rules); (2) sandbox *launch* (build the `bwrap`
    / `rootlesskit` command from config). Worth splitting.
  - `sandbox_capabilities()` probes the host for bwrap + rootlesskit at
    runtime — called before every experiment start if sandbox is enabled.
  - **Gotcha**: disabled by default (`enabled: false`). Agents that want
    sandbox protection must explicitly set `enabled: true` and configure
    their allow/deny lists.

- [ ] ⚫ **`the_lab/sandbox_guest.py`** *(646 LOC)*
  - HTTP/HTTPS proxy run *inside* the sandbox namespace. Enforces the
    denylist + allowlist; logs connections.
  - Only active when `sandbox.enabled = true`. Most users never exercise
    this path.
  - Dense parsing code; not a primary refactor target unless sandbox
    becomes more widely used.

- [ ] 🟡 **`the_lab/deps.py`** *(282 LOC)*
  - Module-level globals (`store`, `runner`, `api_stats`, `REPO_DIR`)
    populated by `init()` at startup.
  - Shared helper functions used by multiple routes:
    - `agent_cwd(request)` — resolves X-Agent-Id to worktree path
    - `resolve_metric(metrics, key)` — dot-notation metric lookup
    - `project_fields(data, fields)` — sparse fieldset projection
    - `_wrap_script(content)` — injects guard + preamble into bash scripts
    - `_description_short(desc)` — first-line truncation for `fields=`
  - **Gotcha**: if a route imports from `deps` at the module level and
    uses a global before `init()` runs, it will get `None`. All route
    modules are safe (they only access globals inside route handlers).

- [ ] 🟢 **`the_lab/cache.py`** *(138 LOC)*
  - `@cached_response(key_fn)` decorator for hot GET endpoints.
  - Two-level: in-flight coalescing (multiple concurrent callers share one
    fetch) + FIFO response cache keyed by `(endpoint_params, store_version)`.
  - Cache is invalidated whenever `store._version` increments. Version
    only bumps on renderable-field writes (not internal meta changes).
  - `/chart-data` is explicitly NOT cached (reads `.progress` files on
    every call).

- [ ] 🟢 **`the_lab/stats.py`** *(162 LOC)*
  - Lightweight endpoint usage tracker. Records call counts + n-grams
    (length 2–5). Normalizes numeric IDs so `/ideas/5` and `/ideas/9`
    group as `/ideas/{id}`.
  - `flush()` is called at shutdown; `record()` flushes every 50 calls.
  - Dashboard calls are excluded from stats (filtered by `X-The-Lab-Source`
    header and User-Agent).

- [ ] 🟢 **`the_lab/perf_log.py`** *(92 LOC)*
  - Optional per-request performance CSV. Enabled by `THE_LAB_PERF_LOG`
    env var. Records method, path, normalized path, status, duration_ms,
    source, query, body_bytes, response_bytes, client_ip.
  - Line-buffered CSV — safe to tail during a run.

- [ ] 🟢 **`the_lab/dev_proxy.py`** *(191 LOC)*
  - Reload proxy for `the-lab dev` mode. Holds incoming requests during
    server restart; re-issues them once the new process is ready.
  - Uses `watchfiles` to detect source changes. Only active in dev mode.

- [ ] 🟡 **`the_lab/agent_skills/skills/lab_api_mcp.py`** *(~500 LOC)*
  - OpenAPI-to-MCP bridge. Fetches `/openapi.json` at startup, generates
    MCP tool definitions for the whitelisted endpoints in `INCLUDE`.
  - `flatten_optional()` collapses Pydantic's `anyOf: [T, null]` into
    plain `T` before building tool schemas (fixes meta double-encoding).
  - `proxy_call()` defensively re-parses stringified JSON bodies (so an
    LLM that passes `meta` as a JSON string is unwrapped before the API
    sees it).
  - **Always preferred over any workspace-local copy.** The workspace-local
    copy precedence was reversed May 8 after it caused X-Agent-Id to
    be stripped from all MCP calls.

---

## Level 7 — Dashboard (frontend, TypeScript)

The dashboard is a Preact SPA served by the API as static files. It is
rebuilt by `cli.py:_build_dashboard()` when sources are newer than the
bundle. Hard-reload required after any rebuild.

- [ ] 🔴 **`dashboard/src/app.tsx`** *(~1,100 LOC)*
  - Main layout. Owns the dockview panel registry, URL↔signal sync, saved
    layouts (localStorage), and the reset-clears-prefs behaviour.
  - `DEFAULT_LAYOUT` is a hard-coded JSON blob — updating it doesn't
    propagate to users with saved layouts in localStorage.
  - `build_notifications` lives in `app.py` (backend); the frontend reads
    `_notifications` from API responses and shows them in the topbar.

- [ ] 🔴 **`dashboard/src/components/detail-panel.tsx`** *(~1,600 LOC)*
  - Full idea detail: description, notes, experiments, diff, script/log/
    output lightboxes, markdown rendering (GFM tables, Mermaid, inline HTML).
  - Markdown renderer is bespoke (no library). Placeholder/restore logic
    for nested blocks is loop-until-stable.

- [ ] 🟡 **`dashboard/src/views/queue-view.tsx`** *(~750 LOC)*
  - Queue management pane. Shows queued + running + recent (history)
    sections. Resource editor with inline form.

- [ ] 🟡 **`dashboard/src/views/dag-view.tsx`** *(~700 LOC)*
  - Canvas-drawn subway-map DAG. Heavy; all draw calls happen inside a
    single `useEffect`.

- [ ] 🟡 **`dashboard/src/views/agents-view.tsx`** *(~350 LOC)*
  - Agent cards + message log. Polls `/agents` and `/messages` every 5s.

- [ ] 🟢 **`dashboard/src/state/ws.ts`** *(~200 LOC — new)*
  - WebSocket connection manager. Connects to `/api/v1/ws?since=<seq>&token=<b64>`.
  - Exponential backoff on reconnect (1→2→4→8→16→30s cap); resets on
    successful open.
  - `lastSeq` tracks the highest seq seen; sent as `?since=` on reconnect
    so the server can replay missed events.
  - On reconnect: flushes all three pollers once for a catch-up refresh,
    then resumes WS-driven mode.
  - Event→handler dispatch table: experiment events → `refreshChartData`,
    idea/graph events → `refreshGraphData` + `refreshBacklogData`.
  - `wsConnected` / `wsAuthFailed` — Preact signals for the topbar dot.
  - Auth: reads `localStorage.getItem("the-lab:wsToken")` (base64
    `user:pass`). If absent, connects without token (works when auth is
    disabled). Close code 1008 → sets `wsAuthFailed`, stops retrying.
  - **Gotcha**: queue-related and message events are intentionally no-ops
    in the handler map — the Queue pane self-polls at 3s, and
    notifications surface via the next backlog fetch.

- [ ] 🟢 **`dashboard/src/state/api.ts`** *(~420 LOC)*
  - Typed `fetchJson<T>` wrappers for every API endpoint.
  - All requests add `X-The-Lab-Source: dashboard` so they're excluded
    from `api_stats.json`.
  - `getAllIdeas(fields?)` — accepts optional `?fields=` projection to
    reduce payload on the log-view poll.

- [ ] 🟡 **`dashboard/src/state/polling.ts`** *(~310 LOC)*
  - Centralised polling: backlog (10s), graph (15s), chart-data (30s),
    log (15s). In-flight coalescing (concurrent callers share one request).
  - **Recent**: `LOG_IDEA_FIELDS` — log-view poll requests only the fields
    it actually uses (`id,status,description,created_at,conclusion,notes`),
    skipping `experiment_summary` and `latest_metrics`. Cut the /ideas
    polling response from ~4 MB to ~70 KB on a large lab.
  - **Recent**: `refreshBacklogData()` exported for WS invalidation.
  - With WS active the timers become safety-net fallbacks; most updates
    arrive event-driven. Tab-backgrounding (browser throttles setInterval
    to ~1/min) no longer matters for latency since WS is always open.

- [ ] 🟢 **`dashboard/src/lib/chart-data.ts`** *(~300 LOC)*
  - Pure chart-building functions: `resolveNumericValue` (dot-notation
    walk), `collectChartKeys` (flattens nested metrics up to depth 3),
    `buildChartData`, `filterMetricExperiments`.

- [ ] 🟢 **`dashboard/src/lib/types.ts`** *(~300 LOC)*
  - TypeScript interfaces for every API response shape. Keep in sync
    with backend schemas when adding fields.

---

## Performance notes

- **`/chart-data` was the main bottleneck** (6.8 MB, 1400ms avg on a
  486-experiment lab). Root cause: `**exp` included `meta` which held
  full ARC scorecards (~11.7 KB/experiment). Fixed by using an explicit
  `_CHART_FIELDS` allowlist — drops response to ~0.3 MB.
- **WebSocket replaces most polling**. With WS connected the dashboard
  only fetches when events arrive. The old timers (10s/15s/30s) become
  fallbacks triggered by WS disconnect.
- **`/ideas` log poll** was ~4 MB/call. Fixed with `?fields=` projection
  in `polling.ts`.
- **`/ideas/{id}`** at 108K dashboard calls (208ms avg) is the remaining
  hotspot. It's the detail panel polling the selected idea. Switching to
  WS-invalidated signal (only re-fetch on `idea_changed` for the open
  idea) would cut this to near-zero.

## Cross-cutting concerns

- **Scheduler bypass paths.** Three entry points used to call
  `runner.start()` directly (bypassing resource allocation): `create_experiment`,
  `rerun`, and `start`. All three are now fixed to queue. Any future route
  that starts an experiment must go through `status="queued"` +
  `runner.wake_scheduler()`.

- **X-Agent-Id propagation.** Git-touching routes call `deps.agent_cwd(request)`
  which reads `request.state.agent_cwd` (set by the `resolve_agent` middleware).
  If `X-Agent-Id` is missing or unknown, operations fall back silently to
  `REPO_DIR` (main repo). The notifications middleware nudges agents who
  omit the header.

- **Store cache vs. disk divergence.** `store._experiments` is in-memory.
  If someone deletes experiment dirs on disk without restarting, the API
  still serves stale data. A periodic re-scan (or inotify) would fix this
  but doesn't exist.

- **Thread-safety surface.** The only async-to-sync bridge is
  `messages.notify_wake()` using `call_soon_threadsafe`. Everything else
  (`store`, `stats`, `messages._lock`) uses `threading.Lock` because
  FastAPI dispatches sync routes in a thread pool. Mixing asyncio primitives
  (e.g. `asyncio.Lock`) in sync routes will deadlock.

- **Bundle staleness.** The mtime-based Vite rebuild in `cli.py` is
  unreliable — file system timestamps can be wrong after `git checkout`
  or NFS operations. If the UI looks wrong, always try a manual
  `vite build` and hard-reload first.

- **Worker count.** The running server uses a single uvicorn worker.
  Scaling to multiple workers would break the in-memory scheduler
  (`runner._allocator`, `_finished_queue`, `_wake_event`) — those are
  per-process and would need IPC or a shared backend to work across
  workers.

- **Dot-notation metrics.** `deps.resolve_metric()` and the frontend
  `resolveNumericValue()` both handle `"a.b.c"` paths into nested dicts.
  Flat keys win on collision. Depth is capped at 3 in the frontend
  `collectChartKeys` (prevents dropdown explosion from deeply-nested blobs).

- **`adopt_idea` checkout.** Before May 14, `adopt_idea` created the
  branch but did NOT checkout. Agents that adopted without calling
  `checkout_idea` afterwards were writing code on `agent_init_<id>`.
  Now fixed — `adopt_idea` calls `checkout_idea` and the response
  includes `checked_out: true`.
