/**
 * AgentTree — structure A. Each live agent is a row: state dot (status color
 * language) + identity-colored id + current action, with nested (└) lines
 * beneath. Rows expand (click) into recent history — lab calls + events.
 * Reused in the Overview sidebar (compact) and the Activity pane (full).
 *
 * Data comes from the live agent-activity store.
 */
import { useState } from "preact/hooks";
import { agentStates, type AgentState, type ApiCall } from "../../state/agent-activity";
import { agentColor } from "../../lib/colors";
import { navigateToIdea } from "../../lib/navigate";

function ago(ts: number): string {
  if (!ts) return "";
  const s = Math.max(0, Math.floor((Date.now() - ts) / 1000));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  return `${Math.floor(s / 3600)}h`;
}

// ── lab calls as compact function calls ──────────────────────────────────────
// "POST /experiments/130.2/cancel" reads as cancel_experiment(130.2) — mirrors
// the MCP bridge's tool naming so the UI and the agent's tools speak the same
// vocabulary. Falls back to verb_segment(args) for unmapped routes.
const FN_ROUTES: Array<[RegExp, string, (m: RegExpMatchArray, get: boolean) => string]> = [
  [/^\/experiments\/log$/, "", () => "get_failed_logs"],
  [/^\/experiments\/([^/]+)\/(progress|log|output|script|timeseries|cancel|rerun|start|tags)$/, "$1", (m) => ({
    progress: "get_progress", log: "get_log", output: "get_output", script: "get_script",
    timeseries: "get_timeseries", cancel: "cancel_experiment", rerun: "rerun_experiment",
    start: "start_experiment", tags: "update_tags",
  }[m[2]] as string)],
  [/^\/experiments\/([^/]+)$/, "$1", () => "get_experiment"],
  [/^\/experiments$/, "", () => "list_experiments"],
  [/^\/ideas\/new$/, "", () => "create_idea"],
  [/^\/ideas\/search$/, "", () => "search_ideas"],
  [/^\/ideas\/(\d+)\/experiments\/batch$/, "$1", () => "create_experiments"],
  [/^\/ideas\/(\d+)\/experiments$/, "$1", (_m, get) => (get ? "list_experiments" : "create_experiment")],
  [/^\/ideas\/(\d+)\/(checkout|conclude|abandon|adopt|reopen|note|notes|diff|tree|parent)$/, "$1", (m) => ({
    checkout: "checkout_idea", conclude: "conclude_idea", abandon: "abandon_idea",
    adopt: "adopt_idea", reopen: "reopen_idea", note: "add_note", notes: "list_notes",
    diff: "get_diff", tree: "get_tree", parent: "get_parent",
  }[m[2]] as string)],
  [/^\/ideas\/(\d+)$/, "$1", () => "get_idea"],
  [/^\/ideas$/, "", () => "list_ideas"],
  [/^\/leaderboard\/search$/, "", () => "leaderboard_search"],
  [/^\/leaderboard$/, "", () => "leaderboard"],
  [/^\/wait$/, "", () => "wait_for_experiment"],
  [/^\/orient$/, "", () => "orient"],
  [/^\/digest$/, "", () => "digest"],
  [/^\/instructions$/, "", () => "get_instructions"],
];

function fnCall(c: ApiCall): string {
  const path = c.path.split("?")[0].replace(/\/+$/, "") || "/";
  const get = c.method.toUpperCase() === "GET";
  for (const [re, argTpl, name] of FN_ROUTES) {
    const m = path.match(re);
    if (m) {
      const arg = argTpl ? argTpl.replace(/\$(\d)/g, (_, i) => m[Number(i)] ?? "") : "";
      return `${name(m, get)}(${arg})`;
    }
  }
  // Fallback: verb from the last static segment, args from the dynamic ones.
  const segs = path.split("/").filter(Boolean);
  const args = segs.filter((s) => /\d/.test(s));
  const last = [...segs].reverse().find((s) => !/\d/.test(s)) ?? segs[segs.length - 1] ?? "call";
  return `${get ? "get" : "do"}_${last.replace(/-/g, "_")}(${args.join(", ")})`;
}

