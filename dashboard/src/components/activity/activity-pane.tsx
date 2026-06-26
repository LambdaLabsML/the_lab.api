/**
 * ActivityPane — the live "what is the swarm doing right now" view, and the
 * default dashboard content. A single calm, scannable column that merges:
 *   1. Now running     — experiments with `_running`, live progress.
 *   2. Agents roster    — one row per agent; click selects the message recipient
 *                         and filters the feed + history to that agent.
 *   3. Activity feed    — reverse-chron, color-coded stream merged from derived
 *                         experiment records/finishes, the WS event ticker, and
 *                         messages. A better, boxless version of the log pane.
 *   4. Message composer — the chat, moved inline: recipient selector + textarea,
 *                         sendMessage, recent history (polled).
 *
 * Data sources:
 *   - signals: allExperiments, allIdeas, runningProgress, agentCostMap
 *   - api polls: listAgents() ~5s, listMessages() ~5s
 *   - ws.ts events surface only as counters (type + seq); we read them as a thin
 *     "live event ticker" — per-endpoint detail is NOT exposed by the backend, so
 *     we do not invent it.
 *
 * See dashboard/DESIGN.md — hairlines not boxes, flat fills, token type, one accent.
 */
import { useEffect, useMemo, useRef, useState } from "preact/hooks";
import { listAgents, listMessages, sendMessage } from "../../state/api";
import {
  allExperiments,
  allIdeas,
  runningProgress,
  agentCostMap,
} from "../../state/signals";
import { isLowerBetter } from "../../lib/colors";
import { ideaTitle, fmtMetricName } from "../../lib/format";
import { useSelection } from "../../lib/hooks";
import type { AgentEntry, Experiment, MessageEntry } from "../../lib/types";
import {
  Panel,
  PanelHeader,
  PanelBody,
  Eyebrow,
  Badge,
  EmptyState,
  IconButton,
  type BadgeTone,
} from "../ui";

// ── small helpers ──────────────────────────────────────────────────────────

