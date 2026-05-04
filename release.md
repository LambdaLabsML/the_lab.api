# The Lab v6

## Highlights

**Sandbox v2** — file isolation via `bubblewrap` layered inside the existing rootlesskit network namespace. The dashboard's Sandbox pane now exposes per-path `rw` / `ro` rules alongside the existing network allow/deny lists. Agents no longer see the host's `~/.ssh`, `~/.bash_history`, or sibling projects unless you explicitly bind them.

**Role-based prompts** — every project can define multiple agent personas as `.the_lab/PROMPT.<role>.md` files. `the-lab-agent --role instructor` (or `--list-roles`) selects one; the new `get_instructions(role=…)` MCP tool returns the right prompt, and the dashboard ships a *Prompts* pane with full CRUD + a "copy launch command" button per role.

**Output viewer** — experiments can write `<script>.output.md` *or* `<script>.output.html` and have it rendered inline with images, GFM tables, mermaid diagrams, nested lists, inline HTML widgets (with live `<script>` execution), and clickable cross-file `.md` / `.html` links that navigate in-place with a back stack.

**Shareable deep links** — opening a log, output, script, or diff lightbox updates the URL hash. Copy the URL and send it; they land directly on the same view, on desktop or mobile.

**Performance overhaul** — response cache with version-keyed invalidation + inflight coalescing, GZip on the wire, frontend polls deduplicated and the eager 52×`/ideas/{id}` page-load burst removed. New `--perf` flag dumps every request's timing to CSV for offline analysis.

**MCP resilience** — the bridge no longer dies on unhandled exceptions, recovers from a startup spec-fetch failure on the first tool call, and exposes `get_instructions`.

---

## New Features

### Agent / CLI

- **`get_instructions(role?)` MCP tool + `GET /api/v1/instructions?role=X`** — returns the role-specific (or default) `PROMPT[.<role>].md` plus `PROMPT_api.md`, so agents can reload the task description and API reference dynamically. Unknown roles fall back to default with an `available_roles` list in the response.
  - Single-run preamble: `Please start by calling get_instructions(role='X') first…`
  - Loop-mode prompt: `/loop <duration> Please re-read instructions via get_instructions(role='X') and continue…`
- **`the-lab-agent --role <name>`** — selects a role; falls back to default with a stderr warning if the role's file doesn't exist.
- **`the-lab-agent --list-roles`** — prints configured roles + size + last-updated and exits.
- **`the-lab --perf [path]`** — opt-in CSV log of every API request's timing, source (dashboard / mcp / agent), status, sizes, query, and client IP. Default path: `<repo>/.the_lab/api_perf.csv`. Zero overhead when disabled.
- **`the-lab init` migrates `PROMPT.md` into `.the_lab/`** — interactive: defaults to yes; the legacy `<repo>/PROMPT.md` keeps working as a read-fallback if you decline.
- **`the-lab init` updates the MCP bridge** — run it in an existing project and say yes to get the latest `lab_api_mcp.py`.

### Dashboard

#### Output viewer

