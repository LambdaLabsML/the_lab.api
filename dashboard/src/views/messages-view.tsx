import { threadLocation } from "../state/navigation";
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "preact/hooks";
import {
  createThread,
  getMessage,
  getThreadConversation,
  getThreadContext,
  type ThreadGap,
  listAgents,
  listMessagesPage,
  sendMessage,
  updateThread,
} from "../state/api";
import { useLiveRefresh, useCopyToClipboard } from "../lib/hooks";
import { routePath } from "../lib/dashboard-url";
import type { AgentEntry, MessageEntry, ThreadMeta, InboxThread } from "../lib/types";
import { Badge, EmptyState, IconButton } from "../components/ui";
import { agentColor, registerAgents, agentInitials } from "../lib/colors";
import { isHuman, humanDisplayName, humanIdForName } from "../lib/identity";
import { messagesReadByMe, myName, serverDemoMode } from "../state/settings";
import { NameGate, requireName } from "../components/name-gate";
import {
  dashboardSentMessageIds,
  markMessagesReadByMe,
  registerDashboardUserMessage,
  isAddressedToMe,
  isMine,
  isSentHere,
  countsAsNew,
} from "../state/messages";
import { focusMessageId } from "../state/agent-activity";
import { MessageInbox } from "../components/message-inbox";
import { savedConversations, toggleSavedConversation, threadListWidth, conversationAudience } from "../state/inbox";
import { relativeTime } from "../lib/format";
import { RichText, stripRouting } from "../components/rich-text";
import { IdeaLink } from "../components/idea-link";
import { ExpLink } from "../components/exp-link";

const threadKey = (m: MessageEntry) => m.thread_id ?? m.id;
/** Grow a textarea with its content, between its CSS min and max height. */
function autoGrow(el: HTMLTextAreaElement | null) {
  if (!el) return;
  el.style.height = "auto";
  el.style.height = `${el.scrollHeight + 2}px`;
}
const timeOf = (m: MessageEntry) => Date.parse(m.created_at) || m.id;
const recipientOf = (m: MessageEntry) =>
  m.from_agent && isHuman(m.from_agent)
    ? (m.from_agent === "agent:user" ? "user" : m.from_agent)
    : m.from_agent
      ? `agent:${m.from_agent}`
      : m.from_role && m.from_role !== "user"
        ? `role:${m.from_role}`
        : null;

function Participant({ id }: { id: string }) {
  const color = agentColor(id);
  const label = isHuman(id) ? humanDisplayName(id) : id;
  return (
    <span class="thread-participant" style={{ "--participant-color": color } as Record<string, string>}>
      <span class="thread-participant-avatar" aria-hidden="true">{agentInitials(id)}</span>
      <span>{label}</span>
    </span>
  );
}

