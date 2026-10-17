import { useCallback, useEffect, useRef, useState } from "preact/hooks";
import { getDashboardInbox } from "../state/api";
import { inboxView, inboxUnreadCount, savedConversations, savedConversationFocus } from "../state/inbox";
import { dashboardSentMessageIds, messagesUnreadCount, markMessagesReadByMe } from "../state/messages";
import { focusMessageId } from "../state/agent-activity";
import { messagesReadByMe, messagesMyNames, myName } from "../state/settings";
import { useLiveRefresh } from "../lib/hooks";
import { relativeTime } from "../lib/format";
import { humanDisplayName, isHuman } from "../lib/identity";
import { IdeaLink } from "./idea-link";
import { allIdeas } from "../state/signals";
import type { AgentEntry, DashboardInboxPage, InboxReply, InboxThread } from "../lib/types";

// Small inline icons for the view tabs (same style as the chart controls).
const SV = { fill: "none", stroke: "currentColor", "stroke-width": 1.6, "stroke-linecap": "round", "stroke-linejoin": "round" } as const;
const VIEW_ICONS: Record<string, any> = {
  for_you: <><circle {...SV} cx="8" cy="8" r="2.6" /><path {...SV} d="M10.6 8v1.2a1.8 1.8 0 0 0 3.4.8A6.2 6.2 0 1 0 11.5 13" /></>,
  replies: <path {...SV} d="M6.5 4 2.5 8l4 4M2.5 8h7a4 4 0 0 1 4 4v1" />,
  mine: <><circle {...SV} cx="8" cy="5.5" r="2.7" /><path {...SV} d="M2.8 14c.8-3 2.8-4.3 5.2-4.3S12.4 11 13.2 14" /></>,
  all: <><path {...SV} d="M2.5 4.5h11v7h-6l-3 2.5v-2.5h-2Z" /></>,
};
function ViewIcon({ id }: { id: string }) {
  return <svg class="inbox-view-icon" width="14" height="14" viewBox="0 0 16 16" aria-hidden="true">{VIEW_ICONS[id]}</svg>;
}