- **Show output** for either `<script>.output.md` (markdown rendered) or `<script>.output.html` (injected raw). HTML wins when both exist — a deliberate signal from the agent.
- **Inline HTML support** — `<span style=…>`, `<sub>`, `<sup>`, `<mark>`, `<kbd>`, `<a href=…>` etc. inside paragraphs render natively (attribute-sanitized). Block-level `<p>`, `<ul>/<ol>/<li>`, `<pre>`, `<blockquote>`, `<h1>`–`<h6>`, `<dl>/<dt>/<dd>`, `<hr>` pass through as raw HTML.
- **Interactive widgets** — `<img>`, `<button>`, `<input>`, `<select>`, `<canvas>`, `<svg>`, `<iframe>`, `<audio>`, `<video>` render. Inline `<script>` blocks (and their multi-line content) are re-created as live elements after `dangerouslySetInnerHTML`, so animation/playback widgets actually run. Script execution is content-deduped to avoid stacking intervals across polls.
- **Mermaid diagrams** — ` ```mermaid ` fenced blocks render as SVG via lazy-loaded mermaid.js. ~600 KB library is fetched only when a diagram is first encountered.
- **GFM pipe tables** — `| col | col |` / `|---|:---:|---:|` renders as a styled `<table>` with per-column alignment. Inline markdown still applies inside cells.
- **Nested lists** — leading whitespace controls depth; mixed 2/4-space conventions both work; `<ul>` nests inside the parent `<li>` per HTML spec.
- **Local `.md` / `.html` link navigation** — clicking a relative link inside the output renders the linked file in the same lightbox with `← Back`. Each push remembers parent scroll; pop restores it. `[link](#section)` and `[link](file.md#section)` jump to slugified heading anchors. Title trail shows `Output — exp/X › filename.md`.
- **Relative URL rewriting** — `<img src="grids/x.png">` and friends in raw HTML are rewritten to `/api/v1/files/<base_path>/grids/x.png` post-render so they actually load.
- **Auto-refresh every 5s while running**; **`↻ Refresh` button** in the toolbar for finished experiments.
- **Preserved `<details>` state** across polls — collapsible reasoning blocks no longer snap shut when the file refreshes.
- **`Cache-Control: no-cache`** on the output endpoint — defeats the browser cache so updates appear on reload.

#### Detail panel

- **Compact meta block** replaces four separate sections:
  - Row 1: `⎇ branch` + `diff ↗` + parent idea links
  - Row 2: experiment count + per-status pills
  - Row 3: note count + per-level pills (milestone, insight, observation, debug)
  - Conclusion shown as italic quote
- **Timeline mode** — interleaves notes and experiments sorted by `created_at`, colored dots per type.
  - Toggle between `⊞ Timeline` and `⊞ Grouped` (persistent across ideas)
  - Sort toggle `↓ Newest first` / `↑ Oldest first` (persistent)
  - Timeline + newest-first are the defaults
- **Auto-follow scroll** for both log and output lightboxes; scrolling up disables, scrolling back to bottom re-enables.
- **Shareable URLs** — opening a lightbox sets `#idea=5&exp=exp001&view=log` (or `output`, `script`, `diff`); page load restores the view automatically. Works on mobile (the deep-link restore now activates the detail panel before reading the hash).

#### Prompts pane

- **CRUD UI** in the bottom bar — left sidebar with role list, right textarea with explicit Save. Add / Delete buttons (refuses to delete `default`). Per-role **Copy launch command** button drops `the-lab-agent --role <name> loop -d 30m` into the clipboard.

#### Sandbox pane

- **File rules** alongside the existing network allow/deny lists. `rw` paths are bind-mounted writable, `ro` paths read-only, anything else is invisible to the sandboxed process. Built-in system binds (`/usr`, `/bin`, `/etc`, repo, agent credentials) shown as a chip list.
- **Sandbox capability probe** updated to verify `rootlesskit` + `bwrap` work together.

#### Performance

- **Polling de-duplicated** — each `pollFoo()` shares an in-flight Promise so re-entrant callers (refresh + interval racing) coalesce to one request.
- **No more eager 52×`/ideas/{id}`** on page load — log view now joins the cheap `/ideas` list response with the existing `allExperiments` signal instead of fetching per-idea details up front. Detail panel still fetches a specific idea when selected.

### API

- **`GET /api/v1/instructions[?role=X]`** — returns combined `PROMPT[.<role>].md` + `PROMPT_api.md`, with `available_roles` on miss when ≥2 roles exist.
- **`GET /api/v1/prompts`** / **`GET|PUT|DELETE /api/v1/prompts/{role}`** — CRUD for role files. Role names: `[a-z0-9_-]{1,32}`; `default` is reserved for the no-suffix file. Writes bump `store._version` so cached `/instructions?role=…` responses invalidate.
- **`GET /api/v1/experiments/{ref}/output`** — reads `<script>.output.html` (preferred) or `.output.md`; returns content + `base_path` + `format` (`"html"` | `"md"`). `Cache-Control: no-cache` on the response.
- **`GET /api/v1/files/{path}`** — serves any file under the repo root (path-traversal protected); used for images and cross-file `.md`/`.html` link navigation in the output viewer.

### Performance

- **Response cache** for hot read endpoints (`/ideas`, `/ideas/{id}`, `/graph`, `/backlog`) keyed by `(endpoint, args, store.version)`. Writes in `Store` bump `_version` so old entries become unreachable. Capped at 64 entries with FIFO eviction.
  - **Inflight coalescing** — concurrent identical misses share one handler execution; followers `await` the leader's result. Collapses page-load thundering-herd from N×1.2 s to ~1.2 s + epsilon.
  - **Narrow version bumps** — `update_experiment` only bumps `_version` when a *renderable* field changes (status, metrics, timing, description, error, tags, label, seq, conclusion, progress, runtime). Meta-only writes (worktree bookkeeping, etc.) leave the cache intact.
  - **`/chart-data` deliberately *not* cached** — it reads each running experiment's `.progress` file on every call; caching it froze the metrics chart for running experiments.
  - **Task-file writes invalidate `/ideas` and `/backlog`** — `_write_task` and `set_task` bump `_version` (those endpoints embed the current task in the response).
- **GZip compression** for any response ≥ 1 KB (~4× smaller on the wire for the multi-MB JSON endpoints).
- **`has_output` checked fresh per call** — `<script>.output.{md,html}` is written by the experiment itself and isn't tracked by `_version`. Without the fix, the **Show output** button stayed hidden until something else (usually the experiment finishing) bumped the cache.
- **`--perf` CSV** — opt-in raw timing log of every API request, including dashboard polls (which the existing stats tracker deliberately skips). Designed for offline analysis with pandas / spreadsheet, no aggregation in-memory.

### Sandbox

- **Layered isolation** — `rootlesskit` (user + network namespace) **+ bwrap** (mount namespace, file isolation) **+ sandbox_guest** (iptables + transparent proxy + privilege drop). Network policy is unchanged; what's new is that paths not explicitly bound are **invisible** to the sandboxed process.
- **File rules** persisted alongside network rules in `.the_lab/sandbox/config.json`:
  - `file_rw: list[str]` — bind-mounted read-write
  - `file_ro: list[str]` — bind-mounted read-only
  - Anything else → invisible (a default set of system + repo + agent-creds paths is always bound to keep things working)
- **Cleaner /etc** — drops the previous `--copy-up=/etc` (which made the sandbox feel like a different OS). `bwrap --ro-bind /etc /etc` preserves real user → uid mapping; `id` and `whoami` show your real username instead of `root`.
- **DNS handled** — a freshly-written `resolv.conf` pointing at slirp4netns's built-in DNS is bind-mounted over `/etc/resolv.conf` so the host's `127.0.0.53` (systemd-resolved) doesn't follow into the namespace.
- **Capability probe** verifies the full stack: `rootlesskit + bwrap` together.

### Markdown viewer (the long form)

The output renderer grew from "headings + lists + code + images" into a near-complete GFM + raw-HTML viewer:

- **Inline HTML extraction** — whitelist of safe inline tags (`span`, `sub`, `sup`, `mark`, `kbd`, `samp`, `var`, `small`, `abbr`, `time`, `q`, `cite`, `code`, `em`, `strong`, `i`, `b`, `u`, `s`, `del`, `ins`, `a`, `br`, `wbr`) extracted to placeholders before the escape pass; survives bold/italic/code processing; restored at the end. Attribute filter strips `on*`, allows `class`/`id`/`title`/`style`/`role`/`lang`/`dir`/`data-*`/safe `href`.
- **Block-level passthrough** expanded: `<details>`, `<summary>`, `<div>`, `<section>`, `<article>`, `<aside>`, `<figure>`, `<figcaption>`, `<table>` family, `<p>`, `<pre>`, `<blockquote>`, `<ul>/<ol>/<li>/<dl>/<dt>/<dd>`, `<h1>`–`<h6>`, `<hr>`, `<br>`, `<wbr>`, plus media/interactive: `<img>`, `<audio>`, `<video>`, `<source>`, `<picture>`, `<iframe>`, `<button>`, `<input>`, `<select>`, `<option>`, `<textarea>`, `<label>`, `<fieldset>`, `<legend>`, `<form>`, `<progress>`, `<meter>`, `<canvas>`, `<svg>`, `<script>`, `<style>`, `<noscript>`.
- **Multi-line consume** for tags whose content can't be paragraph-wrapped: `<pre>`, `<script>`, `<style>`, `<textarea>`, `<svg>`. A `<pre><code>` block of multi-line JSON now passes through verbatim instead of getting shredded into `<p>`s.
- **Heading anchors** — `# Foo` emits `<h1 id="foo">`. `[link](#foo)` and `[link](file.md#foo)` work.
- **Fixed nested placeholder restore** — link text containing a code span (e.g. `` [`world_model`](url) ``) was rendering as `S0`/`S1` because the restore loop ran forward, single-pass, and missed the inner placeholder revealed when the outer one expanded. Now loops until stable, with a function-callback to dodge `replace`'s `$&` substitution gotcha.
- **Live `<script>` execution** with content-keyed dedupe so polling refreshes don't stack listeners/intervals; reset on context switch (new experiment, navigated linked file).
- **Relative URL rewriting** for `<img>`, `<a>`, `<source>`, `<iframe>`, `<video>`, `<audio>`, `<script src=…>` against the current `basePath`.
- **Mermaid lazy-load** with theme=dark + securityLevel=loose.
- **GFM pipe tables** with per-column alignment.
- **Nested lists** by indent.
- **Local `.md` / `.html` link navigation** with stack + back + scroll restore + cross-file anchors.

### Git

- **Carry uncommitted changes to new idea branch** — when `create_idea` (or `adopt`) is called, any uncommitted work is stashed, the new `idea/N` branch is checked out, and the stash is popped and committed there. Changes no longer land on the old branch by accident.
  - Applies to single-parent (new `checkout_idea_carry`) and multi-parent merge (`carry=True` in `create_branch_from_merge`).
  - Experiment creation is unaffected — commits to the current branch as before.

### MCP bridge

- **Resilient message loop** — unhandled exceptions in the dispatch loop now return a JSON-RPC `-32603` error instead of killing the server process.
- **`BrokenPipeError` handled** — exits cleanly when the client closes the pipe.
- **Lazy spec retry** — if the OpenAPI spec fetch failed at startup (Lab API not yet up), the bridge re-fetches automatically on the first `tools/list` or `tools/call`.
- **`get_instructions` added** to the tool list (auto-generated from OpenAPI, including the new `role` query parameter).

---

## Bug fixes (other than the above)

- **Lightbox deep links on mobile** — dockview lazy-mounts panels, so `DetailPanel` never ran its hash-restore effect on a fresh mobile session. The app now reads the hash right after layout build, sets `selectedIdea`, and activates the detail panel via `setActive()`, which causes dockview to mount it.
- **Mobile detail-panel scroll cutoff** — bottom-anchored UI was hidden under the browser chrome. Added `padding-bottom: max(80px, env(safe-area-inset-bottom, 16px))`.
- **Mobile sash drag** — the touch-target widening CSS targeted the wrong selectors; corrected to `.dv-split-view-container.dv-{horizontal,vertical} > .dv-sash-container > .dv-sash`.
- **Follow button on output lightbox** — was hidden once the file finished. Now always visible; re-engages when the user scrolls back to the bottom.
- **`the-lab init` NameError** during the Claude pre-fill step after the `.the_lab/PROMPT.md` migration refactor (referenced an old variable name).

---

## Breaking changes

- **Sandbox now requires `bubblewrap`** in addition to `rootlesskit` / `slirp4netns` / `iptables` / `ip`. The capability probe will report it missing if not installed.
- **Sandbox off by default** — `--sandbox` flag required to enable; previously on by default.
- **`the-lab-agent` defaults to single-run** — loop mode now requires the explicit `loop` subcommand (`the-lab-agent loop`).
- **`PROMPT_problem.md` → `PROMPT.md`** — old filename no longer recognised.
- **`PROMPT.md` lives under `.the_lab/`** — `the-lab init` offers an interactive migration; if you decline, the legacy `<repo>/PROMPT.md` keeps working as a read-fallback for the default role only (named roles must live under `.the_lab/`).

---

**Full changelog:** 64 commits since v5
