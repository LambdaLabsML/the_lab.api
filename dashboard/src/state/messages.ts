// Dashboard-user message notifications.
//
// The API deliberately has no "human inbox" identity: the dashboard is an
// operator view.  This small client-side store therefore keeps the human's
// read state separate from agents' `read_by` acknowledgements.
//
// We inspect only the newest 200 messages.  On a browser's first snapshot we
// baseline broadcasts at the newest id, so opening an old, chatty lab does not
// produce a giant badge.  Direct messages to the dashboard user are still
// shown as unread.  Future websocket events reconcile that bounded window.

import { effect, signal } from "@preact/signals";
import { listMessagesPage } from "./api";
import { messagesReadByMe } from "./settings";
import { subscribeWsEvents } from "./ws";
import type { MessageEntry } from "../lib/types";

const STORAGE_KEY = "the-lab:messagesUnreadBaseline";
const THREADS_KEY = "the-lab:messagesUserThreads";
const PENDING_KEY = "the-lab:messagesUnreadPending";
const HIGH_WATER_KEY = "the-lab:messagesUnreadHighWater";
const BACKFILL_KEY = "the-lab:messagesUnreadBackfill";
const PAGE_LIMIT = 200;
const MAX_RECONCILE_PAGES = 4;

interface Baseline { broadcastsThroughId: number }
interface Backfill { targetId: number; offset: number; newestId: number }

function load<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(key);
    return raw == null ? fallback : JSON.parse(raw) as T;
  } catch {
    return fallback;
  }
}

function save(key: string, value: unknown): void {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* unavailable storage */ }
}

let baseline: Baseline | null = load<Baseline | null>(STORAGE_KEY, null);
let highWaterId = load<number>(HIGH_WATER_KEY, 0);
let backfill = load<Backfill | null>(BACKFILL_KEY, null);
const userThreads = new Set<number>(load<number[]>(THREADS_KEY, []));
// Keep qualifying ids after they leave the newest-200 API window.  The read
// map remains the source of truth, so toggling a message back to unread works.
const pendingUnreadIds = new Set<number>(load<number[]>(PENDING_KEY, []));
let messages: MessageEntry[] = [];

/** Number of unread incoming messages relevant to the dashboard user. */
export const messagesUnreadCount = signal(0);

/** Newest message id seen; bounds which bare numbers in text can be message refs. */
export const latestMessageId = signal(0);

function isDashboardUserSender(m: MessageEntry): boolean {
  // Do not treat null as human: legacy/system messages may be anonymous.
  return m.from_agent === "user" || m.from_agent === "agent:user";
}

function isDirectToDashboardUser(m: MessageEntry): boolean {
  return m.to === "user" || m.to === "agent:user";
}

function threadOf(m: MessageEntry): number {
  return m.thread_id ?? m.id;
}

/**
 * Record a dashboard-authored message (or a direct message to the dashboard)
 * as participation in its conversation.  Exported for composers which have
 * the POST response immediately; ingestion also discovers it on refresh.
 */
export function registerDashboardUserMessage(m: MessageEntry): void {
  if (!isDashboardUserSender(m) && !isDirectToDashboardUser(m)) return;
  const id = threadOf(m);
  if (!userThreads.has(id)) {
    userThreads.add(id);
    save(THREADS_KEY, [...userThreads]);
  }
}

/** Use the same persisted read map as the full Messages view. */
export function markMessagesReadByMe(ids: number[], read = true): void {
  const next = { ...messagesReadByMe.value };
  for (const id of ids) {
    if (read) next[String(id)] = true;
    else delete next[String(id)];
  }
  messagesReadByMe.value = next;
}

/** Mark every loaded message in a conversation read (safe no-op if paged out). */
export function markThreadReadByMe(threadId: number): void {
  markMessagesReadByMe(messages.filter((m) => threadOf(m) === threadId).map((m) => m.id));
}

function recompute(): void {
  const read = messagesReadByMe.value;
  messagesUnreadCount.value = [...pendingUnreadIds].reduce(
    (count, id) => count + (read[String(id)] ? 0 : 1),
    0,
  );
}

function ingest(next: MessageEntry[]): void {
  messages = next;
  const newest = next.reduce((max, m) => Math.max(max, m.id), 0);
  if (newest > latestMessageId.value) latestMessageId.value = newest;
  for (const message of next) registerDashboardUserMessage(message);
  if (baseline == null) {
    baseline = { broadcastsThroughId: next.reduce((max, m) => Math.max(max, m.id), 0) };
    save(STORAGE_KEY, baseline);
  }
  const broadcastFloor = baseline.broadcastsThroughId;
  let pendingChanged = false;
  for (const m of next) {
    if (isDashboardUserSender(m)) continue;
    const direct = isDirectToDashboardUser(m);
    const recentBroadcast = m.to === "all" && m.id > broadcastFloor;
    // Only replies or thread posts in a conversation in which the human was
    // explicitly a sender/recipient qualify.  This prevents agent-to-agent
    // chatter from leaking into the global badge.
    const replyToMyThread =
      (m.in_reply_to != null || m.to.startsWith("thread:")) && userThreads.has(threadOf(m));
    if ((direct || recentBroadcast || replyToMyThread) && !pendingUnreadIds.has(m.id)) {
      pendingUnreadIds.add(m.id);
      pendingChanged = true;
    }
  }
  if (pendingChanged) save(PENDING_KEY, [...pendingUnreadIds]);
  recompute();
}

