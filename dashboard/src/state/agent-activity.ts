// ------------------------------------------------------------
// Live agent-activity store.
//
// Turns the raw WS event stream (experiment_*, message_received,
// note_added, idea_changed, agent_api_call, …) plus the periodic
// /agents poll into two reactive views:
//
//   activityFeed  — a global, newest-first ring buffer of normalized
//                   events (drives the grouped activity feed).
//   agentStates   — per-agent current action + recent trail + role/idea/
//                   listening/live flags (drives the agent tree).
//
// Attribution: message/api_call events name their actor directly
// (from_agent / agent_id); experiment/note/idea events are attributed to
// whichever live agent currently has that idea checked out.
// ------------------------------------------------------------

import { signal } from "@preact/signals";
import { subscribeWsEvents } from "./ws";
import { listAgents } from "./api";
import type { AgentEntry } from "../lib/types";

export interface ActivityEvent {
  key: string;            // stable unique id (for keyed rendering)
  ts: number;             // epoch ms
  agentId: string | null; // attributed actor (null = system / unattributed)
  kind: string;           // running | done | failed | queued | message | note | idea | api | cancelled
  glyph: string;          // ▶ ✓ ✗ ◽ ↔ ✎ ◆ ⟳
  text: string;           // one-line description
  tone?: string;          // ui tone: good | bad | warn | accent | neutral
  ideaId?: number;
  expLabel?: string;      // experiment label for exp-* events (drives currentExp)
}

export interface ApiCall {
  method: string;
  path: string;    // /api/v1 prefix stripped
  status?: number;
  ts: number;
}

export interface AgentState {
  agentId: string;
  role: string;
  ideaId: number | null;
  listening: boolean;
  live: boolean;            // pid != null (registry — info only, not "active")
  active: boolean;          // seen activity within ACTIVE_WINDOW_MS, or listening
  current: ActivityEvent | null;   // latest meaningful action
  currentExp: string | null;       // label of the running experiment (persists past other events)
  recent: ActivityEvent[];  // newest-first, capped
  apiCalls: ApiCall[];      // last labapi/MCP calls, newest-first
  pendingCall: ApiCall | null;  // long-poll call in flight right now (e.g. /wait)
  lastActiveTs: number;     // epoch ms of most recent activity (0 = none seen)
}

const FEED_MAX = 120;
const PER_AGENT_MAX = 12;  // events kept per agent (expanded rows page via the stepper)
const API_KEEP = 12;       // labapi/MCP calls kept per agent
// "Active" = we've seen activity from the agent this recently. Purely
// client-side (doesn't trust registry pid/pruning), so quiet/old agents drop
// off both the sidebar and the feed on their own.
const ACTIVE_WINDOW_MS = 120_000;
// Events that prove an agent is working but aren't worth their own feed line —
// they only refresh recency (progress heartbeats, log growth).
const TOUCH_TYPES = new Set(["experiment_progress_updated", "experiment_log_updated"]);

/** Global newest-first activity feed (structure B). */
export const activityFeed = signal<ActivityEvent[]>([]);
/** Per-agent state keyed by agent_id (structure A). */
export const agentStates = signal<Record<string, AgentState>>({});

// --- internal indexes -------------------------------------------------------
let _ideaToAgent: Record<number, string> = {};   // idea_id -> agent_id (live)
let _agentMeta: Record<string, AgentEntry> = {};  // agent_id -> entry
let _lastActive: Record<string, number> = {};     // agent_id -> epoch ms of last activity
let _apiByAgent: Record<string, ApiCall[]> = {};   // agent_id -> last API calls (newest-first)
let _pendingByAgent: Record<string, ApiCall> = {}; // agent_id -> in-flight long-poll call
let _curExp: Record<string, string | null> = {};   // agent_id -> running experiment label
let _seq = 0;
let _started = false;
let _pollTimer: ReturnType<typeof setInterval> | null = null;

function touch(agentId: string | null | undefined): void {
  if (agentId) _lastActive[agentId] = Date.now();
}

function agentForIdea(ideaId: unknown): string | null {
  return typeof ideaId === "number" ? _ideaToAgent[ideaId] ?? null : null;
}

function excerpt(s: unknown, n = 52): string {
  return String(s ?? "").replace(/\s+/g, " ").trim().slice(0, n);
}

function nextKey(): string {
  _seq += 1;
  return `${Date.now()}-${_seq}`;
}

