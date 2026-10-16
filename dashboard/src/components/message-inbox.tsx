import { useCallback, useEffect, useRef, useState } from "preact/hooks";
import { getDashboardInbox } from "../state/api";
import { inboxView, inboxUnreadCount, savedConversations, savedConversationFocus } from "../state/inbox";
import { dashboardSentMessageIds, messagesUnreadCount } from "../state/messages";
import { messagesReadByMe, messagesMyNames, myName } from "../state/settings";
import { useLiveRefresh } from "../lib/hooks";
import { relativeTime } from "../lib/format";
import { humanDisplayName, isHuman } from "../lib/identity";
import { IdeaLink } from "./idea-link";
import { allIdeas } from "../state/signals";
import type { AgentEntry, DashboardInboxPage, InboxThread } from "../lib/types";

export function MessageInbox({ selectedId, agents, onSelect, onNew, onMetadata }: {
  selectedId: number | null;
  agents: AgentEntry[];
  onSelect: (thread: InboxThread) => void;
  onNew: () => void;
  onMetadata: (threads: InboxThread[]) => void;
}) {
  const [rows, setRows] = useState<InboxThread[]>([]);
  const [counts, setCounts] = useState<DashboardInboxPage["counts"]>({for_you: 0, mine: 0, all: 0, saved: 0});
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
    setRows([]); setNext(null);
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
  const labels = {for_you: "For you", mine: "My conversations", all: "All conversations", saved: "Saved"};
  return <aside class="thread-sidebar message-inbox" aria-label="Conversations">
    <div class="inbox-heading"><strong>Messages</strong><button class="ui-btn" onClick={onNew}>+ New conversation</button></div>
    <div class="inbox-views" role="tablist" aria-label="Conversation views">{(["for_you", "mine", "all"] as const).map(id => <button role="tab" aria-selected={view === id} class={view === id ? "is-active" : ""} onClick={() => { inboxView.value = id; }}><span>{labels[id]}</span><span class="inbox-view-count">{id === "for_you" ? badgeCount : counts[id]}</span></button>)}</div>
    <div class="inbox-search"><input type="search" aria-label="Search all conversation history" placeholder="Search all conversation history…" value={query} onInput={e => setQuery(e.currentTarget.value)} /></div>
    <div class="inbox-shortcuts">
      <button class={`ui-btn${view === "saved" ? " is-active" : ""}`} aria-pressed={view === "saved"} onClick={() => { inboxView.value = view === "saved" ? "for_you" : "saved"; }}>Saved · {saved.length}</button>
      <details class="inbox-filters"><summary>Filters{status !== "all" || sender ? " · active" : ""}</summary><div><label>Status<select aria-label="Conversation status" value={status} onChange={e => setStatus(e.currentTarget.value as typeof status)}><option value="all">Open and closed</option><option value="open">Open</option><option value="closed">Closed</option></select></label><label>Participant<select aria-label="Conversation participant" value={sender} onChange={e => setSender(e.currentTarget.value)}><option value="">Everyone</option>{agents.map(a => <option value={a.agent_id}>{a.role || a.agent_id}</option>)}</select></label><button class="ui-btn" onClick={() => { setStatus("all"); setSender(""); }}>Reset filters</button></div></details>
      <button class="ui-btn inbox-identity" onClick={() => { setNameDraft(name); setEditingName(v => !v); }}>You: {name || "set your name"}</button>
    </div>
    {editingName && <form class="inbox-name-form" onSubmit={e => { e.preventDefault(); myName.value = nameDraft.trim(); setEditingName(false); }}><label>Your name<input autoFocus value={nameDraft} onInput={e => setNameDraft(e.currentTarget.value)} /></label><button class="ui-btn">Save</button></form>}
    {!name && <p class="inbox-identity-note">Set your name to include conversations from your other browsers.</p>}
    <div class="inbox-description">{view === "for_you" ? `${badgeCount} unread ${badgeCount === 1 ? "reply or message" : "replies and messages"} for you` : `${labels[view]} · ${total}`}</div>
    <div ref={list} class="inbox-list" role="tabpanel" aria-label={labels[view]}>
      {error && <div class="inbox-error" role="alert">{error}<button class="ui-btn" onClick={() => load()}>Retry</button></div>}
      {rows.map(row => {
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
      {!loading && !error && !rows.length && <div class="inbox-empty"><strong>{query || status !== "all" || sender ? "No matching conversations" : view === "for_you" ? "You're caught up" : view === "saved" ? "No saved conversations" : "No conversations yet"}</strong><p>{query || status !== "all" || sender ? "Clear search or reset filters to see more conversations." : view === "for_you" ? "New replies and messages addressed to you will appear here. Reading clears unread status; Save keeps a conversation for later." : view === "saved" ? "Use “Keep for later” in a conversation to save it here." : "Try another view or start a conversation."}</p></div>}
      <div ref={sentinel} class="inbox-sentinel">{loading ? "Loading conversations…" : next != null ? <button class="ui-btn" onClick={() => load(true)}>Load more conversations</button> : rows.length ? "End of conversations" : ""}</div>
    </div>
  </aside>;
}
