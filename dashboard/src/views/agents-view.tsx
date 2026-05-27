import { useEffect, useMemo, useRef, useState } from "preact/hooks";
import { listAgents, listMessages, unregisterAgent } from "../state/api";
import type { AgentEntry, MessageEntry } from "../lib/types";

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

export function AgentsView() {
  const [agents, setAgents] = useState<AgentEntry[]>([]);
  const [messages, setMessages] = useState<MessageEntry[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [outputAgentId, setOutputAgentId] = useState<string | null>(null);
  // Re-render every ~30s so "Xm ago" stays fresh
  const [, setTick] = useState(0);

  const cancelledRef = useRef(false);

  async function refresh(): Promise<AgentEntry[] | null> {
    try {
      // Fetch agents + messages in parallel; the inbox is small and the
      // agents pane is exactly where the conversation belongs.
      const [list, msgs] = await Promise.all([
        listAgents(),
        listMessages(100).catch(() => [] as MessageEntry[]),
      ]);
      if (cancelledRef.current) return null;
      setAgents(list);
      setMessages(msgs);
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

  // Resolve agent_id -> role for nicer "from"/"to" display when available.
  const roleByAgent = useMemo(() => {
    const map: Record<string, string> = {};
    for (const a of agents) map[a.agent_id] = a.role || "default";
    return map;
  }, [agents]);

  function describeRecipient(to: string): string {
    if (to === "all") return "everyone";
    if (to.startsWith("agent:")) {
      const id = to.slice(6);
      const role = roleByAgent[id];
      return role ? `${id} (${role})` : id;
    }
    if (to.startsWith("role:")) return `role:${to.slice(5)}`;
    return to;
  }

  function describeSender(m: MessageEntry): string {
    if (m.from_agent) {
      const role = m.from_role || roleByAgent[m.from_agent];
      return role ? `${m.from_agent} (${role})` : m.from_agent;
    }
    return m.from_role || "system";
  }

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

      <section class="agents-messages">
        <div class="agents-messages-header">
          <h3>Message log</h3>
          <span class="agents-messages-meta">
            {messages.length === 0 ? "no messages yet" : `${messages.length} message${messages.length === 1 ? "" : "s"}`}
          </span>
        </div>
        {messages.length === 0 ? (
          <div class="agents-messages-empty">
            Inter-agent messages will appear here as agents send them via
            <code> POST /api/v1/messages</code>.
          </div>
        ) : (
          <ol class="agents-messages-list">
            {messages.map((m) => (
              <li class="agents-message" key={m.id}>
                <div class="agents-message-head">
                  <span class="agents-message-from" title={`agent ${m.from_agent ?? "system"}`}>
                    {describeSender(m)}
                  </span>
                  <span class="agents-message-arrow">→</span>
                  <span class="agents-message-to">{describeRecipient(m.to)}</span>
                  <span class="agents-message-time" title={m.created_at}>
                    {relativeTime(m.created_at)}
                  </span>
                  {m.read_by && m.read_by.length > 0 ? (
                    <span class="agents-message-read" title={`read by: ${m.read_by.join(", ")}`}>
                      ✓ read by {m.read_by.length}
                    </span>
                  ) : (
                    <span class="agents-message-unread">unread</span>
                  )}
                </div>
                <div class="agents-message-body">{m.text}</div>
              </li>
            ))}
          </ol>
        )}
      </section>
    </div>
    </>
  );
}