/** Best-effort focus target for a lab call: idea id from /ideas/N, or an
 *  "idea.seq" experiment ref whose prefix is the idea id. */
function apiFocus(c: ApiCall): { ideaId: number; expLabel?: string } | null {
  const path = c.path.split("?")[0];
  const idea = path.match(/^\/ideas\/(\d+)/);
  if (idea) return { ideaId: Number(idea[1]) };
  const exp = path.match(/^\/experiments\/((\d+)\.[\w.-]+)/);
  if (exp) return { ideaId: Number(exp[2]), expLabel: exp[1] };
  return null;
}

// `st.active` (recency-based) comes from the store. An active agent keeps
// showing its current/last action (or its idea) rather than flipping to idle.
// Messages are excluded here — they have their own sublist, and repeating the
// last message in the head just duplicated the line below it.
function head(st: AgentState): { glyph: string; kind: string; text: string } {
  const cur = st.current?.kind === "message"
    ? st.recent.find((e) => e.kind !== "message") ?? null
    : st.current;
  if (cur && st.active) {
    return { glyph: cur.glyph, kind: cur.kind, text: cur.text };
  }
  if (st.active) {
    // No glyph — the state dot already says "working"; a second dot after the
    // id read as clutter.
    return { glyph: "", kind: "active", text: st.ideaId != null ? `on idea/${st.ideaId}` : "working" };
  }
  return { glyph: "○", kind: "idle", text: "idle" };
}

function Line({ glyph, text, tone, age, onClick, pending }: {
  glyph: string; text: string; tone?: string; age?: string; onClick?: () => void; pending?: boolean;
}) {
  return (
    <div
      class={`aa-sub aa-tone-${tone ?? "neutral"}${onClick ? " is-click" : ""}`}
      role={onClick ? "button" : undefined}
      onClick={onClick ? (e) => { e.stopPropagation(); onClick(); } : undefined}
    >
      <span class="aa-branch">└</span>
      <span class={`aa-glyph${pending ? " aa-glyph--pending" : ""}`}>{glyph}</span>
      <span class="aa-sub-text">{text}</span>
      {age && <span class="aa-sub-age">{age}</span>}
    </div>
  );
}

/** One detail row: an api call or an event, unified for recency sorting. */
interface DetailRow {
  key: string;
  ts: number;
  glyph: string;
  tone?: string;
  text: string;
  onClick?: () => void;
}

// Hysteresis for activeOnly listing: an agent ENTERS when active, but only
// LEAVES after this much quiet — so a row hovering at the active-window
// boundary dims instead of flapping in and out of the sidebar.
const LINGER_MS = 5 * 60_000;

/** Jump to the Messages tool (used by message rows). */
function openMessages(): void {
  window.location.hash = "#tools/messages";
}

