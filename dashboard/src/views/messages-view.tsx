import { useEffect, useMemo, useRef, useState } from "preact/hooks";
import { listAgents, listMessages } from "../state/api";
import type { AgentEntry, MessageEntry } from "../lib/types";
import { Badge, EmptyState, IconButton } from "../components/ui";
import { agentColor, agentInitials } from "../lib/colors";
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

type ReadFilter = "all" | "unread" | "read" | "agent-unread";
type SortOrder = "newest" | "oldest";

/** Mirror of the server's addressing rules (messages.is_for / unread_for):
 *  a message is in an agent's inbox if it's to them (id/role) or a broadcast
 *  from someone else — never their own messages. */
function inboxOf(m: MessageEntry, agentId: string, role: string | undefined): boolean {
  if (m.from_agent === agentId) return false;
  if (m.to === "all") return true;
  if (m.to === `agent:${agentId}`) return true;
  if (role && m.to === `role:${role}`) return true;
  return false;
}

function isLong(text: string): boolean {
  return text.length > 360 || (text.match(/\n/g)?.length ?? 0) > 8;
}

export function MessagesView() {
  const [agents, setAgents] = useState<AgentEntry[]>([]);
  const [messages, setMessages] = useState<MessageEntry[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Filter / sort controls
  const [query, setQuery] = useState("");
  const [agentFilter, setAgentFilter] = useState("");
  const [readFilter, setReadFilter] = useState<ReadFilter>("all");
  const [sort, setSort] = useState<SortOrder>("newest");
  /** "" = me (the UI user); otherwise read-only perspective of that agent. */
  const [viewAs, setViewAs] = useState("");
  const [expanded, setExpanded] = useState<Set<number>>(new Set());

  // Re-render every ~30s so "Xm ago" stays fresh
  const [, setTick] = useState(0);
  const cancelledRef = useRef(false);

  // My personal read state — client-side, reactive (only used in "me" view).
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
  function recipientColor(to: string): string {
    if (to.startsWith("agent:")) return agentColor(to.slice(6));
    return "var(--accent)";
  }
  function describeSender(m: MessageEntry): string {
    if (m.from_agent) {
      const role = m.from_role || roleByAgent[m.from_agent];
      return role ? `${m.from_agent} · ${role}` : m.from_agent;
    }
    return m.from_role || "system";
  }

  // Perspective: "me" uses local read state; an agent uses its server read_by.
  const inPerspective = !!viewAs;
  const viewRole = viewAs ? roleByAgent[viewAs] : undefined;
  const readStateOf = (m: MessageEntry): boolean =>
    inPerspective ? (m.read_by || []).includes(viewAs) : isReadByMe(m.id);
  const relevant = (m: MessageEntry): boolean =>
    inPerspective ? inboxOf(m, viewAs, viewRole) : true;

  const displayed = useMemo(() => {
    const q = query.trim().toLowerCase();
    let list = messages.filter(relevant);

    if (agentFilter) {
      list = list.filter(
        (m) =>
          m.from_agent === agentFilter ||
          m.to === `agent:${agentFilter}` ||
          (m.read_by || []).includes(agentFilter),
      );
    }
    if (readFilter === "unread") list = list.filter((m) => !readStateOf(m));
    else if (readFilter === "read") list = list.filter((m) => readStateOf(m));
    else if (readFilter === "agent-unread") list = list.filter((m) => !(m.read_by || []).length);

    if (q) {
      list = list.filter((m) =>
        `${m.from_agent || ""} ${m.from_role || ""} ${m.to} ${describeRecipient(m.to)} ${m.text} ${(m.read_by || []).join(" ")}`
          .toLowerCase()
          .includes(q),
      );
    }
    list = list.slice().sort((a, b) => {
      const da = Date.parse(a.created_at) || a.id;
      const db = Date.parse(b.created_at) || b.id;
      return sort === "newest" ? db - da : da - db;
    });
    return list;
  }, [messages, query, agentFilter, readFilter, sort, viewAs, readMap, roleByAgent]);

  const relevantMsgs = messages.filter(relevant);
  const unreadCount = relevantMsgs.filter((m) => !readStateOf(m)).length;
  const who = inPerspective ? viewAs : "you";
  const countLabel = !loaded
    ? "…"
    : `${displayed.length} shown · ${unreadCount} unread by ${who}`;

  function toggleExpand(id: number) {
    setExpanded((prev) => {
      const n = new Set(prev);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });
  }

  return (
    <div id="messages-container">
      <div class="pane-bar">
        <h2 class="pane-bar-title">Messages</h2>
        <span class="pane-bar-count">{countLabel}</span>
        <div class="pane-bar-actions">
          {!inPerspective && (
            <IconButton
              onClick={() => setReadByMe(displayed.map((m) => m.id), true)}
              title="Mark all shown messages read (by you)"
            >
              ✓✓ Read all
            </IconButton>
          )}
          <IconButton onClick={() => refresh()} title="Refresh">↺</IconButton>
        </div>
      </div>

      {/* ── Sort + filter bar ── */}
      <div class="messages-filterbar">
        <input
          class="messages-search"
          type="search"
          placeholder="Search from / to / text…"
          value={query}
          onInput={(e) => setQuery((e.target as HTMLInputElement).value)}
        />
        <label class="messages-viewas" title="See messages from another agent's perspective (read-only)">
          <span>view as</span>
          <select
            class="messages-select"
            value={viewAs}
            onChange={(e) => setViewAs((e.target as HTMLSelectElement).value)}
            style={viewAs ? { color: agentColor(viewAs), fontWeight: 600 } : undefined}
          >
            <option value="">you (the dashboard)</option>
            {agents.map((a) => (
              <option key={a.agent_id} value={a.agent_id}>{labelAgent(a.agent_id)}</option>
            ))}
          </select>
        </label>
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
          <option value="unread">{inPerspective ? `Unseen by ${viewAs}` : "Unread by you"}</option>
          <option value="read">{inPerspective ? `Seen by ${viewAs}` : "Read by you"}</option>
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

      {inPerspective && (
        <div class="messages-perspective-note">
          👁 Viewing <b style={{ color: agentColor(viewAs) }}>{labelAgent(viewAs)}</b>'s inbox — read-only;
          nothing is marked read for them.
        </div>
      )}

      {error && <div class="agents-error">{error}</div>}

      <section class="agents-messages">
        {displayed.length === 0 ? (
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
                <>Adjust the search, filters, or perspective above to see more.</>
              )
            }
          />
        ) : (
          <ol class="msg-list">
            {displayed.map((m) => {
              const read = readStateOf(m);
              const readers = m.read_by || [];
              const long = isLong(m.text);
              const open = expanded.has(m.id);
              const sColor = agentColor(m.from_agent);
              return (
                <li class={`msg${read ? " is-read" : " is-new"}`} key={m.id}>
                  <div
                    class="msg-avatar"
                    style={{ background: sColor }}
                    title={`agent ${m.from_agent ?? "system"}`}
                  >
                    {agentInitials(m.from_agent)}
                  </div>
                  <div class="msg-main">
                    <div class="msg-head">
                      <span class="msg-from" style={{ color: sColor }}>{describeSender(m)}</span>
                      <span class="msg-arrow">→</span>
                      <span class="msg-to" style={{ color: recipientColor(m.to) }}>{describeRecipient(m.to)}</span>
                      <span class="msg-time" title={m.created_at}>{relativeTime(m.created_at)}</span>
                      {inPerspective ? (
                        <span
                          class={`msg-readdot is-static${read ? " is-read" : ""}`}
                          title={read ? `Seen by ${viewAs}` : `Not seen by ${viewAs}`}
                        />
                      ) : (
                        <button
                          class={`msg-readdot${read ? " is-read" : ""}`}
                          title={read ? "Read by you — click to mark unread" : "Unread by you — click to mark read"}
                          onClick={() => setReadByMe([m.id], !read)}
                        />
                      )}
                    </div>

                    <div class={`msg-body${long && !open ? " is-clamped" : ""}`}>{m.text}</div>
                    {long && (
                      <button class="msg-expand" onClick={() => toggleExpand(m.id)}>
                        {open ? "Show less" : "Show more"}
                      </button>
                    )}

                    <div class="msg-readers">
                      {readers.length > 0 ? (
                        <>
                          <span class="msg-readers-label">read by</span>
                          {readers.map((rid) => (
                            <span
                              class="msg-reader"
                              key={rid}
                              title={`read by ${rid}`}
                              style={{ color: agentColor(rid), borderColor: agentColor(rid) }}
                            >
                              {labelAgent(rid)}
                            </span>
                          ))}
                        </>
                      ) : (
                        <Badge tone="warn">no agent has read</Badge>
                      )}
                    </div>
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