/** "Xs/Xm/Xh/Xd ago" — mirrors agents-view / chat-panel. */
function relTime(iso: string): string {
  if (!iso) return "--";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return iso;
  const sec = Math.max(0, Math.floor((Date.now() - t) / 1000));
  if (sec < 60) return `${sec}s ago`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}m ago`;
  const hr = Math.floor(min / 60);
  if (hr < 48) return `${hr}h ago`;
  return `${Math.floor(hr / 24)}d ago`;
}

function fmtVal(v: number): string {
  if (!isFinite(v)) return String(v);
  if (Math.abs(v) >= 1000) return v.toFixed(0);
  if (Math.abs(v) >= 10) return v.toFixed(1);
  if (Math.abs(v) >= 1) return v.toFixed(2);
  return v.toFixed(3);
}

const IDEA_STATUS_TONE: Record<string, BadgeTone> = {
  active: "active",
  running: "running",
  concluded: "concluded",
  abandoned: "abandoned",
  suggested: "warn",
};

/** Latest known cost for an agent from the global cost signal. */
function agentCost(costMap: Record<string, any>, id: string): number | null {
  const e = costMap[id];
  if (!e) return null;
  if (e.live && e.readings?.length) return e.readings[e.readings.length - 1].cost;
  return e.cost ?? null;
}

/**
 * Pick the single most meaningful metric across all experiments — the one that
 * the most experiments report. Mirrors the "primary metric" intuition the
 * mini-metrics-chart uses (most-populated numeric series).
 */
function pickPrimaryMetric(exps: Experiment[]): string | null {
  const counts = new Map<string, number>();
  for (const e of exps) {
    if (!e.metrics) continue;
    for (const [k, v] of Object.entries(e.metrics)) {
      if (typeof v === "number" && isFinite(v)) counts.set(k, (counts.get(k) ?? 0) + 1);
    }
  }
  let best: string | null = null;
  let bestN = 0;
  for (const [k, n] of counts) {
    if (n > bestN) { bestN = n; best = k; }
  }
  return best;
}

// ── feed entry model ─────────────────────────────────────────────────────────

type FeedKind = "record" | "finished" | "running" | "message" | "event";

interface FeedEntry {
  key: string;
  kind: FeedKind;
  t: number; // epoch ms (0 = undated event ticker)
  ideaId?: number;
  agentId?: string; // for filtering by selected agent
  tone: BadgeTone | "accent" | "muted";
  label: string; // short colored lead
  text: string;
  meta?: string;
}

const KIND_DOT: Record<FeedKind, string> = {
  record: "var(--purple)",
  finished: "var(--green)",
  running: "var(--yellow)",
  message: "var(--accent)",
  event: "var(--text-faint)",
};

// ─────────────────────────────────────────────────────────────────────────────

export function ActivityPane() {
  // ── live data from signals ──
  const experiments = allExperiments.value;
  const ideas = allIdeas.value;
  const progress = runningProgress.value;
  const costMap = agentCostMap.value;

  // ── polled API state ──
  const [agents, setAgents] = useState<AgentEntry[]>([]);
  const [messages, setMessages] = useState<MessageEntry[]>([]);
  const [agentsLoaded, setAgentsLoaded] = useState(false);

  // ── composer state ──
  const [to, setTo] = useState("all");
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState<string | null>(null);

  // ── selection: clicking an agent row sets recipient + filters ──
  const { selected: selectedAgent, select: selectAgent } = useSelection<string>();

  // Re-render every 30s so relative times stay fresh.
  const [, setTick] = useState(0);
  const cancelled = useRef(false);

  // Agents poll ~5s (mirrors agents-view cadence)
  useEffect(() => {
    cancelled.current = false;
    const load = () =>
      listAgents()
        .then((list) => { if (!cancelled.current) { setAgents(list); setAgentsLoaded(true); } })
        .catch(() => { if (!cancelled.current) setAgentsLoaded(true); });
    load();
    const poll = window.setInterval(load, 5000);
    const tick = window.setInterval(() => setTick((n) => n + 1), 30000);
    return () => {
      cancelled.current = true;
      window.clearInterval(poll);
      window.clearInterval(tick);
    };
  }, []);

  // Messages poll ~5s (mirrors chat-panel cadence)
  useEffect(() => {
    let dead = false;
    const load = () => listMessages(40).then((m) => { if (!dead) setMessages(m); }).catch(() => {});
    load();
    const t = window.setInterval(load, 5000);
    return () => { dead = true; window.clearInterval(t); };
  }, []);

  // When the selected recipient is an agent, mirror it into the composer `to`.
  useEffect(() => {
    if (selectedAgent) setTo(`agent:${selectedAgent}`);
  }, [selectedAgent]);

  // ── derived: running experiments strip ──
  const running = useMemo(
    () => experiments.filter((e) => e._running || e.status === "running"),
    [experiments],
  );

  // ── derived: the primary metric + best-score "records" (milestones) ──
  const primaryMetric = useMemo(() => pickPrimaryMetric(experiments), [experiments]);

  /**
   * Walk completed experiments chronologically; each time the primary metric
   * sets a new global best, that's a "record". Client-side, like the
   * mini-metrics chart's milestone detection.
   */
  const records = useMemo<FeedEntry[]>(() => {
    if (!primaryMetric) return [];
    const lower = isLowerBetter(primaryMetric);
    const done = experiments
      .filter((e) =>
        !e._running && e.status === "completed" &&
        e.metrics && typeof e.metrics[primaryMetric] === "number" &&
        isFinite(e.metrics[primaryMetric] as number) &&
        (e.finished_at || e.created_at),
      )
      .sort((a, b) => {
        const ta = Date.parse(a.finished_at || a.created_at || "");
        const tb = Date.parse(b.finished_at || b.created_at || "");
        return ta - tb;
      });
    const out: FeedEntry[] = [];
    let best: number | null = null;
    for (const e of done) {
      const v = e.metrics![primaryMetric] as number;
      const better = best === null || (lower ? v < best : v > best);
      if (!better) continue;
      best = v;
      const t = Date.parse(e.finished_at || e.created_at || "");
      out.push({
        key: `rec-${e.id}`,
        kind: "record",
        t: Number.isNaN(t) ? 0 : t,
        ideaId: e.idea_id,
        tone: "best",
        label: "RECORD",
        text: `${fmtMetricName(primaryMetric)} → ${fmtVal(v)} · idea #${e.idea_id} ${ideaTitle(ideas[e.idea_id]?.description ?? "")}`.trim(),
        meta: e.label ? `exp ${e.label}` : undefined,
      });
    }
    return out;
  }, [experiments, ideas, primaryMetric]);

  // ── derived: recent finished experiments (latest N) ──
  const finished = useMemo<FeedEntry[]>(() => {
    return experiments
      .filter((e) => !e._running && e.finished_at &&
        (e.status === "completed" || e.status === "failed" || e.status === "cancelled"))
      .map((e) => {
        const t = Date.parse(e.finished_at!);
        const failed = e.status !== "completed";
        return {
          key: `fin-${e.id}`,
          kind: "finished" as FeedKind,
          t: Number.isNaN(t) ? 0 : t,
          ideaId: e.idea_id,
          tone: (failed ? "bad" : "good") as BadgeTone,
          label: e.status === "completed" ? "DONE" : e.status.toUpperCase(),
          text: `exp ${e.label ?? e.id} · idea #${e.idea_id} ${ideaTitle(ideas[e.idea_id]?.description ?? "")}`.trim(),
          meta: e.runtime ? `ran ${e.runtime}` : undefined,
        };
      })
      .sort((a, b) => b.t - a.t)
      .slice(0, 40);
  }, [experiments, ideas]);

  // ── derived: messages as feed entries ──
  const messageEntries = useMemo<FeedEntry[]>(() => {
    return messages.map((m) => {
      const t = m.created_at ? Date.parse(m.created_at) : 0;
      const who = m.from_agent
        ? (m.from_role ? `${m.from_agent} (${m.from_role})` : m.from_agent)
        : "you";
      return {
        key: `msg-${m.id}`,
        kind: "message" as FeedKind,
        t: Number.isNaN(t) ? 0 : t,
        agentId: m.from_agent ?? undefined,
        tone: "accent" as const,
        label: `${who} → ${m.to}`,
        text: m.text,
      };
    });
  }, [messages]);

  // ── merge feed, newest first, filter by selected agent when set ──
  const feed = useMemo<FeedEntry[]>(() => {
    let merged = [...records, ...finished, ...messageEntries];
    if (selectedAgent) {
      // Filter to entries we can attribute to this agent:
      //  - its messages, and
      //  - records/finishes for the idea it is currently working on.
      const a = agents.find((x) => x.agent_id === selectedAgent);
      const ideaId = a?.current_idea?.id;
      merged = merged.filter((e) =>
        e.agentId === selectedAgent ||
        (ideaId != null && e.ideaId === ideaId),
      );
    }
    return merged.sort((a, b) => b.t - a.t).slice(0, 80);
  }, [records, finished, messageEntries, selectedAgent, agents]);

  // ── composer send ──
  async function handleSend() {
    const text = input.trim();
    if (!text || sending) return;
    setSending(true);
    setSendError(null);
    try {
      await sendMessage(to, text);
      setInput("");
      const updated = await listMessages(40);
      setMessages(updated);
    } catch (err) {
      setSendError(err instanceof Error ? err.message : "Failed to send");
    } finally {
      setSending(false);
    }
  }
  function handleKeyDown(e: KeyboardEvent) {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); handleSend(); }
  }

  const liveAgents = agents.filter((a) => a.pid != null).length;
  const headerCount = `${liveAgents}/${agents.length} live · ${running.length} running`;

  return (
    <Panel scroll class="activity-pane">
      <PanelHeader
        title="Activity"
        count={agentsLoaded ? headerCount : "…"}
        actions={
          selectedAgent ? (
            <IconButton title="Clear agent filter" onClick={() => selectAgent(selectedAgent)}>
              clear ✕
            </IconButton>
          ) : undefined
        }
      />
      <PanelBody pad={false} class="activity-body">
        {/* ── 1. NOW RUNNING ─────────────────────────────────────────── */}
        <section class="activity-section">
          <div class="activity-section-head">
            <Eyebrow>Now running</Eyebrow>
            <span class="activity-section-count">{running.length}</span>
          </div>
          {running.length === 0 ? (
            <EmptyState icon="◷" title="Idle" body="No experiments are running right now." />
          ) : (
            <div class="activity-running">
              {running.map((e) => {
                const label = e.label || String(e.id);
                const pct = progress[label];
                return (
                  <div class="activity-run-row" key={`run-${e.id}`}>
                    <div class="activity-run-top">
                      <Badge tone="running" dot>{label}</Badge>
                      <span class="activity-run-title" title={ideas[e.idea_id]?.description}>
                        idea #{e.idea_id} {ideaTitle(ideas[e.idea_id]?.description ?? e.idea_description ?? "")}
                      </span>
                      <span class="activity-run-pct">
                        {typeof pct === "number" ? `${Math.round(pct)}%` : ""}
                      </span>
                    </div>
                    <div class="activity-progress">
                      <span
                        class={`activity-progress-fill${typeof pct === "number" ? "" : " is-indeterminate"}`}
                        style={typeof pct === "number" ? { width: `${Math.min(100, Math.max(0, pct))}%` } : undefined}
                      />
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </section>

        {/* ── 2. AGENTS ROSTER ───────────────────────────────────────── */}
        <section class="activity-section">
          <div class="activity-section-head">
            <Eyebrow>Agents</Eyebrow>
            <span class="activity-section-count">{agents.length}</span>
          </div>
          {agentsLoaded && agents.length === 0 ? (
            <EmptyState
              icon="⌁"
              title="No active agents"
              body={<>Launch one with <code>the-lab-agent</code>.</>}
            />
          ) : (
            <div class="activity-roster">
              {agents.map((a) => {
                const alive = a.pid != null;
                const cost = agentCost(costMap, a.agent_id);
                const sel = selectedAgent === a.agent_id;
                const unread = a.unread_messages ?? 0;
                return (
                  <button
                    type="button"
                    key={a.agent_id}
                    class={`activity-agent${sel ? " is-selected" : ""}`}
                    onClick={() => selectAgent(a.agent_id)}
                    title={sel ? "Click to deselect" : "Message this agent / filter feed to it"}
                  >
                    <span class={`activity-dot${alive ? " is-live" : " is-dead"}`} />
                    <span class="activity-agent-id">{a.agent_id}</span>
                    <Badge tone="neutral">{a.role || "default"}</Badge>
                    <span class="activity-agent-work">
                      {a.current_idea ? (
                        <>
                          working on idea #{a.current_idea.id}
                          {" — "}
                          <span class="activity-agent-work-title">
                            {ideaTitle(a.current_idea.description)}
                          </span>
                          {" "}
                          <Badge tone={IDEA_STATUS_TONE[a.current_idea.status] ?? "neutral"}>
                            {a.current_idea.status}
                          </Badge>
                        </>
                      ) : (
                        <span class="activity-agent-idle">no idea yet</span>
                      )}
                    </span>
                    <span class="activity-agent-tail">
                      {unread > 0 && <Badge tone="warn">✉ {unread}</Badge>}
                      {cost != null && <span class="activity-agent-cost">${cost.toFixed(3)}</span>}
                    </span>
                  </button>
                );
              })}
            </div>
          )}
        </section>

        {/* ── 3. ACTIVITY FEED ───────────────────────────────────────── */}
        <section class="activity-section activity-section--feed">
          <div class="activity-section-head">
            <Eyebrow>Feed{selectedAgent ? ` · agent ${selectedAgent}` : ""}</Eyebrow>
            <span class="activity-section-count">{feed.length}</span>
          </div>
          {feed.length === 0 ? (
            <EmptyState
              icon="⌗"
              title={selectedAgent ? "Nothing yet for this agent" : "No activity yet"}
              body={selectedAgent ? "Records, finishes, and messages will appear here." : undefined}
            />
          ) : (
            <div class="activity-feed">
              {feed.map((e) => (
                <div class={`activity-line activity-line--${e.kind}`} key={e.key}>
                  <span class="activity-line-dot" style={{ background: KIND_DOT[e.kind] }} />
                  <span class="activity-line-time">{e.t ? relTime(new Date(e.t).toISOString()) : "—"}</span>
                  <span class={`activity-line-label activity-line-label--${e.tone}`}>{e.label}</span>
                  <span class="activity-line-text">{e.text}</span>
                  {e.meta && <span class="activity-line-meta">{e.meta}</span>}
                </div>
              ))}
            </div>
          )}
        </section>

        {/* ── 4. MESSAGE COMPOSER ────────────────────────────────────── */}
        <section class="activity-section activity-composer">
          <div class="activity-section-head">
            <Eyebrow>Message agents</Eyebrow>
          </div>
          <div class="activity-composer-controls">
            <select
              class="activity-to-select"
              value={to}
              onChange={(ev) => {
                const v = (ev.target as HTMLSelectElement).value;
                setTo(v);
                // Keep roster selection in sync when an agent is chosen here.
                if (v.startsWith("agent:")) selectAgent(v.slice("agent:".length));
                else if (selectedAgent) selectAgent(selectedAgent); // toggle off
              }}
            >
              <option value="all">→ all agents</option>
              {[...new Set(agents.map((a) => a.role || "default"))].map((role) => (
                <option key={`role-${role}`} value={`role:${role}`}>→ role: {role}</option>
              ))}
              {agents.map((a) => (
                <option key={`agent-${a.agent_id}`} value={`agent:${a.agent_id}`}>
                  → {a.agent_id}{a.role ? ` (${a.role})` : ""}
                </option>
              ))}
            </select>
          </div>
          {sendError && <div class="activity-send-error">{sendError}</div>}
          <div class="activity-composer-row">
            <textarea
              class="activity-input"
              value={input}
              onInput={(ev) => setInput((ev.target as HTMLTextAreaElement).value)}
              onKeyDown={handleKeyDown}
              placeholder="Message agents… (Enter to send, Shift+Enter for newline)"
              rows={2}
              disabled={sending}
            />
            <button
              type="button"
              class={`ui-btn activity-send-btn${input.trim() && !sending ? " is-active" : ""}`}
              onClick={handleSend}
              disabled={!input.trim() || sending}
            >
              {sending ? "…" : "Send"}
            </button>
          </div>
        </section>
      </PanelBody>
    </Panel>
  );
}
