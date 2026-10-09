// Dashboard-user message notifications, per browser.
//
// Humans are addressable as the catch-all "user" or, once named, personally
// as "user:<name>" (see the_lab/messages.py). This client-side store decides
// per browser what is addressed to the person using it, and keeps their read
// state (separate from agents' `read_by` acknowledgements). A message is
// addressed to this browser's user when it is
//   - sent to the catch-all "user" (everyone), or directly to this browser's
//     own "user:<name>" (see settings.myName), or
//   - a reply to a message sent from this browser, or as this browser's name
//     from elsewhere, or
//   - mentions one of the names set in this browser (messagesMyNames), since
//     agents address the human by name inside thread posts ("→ David 5843").
// Agent chatter, broadcasts, and other humans' messages never count. The
// newest 200 messages are inspected; websocket events reconcile that window.

import { effect, signal } from "@preact/signals";
import { listMessagesPage } from "./api";
import { messagesMyNames, messagesReadByMe, myName } from "./settings";
import { humanIdForName } from "../lib/identity";
import { subscribeWsEvents } from "./ws";
import type { MessageEntry } from "../lib/types";

// v2: the addressing rule changed (no broadcasts / whole threads any more),
// so ids collected under the old rule are dropped.
const PENDING_KEY = "the-lab:messagesUnreadPending.v2";
const MINE_KEY = "the-lab:messagesSentHere";
const HIGH_WATER_KEY = "the-lab:messagesUnreadHighWater";
const BACKFILL_KEY = "the-lab:messagesUnreadBackfill";
const PAGE_LIMIT = 200;
const MAX_RECONCILE_PAGES = 4;

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

let highWaterId = load<number>(HIGH_WATER_KEY, 0);
let backfill = load<Backfill | null>(BACKFILL_KEY, null);
// Messages sent from THIS browser (ids from the POST responses).
const sentHere = new Set<number>(load<number[]>(MINE_KEY, []));
// Keep qualifying ids after they leave the newest-200 API window.  The read
// map remains the source of truth, so toggling a message back to unread works.
const pendingUnreadIds = new Set<number>(load<number[]>(PENDING_KEY, []));
let messages: MessageEntry[] = [];

/** Number of unread messages addressed to this browser's user (@user). */
export const messagesUnreadCount = signal(0);

/** Number of unread messages of any kind (@anyone) in the newest window,
 *  newer than this browser's first visit, not sent from here. */
export const messagesUnreadAnyCount = signal(0);
const ANY_BASELINE_KEY = "the-lab:messagesUnreadAnyBaseline";
let anyBaselineId = load<number | null>(ANY_BASELINE_KEY, null);

/** Whether a message counts towards the @anyone number (unread state aside). */
export function countsAsNew(m: MessageEntry): boolean {
  return !isMine(m) && anyBaselineId != null && m.id > anyBaselineId;
}

/** Newest message id seen; bounds which bare numbers in text can be message refs. */
export const latestMessageId = signal(0);

function isDirectToDashboardUser(m: MessageEntry): boolean {
  if (m.to === "user" || m.to === "agent:user") return true;
  const mine = humanIdForName(myName.value);
  return !!mine && m.to === mine;
}

/** Whether *m* replies to something this browser's user sent — either from
 *  this browser (sentHere) or, elsewhere, as this browser's named identity. */
function isReplyToMine(m: MessageEntry): boolean {
  if (m.in_reply_to == null) return false;
  if (sentHere.has(m.in_reply_to)) return true;
  const mine = humanIdForName(myName.value);
  if (!mine) return false;
  return messages.some((x) => x.id === m.in_reply_to && x.from_agent === mine);
}

function threadOf(m: MessageEntry): number {
  return m.thread_id ?? m.id;
}

