import { useEffect, useMemo, useRef, useState } from "preact/hooks";
import { listAgents, unregisterAgent } from "../state/api";
import type { AgentEntry } from "../lib/types";

// ── History / cost lightbox ───────────────────────────────────────────────────

interface HistorySession {
  session_id: string;
  started_at: string | null;
  message_count: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
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
    cost_usd: number;
  };
}

function fmtK(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(0)}k`;
  return String(n);
}

function AgentHistoryLightbox({ agentId, onClose }: { agentId: string; onClose: () => void }) {
  const [data, setData] = useState<HistoryData | null>(null);
  const [error, setError] = useState<string | null>(null);

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
          {error && <div style={{ color: "var(--red)", padding: "12px 16px" }}>{error}</div>}
          {!data && !error && <div style={{ padding: "12px 16px", color: "var(--text-muted)" }}>Loading…</div>}
          {data && (
            <>
              {/* Totals bar */}
              <div style={{
                display: "flex", gap: 24, flexWrap: "wrap",
                padding: "10px 16px", borderBottom: "1px solid var(--border)",
                background: "var(--bg-elev)",
              }}>
                <div><span style={{ color: "var(--text-muted)", fontSize: "var(--text-xs)" }}>SESSIONS</span><br/><strong>{data.totals.sessions}</strong></div>
                <div><span style={{ color: "var(--text-muted)", fontSize: "var(--text-xs)" }}>MESSAGES</span><br/><strong>{data.totals.message_count}</strong></div>
                <div><span style={{ color: "var(--text-muted)", fontSize: "var(--text-xs)" }}>INPUT</span><br/><strong>{fmtK(data.totals.input_tokens)}</strong></div>
                <div><span style={{ color: "var(--text-muted)", fontSize: "var(--text-xs)" }}>OUTPUT</span><br/><strong>{fmtK(data.totals.output_tokens)}</strong></div>
                <div><span style={{ color: "var(--text-muted)", fontSize: "var(--text-xs)" }}>CACHE HIT</span><br/><strong>{fmtK(data.totals.cache_read_tokens)}</strong></div>
                <div><span style={{ color: "var(--text-muted)", fontSize: "var(--text-xs)" }}>EST. COST</span><br/><strong style={{ color: "var(--accent)" }}>${data.totals.cost_usd.toFixed(3)}</strong></div>
              </div>
              {/* Per-session table */}
              {data.sessions.length === 0 ? (
                <div style={{ padding: "12px 16px", color: "var(--text-muted)" }}>No session files found yet.</div>
              ) : (
                <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "var(--text-sm)" }}>
                  <thead>
                    <tr style={{ borderBottom: "1px solid var(--border)", color: "var(--text-muted)", textAlign: "left" }}>
                      <th style={{ padding: "6px 16px" }}>Session</th>
                      <th style={{ padding: "6px 8px" }}>Started</th>
                      <th style={{ padding: "6px 8px", textAlign: "right" }}>Msgs</th>
                      <th style={{ padding: "6px 8px", textAlign: "right" }}>In</th>
                      <th style={{ padding: "6px 8px", textAlign: "right" }}>Out</th>
                      <th style={{ padding: "6px 8px", textAlign: "right" }}>Cache</th>
                      <th style={{ padding: "6px 16px", textAlign: "right" }}>Cost</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.sessions.map((s) => (
                      <tr key={s.session_id} style={{ borderBottom: "1px solid var(--border-faint, var(--border))" }}>
                        <td style={{ padding: "6px 16px", fontFamily: "var(--font-mono)", fontSize: "var(--text-xs)", color: "var(--text-muted)" }}>{s.session_id.slice(0, 16)}…</td>
                        <td style={{ padding: "6px 8px", color: "var(--text-muted)", fontSize: "var(--text-xs)" }}>{s.started_at ? s.started_at.slice(0, 16).replace("T", " ") : "—"}</td>
                        <td style={{ padding: "6px 8px", textAlign: "right" }}>{s.message_count}</td>
                        <td style={{ padding: "6px 8px", textAlign: "right", color: "var(--text-muted)" }}>{fmtK(s.input_tokens)}</td>
                        <td style={{ padding: "6px 8px", textAlign: "right", color: "var(--text-muted)" }}>{fmtK(s.output_tokens)}</td>
                        <td style={{ padding: "6px 8px", textAlign: "right", color: "var(--text-muted)" }}>{fmtK(s.cache_read_tokens)}</td>
                        <td style={{ padding: "6px 16px", textAlign: "right", color: "var(--accent)" }}>${s.cost_usd.toFixed(3)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
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
  const [follow, setFollow] = useState(true);
  const bodyRef = useRef<HTMLPreElement>(null);
  const pollRef = useRef<number | null>(null);

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
              onClick={() => setFollow(!follow)}
            >
              {follow ? "↓ following" : "↓ follow"}
            </button>
          </div>
          <span class="lightbox-close" onClick={onClose}>×</span>
        </div>
        <div class="lightbox-body" style={{ padding: 0 }}>
          <pre
            ref={bodyRef}
            style={{
              margin: 0, padding: "10px 14px",
              fontFamily: "var(--font-mono)", fontSize: "var(--text-sm)",
              color: "var(--text)", lineHeight: 1.5,
              whiteSpace: "pre-wrap", wordBreak: "break-word",
              overflowY: "auto", maxHeight: "75vh",
              background: "var(--bg)",
            }}
          >{text}</pre>
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

  return (
    <>
    {outputAgentId && (
      <AgentOutputLightbox agentId={outputAgentId} onClose={() => setOutputAgentId(null)} />
    )}
    {historyAgentId && (
      <AgentHistoryLightbox agentId={historyAgentId} onClose={() => setHistoryAgentId(null)} />
    )}
    <div id="agents-container">
      <div class="agents-header">
        <div class="agents-header-left">
          <h2>Agents</h2>
          <p>
            Per-agent git worktrees registered by <code>the-lab-agent</code>.
            Each agent gets its own branch off the most recent active idea, isolating its
            edits from other concurrent agents.
          </p>
        </div>
        <div class="agents-header-right">
          <div class="agents-summary">{loaded ? summary : "Loading..."}</div>
          <button
            class="agents-btn"
            onClick={() => refresh()}
            title="Reload agent list"
          >
            Refresh
          </button>
        </div>
      </div>

      {error && <div class="agents-error">{error}</div>}

      {loaded && agents.length === 0 && !error ? (
        <div class="agents-empty">
          <div class="agents-empty-title">No active agents.</div>
          <div class="agents-empty-body">
            Launch one with <code>the-lab-agent</code>
            {" "}(or <code>the-lab-agent --no-isolated</code> for legacy mode).
          </div>
        </div>
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
                    <span class="agents-chip">{agent.role || "default"}</span>
                  </div>
                  <div
                    class={`agents-pid-status ${alive ? "alive" : "dead"}`}
                    title={alive ? `pid ${agent.pid}` : "no pid recorded"}
                  >
                    <span class="agents-dot" />
                    <span class="agents-pid-label">
                      {alive ? `pid ${agent.pid}` : "no pid"}
                    </span>
                  </div>
                </div>

                <div class="agents-card-row">
                  <div class="agents-card-label">Working on</div>
                  <div class="agents-card-value">
                    {agent.current_idea ? (
                      <>
                        <div class="agents-branch">
                          <code>{agent.current_branch}</code>
                          {" — idea #"}{agent.current_idea.id}
                          {" "}<span class="agents-chip">{agent.current_idea.status}</span>
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
            <h3>Recent agents</h3>
            <span class="agents-messages-meta">{pastAgents.length} completed</span>
          </div>
          <div class="agents-past-list">
            {pastAgents.map((a) => (
              <div class="agents-past-row" key={`${a.agent_id}-${a.completed_at}`}>
                <span class="agents-card-id" style={{ minWidth: 60 }}>{a.agent_id}</span>
                <span class="agents-chip">{a.role || "default"}</span>
                <code style={{ fontSize: "var(--text-xs)", color: "var(--text-muted)", flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {a.branch || "—"}
                </code>
                <span style={{ fontSize: "var(--text-xs)", color: "var(--text-muted)", whiteSpace: "nowrap" }}>
                  {a.completed_at ? relativeTime(a.completed_at) : "—"}
                </span>
                <button
                  class="agents-btn"
                  onClick={() => setHistoryAgentId(a.agent_id)}
                  title="View conversation history and token cost"
                  style={{ padding: "2px 8px", fontSize: "var(--text-xs)" }}
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
