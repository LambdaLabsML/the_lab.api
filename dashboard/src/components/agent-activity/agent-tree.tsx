/**
 * AgentTree — structure A. Each live agent is a row: state dot (status color
 * language) + identity-colored id + current action, with nested (└) lines
 * beneath. Rows expand (click) into recent history — lab calls + events.
 * Reused in the Overview sidebar (compact) and the Activity pane (full).
 *
 * Data comes from the live agent-activity store.
 */
import { useState } from "preact/hooks";
import {
  agentStates, focusMessageId, showMessagePreview, hideMessagePreview,
  type ActivityEvent, type AgentState, type ApiCall,
} from "../../state/agent-activity";
import { navigateToIdea } from "../../lib/navigate";
import { ExpLink } from "../exp-link";
import { AgentPill } from "../agent-pill";
import { RichText } from "../rich-text";
import { ApiIcon, MsgIcon } from "./icons";

/** Experiments live on the Overview page — switch there before focusing. */
function goOverview(ideaId: number, expLabel?: string): void {
  window.location.hash = "#review";
  navigateToIdea(ideaId, expLabel);
}

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

// Head state. Event texts are NOT repeated here — they live in the detail rows;
// the head answers "what is it doing right now":
//   running exp (caller) > thinking (fresh OWN work) > waiting (listening, no
//   fresh work — e.g. re-armed the listener for replies) > on idea/N > idle.
// Note thinking keys off lastWorkTs, not lastActiveTs: experiment heartbeats
// keep an agent active but must not read as the agent itself thinking.
const THINKING_MS = 30_000;

function head(st: AgentState): { kind: "thinking" | "waiting" | "active" | "idle"; text: string } {
  if (st.active && st.lastWorkTs > 0 && Date.now() - st.lastWorkTs < THINKING_MS) {
    return { kind: "thinking", text: "thinking" };
  }
  if (st.listening) {
    return { kind: "waiting", text: "waiting" };
  }
  if (st.active) {
    return { kind: "active", text: st.ideaId != null ? `on idea/${st.ideaId}` : "working" };
  }
  return { kind: "idle", text: "idle" };
}

/** Row glyph: ⟳ / ↔ get real SVG icons (stepper-chevron style); the rest stay text. */
function LineGlyph({ glyph }: { glyph: string }) {
  if (glyph === "⟳") return <ApiIcon />;
  if (glyph === "↔") return <MsgIcon />;
  return <>{glyph}</>;
}

