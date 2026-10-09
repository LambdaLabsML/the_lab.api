import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "preact/hooks";
import {
  createThread,
  getMessage,
  getMessagePage,
  listAgents,
  listMessagesPage,
  listThreads,
  sendMessage,
  updateThread,
} from "../state/api";
import { useLiveRefresh } from "../lib/hooks";
import type { AgentEntry, MessageEntry, ThreadMeta } from "../lib/types";
import { Badge, EmptyState, IconButton } from "../components/ui";
import { agentColor, registerAgents, agentInitials } from "../lib/colors";
import { messagesMyNames, messagesReadByMe, serverDemoMode } from "../state/settings";
import {
  markMessagesReadByMe,
  markThreadReadByMe,
  markAllMessagesReadByMe,
  registerDashboardUserMessage,
  isAddressedToMe,
  isSentHere,
  countsAsNew,
} from "../state/messages";
import { focusMessageId } from "../state/agent-activity";
import { RichText, stripRouting } from "../components/rich-text";
import { IdeaLink } from "../components/idea-link";
import { ExpLink } from "../components/exp-link";

type ReadFilter = "all" | "unread" | "read" | "agent-unread";
type SortOrder = "newest" | "oldest";
const threadKey = (m: MessageEntry) => m.thread_id ?? m.id;
const timeOf = (m: MessageEntry) => Date.parse(m.created_at) || m.id;
const recipientOf = (m: MessageEntry) =>
  m.from_agent && m.from_agent !== "user"
    ? `agent:${m.from_agent}`
    : m.from_role && m.from_role !== "user"
      ? `role:${m.from_role}`
      : null;

function relativeTime(iso: string) {
  const s = Math.max(0, Math.floor((Date.now() - Date.parse(iso)) / 1000));
  if (!Number.isFinite(s)) return iso || "--";
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 172800) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}
function inboxOf(
  m: MessageEntry,
  agent: string,
  role?: string,
  participants?: Map<number, Set<string>>,
) {
  return (
    m.from_agent !== agent &&
    (m.to === "all" ||
      m.to === `agent:${agent}` ||
      (!!role && m.to === `role:${role}`) ||
      // Thread posts reach members; posting in a thread is how agents
      // usually join, so participants stand in for the member list.
      (m.to.startsWith("thread:") && !!participants?.get(threadKey(m))?.has(agent)))
  );
}

function Participant({ id }: { id: string }) {
  const color = agentColor(id);
  return (
    <span class="thread-participant" style={{ "--participant-color": color } as Record<string, string>}>
      <span class="thread-participant-avatar" aria-hidden="true">{agentInitials(id)}</span>
      <span>{id}</span>
    </span>
  );
}

function MessageReferences({ message }: { message: MessageEntry }) {
  if (!message.references?.length) return null;
  return (
    <div class="thread-message-references" aria-label="Attached references">
      {message.references.map((ref, index) => {
        if (ref.kind === "idea") return <IdeaLink key={`idea-${ref.id}-${index}`} id={ref.id} />;
        if (ref.kind === "experiment") return <ExpLink key={`exp-${ref.id}-${index}`} label={ref.id} />;
        const href = `/api/v1/files/${ref.path.split("/").map(encodeURIComponent).join("/")}`;
        return <a key={`resource-${ref.path}-${index}`} href={href} target="_blank" rel="noopener noreferrer">↗ {ref.path}</a>;
      })}
    </div>
  );
}

