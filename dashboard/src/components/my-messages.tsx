/**
 * "My messages": everything you sent, across all threads, newest first.
 * Clicking one opens its thread scrolled to the message (and flashes it).
 * Yours = sent as your user:<name> identity, plus anonymous "user" messages
 * sent from this browser.
 */
import { useEffect, useState } from "preact/hooks";
import { getThreadTitle, searchMessages, type MessageSearchHit } from "../state/api";
import { isSentHere } from "../state/messages";
import type { MessageEntry } from "../lib/types";
import { relativeTime } from "../lib/format";

const PAGE = 100;

export function MyMessagesPanel({ humanId, titleOf, onOpen, onBack }: {
  humanId: string | null;
  titleOf: (threadId: number) => string | undefined;
  onOpen: (messageId: number) => void;
  onBack: () => void;
}) {
  const [hits, setHits] = useState<MessageSearchHit[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [titles, setTitles] = useState<Record<number, string | null>>({});
  const [filter, setFilter] = useState("");

  async function load(from: number) {
    setLoading(true); setError(null);
    try {
      const named = humanId
        ? await searchMessages({ from_agent: humanId, limit: PAGE, offset: from })
        : { results: [], total: 0 };
      // Anonymous "user" messages count as yours when sent from this browser.
      const anon = from === 0
        ? (await searchMessages({ from_agent: "user", limit: PAGE })).results
            .filter((m) => isSentHere({ id: m.id } as MessageEntry))
        : [];
      setHits((cur) => {
        const byId = new Map((from === 0 ? [] : cur).map((m) => [m.id, m]));
        for (const m of [...named.results, ...anon]) byId.set(m.id, m);
        return [...byId.values()].sort((a, b) => b.id - a.id);
      });
      setTotal(named.total + anon.length);
      setOffset(from + named.results.length);
    } catch (e) {
      setError(String((e as Error).message || e));
    } finally {
      setLoading(false);
    }
  }
  useEffect(() => { void load(0); }, [humanId]);

  // Titles for threads the list view hasn't loaded.
  useEffect(() => {
    const missing = [...new Set(hits.map((m) => m.thread_id ?? m.id))]
      .filter((t) => titleOf(t) === undefined && !(t in titles));
    for (const t of missing) {
      void getThreadTitle(t).then((title) => setTitles((cur) => ({ ...cur, [t]: title })));
    }
  }, [hits]);

  const terms = filter.toLowerCase().split(/\s+/).filter(Boolean);
  const shown = terms.length
    ? hits.filter((m) => {
        const hay = `${m.snippet} ${m.tldr ?? ""} ${titleOf(m.thread_id ?? m.id) ?? titles[m.thread_id ?? m.id] ?? ""}`.toLowerCase();
        return terms.every((t) => hay.includes(t));
      })
    : hits;
  const hasMore = !!humanId && offset < total - hits.filter((m) => m.from_agent === "user").length;

  return (
    <div class="my-messages">
      <div class="thread-detail-head">
        <button class="thread-back" type="button" onClick={onBack} aria-label="Back to conversations">←</button>
        <span class="thread-detail-title">My messages</span>
        <span>{total} sent{humanId ? ` as ${humanId}` : ""}</span>
        <input class="my-messages-filter" type="search" placeholder="filter…" value={filter}
          onInput={(e) => setFilter((e.target as HTMLInputElement).value)} />
      </div>
      {!humanId && (
        <div class="messages-perspective-note">Set your name ("You: …") to include messages you sent from other browsers.</div>
      )}
      {error && <div class="agents-error">{error}</div>}
      <ol class="my-messages-list">
        {shown.map((m) => {
          const tid = m.thread_id ?? m.id;
          const title = titleOf(tid) ?? titles[tid];
          return (
            <li key={m.id}>
              <button type="button" class="my-messages-row" onClick={() => onOpen(m.id)}
                title={`Open thread #${tid} at message #${m.id}`}>
                <span class="my-messages-line">
                  <span class="my-messages-thread">{title || `thread #${tid}`}</span>
                  <span class="my-messages-meta">#{m.id} · {relativeTime(m.created_at)}{m.to ? ` · → ${m.to}` : ""}</span>
                </span>
                <span class="my-messages-snippet">{m.tldr || m.snippet}</span>
              </button>
            </li>
          );
        })}
      </ol>
      {!loading && !shown.length && <div class="my-messages-empty">{hits.length ? "No match." : "No messages yet."}</div>}
      {hasMore && (
        <button class="thread-load-more" disabled={loading} onClick={() => load(offset)}>
          {loading ? "Loading…" : "Load older"}
        </button>
      )}
      {loading && !hits.length && <div class="my-messages-empty">Loading…</div>}
    </div>
  );
}