export function AgentTree({ compact = false, activeOnly = false, historyLimit = 6 }: {
  compact?: boolean;
  activeOnly?: boolean;
  /** Detail rows shown per agent (the ^/v stepper steers this). Expanding a
   *  row shows everything kept (up to 12 calls + 12 events). */
  historyLimit?: number;
}) {
  // Per-row disclosure: expanded rows show the agent's recent history.
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const toggle = (id: string) =>
    setExpanded((prev) => {
      const n = new Set(prev);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });

  let states = Object.values(agentStates.value);
  if (activeOnly) {
    const now = Date.now();
    states = states.filter(
      (s) => s.active || (s.lastActiveTs > 0 && now - s.lastActiveTs < LINGER_MS),
    );
  }
  if (states.length === 0) {
    return <div class="aa-empty">{activeOnly ? "no active agents" : "no agents registered"}</div>;
  }
  // Active + most-recently-active first.
  states.sort((a, b) =>
    (Number(b.active) - Number(a.active)) || (b.lastActiveTs - a.lastActiveTs));

  return (
    <div class={`aa-tree${compact ? " aa-compact" : ""}`}>
      {states.map((st) => {
        const color = agentColor(st.agentId);
        const h = head(st);
        const active = h.kind !== "idle";
        // Head prefers the assigned/running experiment (persists past other
        // events); otherwise the current action.
        const showExp = active && st.currentExp;
        const glyph = showExp ? "▶" : h.glyph;
        const text = showExp ? `exp/${st.currentExp}` : h.text;
        const tone = showExp ? "accent" : (active ? (st.current?.tone ?? "accent") : "neutral");
        // Detail rows: lab calls + events merged and sorted by recency (newest
        // first), so messaging and labapi activity intertwine chronologically.
        const rows: DetailRow[] = [
          ...st.apiCalls.map((c, i): DetailRow => {
            const focus = apiFocus(c);
            return {
              key: `api-${c.ts}-${i}`, ts: c.ts, glyph: "⟳", tone: "warn", text: fnCall(c),
              onClick: focus ? () => navigateToIdea(focus.ideaId, focus.expLabel) : undefined,
            };
          }),
          ...st.recent.map((e): DetailRow => ({
            key: e.key, ts: e.ts, glyph: e.glyph, tone: e.tone, text: e.text,
            onClick: e.kind === "message"
              ? openMessages
              : e.ideaId != null
                ? () => navigateToIdea(e.ideaId!, e.expLabel)
                : undefined,
          })),
        ].sort((a, b) => b.ts - a.ts);
        const open = expanded.has(st.agentId);
        const shown = open ? rows : rows.slice(0, historyLimit);
        return (
          <div class={`aa-agent${active ? "" : " aa-quiet"}`} key={st.agentId}>
            {/* Row click = expand/collapse history. Clicking the action text
                still navigates to the idea. */}
            <div class="aa-head" role="button" onClick={() => toggle(st.agentId)}>
              {/* Status-language state dot: yellow = active/working, faint =
                  quiet; filled = listening on the-lab messages, hollow = not.
                  (Identity color lives on the agent id only.) */}
              <span
                class={`aa-dot aa-dot--state${active ? " is-active" : ""}${st.listening ? " is-listening" : ""}${st.listening && active ? " aa-pulse" : ""}`}
                title={`${st.listening ? "listening" : "not listening"} · ${active ? "working" : "quiet"}`}
              />
              <span class="aa-id" style={{ color }}>{st.agentId}</span>
              <span class="aa-role">{st.role}</span>
              {glyph && <span class={`aa-glyph aa-tone-${tone}`}>{glyph}</span>}
              <span
                class={`aa-head-text${st.ideaId != null ? " is-link" : ""}`}
                onClick={st.ideaId != null
                  ? (e) => { e.stopPropagation(); navigateToIdea(st.ideaId!); }
                  : undefined}
              >
                {text}
              </span>
              {st.lastActiveTs > 0 && <span class="aa-age">{ago(st.lastActiveTs)}</span>}
              <span class="aa-chev" aria-hidden="true">{open ? "▾" : "▸"}</span>
            </div>
            <div class="aa-subs">
              {/* In-flight long-poll (e.g. wait_for_experiment) pinned first,
                  blinking until its completion event clears it. */}
              {st.pendingCall && (
                <Line glyph="⟳" tone="warn" pending
                  text={`${fnCall(st.pendingCall)} …`} age={ago(st.pendingCall.ts)} />
              )}
              {shown.map((r) => (
                <Line key={r.key} glyph={r.glyph} tone={r.tone} text={r.text}
                  age={ago(r.ts)} onClick={r.onClick} />
              ))}
              {open && rows.length === 0 && !st.pendingCall && (
                <div class="aa-sub"><span class="aa-branch">└</span><span class="aa-sub-text">no history yet</span></div>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}