export function MessagesView() {
  const [agents, setAgents] = useState<AgentEntry[]>([]),
    [messages, setMessages] = useState<MessageEntry[]>([]);
  const [loaded, setLoaded] = useState(false),
    [error, setError] = useState<string | null>(null);
  const [messageTotal, setMessageTotal] = useState(0);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [pendingFocus, setPendingFocus] = useState<number | null>(null);
  const [newThreadOpen, setNewThreadOpen] = useState(false);
  const [query, setQuery] = useState(""),
    [agentFilter, setAgentFilter] = useState(""),
    [conversationScope, setConversationScope] = useState<"all" | "mine">("all"),
    [readFilter, setReadFilter] = useState<ReadFilter>("all"),
    [sort, setSort] = useState<SortOrder>("newest"),
    [viewAs, setViewAs] = useState("");
  const [selectedThread, setSelectedThread] = useState<number | null>(null),
    [threadMessages, setThreadMessages] = useState<MessageEntry[]>([]),
    [threadTotal, setThreadTotal] = useState(0),
    [threadLoading, setThreadLoading] = useState(false),
    [threadError, setThreadError] = useState<string | null>(null);
  const [threadMeta, setThreadMeta] = useState<Map<number, ThreadMeta>>(new Map());
  // Inline rename of the open thread: the draft title while editing, else null.
  const [renameDraft, setRenameDraft] = useState<string | null>(null);
  // Escape unmounts the input, which can still fire blur with the old draft.
  const renameCancelled = useRef(false);
  useEffect(() => { setRenameDraft(null); }, [selectedThread]);
  // Focus + select the title when editing starts (autoFocus misses dblclick).
  const renameInputRef = useRef<HTMLInputElement>(null);
  const renaming = renameDraft !== null;
  useEffect(() => {
    if (renaming) { renameInputRef.current?.focus(); renameInputRef.current?.select(); }
  }, [renaming]);
  const [newTo, setNewTo] = useState("all"),
    [newTitle, setNewTitle] = useState(""),
    [newTldr, setNewTldr] = useState(""),
    [newText, setNewText] = useState(""),
    [replyTo, setReplyTo] = useState("all"),
    [replyTldr, setReplyTldr] = useState(""),
    [replyText, setReplyText] = useState(""),
    [sending, setSending] = useState<"new" | "reply" | null>(null),
    [sendError, setSendError] = useState<string | null>(null);
  const cancelled = useRef(false),
    requestNumber = useRef(0),
    selectedThreadRef = useRef<number | null>(null),
    viewAsRef = useRef(""),
    replyTargetThreadRef = useRef<number | null>(null);
  const sendingRef = useRef(false);
  const replyDrafts = useRef(new Map<number, string>());
  const replyTldrDrafts = useRef(new Map<number, string>());
  const messageListRef = useRef<HTMLOListElement>(null);
  const followNewestRef = useRef(true);
  const scrollSnapshot = useRef<{ thread: number; first: number; last: number; height: number; top: number } | null>(null);
  const readMap = messagesReadByMe.value;
  const myNames = messagesMyNames.value; // re-render when the names change
  const demo = serverDemoMode.value;
  const readByMe = (id: number) => !!readMap[String(id)];
  const setReadByMe = (ids: number[], read: boolean) =>
    markMessagesReadByMe(ids, read);
  // Distinct colours for every agent in view (registry in lib/colors).
  registerAgents([...agents.map((a) => a.agent_id), ...messages.map((m) => m.from_agent)]);
  const roles = useMemo(
    () => [...new Set(agents.map((a) => a.role).filter(Boolean))].sort(),
    [agents],
  );
  const roleByAgent = useMemo(
    () =>
      Object.fromEntries(agents.map((a) => [a.agent_id, a.role || "default"])),
    [agents],
  );
  const labelAgent = (id: string) =>
    roleByAgent[id] ? `${id} · ${roleByAgent[id]}` : id;
  const describeRecipient = (to: string) =>
    to === "all"
      ? "everyone"
      : to.startsWith("thread:")
        ? "thread members"
        : to.replace(/^agent:/, "").replace(/^role:/, "role:");
  const inPerspective = !!viewAs;
  selectedThreadRef.current = selectedThread;
  viewAsRef.current = viewAs;
  const participants = useMemo(() => {
    const map = new Map<number, Set<string>>();
    for (const m of messages) {
      if (!m.from_agent) continue;
      const k = threadKey(m);
      if (!map.has(k)) map.set(k, new Set());
      map.get(k)!.add(m.from_agent);
    }
    return map;
  }, [messages]);
  const relevant = (m: MessageEntry) =>
    !inPerspective || inboxOf(m, viewAs, roleByAgent[viewAs], participants);
  // Two kinds of unread for you: addressed to you (@user, see state/messages)
  // and anything new since this browser's first visit (@anyone).
  const isRead = (m: MessageEntry) =>
    inPerspective
      ? (m.read_by || []).includes(viewAs)
      : isSentHere(m) || readByMe(m.id) || !(isAddressedToMe(m) || countsAsNew(m));
  const isUnreadForMe = (m: MessageEntry) =>
    !inPerspective && !isSentHere(m) && !readByMe(m.id) && isAddressedToMe(m);

  async function refresh() {
    try {
      const [a, m, t] = await Promise.all([
        listAgents().catch(() => []),
        listMessagesPage(200),
        listThreads({ status: "all", limit: 100 }).catch(() => null),
      ]);
      if (!cancelled.current) {
        setAgents(a);
        if (t) setThreadMeta(new Map(t.threads.map((x) => [x.id, x])));
        setMessages((old) => [...new Map([...old, ...m.messages].map((item) => [item.id, item])).values()]);
        setMessageTotal(m.total);
        setError(null);
        setLoaded(true);
        const selected = selectedThreadRef.current;
        if (selected != null) void loadThread(selected);
      }
    } catch (e) {
      if (!cancelled.current) {
        setError(e instanceof Error ? e.message : String(e));
        setLoaded(true);
      }
    }
  }
  async function loadOlderConversations() {
    if (loadingOlder) return;
    setLoadingOlder(true);
    try {
      const page = await listMessagesPage(200, messages.length);
      if (!cancelled.current) {
        setMessages((old) => [...new Map([...old, ...page.messages].map((m) => [m.id, m])).values()]);
        setMessageTotal(page.total);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally { setLoadingOlder(false); }
  }
  async function loadThread(id: number, offset = 0, append = false) {
    const request = ++requestNumber.current;
    setThreadLoading(true);
    setThreadError(null);
    try {
      const page = await getMessagePage(id, 200, offset);
      if (
        !cancelled.current &&
        request === requestNumber.current &&
        selectedThreadRef.current === id
      ) {
        setThreadTotal(page.total);
        setThreadMessages((old) =>
          [
            ...new Map(
              [...old.filter((m) => threadKey(m) === id), ...page.messages].map((m) => [
                m.id,
                m,
              ]),
            ).values(),
          ].sort((a, b) => timeOf(a) - timeOf(b)),
        );
        if (!viewAsRef.current) {
          markMessagesReadByMe(page.messages.map((m) => m.id));
          markThreadReadByMe(id);
        }
        if (!append && replyTargetThreadRef.current !== id) {
          const newestFirst = [...page.messages].sort((a, b) => timeOf(b) - timeOf(a));
          // Follow the conversation's addressing: thread-addressed threads
          // reply to their members; older ones to the last other speaker.
          const threadAddressed = newestFirst.find((m) => m.to.startsWith("thread:"));
          const latestOther = newestFirst.find(recipientOf);
          setReplyTo(
            threadAddressed
              ? threadAddressed.to
              : latestOther ? recipientOf(latestOther)! : page.messages[0]?.to || "all",
          );
          replyTargetThreadRef.current = id;
        }
      }
    } catch (e) {
      if (!cancelled.current && request === requestNumber.current && selectedThreadRef.current === id)
        setThreadError(e instanceof Error ? e.message : String(e));
    } finally {
      if (!cancelled.current && request === requestNumber.current)
        setThreadLoading(false);
    }
  }
  useLiveRefresh(["message_received", "agent_changed", "thread_changed"], refresh, 30_000);
  useEffect(() => {
    cancelled.current = false;
    refresh();
    return () => {
      cancelled.current = true;
    };
  }, []);
  useEffect(() => {
    if (selectedThread != null) {
      setReplyText(replyDrafts.current.get(selectedThread) || "");
      setReplyTldr(replyTldrDrafts.current.get(selectedThread) || "");
      replyTargetThreadRef.current = null;
      setThreadMessages([]);
      setThreadTotal(0);
      loadThread(selectedThread);
      if (!inPerspective) markThreadReadByMe(selectedThread);
    } else setThreadMessages([]);
  }, [selectedThread, inPerspective]);

  const displayed = useMemo(() => {
    const q = query.trim().toLowerCase();
    return messages.filter(relevant).filter((m) => {
      if (
        agentFilter &&
        m.from_agent !== agentFilter &&
        m.to !== `agent:${agentFilter}` &&
        !(m.read_by || []).includes(agentFilter)
      )
        return false;
      if (readFilter === "unread" && isRead(m)) return false;
      if (readFilter === "read" && !isRead(m)) return false;
      if (readFilter === "agent-unread" && (m.read_by || []).length)
        return false;
      return (
        !q ||
        `${m.from_agent} ${m.from_role} ${m.to} ${describeRecipient(m.to)} ${m.tldr || ""} ${m.text} ${(m.references || []).map((ref) => "path" in ref ? ref.path : ref.id).join(" ")}`
          .toLowerCase()
          .includes(q)
      );
    });
  }, [messages, query, agentFilter, readFilter, viewAs, readMap, roleByAgent, myNames]);
  const threads = useMemo(() => {
    const groups = new Map<number, MessageEntry[]>();
    displayed.forEach((m) =>
      groups.set(threadKey(m), [...(groups.get(threadKey(m)) || []), m]),
    );
    return [...groups.entries()]
      .map(([id, items]) => {
        const latest = items.reduce((a, b) => (timeOf(a) > timeOf(b) ? a : b));
        return {
          id,
          items,
          latest,
          unread: items.filter((m) => !isRead(m)).length,
          unreadForMe: items.filter(isUnreadForMe).length,
          forMe: !inPerspective && items.some((m) => !isSentHere(m) && isAddressedToMe(m)),
        };
      })
      .filter(
        (thread) =>
          conversationScope === "all" ||
          threadMeta.get(thread.id)?.created_by === "user" ||
          thread.items.some(
            (m) =>
              m.from_agent === "user" ||
              m.from_agent === "agent:user" ||
              m.to === "user" ||
              m.to === "agent:user",
          ),
      )
      .sort((a, b) =>
        sort === "newest"
          ? timeOf(b.latest) - timeOf(a.latest)
          : timeOf(a.latest) - timeOf(b.latest),
      );
  }, [displayed, sort, readMap, viewAs, conversationScope, threadMeta, myNames]);
  const focusId = focusMessageId.value;
  useLayoutEffect(() => {
    const list = messageListRef.current;
    if (!list || selectedThread == null || !threadMessages.length || threadKey(threadMessages[0]) !== selectedThread) return;
    const first = threadMessages[0].id;
    const last = threadMessages[threadMessages.length - 1].id;
    const previous = scrollSnapshot.current;
    const changedThread = previous?.thread !== selectedThread;
    if (pendingFocus == null && focusId == null) {
      if (!changedThread && previous.first !== first && previous.last === last) {
        // Prepending history should keep the message being read in place.
        list.scrollTop = previous.top + list.scrollHeight - previous.height;
      } else if (changedThread || followNewestRef.current) {
        list.scrollTop = list.scrollHeight;
        followNewestRef.current = true;
      }
    } else {
      // Explicit message links take precedence over following the newest reply.
      followNewestRef.current = false;
    }
    scrollSnapshot.current = { thread: selectedThread, first, last, height: list.scrollHeight, top: list.scrollTop };
  }, [selectedThread, threadMessages, pendingFocus, focusId]);
  useEffect(() => {
    if (focusId == null || !loaded) return;
    const focus = (message: MessageEntry) => {
      if (cancelled.current || focusMessageId.value !== focusId) return;
      focusMessageId.value = null;
      setConversationScope("all");
      setNewThreadOpen(false);
      setSelectedThread(threadKey(message));
      setPendingFocus(message.id);
    };
    const found = messages.find((m) => m.id === focusId);
    if (found) focus(found);
    else
      getMessage(focusId)
        .then(focus)
        .catch((e) => { if (!cancelled.current) setError(String(e)); });
  }, [focusId, loaded, messages]);
  useEffect(() => {
    if (pendingFocus == null || threadLoading || threadError || !threadMessages.length || threadKey(threadMessages[0]) !== selectedThread) return;
    const element = document.getElementById(`thread-message-${pendingFocus}`);
    if (element) {
      element.scrollIntoView({ behavior: "smooth", block: "center" });
      // Flash it so the eye finds it after the scroll (restart if repeated).
      element.classList.remove("is-flash");
      void element.offsetWidth;
      element.classList.add("is-flash");
      window.setTimeout(() => element.classList.remove("is-flash"), 2200);
      setPendingFocus(null);
    } else if (threadMessages.length < threadTotal && selectedThread != null) {
      void loadThread(selectedThread, threadMessages.length, true);
    }
  }, [pendingFocus, threadMessages, threadLoading, threadTotal, threadError]);
  async function submit(kind: "new" | "reply") {
    const text = (kind === "new" ? newText : replyText).trim(),
      to = kind === "new" ? newTo : replyTo,
      tldr = (kind === "new" ? newTldr : replyTldr).trim() || undefined;
    if (!text || sendingRef.current || demo || inPerspective) return;
    if (kind === "reply" && (threadLoading || !threadMessages.length || threadKey(threadMessages[0]) !== selectedThread)) return;
    sendingRef.current = true;
    const sentThread = selectedThread;
    setSending(kind);
    setSendError(null);
    try {
      const parent =
        kind === "reply"
          ? threadMessages[threadMessages.length - 1]
          : undefined;
      const title = kind === "new" ? newTitle.trim() : "";
      const sent = title
        ? (await createThread({
            title,
            text,
            tldr,
            // "All agents" announces a public thread; otherwise the chosen
            // agent / role members join it.
            announce: to === "all",
            members: to.startsWith("agent:")
              ? [to.slice("agent:".length)]
              : to.startsWith("role:")
                ? agents.filter((a) => `role:${a.role}` === to).map((a) => a.agent_id)
                : [],
          })).message
        : await sendMessage(
            to,
            text,
            parent ? { thread_id: selectedThread!, in_reply_to: parent.id, tldr } : { tldr },
          );
      registerDashboardUserMessage(sent);
      if (kind === "new") {
        setNewText("");
        setNewTitle("");
        setNewTldr("");
        setNewThreadOpen(false);
        setSelectedThread(threadKey(sent));
      } else {
        if (sentThread != null) replyDrafts.current.delete(sentThread);
        if (sentThread != null) replyTldrDrafts.current.delete(sentThread);
        if (selectedThreadRef.current === sentThread) {
          followNewestRef.current = true;
          setReplyText("");
          setReplyTldr("");
          setThreadMessages((old) => [...old, sent]);
        }
      }
      await refresh();
    } catch (e) {
      setSendError(e instanceof Error ? e.message : String(e));
    } finally {
      sendingRef.current = false;
      setSending(null);
    }
  }
  const picker = (value: string, set: (v: string) => void, label: string, threadId?: number | null) => (
    <select
      class="messages-select"
      aria-label={label}
      disabled={demo || inPerspective || !!sending}
      value={value}
      onChange={(e) => set((e.target as HTMLSelectElement).value)}
    >
      {threadId != null && <option value={`thread:${threadId}`}>Thread members</option>}
      <option value="all">All agents</option>
      {value !== "all" && value !== `thread:${threadId}` && !agents.some((a) => value === `agent:${a.agent_id}`) && !roles.some((r) => value === `role:${r}`) && <option value={value}>{value}</option>}
      <optgroup label="Agents">
        {agents.map((a) => (
          <option key={a.agent_id} value={`agent:${a.agent_id}`}>
            {labelAgent(a.agent_id)}
          </option>
        ))}
      </optgroup>
      {roles.length > 0 && (
        <optgroup label="Roles">
          {roles.map((r) => (
            <option key={r} value={`role:${r}`}>
              role:{r}
            </option>
          ))}
        </optgroup>
      )}
    </select>
  );
  const selected = threads.find((t) => t.id === selectedThread);

  return (
    <div id="messages-container" class={`message-threads${selectedThread != null || newThreadOpen ? " has-selection" : ""}`}>
      <div class="thread-layout">
        <div class="thread-sidebar">
          <div class="thread-sidebar-toolbar">
            <input
              class="messages-search"
              type="search"
              aria-label="Search conversations"
              placeholder="Search messages…"
              value={query}
              onInput={(e) => setQuery((e.target as HTMLInputElement).value)}
            />
            <button
              class="ui-btn thread-new-button"
              type="button"
              disabled={demo || inPerspective}
              onClick={() => {
                setSendError(null);
                setSelectedThread(null);
                setNewThreadOpen(true);
              }}
            >
              + New
            </button>
            <IconButton onClick={refresh} title="Refresh">↺</IconButton>
          </div>
          <div class="thread-sidebar-subhead">
            <span>{loaded ? `${threads.length} threads` : "Loading…"}</span>
            {!inPerspective && (
              <button
                type="button"
                class="thread-read-all"
                onClick={() => markAllMessagesReadByMe([...messages, ...threadMessages].map((m) => m.id))}
                title="Mark everything read (in this browser)"
              >
                ✓ Read all
              </button>
            )}
            <details class="thread-filters">
              <summary>Filters</summary>
              <div class="thread-filter-menu">
                <label>
                  <span>View as</span>
                  <select class="messages-select" value={viewAs} onChange={(e) => setViewAs((e.target as HTMLSelectElement).value)}>
                    <option value="">you (dashboard)</option>
                    {agents.map((a) => <option key={a.agent_id} value={a.agent_id}>{labelAgent(a.agent_id)}</option>)}
                  </select>
                </label>
                <label title="Messages that mention one of these names notify you (this browser only); so do direct messages and replies to what you sent from here">
                  <span>Your names</span>
                  <input
                    class="messages-select"
                    value={myNames}
                    onChange={(e) => { messagesMyNames.value = (e.target as HTMLInputElement).value; }}
                    placeholder="e.g. David, Dave"
                  />
                </label>
                <label>
                  <span>Participant</span>
                  <select class="messages-select" value={agentFilter} onChange={(e) => setAgentFilter((e.target as HTMLSelectElement).value)}>
                    <option value="">All agents</option>
                    {agents.map((a) => <option key={a.agent_id} value={a.agent_id}>{labelAgent(a.agent_id)}</option>)}
                  </select>
                </label>
                <select class="messages-select" aria-label="Conversation scope" value={conversationScope} onChange={(e) => setConversationScope((e.target as HTMLSelectElement).value as "all" | "mine")}>
                  <option value="all">All threads</option>
                  <option value="mine">My conversations</option>
                </select>
                <select class="messages-select" aria-label="Read status" value={readFilter} onChange={(e) => setReadFilter((e.target as HTMLSelectElement).value as ReadFilter)}>
                  <option value="all">All messages</option>
                  <option value="unread">Unread</option>
                  <option value="read">Read</option>
                  <option value="agent-unread">No agent has read</option>
                </select>
                <select class="messages-select" aria-label="Sort order" value={sort} onChange={(e) => setSort((e.target as HTMLSelectElement).value as SortOrder)}>
                  <option value="newest">Latest activity</option>
                  <option value="oldest">Oldest activity</option>
                </select>
              </div>
            </details>
          </div>
          {inPerspective && <div class="messages-perspective-note">Viewing {labelAgent(viewAs)} read-only</div>}
          {error && <div class="agents-error">{error}</div>}
          <aside class="thread-list" aria-label="Message threads">
          {threads.map((t) => (
            <button
              key={t.id}
              class={`thread-list-row${selectedThread === t.id ? " is-selected" : ""}${t.unreadForMe > 0 ? " has-unread-for-me" : t.forMe ? " has-for-me" : ""}${t.unread > 0 ? " has-unread" : ""}`}
              onClick={() => {
                setSendError(null);
                setNewThreadOpen(false);
                setSelectedThread(t.id);
              }}
            >
              <span class="thread-list-line">
                <Participant id={t.latest.from_agent ?? t.latest.from_role ?? "system"} />
                <span class="thread-list-time">
                  {relativeTime(t.latest.created_at)}
                </span>
              </span>
              {threadMeta.get(t.id) && (
                <span class="thread-list-title">{threadMeta.get(t.id)!.title}</span>
              )}
              <span class="thread-list-preview">
                {stripRouting(t.latest.tldr || t.latest.text)}
              </span>
              <span class="thread-list-meta">
                <span>
                  {t.items.length} msg{t.items.length === 1 ? "" : "s"}
                </span>
                {threadMeta.get(t.id)?.kind === "suggestion" && <Badge tone="running">suggestion</Badge>}
                {threadMeta.get(t.id)?.status === "closed" && <Badge>closed</Badge>}
                {threadMeta.get(t.id)?.visibility === "private" && <Badge>private</Badge>}
                {t.unreadForMe > 0 && (
                  <span class="thread-unread-me" title={`${t.unreadForMe} unread message${t.unreadForMe === 1 ? "" : "s"} for you`}>@{t.unreadForMe}</span>
                )}
                {t.unread - t.unreadForMe > 0 && (
                  <span class="thread-unread-any" title="unread messages in this thread (not addressed to you)">{t.unread - t.unreadForMe} unread</span>
                )}
              </span>
            </button>
          ))}
          {messages.length < messageTotal && <button class="thread-load-more" disabled={loadingOlder} onClick={loadOlderConversations}>{loadingOlder ? "Loading…" : "Load older conversations"}</button>}
          {!threads.length && (
            <EmptyState
              icon="✉"
              title="No threads"
              body="Start a new conversation."
            />
          )}
          </aside>
        </div>
        <section class="thread-detail">
          {newThreadOpen ? (
            <div class="thread-new-panel">
              <div class="thread-detail-head">
                <button class="thread-back" type="button" onClick={() => setNewThreadOpen(false)} aria-label="Back to conversations">←</button>
                <span>New thread</span>
              </div>
              <form class="thread-new-form" onSubmit={(e) => { e.preventDefault(); submit("new"); }}>
                <label><span>To</span>{picker(newTo, setNewTo, "New thread recipient")}</label>
                <label><span>Title <small>optional — makes it a shared thread agents can find and join</small></span><input class="thread-compose-tldr" disabled={demo || inPerspective || !!sending} value={newTitle} maxLength={200} onInput={(e) => setNewTitle((e.target as HTMLInputElement).value)} placeholder="What is this thread about?" /></label>
                <label><span>TL;DR <small>optional</small></span><input class="thread-compose-tldr" disabled={demo || inPerspective || !!sending} value={newTldr} maxLength={500} onInput={(e) => setNewTldr((e.target as HTMLInputElement).value)} placeholder="A short summary" /></label>
                <label class="thread-new-message"><span>Message</span><textarea autoFocus rows={12} disabled={demo || inPerspective || !!sending} value={newText} onInput={(e) => setNewText((e.target as HTMLTextAreaElement).value)} placeholder={demo ? "Messaging is disabled in demo mode" : "Write to agents…"} /></label>
                {sendError && <div class="agents-error">{sendError}</div>}
                <div class="thread-new-actions">
                  <button type="button" class="ui-btn" onClick={() => setNewThreadOpen(false)}>Cancel</button>
                  <button class="ui-btn" disabled={demo || inPerspective || !newText.trim() || !!sending}>{sending === "new" ? "Sending…" : "Send message"}</button>
                </div>
              </form>
            </div>
          ) : selectedThread == null ? (
            <EmptyState
              icon="✉"
              title="Select a thread"
              body="Choose a conversation to read and reply."
            />
          ) : (
            <>
              <div class="thread-detail-head">
                <button class="thread-back" type="button" onClick={() => setSelectedThread(null)} aria-label="Back to conversations">←</button>
                {renameDraft !== null ? (
                  <input
                    class="thread-rename-input"
                    ref={renameInputRef}
                    maxLength={200}
                    value={renameDraft}
                    aria-label="Thread title"
                    onInput={(e) => setRenameDraft((e.target as HTMLInputElement).value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") (e.target as HTMLInputElement).blur();
                      if (e.key === "Escape") { renameCancelled.current = true; setRenameDraft(null); }
                    }}
                    onBlur={async () => {
                      const title = renameDraft?.trim() ?? "";
                      const cancelled = renameCancelled.current;
                      renameCancelled.current = false;
                      setRenameDraft(null);
                      if (cancelled || !title || title === threadMeta.get(selectedThread)?.title) return;
                      try {
                        await updateThread(selectedThread, { title });
                        await refresh();
                      } catch (e) {
                        setSendError(e instanceof Error ? e.message : String(e));
                      }
                    }}
                  />
                ) : (
                  <span
                    class="thread-detail-title"
                    onDblClick={() => {
                      const meta = threadMeta.get(selectedThread);
                      if (meta && !inPerspective && !demo) setRenameDraft(meta.title ?? "");
                    }}
                  >
                    {threadMeta.get(selectedThread)?.title ?? `Thread #${selectedThread}`}
                  </span>
                )}
                {threadMeta.get(selectedThread) && !inPerspective && !demo && renameDraft === null && (
                  <button
                    type="button"
                    class="thread-rename-btn"
                    onClick={() => setRenameDraft(threadMeta.get(selectedThread)!.title ?? "")}
                    title="Rename this thread (or double-click the title)"
                    aria-label="Rename thread"
                  >
                    ✎
                  </button>
                )}
                {selected && (
                  <span>{selected.items.length} recent messages</span>
                )}
                {threadMeta.get(selectedThread) && (
                  <span>{threadMeta.get(selectedThread)!.member_count} members</span>
                )}
                {threadMeta.get(selectedThread) && !inPerspective && !demo && (
                  <IconButton
                    onClick={async () => {
                      const meta = threadMeta.get(selectedThread)!;
                      try {
                        await updateThread(selectedThread, {
                          status: meta.status === "closed" ? "open" : "closed",
                        });
                        await refresh();
                      } catch (e) {
                        setSendError(e instanceof Error ? e.message : String(e));
                      }
                    }}
                    title={threadMeta.get(selectedThread)!.status === "closed" ? "Reopen this thread" : "Close this thread (posts a closing message)"}
                  >
                    {threadMeta.get(selectedThread)!.status === "closed" ? "Reopen" : "Close"}
                  </IconButton>
                )}
                {!inPerspective && (
                  <IconButton
                    onClick={() =>
                      setReadByMe(
                        threadMessages.map((m) => m.id),
                        true,
                      )
                    }
                    title="Mark loaded messages read"
                  >
                    ✓ Read thread
                  </IconButton>
                )}
              </div>
              {(threadMeta.get(selectedThread)?.summary || threadMeta.get(selectedThread)?.conclusion) && (
                // Collapsed by default (summaries get long); the header shows
                // what is inside plus the start of the text.
                <details class="thread-summary" key={`summary-${selectedThread}`}>
                  <summary>
                    <strong>{[threadMeta.get(selectedThread)!.summary && "Summary", threadMeta.get(selectedThread)!.conclusion && "Conclusion"].filter(Boolean).join(" · ")}</strong>
                    <span class="thread-summary-preview">
                      {stripRouting(threadMeta.get(selectedThread)!.conclusion || threadMeta.get(selectedThread)!.summary || "").replace(/\s+/g, " ").slice(0, 160)}
                    </span>
                  </summary>
                  {threadMeta.get(selectedThread)!.summary && <div class="thread-summary-part"><strong>Summary</strong><RichText text={threadMeta.get(selectedThread)!.summary!} /></div>}
                  {threadMeta.get(selectedThread)!.conclusion && <div class="thread-summary-part"><strong>Conclusion</strong><RichText text={threadMeta.get(selectedThread)!.conclusion!} /></div>}
                </details>
              )}
              {threadError && <div class="agents-error">{threadError}</div>}
              {sendError && <div class="agents-error">{sendError}</div>}
              {threadTotal > threadMessages.length && (
                <button
                  class="thread-load-more"
                  disabled={threadLoading}
                  onClick={() =>
                    loadThread(selectedThread, threadMessages.length, true)
                  }
                >
                  {threadLoading
                    ? "Loading…"
                    : `Load ${Math.min(200, threadTotal - threadMessages.length)} older messages`}
                </button>
              )}
              <ol
                class="thread-message-list"
                ref={messageListRef}
                onScroll={() => {
                  const list = messageListRef.current;
                  if (!list) return;
                  followNewestRef.current = list.scrollHeight - list.scrollTop - list.clientHeight < 48;
                  if (scrollSnapshot.current) {
                    scrollSnapshot.current.top = list.scrollTop;
                    scrollSnapshot.current.height = list.scrollHeight;
                  }
                }}
                onLoadCapture={() => {
                  const list = messageListRef.current;
                  if (list && followNewestRef.current) list.scrollTop = list.scrollHeight;
                }}
              >
                {threadMessages.map((m) => (
                  <li
                    id={`thread-message-${m.id}`}
                    key={m.id}
                    class={`thread-message${isRead(m) ? " is-read" : " is-unread"}${!inPerspective && !isSentHere(m) && isAddressedToMe(m) ? " is-for-me" : ""}${m.from_agent === "user" || m.from_agent === "agent:user" ? " is-from-user" : ""}`}
                  >
                    <div class="thread-message-meta">
                      <span
                        class="msg-dot"
                        style={{ background: agentColor(m.from_agent) }}
                      />
                      <Participant id={m.from_agent ?? m.from_role ?? "system"} />
                      <span>→</span>
                      <Participant id={describeRecipient(m.to)} />
                      <time title={m.created_at}>
                        {relativeTime(m.created_at)}
                      </time>
                      <button
                        type="button"
                        class="thread-message-id"
                        title={`Message id ${m.id} — click to copy`}
                        onClick={() => { void navigator.clipboard?.writeText(String(m.id)).catch(() => {}); }}
                      >
                        #{m.id}
                      </button>
                      {!inPerspective && (
                        <button
                          class={`msg-readdot${isRead(m) ? " is-read" : ""}`}
                          onClick={() => setReadByMe([m.id], !isRead(m))}
                          title="Toggle your local read state"
                        />
                      )}
                    </div>
                    <div class="thread-message-body">
                      {m.tldr ? (
                        <>
                          <div class="thread-message-tldr"><RichText text={stripRouting(m.tldr)} /></div>
                          <details class="thread-message-full">
                            <summary>Full message</summary>
                            <RichText text={stripRouting(m.text)} />
                          </details>
                        </>
                      ) : <RichText text={stripRouting(m.text)} />}
                      <MessageReferences message={m} />
                    </div>
                  </li>
                ))}
              </ol>
              {threadLoading && !threadMessages.length && (
                <div class="thread-loading">Loading thread…</div>
              )}
              <form
                class="thread-compose thread-compose-reply"
                onSubmit={(e) => {
                  e.preventDefault();
                  submit("reply");
                }}
              >
                <span class="thread-compose-label">Reply</span>
                {picker(replyTo, setReplyTo, "Reply recipient", selectedThread)}
                <input
                  class="thread-compose-tldr"
                  disabled={demo || inPerspective || !!sending}
                  value={replyTldr}
                  maxLength={500}
                  onInput={(e) => {
                    const text = (e.target as HTMLInputElement).value;
                    if (selectedThread != null) replyTldrDrafts.current.set(selectedThread, text);
                    setReplyTldr(text);
                  }}
                  placeholder="TL;DR (optional)"
                />
                <textarea
                  disabled={demo || inPerspective || !!sending}
                  value={replyText}
                  onInput={(e) => {
                    const text = (e.target as HTMLTextAreaElement).value;
                    if (selectedThread != null) replyDrafts.current.set(selectedThread, text);
                    setReplyText(text);
                  }}
                  placeholder={
                    demo
                      ? "Messaging is disabled in demo mode"
                      : "Reply in this thread…"
                  }
                />
                <button
                  class="ui-btn"
                  disabled={
                    demo ||
                    inPerspective ||
                    threadLoading ||
                    !threadMessages.length ||
                    !replyText.trim() ||
                    !!sending
                  }
                >
                  {sending === "reply" ? "Sending…" : "Reply"}
                </button>
              </form>
            </>
          )}
        </section>
      </div>
    </div>
  );
}