// Map a raw WS event to a normalized ActivityEvent (or null to ignore).
function normalize(ev: Record<string, unknown>): ActivityEvent | null {
  const type = String(ev.type ?? "");
  const ideaId = typeof ev.idea_id === "number" ? (ev.idea_id as number) : undefined;
  const byIdea = ideaId != null ? _ideaToAgent[ideaId] ?? null : null;
  const base = { key: nextKey(), ts: Date.now(), ideaId };

  const expLabel = String(ev.label ?? ev.experiment_id ?? "") || undefined;
  switch (type) {
    case "experiment_started":
      return { ...base, agentId: byIdea, kind: "running", glyph: "▶", tone: "accent",
        expLabel, text: `started exp/${expLabel ?? "?"}` };
    case "experiment_finished": {
      const status = String(ev.status ?? "");
      const ok = status === "completed";
      const msg = ev.status_msg ? ` — ${excerpt(ev.status_msg, 40)}` : "";
      return { ...base, agentId: byIdea, kind: ok ? "done" : "failed",
        glyph: ok ? "✓" : "✗", tone: ok ? "good" : "bad",
        expLabel, text: `exp/${expLabel ?? "?"} ${status || "finished"}${msg}` };
    }
    case "experiment_cancelled":
      return { ...base, agentId: byIdea, kind: "cancelled", glyph: "✗", tone: "warn",
        expLabel, text: `cancelled exp/${expLabel ?? "?"}` };
    case "experiment_queued":
      return { ...base, agentId: byIdea, kind: "queued", glyph: "◽", tone: "neutral",
        expLabel, text: `queued exp/${expLabel ?? "?"}` };
    case "message_received":
    case "message": {
      const from = (ev.from_agent as string) || (ev.from_role as string) || null;
      const to = ev.to === "all" ? "all" : String(ev.to ?? "").replace(/^agent:|^role:/, "");
      // Agents often prefix their text with "[from → to]" — we already render
      // the routing, so strip the prefix instead of saying it twice. The ↔
      // glyph carries direction; "@to" avoids a second arrow symbol.
      const raw = String(ev.text ?? "").replace(/^\s*\[[^\]]{0,60}\]\s*/, "");
      const body = excerpt(raw, 40);
      return { ...base, agentId: from, kind: "message", glyph: "↔", tone: "accent",
        text: body ? `@${to || "?"} "${body}"` : `@${to || "?"}` };
    }
    case "note_added":
      return { ...base, agentId: byIdea, kind: "note", glyph: "✎", tone: "neutral",
        text: `note on idea/${ideaId ?? "?"}${ev.level ? ` (${ev.level})` : ""}` };
    case "idea_changed":
      return { ...base, agentId: byIdea, kind: "idea", glyph: "◆", tone: "neutral",
        text: `idea/${ideaId ?? "?"} ${ev.change ?? "updated"}` };
    default:
      return null;   // api_call (per-agent, below), queue_changed, ping, init: not feed lines
  }
}

function mkState(id: string): AgentState {
  const entry = _agentMeta[id];
  return {
    agentId: id,
    role: entry?.role || "agent",
    ideaId: entry?.current_idea?.id ?? null,
    listening: !!entry?.listening,
    live: entry?.pid != null,
    active: false,
    current: null,
    currentExp: _curExp[id] ?? null,
    recent: [],
    apiCalls: _apiByAgent[id] ?? [],
    pendingCall: _pendingByAgent[id] ?? null,
    lastActiveTs: _lastActive[id] ?? 0,
  };
}

function rebuildAgentStates(events: ActivityEvent[]): void {
  const now = Date.now();
  // A pending long-poll that never saw its completion (socket drop, agent
  // gone) shouldn't spin forever — expire after 15min.
  for (const [id, c] of Object.entries(_pendingByAgent)) {
    if (now - c.ts > 15 * 60_000) delete _pendingByAgent[id];
  }
  const next: Record<string, AgentState> = {};
  // Only REAL registered agents get a state — attribute events to them by their
  // agent id. This drops phantom role-keyed entries (a pre-enrichment message
  // event that only carried from_role) so we always show the real agent id.
  for (const id of Object.keys(_agentMeta)) next[id] = mkState(id);
  for (const e of events) {
    if (!e.agentId || !next[e.agentId]) continue;   // ignore non-agent / unknown
    const st = next[e.agentId];
    if (st.recent.length < PER_AGENT_MAX) st.recent.push(e);
    if (!st.current) st.current = e;                 // first (newest) = current action
  }
  for (const st of Object.values(next)) {
    st.currentExp = _curExp[st.agentId] ?? null;
  }
  // Recency-based "active": trust observed activity, not the registry pid.
  for (const st of Object.values(next)) {
    st.lastActiveTs = _lastActive[st.agentId] ?? 0;
    st.active = st.listening || (st.lastActiveTs > 0 && now - st.lastActiveTs < ACTIVE_WINDOW_MS);
  }
  agentStates.value = next;
}

