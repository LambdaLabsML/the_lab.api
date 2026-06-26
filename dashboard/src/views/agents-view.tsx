import { useEffect, useMemo, useRef, useState } from "preact/hooks";
import { listAgents, unregisterAgent } from "../state/api";
import type { AgentEntry } from "../lib/types";
import { agentCostMap } from "../state/signals";
import { useCopyToClipboard, useDisclosure, useEscape } from "../lib/hooks";
import { Badge, Stat, EmptyState, type BadgeTone } from "../components/ui";

// Idea status → design-language Badge tone.
const IDEA_STATUS_TONE: Record<string, BadgeTone> = {
  active: "active",
  running: "running",
  concluded: "concluded",
  abandoned: "abandoned",
  suggested: "warn",
};

// ── History / cost lightbox ───────────────────────────────────────────────────

interface HistorySession {
  session_id: string;
  started_at: string | null;
  message_count: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_creation_tokens: number;
  cost_usd: number;
}
interface HistoryData {
  sessions: HistorySession[];
  totals: {
    sessions: number;
    message_count: number;
    input_tokens: number;
    output_tokens: number;
    cache_read_tokens: number;
    cache_creation_tokens: number;
    cost_usd: number;
  };
}

function fmtK(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(0)}k`;
  return String(n);
}

function CopyButton({ text, label = "copy" }: { text: string; label?: string }) {
  const { copied, copy } = useCopyToClipboard();
  return (
    <button
      class="agents-btn agents-btn-copy"
      style={{ opacity: copied ? 0.7 : 1 }}
      title={`Copy: ${text}`}
      onClick={() => copy(text)}
    >
      {copied ? "✓" : label}
    </button>
  );
}

function AgentHistoryLightbox({ agentId, onClose }: { agentId: string; onClose: () => void }) {
  const [data, setData] = useState<HistoryData | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEscape(onClose);

  useEffect(() => {
    fetch(`/api/v1/agents/${agentId}/history`)
      .then(async (r) => {
        if (!r.ok) throw new Error(await r.text());
        return r.json() as Promise<HistoryData>;
      })
      .then(setData)
      .catch((e) => setError(e.message));
  }, [agentId]);

  return (
    <div class="lightbox-backdrop" onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div class="lightbox" style={{ maxWidth: 700, width: "95vw" }}>
        <div class="lightbox-header">
          <span class="lightbox-title">History · agent/{agentId}</span>
          <span class="lightbox-close" onClick={onClose}>×</span>
        </div>
        <div class="lightbox-body" style={{ overflowY: "auto", maxHeight: "75vh" }}>
          {error && <div class="agents-lb-error">{error}</div>}
          {!data && !error && <div class="agents-lb-loading">Loading…</div>}
          {data && (
            <>
              {/* Totals bar */}
              <div class="agents-history-totals">
                {[
                  ["SESSIONS",    String(data.totals.sessions)],
                  ["MESSAGES",    String(data.totals.message_count)],
                  ["INPUT",       fmtK(data.totals.input_tokens)],
                  ["OUTPUT",      fmtK(data.totals.output_tokens)],
                  ["CACHE READ",  fmtK(data.totals.cache_read_tokens)],
                  ["CACHE WRITE", fmtK(data.totals.cache_creation_tokens)],
                ].map(([label, val]) => (
                  <Stat key={label} size="sm" label={label} value={val} />
                ))}
                <Stat
                  class="agents-history-cost"
                  size="sm"
                  tone="accent"
                  label="EST. COST"
                  value={`$${data.totals.cost_usd.toFixed(3)}`}
                />
              </div>
              {/* Per-session table */}
              {data.sessions.length === 0 ? (
                <div class="agents-lb-empty">No session files found yet.</div>
              ) : (
                <table class="agents-history-table">
                  <thead>
                    <tr>
                      <th>Session</th>
                      <th>Started</th>
                      <th class="num">Msgs</th>
                      <th class="num">In</th>
                      <th class="num">Out</th>
                      <th class="num">↓Cache</th>
                      <th class="num">↑Cache</th>
                      <th class="num">Cost</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.sessions.map((s, i) => (
                      <tr key={s.session_id}>
                        <td>
                          <div class="agents-history-session">
                            <code class={i === 0 ? "is-latest" : undefined} title={s.session_id}>
                              {s.session_id.slice(0, 8)}…
                            </code>
                            <CopyButton text={s.session_id} />
                          </div>
                        </td>
                        <td class="agents-history-started">{s.started_at ? s.started_at.slice(0, 16).replace("T", " ") : "—"}</td>
                        <td class="num">{s.message_count}</td>
                        <td class="num muted">{fmtK(s.input_tokens)}</td>
                        <td class="num muted">{fmtK(s.output_tokens)}</td>
                        <td class="num muted">{fmtK(s.cache_read_tokens)}</td>
                        <td class="num muted">{fmtK(s.cache_creation_tokens)}</td>
                        <td class="num accent">${s.cost_usd.toFixed(3)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
              {/* Resume command — most recent session */}
              {data.sessions.length > 0 && (
                <div class="agents-resume">
                  <span class="ui-eyebrow">Resume latest session</span>
                  <div class="agents-resume-cmd">
                    <code>
                      the-lab-agent loop --resume {data.sessions[0].session_id}
                    </code>
                    <CopyButton text={`the-lab-agent loop --resume ${data.sessions[0].session_id}`} label="copy cmd" />
                  </div>
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}

// ── Output log lightbox ──────────────────────────────────────────────────────

function AgentOutputLightbox({ agentId, onClose }: { agentId: string; onClose: () => void }) {
  const [text, setText] = useState<string>("Loading…");
  const { open: follow, toggle: toggleFollow } = useDisclosure(true);
  const bodyRef = useRef<HTMLPreElement>(null);
  const pollRef = useRef<number | null>(null);
  useEscape(onClose);

  async function load() {
    try {
      const resp = await fetch(`/api/v1/agents/${agentId}/output`);
      if (resp.ok) {
        setText(await resp.text());
      } else if (resp.status === 404) {
        setText("(no output log yet — agent may not have started)");
      }
    } catch {
      setText("(failed to load output)");
    }
  }

  useEffect(() => {
    load();
    pollRef.current = window.setInterval(load, 3000);
    return () => { if (pollRef.current) window.clearInterval(pollRef.current); };
  }, [agentId]);

  useEffect(() => {
    if (follow && bodyRef.current) {
      bodyRef.current.scrollTop = bodyRef.current.scrollHeight;
    }
  }, [text, follow]);

  return (
    <div class="lightbox-backdrop" onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div class="lightbox" style={{ maxWidth: 900, width: "95vw" }}>
        <div class="lightbox-header">
          <span class="lightbox-title">Output · agent/{agentId}</span>
          <div class="lightbox-toolbar">
            <button
              class={`follow-btn${follow ? " follow-active" : ""}`}
              onClick={() => toggleFollow()}
            >
              {follow ? "↓ following" : "↓ follow"}
            </button>
          </div>
          <span class="lightbox-close" onClick={onClose}>×</span>
        </div>
        <div class="lightbox-body" style={{ padding: 0 }}>
          <pre ref={bodyRef} class="agents-output-pre">{text}</pre>
        </div>
      </div>
    </div>
  );
}

/** Shorten a long path by replacing the middle with an ellipsis. */
function truncateMiddle(s: string, max: number): string {
  if (!s) return "";
  if (s.length <= max) return s;
  const keep = max - 1; // leave room for the ellipsis char
  const head = Math.ceil(keep / 2);
  const tail = Math.floor(keep / 2);
  return s.slice(0, head) + "…" + s.slice(s.length - tail);
}

/** Format a created_at ISO timestamp as "Xs ago" / "Xm ago" / "Xh ago" / "Xd ago". */
function relativeTime(iso: string): string {
  if (!iso) return "--";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return iso;
  const sec = Math.max(0, Math.floor((Date.now() - t) / 1000));
  if (sec < 60) return `${sec}s ago`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}m ago`;
  const hr = Math.floor(min / 60);
  if (hr < 48) return `${hr}h ago`;
  const day = Math.floor(hr / 24);
  return `${day}d ago`;
}