/** Copy a minimal, shareable link without the sender's inbox filters. */
function MessagePermalink({ message }: { message: MessageEntry }) {
  const { copied, error, copy } = useCopyToClipboard();
  const label = copied ? "Link copied" : error ? "Copy failed — click to retry" : `Copy link to message #${message.id}`;
  return <button
    type="button"
    class="thread-message-id"
    title={label}
    aria-label={label}
    onClick={() => {
      const path = routePath({ section: "messages", thread: threadKey(message), message: message.id });
      void copy(new URL(path, window.location.origin).href);
    }}
  >
    #{message.id}<span role="status">{copied ? " · Copied" : error ? " · Copy failed" : ""}</span>
  </button>;
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
  const [pendingFocus, setPendingFocus] = useState<number | null>(null);
  const [newThreadOpen, setNewThreadOpen] = useState(false);
  // Phones: the reply form stays a one-line "Reply…" until focused.
  const [composeOpen, setComposeOpen] = useState(false);
  // Writing needs a name (name-gate.tsx asks for one first).
  const hasName = !!(myName.value || "").trim();
  const replyBoxRef = useRef<HTMLTextAreaElement>(null);
  // Message to put in the URL after a jump.
  const urlMessage = threadLocation.value.message;
  const setUrlMessage = (message: number | null) => { threadLocation.value = { ...threadLocation.value, message }; };
  const inPerspective = false;
  const selectedThread = threadLocation.value.thread;
  const setSelectedThread = (thread: number | null) => { threadLocation.value = { thread, message: thread === threadLocation.value.thread ? threadLocation.value.message : null }; };
  const [threadMessages, setThreadMessages] = useState<MessageEntry[]>([]),
    [threadTotal, setThreadTotal] = useState(0),
    [threadLoading, setThreadLoading] = useState(false),
    [threadError, setThreadError] = useState<string | null>(null);
  const [contextGaps, setContextGaps] = useState<ThreadGap[]>([]);
  const [contextBusy, setContextBusy] = useState<Record<string, boolean>>({});
  const [contextErrors, setContextErrors] = useState<Record<string, string>>({});
  const contextGeneration = useRef(0);
  const contextScrollTop = useRef<number | null>(null);
  const [threadMeta, setThreadMeta] = useState<Map<number, ThreadMeta>>(new Map());
  // Inline rename of the open thread: the draft title while editing, else null.
  const [renameDraft, setRenameDraft] = useState<string | null>(null);
  // Escape unmounts the input, which can still fire blur with the old draft.
  const renameCancelled = useRef(false);
  // Phones hide the global top bars while a thread is open (more room to read).
  useEffect(() => {
    document.body.classList.toggle("thread-open", selectedThread != null);
    return () => document.body.classList.remove("thread-open");
  }, [selectedThread]);
  useEffect(() => { setRenameDraft(null); }, [selectedThread]);
  useEffect(() => {
    if (threadLocation.value.message != null) focusMessageId.value = threadLocation.value.message;
  }, [selectedThread, urlMessage]);
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
  // Drafts switch with the thread: size the reply box to its draft.
  useEffect(() => { autoGrow(replyBoxRef.current); }, [replyText, selectedThread]);
  const cancelled = useRef(false),
    requestNumber = useRef(0),
    selectedThreadRef = useRef<number | null>(null),
    replyTargetThreadRef = useRef<number | null>(null);
  const sendingRef = useRef(false);
  const replyDrafts = useRef(new Map<number, string>());
  const replyTldrDrafts = useRef(new Map<number, string>());
  const messageListRef = useRef<HTMLOListElement>(null);
  const followNewestRef = useRef(true);
  const scrollSnapshot = useRef<{ thread: number; first: number; last: number; height: number; top: number } | null>(null);
  const readMap = messagesReadByMe.value;
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
  selectedThreadRef.current = selectedThread;
  const anchorRef = useRef<number | null>(null);
  const lastFocusedMessage = useRef<number | null>(null);
  const beforeRef = useRef<number | null>(null);
  const afterRef = useRef<number | null>(null);
  const [hasBefore, setHasBefore] = useState(false);
  const [hasAfter, setHasAfter] = useState(false);
  const [personalUnreadIds, setPersonalUnreadIds] = useState<Set<number>>(new Set());
  const addressedToMe = (m: MessageEntry) => personalUnreadIds.has(m.id) || isAddressedToMe(m);
  const isRead = (m: MessageEntry) => isSentHere(m) || readByMe(m.id) || !(addressedToMe(m) || countsAsNew(m));

  async function refresh() {
    try {
      const [a, m] = await Promise.all([
        listAgents().catch(() => []),
        listMessagesPage(200),
      ]);
      if (!cancelled.current) {
        setAgents(a);
        setMessages((old) => [...new Map([...old, ...m.messages].map((item) => [item.id, item])).values()]);
        setError(null);
        setLoaded(true);
        const selected = selectedThreadRef.current;
        if (selected != null && afterRef.current == null) void loadThread(selected);
      }
    } catch (e) {
      if (!cancelled.current) {
        setError(e instanceof Error ? e.message : String(e));
        setLoaded(true);
      }
    }
  }
  async function loadThread(id: number, _offset = 0, append = false, direction: "older" | "newer" = "older") {
    const request = ++requestNumber.current;
    setThreadLoading(true);
    setThreadError(null);
    try {
      const cursor = append ? direction === "older" ? { before_id: beforeRef.current ?? undefined } : { after_id: afterRef.current ?? undefined }
        : anchorRef.current != null ? { around_id: anchorRef.current } : {};
      const page = await getThreadConversation(id, cursor, conversationAudience.value, dashboardSentMessageIds());
      if (
        !cancelled.current &&
        request === requestNumber.current &&
        selectedThreadRef.current === id
      ) {
        const pageIds = new Set(page.messages.map(message => message.id));
        setContextGaps(previous => [...previous.filter(gap => !pageIds.has(gap.after_id ?? -1) && !pageIds.has(gap.before_id ?? -1)), ...(page.gaps ?? [])]);
        setThreadTotal(page.total);
        setThreadMeta(old => new Map(old).set(id, page));
        if (!append || direction === "older") { beforeRef.current = page.next_before_id ?? null; setHasBefore(beforeRef.current != null); }
        if (!append || direction === "newer") { afterRef.current = page.next_after_id ?? null; setHasAfter(afterRef.current != null); }
        anchorRef.current = null;
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
  const gapKey = (gap: ThreadGap) => `${gap.after_id ?? "start"}:${gap.before_id ?? "end"}`;
  function gapContents(gap: ThreadGap) {
    const revealed = threadMessages.filter(message => (gap.after_id == null || message.id > gap.after_id) && (gap.before_id == null || message.id < gap.before_id));
    return { revealed, remaining: Math.max(0, gap.count - revealed.length) };
  }
  async function revealContext(gap: ThreadGap) {
    const id = selectedThreadRef.current;
    if (id == null) return;
    const key = gapKey(gap), generation = contextGeneration.current;
    if (contextBusy[key]) return;
    const { revealed } = gapContents(gap);
    const after = revealed.length ? Math.max(...revealed.map(message => message.id)) : gap.after_id;
    setContextBusy(previous => ({ ...previous, [key]: true }));
    setContextErrors(previous => ({ ...previous, [key]: "" }));
    try {
      const page = await getThreadContext(id, { after_id: after, before_id: gap.before_id });
      if (cancelled.current || generation !== contextGeneration.current || selectedThreadRef.current !== id) return;
      followNewestRef.current = false;
      contextScrollTop.current = messageListRef.current?.scrollTop ?? 0;
      setContextGaps(previous => previous.map(item => gapKey(item) === key ? { ...item, count: revealed.length + page.total } : item));
      setThreadMessages(previous => [...new Map([...previous, ...page.messages].map(message => [message.id, message])).values()].sort((a, b) => timeOf(a) - timeOf(b)));
    } catch (error) {
      if (generation === contextGeneration.current) setContextErrors(previous => ({ ...previous, [key]: error instanceof Error ? error.message : String(error) }));
    } finally {
      if (generation === contextGeneration.current) setContextBusy(previous => ({ ...previous, [key]: false }));
    }
  }
  function renderGap(gap: ThreadGap) {
    const { remaining } = gapContents(gap), key = gapKey(gap);
    if (!remaining || conversationAudience.value === "all") return null;
    if (gap.after_id != null && !threadMessages.some(message => message.id === gap.after_id)) return null;
    return <li key={`gap:${key}`} class="thread-context-gap" data-context-gap={key}>
      <button class="ui-btn" disabled={contextBusy[key]} onClick={() => revealContext(gap)}>{contextBusy[key] ? "Loading…" : `Show ${Math.min(3, remaining)} more`}</button>
      <span>{remaining} {remaining === 1 ? "message hidden" : "messages hidden"}</span>
      {contextErrors[key] && <span class="thread-context-error" role="alert">{contextErrors[key]}</span>}
    </li>;
  }
  useLiveRefresh(["message_received", "agent_changed", "thread_changed", "message_claim_changed"], refresh, 30_000);
  useEffect(() => {
    cancelled.current = false;
    refresh();
    return () => {
      cancelled.current = true;
    };
  }, []);
  useEffect(() => {
    contextGeneration.current++; setContextGaps([]); setContextBusy({}); setContextErrors({}); contextScrollTop.current = null;
    if (selectedThread != null) {
      setReplyText(replyDrafts.current.get(selectedThread) || "");
      setReplyTldr(replyTldrDrafts.current.get(selectedThread) || "");
      replyTargetThreadRef.current = null;
      setThreadMessages([]);
      setThreadTotal(0);
      loadThread(selectedThread);
    } else setThreadMessages([]);
  }, [selectedThread, inPerspective, conversationAudience.value]);

  const focusId = focusMessageId.value;
  useLayoutEffect(() => {
    const list = messageListRef.current;
    if (!list || selectedThread == null || !threadMessages.length || threadKey(threadMessages[0]) !== selectedThread) return;
    const first = threadMessages[0].id;
    const last = threadMessages[threadMessages.length - 1].id;
    const previous = scrollSnapshot.current;
    const changedThread = previous?.thread !== selectedThread;
    if (contextScrollTop.current != null) {
      list.scrollTop = contextScrollTop.current; contextScrollTop.current = null;
    } else if (pendingFocus == null && focusId == null) {
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
      if (threadLocation.value.message !== message.id) conversationAudience.value = "all";
      setNewThreadOpen(false);
      anchorRef.current = message.id;
      lastFocusedMessage.current = message.id;
      setPendingFocus(message.id);
      const keepInUrl = () => setUrlMessage(message.id);
      window.setTimeout(keepInUrl, 0);  // after the thread switch resets it
      if (selectedThreadRef.current === threadKey(message)) {
        setThreadMessages([]); void loadThread(threadKey(message));
      } else setSelectedThread(threadKey(message));
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
      const fullMessage = element.querySelector<HTMLDetailsElement>("details.thread-message-full");
      if (fullMessage) fullMessage.open = true;
      element.scrollIntoView({ behavior: "smooth", block: "center" });
      // Flash it so the eye finds it after the scroll (restart if repeated).
      element.classList.remove("is-flash");
      void element.offsetWidth;
      element.classList.add("is-flash");
      window.setTimeout(() => element.classList.remove("is-flash"), 2200);
      setPendingFocus(null);
    }
  }, [pendingFocus, threadMessages, threadLoading, threadTotal, threadError]);
  // ◀ / ▶ through your own messages in the open thread, relative to what is
  // in view; at the top of what's loaded, ◀ fetches older messages first.
  const [mineCursor, setMineCursor] = useState<number | null>(null);
  const olderJump = useRef<number | null>(null); // first loaded id before an older-page fetch
  useEffect(() => { setMineCursor(null); olderJump.current = null; }, [selectedThread]);
  const mineIds = threadMessages.filter((m) => isMine(m)).map((m) => m.id);
  const hasOlder = hasBefore;
  function focusMine(id: number) {
    setMineCursor(id);
    setPendingFocus(id);
  }
  function jumpMine(dir: -1 | 1) {
    const list = messageListRef.current;
    if (!list || selectedThread == null) return;
    const box = list.getBoundingClientRect();
    const center = box.top + box.height / 2;
    const rect = (id: number) => document.getElementById(`thread-message-${id}`)?.getBoundingClientRect();
    // Step from the last one jumped to while it's still on screen (a short
    // thread can't scroll it to the middle); after scrolling away, start from
    // the middle of the view.
    const cur = mineCursor != null ? mineIds.indexOf(mineCursor) : -1;
    const curRect = cur >= 0 ? rect(mineIds[cur]) : undefined;
    const target = curRect && curRect.bottom > box.top && curRect.top < box.bottom
      ? mineIds[cur + dir]
      : dir > 0
        ? mineIds.find((id) => { const r = rect(id); return !!r && r.top > center + 4; })
        : [...mineIds].reverse().find((id) => { const r = rect(id); return !!r && r.bottom < center - 4; });
    if (target != null) focusMine(target);
    else if (dir < 0 && hasOlder && !threadLoading && threadMessages.length) {
      olderJump.current = threadMessages[0].id;
      void loadThread(selectedThread, threadMessages.length, true);
    }
  }
  useEffect(() => {
    // After an older page arrived for ◀: go to the newest of mine in it, or
    // keep paging back while there is more and none of mine yet.
    const before = olderJump.current;
    if (before == null || threadLoading || selectedThread == null) return;
    if (!threadMessages.length || threadMessages[0].id === before) return;
    const older = threadMessages.filter((m) => m.id < before && isMine(m));
    if (older.length) { olderJump.current = null; focusMine(older[older.length - 1].id); }
    else if (hasBefore) {
      olderJump.current = threadMessages[0].id;
      void loadThread(selectedThread, threadMessages.length, true);
    } else olderJump.current = null;
  }, [threadMessages, threadLoading]);
  useEffect(() => {
    if (selectedThread == null) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "[" && e.key !== "]") return;
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      const t = e.target as HTMLElement | null;
      if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
      e.preventDefault();
      jumpMine(e.key === "[" ? -1 : 1);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });
  async function submit(kind: "new" | "reply") {
    const text = (kind === "new" ? newText : replyText).trim(),
      to = kind === "new" ? newTo : replyTo,
      tldr = (kind === "new" ? newTldr : replyTldr).trim() || undefined;
    if (!text || sendingRef.current || demo || inPerspective) return;
    if (!requireName(() => { void submit(kind); })) return;
    if (kind === "reply" && (threadLoading || !threadMessages.length || threadKey(threadMessages[0]) !== selectedThread)) return;
    sendingRef.current = true;
    const sentThread = selectedThread;
    setSending(kind);
    setSendError(null);
    try {
      const parent =
        kind === "reply"
          ? threadMessages.find(message => message.id === lastFocusedMessage.current) ?? threadMessages[threadMessages.length - 1]
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
        lastFocusedMessage.current = sent.id;
        anchorRef.current = sent.id;
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
          if (afterRef.current != null) setThreadMessages([sent]);
          else setThreadMessages((old) => [...old, sent]);
          afterRef.current = null; anchorRef.current = null; setHasAfter(false);
        }
      }
      await refresh();
      window.dispatchEvent(new Event("the-lab:refresh-inbox"));
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
      {value !== "all" && value !== `thread:${threadId}` && !agents.some((a) => value === `agent:${a.agent_id}`) && !roles.some((r) => value === `role:${r}`) && (
        <option value={value}>{isHuman(value) ? humanDisplayName(value) : value}</option>
      )}
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
  function openInboxThread(thread: InboxThread) {
    setThreadMeta(old => new Map(old).set(thread.id, thread));
    setPersonalUnreadIds(new Set(thread.unread_ids));
    setNewThreadOpen(false); setSendError(null);
    anchorRef.current = thread.focus.id;
    lastFocusedMessage.current = thread.focus.id;
    setPendingFocus(thread.focus.id);
    if (selectedThreadRef.current === thread.id) { setThreadMessages([]); void loadThread(thread.id); }
    else setSelectedThread(thread.id);
    setUrlMessage(thread.focus.id);
  }
  const updateMetadata = useRef((threads: InboxThread[]) => {
    setThreadMeta(old => { const next = new Map(old); for (const thread of threads) next.set(thread.id, thread); return next; });
  }).current;
  useEffect(() => {
    const showInbox = () => { setSelectedThread(null); setNewThreadOpen(false); };
    window.addEventListener("the-lab:show-inbox", showInbox);
    return () => window.removeEventListener("the-lab:show-inbox", showInbox);
  }, []);
  // Read acknowledgement is about what was visible, never the whole fetched
  // page. A short dwell avoids clearing replies while merely scrolling past.
  useEffect(() => {
    const list = messageListRef.current;
    if (!list || selectedThread == null) return;
    const timers = new Map<Element, ReturnType<typeof setTimeout>>();
    const observer = new IntersectionObserver(entries => {
      for (const entry of entries) {
        const old = timers.get(entry.target);
        if (old) clearTimeout(old);
        timers.delete(entry.target);
        if (!entry.isIntersecting || entry.intersectionRect.height < Math.min(48, entry.boundingClientRect.height)) continue;
        timers.set(entry.target, setTimeout(() => {
          timers.delete(entry.target);
          if (document.hidden || entry.target.closest("[hidden]") || !entry.target.getClientRects().length) return;
          const box = entry.target.getBoundingClientRect(), bounds = list.getBoundingClientRect();
          if (box.bottom <= bounds.top || box.top >= bounds.bottom) return;
          const id = Number((entry.target as HTMLElement).dataset.messageId);
          if (id && !messagesReadByMe.peek()[String(id)]) markMessagesReadByMe([id]);
        }, 400));
      }
    }, { root: list, threshold: [0, 0.05, 0.5, 1] });
    list.querySelectorAll("[data-message-id]").forEach(node => observer.observe(node));
    return () => { observer.disconnect(); for (const timer of timers.values()) clearTimeout(timer); };
  }, [threadMessages, selectedThread, newThreadOpen]);

  return (
    <div id="messages-container" class={`message-threads${selectedThread != null || newThreadOpen ? " has-selection" : ""}`}>
      <NameGate />
      <div class="thread-layout" style={{ "--thread-list-width": `${threadListWidth.value}%` }}>
        <MessageInbox selectedId={selectedThread} agents={agents} onSelect={openInboxThread}
          onMetadata={updateMetadata} onNew={() => {
            const open = () => { setSendError(null); setSelectedThread(null); setNewThreadOpen(true); };
            if (requireName(open)) open();
          }} />
        <div class="thread-list-resize" role="separator" aria-label="Resize conversation list" aria-orientation="vertical" aria-valuemin={20} aria-valuemax={65} aria-valuenow={Math.round(threadListWidth.value)} tabIndex={0}
          onKeyDown={e => {
            if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
              e.preventDefault();
              threadListWidth.value = Math.max(20, Math.min(65, threadListWidth.value + (e.key === "ArrowRight" ? 2 : -2)));
            }
          }}
          onPointerDown={e => {
            if (e.button !== 0) return;
            e.preventDefault();
            e.currentTarget.setPointerCapture(e.pointerId);
          }}
          onPointerMove={e => {
            if (!e.currentTarget.hasPointerCapture(e.pointerId)) return;
            const bounds = e.currentTarget.parentElement!.getBoundingClientRect();
            threadListWidth.value = Math.max(20, Math.min(65, (e.clientX - bounds.left) / bounds.width * 100));
          }}
          onPointerUp={e => { if (e.currentTarget.hasPointerCapture(e.pointerId)) e.currentTarget.releasePointerCapture(e.pointerId); }} />
        <section class="thread-detail">
          {newThreadOpen ? (
            <div class="thread-new-panel">
              <div class="thread-detail-head">
                <button class="thread-back" type="button" onClick={() => setNewThreadOpen(false)} aria-label="Back to conversations">←</button>
                <span>New thread</span>
              </div>
              <form class="thread-new-form compose-card" onSubmit={(e) => { e.preventDefault(); submit("new"); }}>
                <div class="compose-meta">
                  <label class="compose-field" title="Recipient"><span>To</span>{picker(newTo, setNewTo, "New thread recipient")}</label>
                  <input class="thread-compose-tldr compose-title" disabled={demo || inPerspective || !!sending} readOnly={!hasName} value={newTitle} maxLength={200} onInput={(e) => setNewTitle((e.target as HTMLInputElement).value)} placeholder="Title (optional — a shared thread agents can find and join)" />
                </div>
                <input class="thread-compose-tldr compose-tldr" disabled={demo || inPerspective || !!sending} readOnly={!hasName} value={newTldr} maxLength={500} onInput={(e) => setNewTldr((e.target as HTMLInputElement).value)} placeholder="TL;DR (optional)" />
                <textarea class="compose-text compose-text--tall" autoFocus={hasName} rows={8} disabled={demo || inPerspective || !!sending} readOnly={!hasName}
                  onFocus={(e) => { if (!requireName(() => (e.target as HTMLTextAreaElement).focus())) (e.target as HTMLTextAreaElement).blur(); }}
                  onKeyDown={(e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); void submit("new"); } }}
                  value={newText} onInput={(e) => setNewText((e.target as HTMLTextAreaElement).value)}
                  placeholder={demo ? "Messaging is disabled in demo mode" : hasName ? "Write to agents…" : "Choose your name to write…"} />
                {sendError && <div class="agents-error">{sendError}</div>}
                <div class="compose-foot">
                  <span class="compose-hint">⌘/Ctrl + Enter to send</span>
                  <button type="button" class="ui-btn" onClick={() => setNewThreadOpen(false)}>Cancel</button>
                  <button class="ui-btn compose-send" disabled={demo || inPerspective || !newText.trim() || !!sending}>{sending === "new" ? "Sending…" : "Send"}</button>
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
                <button class="thread-back" type="button"
                  onClick={() => setSelectedThread(null)}
                  aria-label="Back to conversations" title="Back to conversations">←</button>
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

                {/* icon-only on phones (see messages.scss) */}
                <button type="button" title={savedConversations.value.includes(selectedThread) ? "Saved — click to remove" : "Keep for later"} class={`ui-btn thread-save${savedConversations.value.includes(selectedThread) ? " is-saved" : ""}`} aria-pressed={savedConversations.value.includes(selectedThread)} onClick={() => toggleSavedConversation(selectedThread, lastFocusedMessage.current ?? threadMessages[threadMessages.length - 1]?.id)}><span class="thread-save-icon" aria-hidden="true">{savedConversations.value.includes(selectedThread) ? "★" : "☆"}</span><span class="thread-save-text">{savedConversations.value.includes(selectedThread) ? "Saved · Remove" : "Keep for later"}</span></button>
                <details class="thread-options"><summary title="Options"><span class="thread-options-icon" aria-hidden="true">⋯</span><span class="thread-options-text">Options</span></summary><div>
                {!demo && <button class="ui-btn" onClick={() => setRenameDraft(threadMeta.get(selectedThread)?.title ?? "")}>Rename conversation</button>}
                {(mineIds.length > 0 || hasOlder) && !inPerspective && (
                  <span class="thread-mine-nav" title="Jump between your own messages in this thread (keys [ and ])">
                    <button type="button" onClick={() => jumpMine(-1)} aria-label="Previous message of mine"
                      disabled={threadLoading && olderJump.current != null}>◀</button>
                    <span>
                      {mineCursor != null && mineIds.includes(mineCursor)
                        ? `${mineIds.indexOf(mineCursor) + 1}/${mineIds.length}${hasOlder ? "+" : ""}`
                        : `${mineIds.length}${hasOlder ? "+" : ""}`} mine
                    </span>
                    <button type="button" onClick={() => jumpMine(1)} aria-label="Next message of mine"
                      disabled={mineIds.length === 0}>▶</button>
                  </span>
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
                    Mark loaded messages read
                  </IconButton>
                )}
                </div></details>
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
              <div class="thread-audience" role="group" aria-label="Messages shown">{([["all", "All"], ["me", "Me + replies"], ["users", "Users + replies"]] as const).map(([mode, label]) => <button class={`ui-btn${conversationAudience.value === mode ? " is-active" : ""}`} aria-pressed={conversationAudience.value === mode} onClick={() => {
                anchorRef.current = null; lastFocusedMessage.current = null; setPendingFocus(null);
                conversationAudience.value = mode;
              }}>{label}</button>)}</div>
              {threadError && <div class="agents-error">{threadError}</div>}
              {sendError && <div class="agents-error">{sendError}</div>}
              {hasBefore && (
                <button
                  class="thread-load-more"
                  disabled={threadLoading}
                  onClick={() =>
                    loadThread(selectedThread, threadMessages.length, true)
                  }
                >
                  {threadLoading
                    ? "Loading…"
                    : "Load earlier messages"}
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
                {!threadLoading && !threadMessages.length && <li class="thread-audience-empty">No messages in this view. Try All to see the full conversation.</li>}
                {threadMessages.map((m) => [
                  ...contextGaps.filter(gap => gap.before_id === m.id).map(renderGap),
                  <li
                    id={`thread-message-${m.id}`}
                    data-message-id={m.id}
                    key={m.id}
                    class={`thread-message${isRead(m) ? " is-read" : " is-unread"}${!inPerspective && !isSentHere(m) && addressedToMe(m) ? " is-for-me" : ""}${isMine(m) ? " is-from-user" : isHuman(m.from_agent) ? " is-from-human" : ""}`}
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
                      {m.claim && <span class="thread-message-claim" title={m.claim.status === "completed" ? "Answer completed" : `Answer claimed until ${m.claim.expires_at ? new Date(m.claim.expires_at).toLocaleString() : "released"}`}>
                        {m.claim.status === "completed" ? "Answered" : "Claimed"} by {m.claim.owner}
                        {m.claim.result_message_id != null && <button class="ui-btn" title="Go to answer" onClick={() => { focusMessageId.value = m.claim!.result_message_id!; }}>↗</button>}
                      </span>}
                      <MessagePermalink message={m} />
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
                  </li>,
                ])}
                {contextGaps.filter(gap => gap.before_id == null).map(renderGap)}
              </ol>
              {hasAfter && <button class="thread-load-more" disabled={threadLoading} onClick={() => loadThread(selectedThread, 0, true, "newer")}>{threadLoading ? "Loading…" : "Load newer messages"}</button>}
              {threadLoading && !threadMessages.length && (
                <div class="thread-loading">Loading thread…</div>
              )}
              <form
                class={`thread-compose thread-compose-reply compose-card${!composeOpen && !replyText.trim() && !replyTldr.trim() ? " is-collapsed" : ""}`}
                onFocusCapture={() => setComposeOpen(true)}
                onBlurCapture={(e) => {
                  // Collapse again when focus leaves the (empty) form.
                  const form = e.currentTarget as HTMLFormElement;
                  window.setTimeout(() => { if (!form.contains(document.activeElement)) setComposeOpen(false); }, 120);
                }}
                onSubmit={(e) => {
                  e.preventDefault();
                  submit("reply");
                }}
              >
                <div class="compose-meta">
                  <label class="compose-field" title="Recipient"><span>To</span>{picker(replyTo, setReplyTo, "Reply recipient", selectedThread)}</label>
                  <input
                    class="thread-compose-tldr compose-tldr"
                    disabled={demo || inPerspective || !!sending}
                    readOnly={!hasName}
                    value={replyTldr}
                    maxLength={500}
                    onInput={(e) => {
                      const text = (e.target as HTMLInputElement).value;
                      if (selectedThread != null) replyTldrDrafts.current.set(selectedThread, text);
                      setReplyTldr(text);
                    }}
                    placeholder="TL;DR (optional)"
                  />
                </div>
                <textarea
                  ref={replyBoxRef}
                  class="compose-text"
                  rows={2}
                  disabled={demo || inPerspective || !!sending}
                  readOnly={!hasName}
                  onFocus={(e) => { if (!requireName(() => replyBoxRef.current?.focus())) (e.target as HTMLTextAreaElement).blur(); }}
                  onKeyDown={(e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); void submit("reply"); } }}
                  value={replyText}
                  onInput={(e) => {
                    const el = e.target as HTMLTextAreaElement;
                    if (selectedThread != null) replyDrafts.current.set(selectedThread, el.value);
                    setReplyText(el.value);
                    autoGrow(el);
                  }}
                  placeholder={
                    demo
                      ? "Messaging is disabled in demo mode"
                      : hasName ? "Reply in this thread…" : "Choose your name to write…"
                  }
                />
                <div class="compose-foot">
                  <span class="compose-hint">⌘/Ctrl + Enter to send</span>
                  <button
                    class="ui-btn compose-send"
                    disabled={
                      demo ||
                      inPerspective ||
                      threadLoading ||
                      !threadMessages.length ||
                      !replyText.trim() ||
                      !!sending
                    }
                  >
                    {sending === "reply" ? "Sending…" : "Send"}
                  </button>
                </div>
              </form>
            </>
          )}
        </section>
      </div>
    </div>
  );
}
