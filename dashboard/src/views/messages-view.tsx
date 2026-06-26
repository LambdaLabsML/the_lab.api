import { useEffect, useMemo, useRef, useState } from "preact/hooks";
import { listAgents, listMessages } from "../state/api";
import type { AgentEntry, MessageEntry } from "../lib/types";
import { Badge, EmptyState, IconButton, Toggle } from "../components/ui";

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

export function MessagesView() {
  const [agents, setAgents] = useState<AgentEntry[]>([]);
  const [messages, setMessages] = useState<MessageEntry[]>([]);
  const [showAll, setShowAll] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Re-render every ~30s so "Xm ago" stays fresh
  const [, setTick] = useState(0);

  const cancelledRef = useRef(false);

  async function refresh() {
    try {
      const [agentList, msgs] = await Promise.all([
        listAgents().catch(() => [] as AgentEntry[]),
        listMessages(100).catch(() => [] as MessageEntry[]),
      ]);
      if (cancelledRef.current) return;
      setAgents(agentList);
      setMessages(msgs);
      setError(null);
      setLoaded(true);
    } catch (err) {
      if (cancelledRef.current) return;
      setError(err instanceof Error ? err.message : String(err));
      setLoaded(true);
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

  const displayed = showAll ? messages : messages.filter((m) => !m.read_by?.length);
  const unreadCount = messages.filter((m) => !m.read_by?.length).length;
  const messageCount = displayed.length;
  const countLabel =
    messageCount === 0
      ? (showAll ? "no messages yet" : "no unread messages")
      : `${messageCount} ${showAll ? "" : "unread "}message${messageCount === 1 ? "" : "s"}`;

  return (
    <div id="messages-container">
      <div class="pane-bar">
        <h2 class="pane-bar-title">Messages</h2>
        <span class="pane-bar-count">{loaded ? countLabel : "…"}</span>
        <div class="pane-bar-actions">
          <Toggle active={showAll} onClick={() => setShowAll((v) => !v)}>
            {showAll ? `Unread only${unreadCount ? ` (${unreadCount})` : ""}` : "Show all"}
          </Toggle>
          <IconButton onClick={() => refresh()} title="Refresh">↺</IconButton>
        </div>
      </div>

      {error && <div class="agents-error">{error}</div>}

      <section class="agents-messages">
        {messageCount === 0 ? (
          <EmptyState
            icon="✉"
            title={showAll ? "No messages yet" : "No unread messages"}
            body={
              <>
                Messages sent here arrive in agents' <code>_notifications</code> on their next API call.
                Use the ✉ button below or <code>POST /api/v1/messages</code>.
              </>
            }
          />
        ) : (
          <ol class="agents-messages-list">
            {displayed.map((m) => (
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
                    <span
                      class="ui-badge ui-badge--good agents-message-read"
                      title={`read by: ${m.read_by.join(", ")}`}
                    >
                      ✓ read by {m.read_by.length}
                    </span>
                  ) : (
                    <Badge tone="warn" class="agents-message-unread">unread</Badge>
                  )}
                </div>
                <div class="agents-message-body">{m.text}</div>
              </li>
            ))}
          </ol>
        )}
      </section>
    </div>
  );
}