interface PastAgent {
  agent_id: string;
  role?: string;
  branch?: string;
  parent_branch?: string;
  worktree?: string;
  pid?: number | null;
  created_at?: string;
  completed_at?: string;
}

// ── Agent cost sparkline chart ────────────────────────────────────────────────

interface ChartPoint { t: number; cost: number; tokens: number; }

function AgentCostChart({
  points,
  width = 260,
  height = 52,
  label: labelOverride,
  color = "var(--accent)",
}: {
  points: ChartPoint[];
  width?: number;
  height?: number;
  label?: string;
  color?: string;
}) {
  const [mode, setMode] = useState<"cost" | "tokens">("cost");
  if (points.length === 0) return null;

  const W = width, H = height;
  const PL = 2, PR = 2, PT = 4, PB = 12;
  const IW = W - PL - PR, IH = H - PT - PB;

  const minT = points[0].t, maxT = points[points.length - 1].t;
  const vals = points.map((p) => (mode === "cost" ? p.cost : p.tokens));
  const maxV = Math.max(...vals, 0.0001);

  const px = (t: number) => PL + (maxT === minT ? IW / 2 : ((t - minT) / (maxT - minT)) * IW);
  const py = (v: number) => PT + IH - (v / maxV) * IH;

  const last = points[points.length - 1];
  const lastV = mode === "cost" ? last.cost : last.tokens;

  const lineD = points.length === 1
    ? `M${PL},${py(lastV).toFixed(1)} L${PL + IW},${py(lastV).toFixed(1)}`
    : points.map((p, i) => {
        const v = mode === "cost" ? p.cost : p.tokens;
        return `${i === 0 ? "M" : "L"}${px(p.t).toFixed(1)},${py(v).toFixed(1)}`;
      }).join(" ");

  const areaD = points.length === 1
    ? `M${PL},${py(lastV).toFixed(1)} L${PL + IW},${py(lastV).toFixed(1)} L${PL + IW},${PT + IH} L${PL},${PT + IH} Z`
    : `${lineD} L${px(last.t).toFixed(1)},${PT + IH} L${px(points[0].t).toFixed(1)},${PT + IH} Z`;

  const valueLabel = mode === "cost"
    ? `$${last.cost.toFixed(3)}`
    : `${(last.tokens / 1000).toFixed(0)}K tok`;
  const dotX = points.length === 1 ? (PL + IW).toFixed(1) : px(last.t).toFixed(1);

  return (
    <div class="agent-cost-chart">
      <div class="agent-cost-chart-header">
        <span class="agent-cost-chart-label ui-eyebrow">
          {labelOverride ?? (mode === "cost" ? "Cumulative cost" : "Cumulative tokens")}
        </span>
        <button
          class="agents-btn agents-btn-copy"
          onClick={() => setMode(mode === "cost" ? "tokens" : "cost")}
        >
          {mode === "cost" ? "tokens →" : "cost →"}
        </button>
      </div>
      <svg width={W} height={H} style={{ display: "block", overflow: "visible" }}>
        {/* flat tint under the line — no gradient (see DESIGN.md) */}
        <path d={areaD} fill={color} fill-opacity="0.1" />
        <path d={lineD} fill="none" stroke={color} stroke-width="1.5"
          stroke-linejoin="round" stroke-linecap="round" />
        <circle cx={dotX} cy={py(lastV).toFixed(1)} r="2.5" fill={color} />
        <text x={W - PR} y={H - 1} text-anchor="end"
          fill={color} font-family="var(--font-mono)"
          style="font-size:var(--text-xs)">{valueLabel}</text>
      </svg>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────

export function AgentsView() {
  const [agents, setAgents] = useState<AgentEntry[]>([]);
  const [pastAgents, setPastAgents] = useState<PastAgent[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [outputAgentId, setOutputAgentId] = useState<string | null>(null);
  const [historyAgentId, setHistoryAgentId] = useState<string | null>(null);
  // Re-render every ~30s so "Xm ago" stays fresh
  const [, setTick] = useState(0);

  // Cost data comes from the global signal (populated by polling.ts, always-on)
  const costMap = agentCostMap.value;

  function agentCost(id: string): number | null {
    const e = costMap[id];
    if (!e) return null;
    if (e.live && e.readings?.length) return e.readings[e.readings.length - 1].cost;
    return e.cost ?? null;
  }

  const cancelledRef = useRef(false);

  async function refresh(): Promise<AgentEntry[] | null> {
    try {
      const [list, past] = await Promise.all([
        listAgents(),
        fetch("/api/v1/agents/past").then((r) => r.ok ? r.json() : []).catch(() => []) as Promise<PastAgent[]>,
      ]);
      if (cancelledRef.current) return null;
      setAgents(list);
      setPastAgents(past);
      setError(null);
      setLoaded(true);
      return list;
    } catch (err) {
      if (cancelledRef.current) return null;
      setError(err instanceof Error ? err.message : String(err));
      setLoaded(true);
      return null;
    }
  }

  useEffect(() => {
    cancelledRef.current = false;
    refresh();
    const poll = window.setInterval(refresh, 5000);
    const tickT = window.setInterval(() => setTick((n) => n + 1), 30000);
    return () => {
      cancelledRef.current = true;
      window.clearInterval(poll);
      window.clearInterval(tickT);
    };
  }, []);

  async function handleUnregister(agent: AgentEntry, dropBranch: boolean) {
    const msg = dropBranch
      ? `Unregister agent ${agent.agent_id} AND drop branch "${agent.branch}"?\n\nThis removes the worktree and deletes the branch.`
      : `Unregister agent ${agent.agent_id}? This removes its worktree.`;
    if (!window.confirm(msg)) return;
    setBusyId(agent.agent_id);
    setError(null);
    try {
      await unregisterAgent(agent.agent_id, !dropBranch);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      if (!cancelledRef.current) setBusyId(null);
    }
  }

  const summary = useMemo(() => {
    const total = agents.length;
    const stale = agents.filter((a) => a.pid == null).length;
    return `${total} active agent${total === 1 ? "" : "s"}, ${stale} stale`;
  }, [agents]);

  // Combined cumulative chart — sum of all agents over time.
  const chartPoints = useMemo<ChartPoint[]>(() => {
    // 1. Completed agents — sorted by ts, build cumulative baseline
    const completed = Object.values(costMap)
      .filter((e) => !e.live && (e.cost ?? 0) > 0)
      .map((e) => ({ t: Date.parse(e.ts), cost: e.cost ?? 0, tok: (e.inTok ?? 0) + (e.outTok ?? 0) }))
      .sort((a, b) => a.t - b.t);

    let baseCost = 0, baseTok = 0;
    const pts: ChartPoint[] = [];
    for (const p of completed) {
      baseCost += p.cost;
      baseTok += p.tok;
      pts.push({ t: p.t, cost: baseCost, tokens: baseTok });
    }

    // 2. Live agents — merge all timestamps, sum each agent's latest reading.
    // Each agent's readings are cumulative for that agent, so at every timestamp
    // we compute baseCost + Σ(latest cost per live agent up to that point).
    const liveEntries = Object.values(costMap).filter((e) => e.live);
    if (liveEntries.length > 0) {
      const allTs = [...new Set(
        liveEntries.flatMap((e) =>
          (e.readings ?? (e.cost != null ? [{ ts: e.ts, cost: e.cost, inTok: e.inTok ?? 0, outTok: e.outTok ?? 0 }] : []))
            .map((r) => r.ts)
        )
      )].sort();

      for (const ts of allTs) {
        let sumCost = 0, sumTok = 0;
        for (const e of liveEntries) {
          const readings = e.readings ?? (e.cost != null ? [{ ts: e.ts, cost: e.cost, inTok: e.inTok ?? 0, outTok: e.outTok ?? 0 }] : []);
          const latest = readings.filter((r) => r.ts <= ts).at(-1);
          if (latest) { sumCost += latest.cost; sumTok += latest.inTok + latest.outTok; }
        }
        pts.push({ t: Date.parse(ts), cost: baseCost + sumCost, tokens: baseTok + sumTok });
      }
    }

    pts.sort((a, b) => a.t - b.t);
    return pts;
  }, [costMap]);

  // Per-agent sparkline points (each agent's own cumulative readings)
  const perAgentPoints = useMemo<Record<string, ChartPoint[]>>(() => {
    const out: Record<string, ChartPoint[]> = {};
    for (const [id, e] of Object.entries(costMap)) {
      const readings = e.readings ?? (e.cost != null ? [{ ts: e.ts, cost: e.cost, inTok: e.inTok ?? 0, outTok: e.outTok ?? 0 }] : []);
      if (readings.length === 0) continue;
      out[id] = readings.map((r) => ({ t: Date.parse(r.ts), cost: r.cost, tokens: r.inTok + r.outTok }));
    }
    return out;
  }, [costMap]);

  return (
    <>
    {outputAgentId && (
      <AgentOutputLightbox agentId={outputAgentId} onClose={() => setOutputAgentId(null)} />
    )}
    {historyAgentId && (
      <AgentHistoryLightbox agentId={historyAgentId} onClose={() => setHistoryAgentId(null)} />
    )}
    <div id="agents-container">
      <div class="pane-bar">
        <span class="ui-eyebrow pane-bar-title">Agents</span>
        <span class="pane-bar-count">{loaded ? summary : "…"}</span>
        <div class="pane-bar-actions">
          <button class="ui-btn" onClick={() => refresh()} title="Refresh">↺</button>
        </div>
      </div>

      {chartPoints.length > 0 && (
        <div style={{ padding: "8px 0 4px" }}>
          <AgentCostChart
            points={chartPoints}
            width={520}
            height={56}
            label="All agents — cumulative cost"
          />
        </div>
      )}

      {error && <div class="agents-error">{error}</div>}

      {loaded && agents.length === 0 && !error ? (
        <EmptyState
          title="No active agents"
          body={<>Launch one with <code>the-lab-agent</code> (or <code>the-lab-agent --no-isolated</code> for legacy mode).</>}
        />
      ) : (
        <div class="agents-grid">
          {agents.map((agent) => {
            const alive = agent.pid != null;
            const busy = busyId === agent.agent_id;
            return (
              <section class="agents-card" key={agent.agent_id}>
                <div class="agents-card-top">
                  <div class="agents-card-id-block">
                    <div class="agents-card-id">{agent.agent_id}</div>
                    <Badge tone="neutral">{agent.role || "default"}</Badge>
                  </div>
                  <span
                    class="agents-pid-status"
                    title={alive ? `pid ${agent.pid}` : "no pid recorded"}
                  >
                    <Badge tone={alive ? "good" : "bad"} dot={alive}>
                      {alive ? `pid ${agent.pid}` : "no pid"}
                    </Badge>
                  </span>
                </div>

                <div class="agents-card-row">
                  <div class="agents-card-label">Working on</div>
                  <div class="agents-card-value">
                    {agent.current_idea ? (
                      <>
                        <div class="agents-branch">
                          <code>{agent.current_branch}</code>
                          {" — idea #"}{agent.current_idea.id}
                          {" "}<Badge tone={IDEA_STATUS_TONE[agent.current_idea.status] ?? "neutral"}>{agent.current_idea.status}</Badge>
                        </div>
                        <div class="agents-branch-parent" title={agent.current_idea.description}>
                          {agent.current_idea.description.split("\n")[0].slice(0, 100)}
                        </div>
                      </>
                    ) : (
                      <>
                        <div class="agents-branch">
                          <code>{agent.current_branch || agent.branch}</code>
                          {(!agent.current_branch || agent.current_branch === agent.branch) && (
                            <span class="agents-branch-parent" style={{ marginLeft: 6 }}>
                              (initial — not on an idea yet)
                            </span>
                          )}
                        </div>
                        <div class="agents-branch-parent">
                          initial branch <code>{agent.branch}</code>, branched from <code>{agent.parent_branch}</code>
                        </div>
                      </>
                    )}
                    {agent.unread_messages != null && agent.unread_messages > 0 && (
                      <div class="agents-branch-parent" style={{ marginTop: 4, color: "var(--yellow)" }}>
                        ✉ {agent.unread_messages} unread message{agent.unread_messages === 1 ? "" : "s"}
                      </div>
                    )}
                  </div>
                </div>

                <div class="agents-card-row">
                  <div class="agents-card-label">Worktree</div>
                  <div class="agents-card-value">
                    <code class="agents-worktree" title={agent.worktree}>
                      {truncateMiddle(agent.worktree, 56)}
                    </code>
                  </div>
                </div>

                <div class="agents-card-row">
                  <div class="agents-card-label">Created</div>
                  <div class="agents-card-value">
                    <span title={agent.created_at}>{relativeTime(agent.created_at)}</span>
                  </div>
                </div>

                {perAgentPoints[agent.agent_id] && (
                  <div class="agents-card-row" style={{ alignItems: "flex-start" }}>
                    <div class="agents-card-label" style={{ paddingTop: 2 }}>Cost</div>
                    <div class="agents-card-value">
                      <AgentCostChart
                        points={perAgentPoints[agent.agent_id]}
                        width={200}
                        height={44}
                        label=""
                        color="var(--green)"
                      />
                    </div>
                  </div>
                )}

                <div class="agents-card-actions">
                  <button
                    class="agents-btn"
                    onClick={() => setOutputAgentId(agent.agent_id)}
                    title="View timestamped output log"
                  >
                    Output
                  </button>
                  <button
                    class="agents-btn"
                    onClick={() => setHistoryAgentId(agent.agent_id)}
                    title="View conversation history and token cost"
                  >
                    History
                  </button>
                  <button
                    class="agents-btn"
                    disabled={busy}
                    onClick={() => handleUnregister(agent, false)}
                  >
                    {busy ? "Working..." : "Unregister"}
                  </button>
                  <button
                    class="agents-btn danger"
                    disabled={busy}
                    onClick={() => handleUnregister(agent, true)}
                    title="Unregister and delete the agent's branch"
                  >
                    Unregister & drop branch
                  </button>
                </div>
              </section>
            );
          })}
        </div>
      )}

      {/* ── Recent / past agents ─────────────────────────────────────────── */}
      {pastAgents.length > 0 && (
        <section class="agents-past">
          <div class="agents-messages-header">
            <span class="ui-eyebrow">Recent agents</span>
            <span class="agents-messages-meta">{pastAgents.length} completed</span>
          </div>
          <div class="agents-past-list">
            {pastAgents.map((a) => (
              <div class="agents-past-row" key={`${a.agent_id}-${a.completed_at}`}>
                <span class="agents-card-id agents-past-id">{a.agent_id}</span>
                <Badge tone="neutral">{a.role || "default"}</Badge>
                <code class="agents-past-branch">
                  {a.branch || "—"}
                </code>
                <span class="agents-past-time">
                  {a.completed_at ? relativeTime(a.completed_at) : "—"}
                </span>
                {agentCost(a.agent_id) != null ? (
                  <span class="agents-past-cost">
                    ${agentCost(a.agent_id)!.toFixed(3)}
                  </span>
                ) : (
                  <span class="agents-past-cost is-empty">—</span>
                )}
                <button
                  class="agents-btn agents-btn-copy"
                  onClick={() => setHistoryAgentId(a.agent_id)}
                  title="View conversation history and token cost"
                >
                  History
                </button>
              </div>
            ))}
          </div>
        </section>
      )}
    </div>
    </>
  );
}