const EXP_KINDS = new Set(["running", "done", "failed", "queued", "cancelled"]);

function pushEvent(e: ActivityEvent): void {
  // Dedup guard: the backend has (had) double-emit paths (experiment_started
  // from dispatch+start, message re-broadcasts) — an identical line from the
  // same agent within 10s is the same event, not new activity.
  const dup = activityFeed.value.slice(0, 8).some(
    (p) => p.agentId === e.agentId && p.kind === e.kind && p.text === e.text && e.ts - p.ts < 10_000,
  );
  if (dup) return;
  touch(e.agentId);
  // Track the running experiment per agent.
  if (e.agentId) {
    if (EXP_KINDS.has(e.kind)) {
      if (e.kind === "running" && e.expLabel) _curExp[e.agentId] = e.expLabel;
      else if ((e.kind === "done" || e.kind === "failed" || e.kind === "cancelled")
        && (!e.expLabel || _curExp[e.agentId] === e.expLabel)) {
        _curExp[e.agentId] = null;
      }
    }
  }
  const feed = [e, ...activityFeed.value].slice(0, FEED_MAX);
  activityFeed.value = feed;
  rebuildAgentStates(feed);
}

async function pollAgents(): Promise<void> {
  try {
    const agents = await listAgents();
    const meta: Record<string, AgentEntry> = {};
    const idea2agent: Record<number, string> = {};
    for (const a of agents) {
      meta[a.agent_id] = a;
      if (a.current_idea?.id != null) idea2agent[a.current_idea.id] = a.agent_id;
    }
    _agentMeta = meta;
    _ideaToAgent = idea2agent;
    rebuildAgentStates(activityFeed.value);   // refresh roster + attribution
  } catch {
    /* keep last-known roster on transient failure */
  }
}

/** Start the activity store (idempotent). Call once on app mount. */
export function startAgentActivity(): void {
  if (_started) return;
  _started = true;
  subscribeWsEvents((raw) => {
    const ev = raw as Record<string, unknown>;
    const type = String(ev.type);

    // Per-agent API/MCP endpoint trail (last N). Not a feed line — shown nested
    // under the agent in the tree.
    if (type === "agent_api_call") {
      const aid = ev.agent_id as string | undefined;
      if (aid) {
        const call: ApiCall = {
          method: String(ev.method ?? "GET"),
          path: String(ev.path ?? "").replace(/^\/api\/v1/, "") || "/",
          status: typeof ev.status === "number" ? (ev.status as number) : undefined,
          ts: Date.now(),
        };
        if (ev.pending) {
          // Long-poll started (e.g. /wait) — show it as in-flight until the
          // completion event for the same path arrives.
          _pendingByAgent[aid] = call;
        } else {
          if (_pendingByAgent[aid]?.path === call.path) delete _pendingByAgent[aid];
          _apiByAgent[aid] = [call, ...(_apiByAgent[aid] ?? [])].slice(0, API_KEEP);
        }
        touch(aid);
        rebuildAgentStates(activityFeed.value);
      }
      return;
    }

    const norm = normalize(ev);
    if (norm) { pushEvent(norm); return; }
    // Touch-only events (progress/log heartbeats) keep a running agent "active"
    // without adding feed noise.
    if (TOUCH_TYPES.has(type)) {
      const idea = agentForIdea(ev.idea_id);
      if (idea) { touch(idea); rebuildAgentStates(activityFeed.value); }
    }
  });
  pollAgents();
  // Re-evaluate active flags periodically (poll refreshes roster AND lets the
  // time-based `active` window expire for agents that went quiet).
  _pollTimer = setInterval(pollAgents, 5000);
}

export function stopAgentActivity(): void {
  if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null; }
  _started = false;
}