function escapeRe(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

let namesKey = "";
let namesRe: RegExp | null = null;
function myNamesRe(): RegExp | null {
  const raw = messagesMyNames.value;
  if (raw !== namesKey) {
    namesKey = raw;
    const names = raw.split(/[,;]/).map((n) => n.trim()).filter((n) => n.length >= 2);
    namesRe = names.length
      ? new RegExp(`(?<![\\w@])@?(?:${names.map(escapeRe).join("|")})(?!\\w)`, "i")
      : null;
  }
  return namesRe;
}

/** Whether a message is addressed to the person using this browser. Your
 *  own messages (sent here, or as your named identity elsewhere) never
 *  count — see isMine. */
export function isAddressedToMe(m: MessageEntry, { names = true }: { names?: boolean } = {}): boolean {
  if (isMine(m)) return false;
  if (isDirectToDashboardUser(m)) return true;
  if (isReplyToMine(m)) return true;
  if (!names) return false;
  const re = myNamesRe();
  return !!re && (re.test(m.tldr ?? "") || re.test(m.text));
}

/** Whether *m* was sent by the person using this browser: from this browser
 *  (sentHere), or — once named — as their "user:<name>" identity, period. */
export function isMine(m: MessageEntry): boolean {
  if (sentHere.has(m.id)) return true;
  const mine = humanIdForName(myName.value);
  return !!mine && m.from_agent === mine;
}

/** Whether a message was sent from this browser. */
export function isSentHere(m: MessageEntry): boolean {
  return sentHere.has(m.id);
}

/**
 * Record a message sent from this browser, so replies to it notify here.
 * Composers call it with the POST response.
 */
export function registerDashboardUserMessage(m: MessageEntry): void {
  if (sentHere.has(m.id)) return;
  sentHere.add(m.id);
  save(MINE_KEY, [...sentHere]);
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

/** "Read all": every pending notification plus the given loaded messages. */
export function markAllMessagesReadByMe(loadedIds: number[] = []): void {
  markMessagesReadByMe([...pendingUnreadIds, ...messages.map((m) => m.id), ...loadedIds]);
}

function recompute(): void {
  const read = messagesReadByMe.value;
  messagesUnreadCount.value = [...pendingUnreadIds].reduce(
    (count, id) => count + (read[String(id)] ? 0 : 1),
    0,
  );
  messagesUnreadAnyCount.value = messages.filter((m) => countsAsNew(m) && !read[String(m.id)]).length;
}

// ``names``: name mentions qualify only in the newest window, not in the
// first-visit history backfill (a long-running lab mentions you by name
// hundreds of times; direct messages and replies to you always count).
function qualify(batch: MessageEntry[], names = true): void {
  let pendingChanged = false;
  for (const m of batch) {
    if (isAddressedToMe(m, { names }) && !pendingUnreadIds.has(m.id)) {
      pendingUnreadIds.add(m.id);
      pendingChanged = true;
    }
  }
  if (pendingChanged) save(PENDING_KEY, [...pendingUnreadIds]);
}

/** ``newest``: this batch is the newest page (the window "Read all", thread
 *  reads and name changes work on); backfill pages only qualify. */
function ingest(next: MessageEntry[], newestPage = true): void {
  if (newestPage) messages = next;
  const newest = next.reduce((max, m) => Math.max(max, m.id), 0);
  if (newest > latestMessageId.value) latestMessageId.value = newest;
  // First visit: everything already there is history, not "new".
  if (anyBaselineId == null && newestPage) {
    anyBaselineId = newest;
    save(ANY_BASELINE_KEY, anyBaselineId);
  }
  qualify(next, newestPage);
  recompute();
}

// Changing your name or aliases re-evaluates the loaded window: drop what no
// longer matches, add the new matches.
let lastNames: string | null = null;
effect(() => {
  const names = `${myName.value}\n${messagesMyNames.value}`;
  if (lastNames === null) { lastNames = names; return; }
  if (names === lastNames) return;
  lastNames = names;
  for (const m of messages) if (!isAddressedToMe(m)) pendingUnreadIds.delete(m.id);
  save(PENDING_KEY, [...pendingUnreadIds]);
  qualify(messages);
  recompute();
});

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
    const firstSnapshot = highWaterId === 0;
    ingest(first.messages);

    // A burst larger than the newest page can push messages out before this
    // refresh sees them.  Walk back only to the last known newest id, four
    // pages per turn; if necessary, continue in a later task so one socket
    // event never turns into an unbounded archive fetch.
    const previousHighWater = highWaterId;
    const newestId = first.messages.reduce((max, m) => Math.max(max, m.id), 0);
    if (!backfill && firstSnapshot && first.total > first.messages.length) {
      // Messages addressed to this user from before the newest page are still
      // meaningful on a first visit ("Read all" clears them).
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
        ingest(page.messages, false);
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