export function MessageInbox({ selectedId, agents, onSelect, onNew, onMetadata }: {
  selectedId: number | null;
  agents: AgentEntry[];
  onSelect: (thread: InboxThread) => void;
  onNew: () => void;
  onMetadata: (threads: InboxThread[]) => void;
}) {
  const [rows, setRows] = useState<InboxThread[]>([]);
  const [replies, setReplies] = useState<InboxReply[]>([]);
  const [unreadOnly, setUnreadOnly] = useState(true);
  // Replies marked read this session stay listed so they can be re-marked.
  const touched = useRef(new Set<number>());
  const [counts, setCounts] = useState<DashboardInboxPage["counts"]>({for_you: 0, mine: 0, all: 0, saved: 0, replies: 0});
  const [next, setNext] = useState<number | null>(null);
  const [total, setTotal] = useState(0);
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState<"all" | "open" | "closed">("all");
  const [sender, setSender] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [editingName, setEditingName] = useState(false);
  const [nameDraft, setNameDraft] = useState("");
  const list = useRef<HTMLDivElement>(null);
  const sentinel = useRef<HTMLDivElement>(null);
  const request = useRef(0);
  const alive = useRef(true);
  const busy = useRef(false);
  const nextRef = useRef<number | null>(null);
  const snapshot = useRef<Parameters<typeof getDashboardInbox>[0] | null>(null);
  const rowCount = useRef(0);
  const initialReadEffect = useRef(true);
  rowCount.current = rows.length;
  const view = inboxView.value;
  const read = messagesReadByMe.value;
  const aliases = messagesMyNames.value;
  const name = myName.value;
  const saved = savedConversations.value;
  const savedFocus = savedConversationFocus.value;
  const badgeCount = inboxUnreadCount.value ?? messagesUnreadCount.value;
  snapshot.current = {
    view, q: query, status, sender, aliases: aliases.split(/[,;]/).map(s => s.trim()).filter(Boolean),
    sent_ids: dashboardSentMessageIds(), read_ids: Object.keys(read).filter(id => read[id]).map(Number),
    saved_threads: saved, saved_focus: savedFocus,
  };
  const load = useCallback(async (append = false, replace = false) => {
    if (!snapshot.current || (append && (busy.current || nextRef.current == null))) return;
    const token = ++request.current;
    busy.current = true; setLoading(true); setError(null);
    const body = { ...snapshot.current, sent_ids: dashboardSentMessageIds(), ...(append ? { before_id: nextRef.current! } : {}) };
    try {
      const page = await getDashboardInbox(body);
      if (!alive.current || token !== request.current) return;
      // A refreshed first page updates loaded rows without deleting older
      // pages. Their query/identity is unchanged; filtering resets separately.
      setRows(previous => {
        const state = snapshot.current!;
        const readSet = new Set(state.read_ids);
        const keep = previous.filter(row => {
          if (state.view === "saved") return state.saved_threads.includes(row.id);
          if (state.view === "for_you") return (row.unread_ids ?? [row.focus.id]).some(id => !readSet.has(id));
          return true;
        });
        const merged = new Map((replace ? [] : keep).map(row => [row.id, row]));
        for (const row of page.threads) merged.set(row.id, row);
        return [...merged.values()].sort((a,b) => b.cursor_id - a.cursor_id);
      });
      if (snapshot.current?.view === "replies") {
        setReplies(previous => {
          const merged = new Map((replace || !append ? [] : previous).map(r => [r.id, r]));
          if (!append && !replace) for (const r of previous) merged.set(r.id, r);
          for (const r of page.replies ?? []) merged.set(r.id, r);
          return [...merged.values()].sort((a, b) => b.id - a.id);
        });
      }
      onMetadata(page.threads);
      setCounts(page.counts); inboxUnreadCount.value = page.counts.for_you;
      setTotal(page.total);
      if (append || replace || rowCount.current <= 30) {
        setNext(page.next_before_id); nextRef.current = page.next_before_id;
      }
    } catch (e) {
      if (alive.current && token === request.current) setError(e instanceof Error ? e.message : String(e));
    } finally {
      if (alive.current && token === request.current) { busy.current = false; setLoading(false); }
    }
  }, []);
  useEffect(() => { alive.current = true; return () => { alive.current = false; request.current++; }; }, []);
  // Query and identity changes reset the cursor. Read-state updates refresh
  // counts without scrolling the list back to the top.
  useEffect(() => {
    request.current++; busy.current = false; nextRef.current = null;
    setRows([]); setReplies([]); setNext(null);
    if (list.current) list.current.scrollTop = 0;
    const timer = setTimeout(() => { void load(false, true); }, query ? 250 : 0);
    return () => clearTimeout(timer);
  }, [view, query, status, sender, name, aliases]);
  useEffect(() => { if (initialReadEffect.current) { initialReadEffect.current = false; return; } const timer = setTimeout(() => { void load(); }, 150); return () => clearTimeout(timer); }, [read, saved, savedFocus]);
  useEffect(() => {
    const refresh = () => { void load(); };
    const showPersonal = () => {
      setQuery(""); setStatus("all"); setSender("");
      if (list.current) list.current.scrollTop = 0;
      setTimeout(() => { if (alive.current) void load(false, true); }, 0);
    };
    window.addEventListener("the-lab:refresh-inbox", refresh);
    window.addEventListener("the-lab:show-inbox", showPersonal);
    return () => { window.removeEventListener("the-lab:refresh-inbox", refresh); window.removeEventListener("the-lab:show-inbox", showPersonal); };
  }, []);
  useLiveRefresh(["message_received", "thread_changed"], () => load(), 30_000);
  useEffect(() => {
    const node = sentinel.current;
    if (!node || loading || error || next == null) return;
    const observer = new IntersectionObserver(entries => {
      if (entries.some(entry => entry.isIntersecting)) void load(true);
    }, { root: list.current, rootMargin: "180px" });
    observer.observe(node);
    return () => observer.disconnect();
  }, [next, loading, error]);
  const labels: Record<string, string> = {for_you: "For you", replies: "Replies", mine: "Mine", all: "All", saved: "Saved"};
  const titles: Record<string, string> = {
    for_you: "For you: conversations with unread messages for you",
    replies: "Replies: each reply to your messages (and messages to you), to open, mark read or keep unread",
    mine: "My conversations: ones you wrote in or joined",
    all: "All conversations",
  };
  const readMap = messagesReadByMe.value;
  const isReplyRead = (r: InboxReply) => readMap[String(r.id)] ?? r.read;
  const shownReplies = replies.filter(r => !unreadOnly || !isReplyRead(r) || touched.current.has(r.id));
  return <aside class="thread-sidebar message-inbox" aria-label="Conversations">
    <div class="inbox-heading"><strong>Messages</strong><button class="ui-btn" onClick={onNew}>+ New conversation</button></div>
    <div class="inbox-views" role="tablist" aria-label="Conversation views">{(["for_you", "replies", "mine", "all"] as const).map(id => <button role="tab" aria-selected={view === id} title={titles[id]} class={view === id ? "is-active" : ""} onClick={() => { inboxView.value = id; }}><ViewIcon id={id} /><span class="inbox-view-label">{labels[id]}</span><span class="inbox-view-count">{id === "for_you" || id === "replies" ? badgeCount : counts[id]}</span></button>)}</div>
    <div class="inbox-search"><input type="search" aria-label="Search all conversation history" placeholder="Search all conversation history…" value={query} onInput={e => setQuery(e.currentTarget.value)} /></div>
    <div class="inbox-shortcuts">
      <button class={`ui-btn${view === "saved" ? " is-active" : ""}`} aria-pressed={view === "saved"} onClick={() => { inboxView.value = view === "saved" ? "for_you" : "saved"; }}>Saved · {saved.length}</button>
      <details class="inbox-filters"><summary>Filters{status !== "all" || sender ? " · active" : ""}</summary><div><label>Status<select aria-label="Conversation status" value={status} onChange={e => setStatus(e.currentTarget.value as typeof status)}><option value="all">Open and closed</option><option value="open">Open</option><option value="closed">Closed</option></select></label><label>Participant<select aria-label="Conversation participant" value={sender} onChange={e => setSender(e.currentTarget.value)}><option value="">Everyone</option>{agents.map(a => <option value={a.agent_id}>{a.role || a.agent_id}</option>)}</select></label><button class="ui-btn" onClick={() => { setStatus("all"); setSender(""); }}>Reset filters</button></div></details>
      <button class="ui-btn inbox-identity" onClick={() => { setNameDraft(name); setEditingName(v => !v); }}>You: {name || "set your name"}</button>
    </div>
    {editingName && <form class="inbox-name-form" onSubmit={e => { e.preventDefault(); myName.value = nameDraft.trim(); setEditingName(false); }}><label>Your name<input autoFocus value={nameDraft} onInput={e => setNameDraft(e.currentTarget.value)} /></label><button class="ui-btn">Save</button></form>}
    {!name && <p class="inbox-identity-note">Set your name to include conversations from your other browsers.</p>}
    <div class="inbox-description">{view === "replies" ? <span class="inbox-replies-head">{badgeCount} unread {badgeCount === 1 ? "reply" : "replies"} for you<label class="inbox-unread-only"><input type="checkbox" checked={unreadOnly} onChange={e => { setUnreadOnly(e.currentTarget.checked); touched.current.clear(); }} /> unread only</label></span> : view === "for_you" ? `${badgeCount} unread ${badgeCount === 1 ? "reply or message" : "replies and messages"} for you` : `${labels[view]} · ${total}`}</div>
    <div ref={list} class="inbox-list" role="tabpanel" aria-label={labels[view]}>
      {error && <div class="inbox-error" role="alert">{error}<button class="ui-btn" onClick={() => load()}>Retry</button></div>}
      {view === "replies" && shownReplies.map(r => {
        const read = isReplyRead(r);
        const who = r.from_agent ? (isHuman(r.from_agent) ? humanDisplayName(r.from_agent) : (agents.find(a => a.agent_id === r.from_agent)?.role ? `${r.from_agent} · ${agents.find(a => a.agent_id === r.from_agent)!.role}` : r.from_agent)) : (r.from_role ?? "system");
        const toggle = (e: Event) => { e.stopPropagation(); touched.current.add(r.id); markMessagesReadByMe([r.id], !read); };
        const open = () => { touched.current.add(r.id); focusMessageId.value = r.id; };
        return <div key={`r${r.id}`} role="button" tabIndex={0} class={`inbox-reply${read ? " is-read" : ""}`} onClick={open}
          onKeyDown={e => { if (e.target === e.currentTarget && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); open(); } }}>
          <span class="inbox-reply-top">
            <span class={`inbox-reply-dot${read ? "" : " is-unread"}`} aria-hidden="true" />
            <strong>{who}</strong>
            <span class="inbox-reply-thread" title={r.thread_title || `Conversation #${r.thread_id}`}>in {r.thread_title || `#${r.thread_id}`}</span>
            <time dateTime={r.created_at} title={r.created_at}>{relativeTime(r.created_at)}</time>
          </span>
          {r.question && <span class="inbox-reply-quote" title={r.question.preview}>↳ you: {r.question.preview}</span>}
          <span class="inbox-reply-text">{r.preview}</span>
          <span class="inbox-reply-actions">
            <button type="button" class="ui-btn" onClick={e => { e.stopPropagation(); open(); }} title="Open the conversation at this message">Open</button>
            <button type="button" class="ui-btn" onClick={toggle} title={read ? "Mark unread again (keep it for later)" : "Mark read"}>{read ? "◌ Keep unread" : "✓ Mark read"}</button>
          </span>
        </div>;
      })}
      {view !== "replies" && rows.map(row => {
        const participants = row.participants ?? [];
        const participantLabel = (id: string) => isHuman(id) ? humanDisplayName(id) : (agents.find(agent => agent.agent_id.replace(/^agent:/, "") === id.replace(/^agent:/, ""))?.role || id.replace(/^agent:/, ""));
        const ideaIds = (row.idea_ids ?? []).filter(id => allIdeas.value[id]);
        return <div role="button" tabIndex={0} onKeyDown={e => { if (e.target === e.currentTarget && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); onSelect(row); } }} key={row.id} class={`inbox-row${selectedId === row.id ? " is-selected" : ""}${row.unread_for_me ? " has-reply" : ""}`} onClick={() => onSelect(row)}>
          <span class="inbox-row-top"><strong title={row.title || `Conversation #${row.id}`}>{row.title || `Conversation #${row.id}`}</strong><span class="inbox-row-badges">
            {row.unread_for_me > 0 && <span class="inbox-unread-pill" aria-label={`${row.unread_for_me} unread messages for you`}>{row.unread_for_me} unread</span>}
            {row.status === "closed" && <span class="inbox-closed-pill">Closed</span>}
          </span></span>
          <span class="inbox-row-meta">
            <time dateTime={row.last_activity} title={row.last_activity}>{relativeTime(row.last_activity || row.latest.created_at)}</time>
            <span>{row.message_count} {row.message_count === 1 ? "message" : "messages"}</span>
            <span class="inbox-my-count">{row.my_message_count ?? 0} from you</span>
          </span>
          {ideaIds.length > 0 && <span class="inbox-row-ideas" aria-label="Ideas mentioned">Ideas {ideaIds.slice(0, 4).map(id => <IdeaLink key={id} id={id} />)}{ideaIds.length > 5 && <span title={`${ideaIds.length - 5} more ideas mentioned`}>…</span>}{ideaIds.length > 4 && <IdeaLink id={ideaIds[ideaIds.length - 1]} />}</span>}
          {participants.length > 0 && <span class="inbox-row-participants" title={participants.map(id => `${isHuman(id) ? "User" : "Agent"}: ${participantLabel(id)} (${id})`).join(" · ")}>
            <span>{participants.length} {participants.length === 1 ? "participant" : "participants"} · </span>
            {participants.map((id, index) => <span class={isHuman(id) ? "is-human" : "is-agent"}>{index > 0 ? ", " : ""}{participantLabel(id)}</span>)}
          </span>}
        </div>;
      })}
      {!loading && !error && view === "replies" && !shownReplies.length && <div class="inbox-empty"><strong>{query ? "No matching replies" : "You're caught up"}</strong><p>Replies to your messages and messages addressed to you appear here, one by one.</p></div>}
      {!loading && !error && view !== "replies" && !rows.length && <div class="inbox-empty"><strong>{query || status !== "all" || sender ? "No matching conversations" : view === "for_you" ? "You're caught up" : view === "saved" ? "No saved conversations" : "No conversations yet"}</strong><p>{query || status !== "all" || sender ? "Clear search or reset filters to see more conversations." : view === "for_you" ? "New replies and messages addressed to you will appear here. Reading clears unread status; Save keeps a conversation for later." : view === "saved" ? "Use “Keep for later” in a conversation to save it here." : "Try another view or start a conversation."}</p></div>}
      <div ref={sentinel} class="inbox-sentinel">{loading ? "Loading…" : next != null ? <button class="ui-btn" onClick={() => load(true)}>{view === "replies" ? "Load older replies" : "Load more conversations"}</button> : (view === "replies" ? replies.length : rows.length) ? (view === "replies" ? "End of replies" : "End of conversations") : ""}</div>
    </div>
  </aside>;
}