// Reading the existing signal makes view actions update the rail immediately.
effect(recompute);

let stop: (() => void) | null = null;
let refreshTimer: number | null = null;
let reconcileTimer: number | null = null;
let backfillTimer: number | null = null;
let refreshing = false;

export async function refreshMessageNotifications(): Promise<void> {
  if (document.visibilityState === "hidden") {
    return;
  }
  if (refreshing) return;
  refreshing = true;
  try {
    const first = await listMessagesPage(PAGE_LIMIT);
    const firstSnapshot = baseline == null;
    ingest(first.messages);

    // A burst larger than the newest page can push messages out before this
    // refresh sees them.  Walk back only to the last known newest id, four
    // pages per turn; if necessary, continue in a later task so one socket
    // event never turns into an unbounded archive fetch.
    const previousHighWater = highWaterId;
    const newestId = first.messages.reduce((max, m) => Math.max(max, m.id), 0);
    if (!backfill && firstSnapshot && first.total > first.messages.length) {
      // Direct messages from before the newest page are still meaningful on a
      // first visit.  Broadcasts remain suppressed by the baseline above.
      backfill = { targetId: 0, offset: PAGE_LIMIT, newestId };
      save(BACKFILL_KEY, backfill);
    } else if (!backfill && previousHighWater > 0 && first.messages.length && first.messages[first.messages.length - 1].id > previousHighWater) {
      backfill = { targetId: previousHighWater, offset: PAGE_LIMIT, newestId };
      // Persist the recovery cursor before making further requests: a reload
      // must never advance the high-water mark past an unseen gap.
      save(BACKFILL_KEY, backfill);
    }
    if (backfill) {
      // Offset pagination shifts under a new burst.  Restart this bounded
      // recovery from page two against the new snapshot rather than trusting
      // an arbitrary overlap, which could otherwise skip a large gap.
      if (backfill.newestId !== newestId) {
        backfill.offset = PAGE_LIMIT;
        backfill.newestId = newestId;
        save(BACKFILL_KEY, backfill);
      }
      let pages = 0;
      let reached = false;
      while (pages < MAX_RECONCILE_PAGES && !reached) {
        const page = await listMessagesPage(PAGE_LIMIT, backfill.offset);
        if (!page.messages.length) { reached = true; break; }
        ingest(page.messages);
        reached = page.messages.some((m) => m.id <= backfill!.targetId);
        backfill.offset += page.messages.length;
        pages += 1;
        if (page.messages.length < PAGE_LIMIT) reached = true;
      }
      if (reached) {
        backfill = null;
        save(BACKFILL_KEY, null);
        if (newestId > highWaterId) {
          highWaterId = newestId;
          save(HIGH_WATER_KEY, highWaterId);
        }
      } else {
        save(BACKFILL_KEY, backfill);
        if (backfillTimer == null) {
          backfillTimer = window.setTimeout(() => {
            backfillTimer = null;
            void refreshMessageNotifications();
          }, 500);
        }
      }
    } else if (newestId > highWaterId) {
      highWaterId = newestId;
      save(HIGH_WATER_KEY, highWaterId);
    }
  } catch {
    // A badge must not turn into a false zero during a transient API outage.
  } finally {
    refreshing = false;
  }
}

/** Start one bounded, visibility-aware global notification lifecycle. */
export function startMessageNotifications(): () => void {
  if (stop) return stop;

  const schedule = () => {
    if (document.visibilityState === "hidden") return;
    if (refreshTimer != null) return;
    refreshTimer = window.setTimeout(() => {
      refreshTimer = null;
      void refreshMessageNotifications();
    }, 250);
  };
  const unsubscribe = subscribeWsEvents((event) => {
    if (event.type === "message_received") schedule();
  });
  const onVisibility = () => {
    if (document.visibilityState === "visible") {
      schedule(); // reconcile after background throttling, even without an event
    }
  };
  document.addEventListener("visibilitychange", onVisibility);
  reconcileTimer = window.setInterval(() => {
    if (document.visibilityState !== "hidden") void refreshMessageNotifications();
  }, 60_000);
  void refreshMessageNotifications();

  stop = () => {
    unsubscribe();
    document.removeEventListener("visibilitychange", onVisibility);
    if (refreshTimer != null) window.clearTimeout(refreshTimer);
    if (reconcileTimer != null) window.clearInterval(reconcileTimer);
    if (backfillTimer != null) window.clearTimeout(backfillTimer);
    refreshTimer = null;
    reconcileTimer = null;
    backfillTimer = null;
    stop = null;
  };
  return stop;
}
