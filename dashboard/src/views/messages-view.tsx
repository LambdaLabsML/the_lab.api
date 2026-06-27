import { useEffect, useMemo, useRef, useState } from "preact/hooks";
import { listAgents, listMessages } from "../state/api";
import type { AgentEntry, MessageEntry } from "../lib/types";
import { Badge, EmptyState, IconButton } from "../components/ui";
import { messagesReadByMe } from "../state/settings";

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

type ReadFilter = "all" | "unread-me" | "read-me" | "agent-unread";
type SortOrder = "newest" | "oldest";

export function MessagesView() {
  const [agents, setAgents] = useState<AgentEntry[]>([]);
  const [messages, setMessages] = useState<MessageEntry[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Filter / sort controls (#3)
  const [query, setQuery] = useState("");
  const [agentFilter, setAgentFilter] = useState("");
  const [readFilter, setReadFilter] = useState<ReadFilter>("all");
  const [sort, setSort] = useState<SortOrder>("newest");

  // Re-render every ~30s so "Xm ago" stays fresh
  const [, setTick] = useState(0);

  const cancelledRef = useRef(false);

  // My personal read state (#2) — client-side, reactive.
  const readMap = messagesReadByMe.value;
  const isReadByMe = (id: number) => !!readMap[String(id)];
  function setReadByMe(ids: number[], read: boolean) {
    const next = { ...messagesReadByMe.value };
    for (const id of ids) {
      if (read) next[String(id)] = true;
      else delete next[String(id)];
    }
    messagesReadByMe.value = next;
  }

  async function refresh() {
    try {
      const [agentList, msgs] = await Promise.all([
        listAgents().catch(() => [] as AgentEntry[]),
        listMessages(200).catch(() => [] as MessageEntry[]),
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

  // Resolve agent_id -> role for nicer "from"/"to"/reader display when available.
  const roleByAgent = useMemo(() => {
    const map: Record<string, string> = {};
    for (const a of agents) map[a.agent_id] = a.role || "default";
    return map;
  }, [agents]);

  function labelAgent(id: string): string {
    const role = roleByAgent[id];
    return role ? `${id} · ${role}` : id;
  }

  function describeRecipient(to: string): string {
    if (to === "all") return "everyone";
    if (to.startsWith("agent:")) return labelAgent(to.slice(6));
    if (to.startsWith("role:")) return `role:${to.slice(5)}`;
    return to;
  }

  function describeSender(m: MessageEntry): string {
    if (m.from_agent) {
      const role = m.from_role || roleByAgent[m.from_agent];
      return role ? `${m.from_agent} · ${role}` : m.from_agent;
    }
    return m.from_role || "system";
  }

  // ── Apply filters + sort ──────────────────────────────────────────────────
  const displayed = useMemo(() => {
    const q = query.trim().toLowerCase();
    let list = messages.slice();

    if (agentFilter) {
      list = list.filter(
        (m) =>
          m.from_agent === agentFilter ||
          m.to === `agent:${agentFilter}` ||
          (m.read_by || []).includes(agentFilter),
      );
    }
    if (readFilter === "unread-me") list = list.filter((m) => !isReadByMe(m.id));
    else if (readFilter === "read-me") list = list.filter((m) => isReadByMe(m.id));
    else if (readFilter === "agent-unread") list = list.filter((m) => !(m.read_by || []).length);

    if (q) {
      list = list.filter((m) =>
        `${m.from_agent || ""} ${m.from_role || ""} ${m.to} ${describeRecipient(m.to)} ${m.text} ${(m.read_by || []).join(" ")}`
          .toLowerCase()
          .includes(q),
      );
    }
    list.sort((a, b) => {
      const da = Date.parse(a.created_at) || a.id;
      const db = Date.parse(b.created_at) || b.id;
      return sort === "newest" ? db - da : da - db;
    });
    return list;
    // readMap drives isReadByMe; include it so read/unread filters update live.
  }, [messages, query, agentFilter, readFilter, sort, readMap, roleByAgent]);

  const unreadByMe = messages.filter((m) => !isReadByMe(m.id)).length;
  const messageCount = displayed.length;
  const countLabel = !loaded
    ? "…"
    : `${messageCount} shown · ${unreadByMe} unread by you`;

  return (
    <div id="messages-container">
      <div class="pane-bar">
        <h2 class="pane-bar-title">Messages</h2>
        <span class="pane-bar-count">{countLabel}</span>
        <div class="pane-bar-actions">
          <IconButton
            onClick={() => setReadByMe(displayed.map((m) => m.id), true)}
            title="Mark all shown messages read (by you)"
          >
            ✓✓ Read all
          </IconButton>
          <IconButton onClick={() => refresh()} title="Refresh">↺</IconButton>
        </div>
      </div>

      {/* ── Sort + filter bar (#3) ── */}
      <div class="messages-filterbar">
        <input
          class="messages-search"
          type="search"
          placeholder="Search from / to / text…"
          value={query}
          onInput={(e) => setQuery((e.target as HTMLInputElement).value)}
        />
        <select
          class="messages-select"
          value={agentFilter}
          title="Filter by agent (sender, recipient, or reader)"
          onChange={(e) => setAgentFilter((e.target as HTMLSelectElement).value)}
        >
          <option value="">All agents</option>
          {agents.map((a) => (
            <option key={a.agent_id} value={a.agent_id}>{labelAgent(a.agent_id)}</option>
          ))}
        </select>
        <select
          class="messages-select"
          value={readFilter}
          title="Filter by read state"
          onChange={(e) => setReadFilter((e.target as HTMLSelectElement).value as ReadFilter)}
        >
          <option value="all">All</option>
          <option value="unread-me">Unread by you</option>
          <option value="read-me">Read by you</option>
          <option value="agent-unread">No agent has read</option>
        </select>
        <select
          class="messages-select"
          value={sort}
          title="Sort order"
          onChange={(e) => setSort((e.target as HTMLSelectElement).value as SortOrder)}
        >
          <option value="newest">Newest first</option>
          <option value="oldest">Oldest first</option>
        </select>
      </div>

      {error && <div class="agents-error">{error}</div>}

      <section class="agents-messages">
        {messageCount === 0 ? (
          <EmptyState
            icon="✉"
            title={messages.length === 0 ? "No messages yet" : "No messages match"}
            body={
              messages.length === 0 ? (
                <>
                  Messages sent here arrive in agents' <code>_notifications</code> on their next API call.
                  Use the ✉ button below or <code>POST /api/v1/messages</code>.
                </>
              ) : (
                <>Adjust the search or filters above to see more.</>
              )
            }
          />
        ) : (
          <ol class="agents-messages-list">
            {displayed.map((m) => {
              const readByMe = isReadByMe(m.id);
              const readers = m.read_by || [];
              return (
                <li class={`agents-message${readByMe ? " is-readme" : " is-newme"}`} key={m.id}>
                  <div class="agents-message-head">
                    {/* #2 — your personal read indicator (click to toggle) */}
                    <button
                      class={`messages-readdot${readByMe ? " is-read" : ""}`}
                      title={readByMe ? "Read by you — click to mark unread" : "Unread by you — click to mark read"}
                      onClick={() => setReadByMe([m.id], !readByMe)}
                    />
                    <span class="agents-message-from" title={`agent ${m.from_agent ?? "system"}`}>
                      {describeSender(m)}
                    </span>
                    <span class="agents-message-arrow">→</span>
                    <span class="agents-message-to">{describeRecipient(m.to)}</span>
                    <span class="agents-message-time" title={m.created_at}>
                      {relativeTime(m.created_at)}
                    </span>
                  </div>

                  <div class="agents-message-body">{m.text}</div>

                  {/* #1 — which agents have read this message */}
                  <div class="agents-message-readers">
                    {readers.length > 0 ? (
                      <>
                        <span class="agents-message-readers-label">read by</span>
                        {readers.map((rid) => (
                          <span class="agents-message-reader" key={rid} title={`read by ${rid}`}>
                            {labelAgent(rid)}
                          </span>
                        ))}
                      </>
                    ) : (
                      <Badge tone="warn">no agent has read</Badge>
                    )}
                  </div>
                </li>
              );
            })}
          </ol>
        )}
      </section>
    </div>
  );
}