function Line({ glyph, text, tone, age, onClick, pending, to, api, latest, fresh, hoverEv }: {
  glyph: string; text: string; tone?: string; age?: string;
  onClick?: () => void; pending?: boolean;
  /** Message recipient — rendered as a mini agent pill before the text. */
  to?: string;
  /** Lab-call row: yellow text; dimmed unless it's the newest (or hovered). */
  api?: boolean;
  latest?: boolean;
  /** Newest of its kind AND used in the last 30s — keeps color in quiet mode. */
  fresh?: boolean;
  /** Message event — hovering previews the full message in the main area. */
  hoverEv?: ActivityEvent;
}) {
  return (
    <div
      class={`aa-sub aa-tone-${tone ?? "neutral"}${onClick ? " is-click" : ""}${api ? " is-api" : ""}${api && !latest ? " is-dimmed" : ""}${to != null ? " is-msg" : ""}${fresh ? " is-fresh" : ""}`}
      role={onClick ? "button" : undefined}
      onClick={onClick ? (e) => { e.stopPropagation(); onClick(); } : undefined}
      onMouseEnter={hoverEv ? () => showMessagePreview(hoverEv) : undefined}
      onMouseLeave={hoverEv ? hideMessagePreview : undefined}
    >
      <span class="aa-branch">└</span>
      <span class={`aa-glyph${pending ? " aa-glyph--pending" : ""}`}><LineGlyph glyph={glyph} /></span>
      {to && <AgentPill id={to} />}
      <span class="aa-sub-text"><RichText text={text} /></span>
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
  to?: string;
  api?: boolean;
  hoverEv?: ActivityEvent;   // message rows: hover-preview source
  onClick?: () => void;
}

// Hysteresis for activeOnly listing: an agent ENTERS when active, but only
// LEAVES after this much quiet — so a row hovering at the active-window
// boundary dims instead of flapping in and out of the sidebar.
const LINGER_MS = 5 * 60_000;

/** Jump to the Messages tool (used by message rows). */
function openMessages(): void {
  window.location.hash = "#messages";
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
        const h = head(st);
        const active = h.kind !== "idle";
        // Head prefers the assigned/running experiment (persists past other
        // events); then thinking… / on idea/N / idle.
        const showExp = active && st.currentExp;
        // Detail rows: lab calls + events merged and sorted by recency (newest
        // first), so messaging and labapi activity intertwine chronologically.
        const rows: DetailRow[] = [
          ...st.apiCalls.map((c, i): DetailRow => {
            const focus = apiFocus(c);
            return {
              key: `api-${c.ts}-${i}`, ts: c.ts, glyph: "⟳", tone: "warn", text: fnCall(c), api: true,
              onClick: focus ? () => goOverview(focus.ideaId, focus.expLabel) : undefined,
            };
          }),
          ...st.recent.map((e): DetailRow => ({
            key: e.key, ts: e.ts, glyph: e.glyph, tone: e.tone,
            // Messages: pill for the recipient + the bare excerpt; hovering
            // previews the full message, clicking focuses it in Messages.
            text: e.kind === "message" ? (e.msgBody ? `"${e.msgBody}"` : "") : e.text,
            to: e.kind === "message" ? e.msgTo : undefined,
            hoverEv: e.kind === "message" ? e : undefined,
            onClick: e.kind === "message"
              ? () => { hideMessagePreview(); if (e.msgId != null) focusMessageId.value = e.msgId; openMessages(); }
              : e.ideaId != null
                ? () => goOverview(e.ideaId!, e.expLabel)
                : undefined,
          })),
        ].sort((a, b) => b.ts - a.ts);
        // Collapse consecutive identical rows (an agent polling get_experiment
        // shouldn't fill the list with copies) — keep the newest, count the rest.
        const dedup: (DetailRow & { n?: number })[] = [];
        for (const r of rows) {
          const last = dedup[dedup.length - 1];
          if (last && last.text === r.text && last.glyph === r.glyph && !last.to) {
            last.n = (last.n ?? 1) + 1;
          } else {
            dedup.push({ ...r });
          }
        }
        const open = expanded.has(st.agentId);
        // Only the NEWEST lab call is fully yellow; older ones dim (hovering
        // the agent block restores them — see .aa-agent:hover in scss). A
        // pending long-poll counts as the newest.
        // In the quiet (un-hovered) palette, only a RECENTLY-used latest call
        // or message keeps its color — stale ones read gray like the rest.
        const FRESH_MS = 30_000;
        const now = Date.now();
        let seenApi = !!st.pendingCall;
        let seenMsg = false;
        const shown = (open ? dedup : dedup.slice(0, historyLimit)).map((r) => {
          const latest = !!r.api && !seenApi;
          if (r.api) seenApi = true;
          const isMsg = r.to != null;
          const latestMsg = isMsg && !seenMsg;
          if (isMsg) seenMsg = true;
          const fresh = (latest || latestMsg) && now - r.ts < FRESH_MS;
          return { ...r, latest, fresh };
        });
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
              <AgentPill id={st.agentId} />
              <span class="aa-role">{st.role}</span>
              {showExp ? (
                // Shared experiment link: canonical styling + hover card +
                // click-through to Overview (see components/exp-link.tsx).
                <>
                  <span class="aa-glyph aa-tone-accent">▶</span>
                  <ExpLink label={st.currentExp!} ideaId={st.ideaId} class="aa-head-text" />
                </>
              ) : h.kind === "thinking" ? (
                // Fresh OWN work — the agent is working between tool calls.
                <span class="aa-head-text aa-thinking">thinking<span class="aa-thinking-dots">…</span></span>
              ) : h.kind === "waiting" ? (
                // Listener armed, no fresh work — standing by for replies/results.
                <span class="aa-head-text aa-waiting">waiting</span>
              ) : (
                <>
                  {h.kind === "idle" && <span class="aa-glyph">○</span>}
                  <span
                    class={`aa-head-text${st.ideaId != null ? " is-link" : ""}`}
                    onClick={st.ideaId != null
                      ? (e) => { e.stopPropagation(); goOverview(st.ideaId!); }
                      : undefined}
                  >
                    {h.text}
                  </span>
                </>
              )}
              {st.lastActiveTs > 0 && <span class="aa-age">{ago(st.lastActiveTs)}</span>}
              <span class="aa-chev" aria-hidden="true">{open ? "▾" : "▸"}</span>
            </div>
            <div class="aa-subs">
              {/* In-flight long-poll (e.g. wait_for_experiment) pinned first,
                  blinking until its completion event clears it. */}
              {st.pendingCall && (
                <Line glyph="⟳" tone="warn" pending api latest fresh
                  text={`${fnCall(st.pendingCall)} …`} age={ago(st.pendingCall.ts)} />
              )}
              {shown.map((r) => (
                <Line key={r.key} glyph={r.glyph} tone={r.tone} to={r.to} api={r.api} latest={r.latest}
                  fresh={r.fresh} hoverEv={r.hoverEv}
                  text={r.n && r.n > 1 ? `${r.text} ×${r.n}` : r.text}
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
