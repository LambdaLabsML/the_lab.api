// ------------------------------------------------------------
// Event-stream connection manager.
//
// One event stream, two transports:
//   1. WebSocket /api/v1/ws          — preferred, server-push
//   2. long-poll GET /api/v1/events  — fallback when WS can't connect
//      (proxy strips upgrades, restrictive network); same events,
//      same seq/ts envelope, shared downstream handling.
//
// Events carry the changed entity (P2.1), so this layer PATCHES the
// data signals via the reducers in polling.ts and only falls back to
// full refetches when a payload is missing or a gap is detected. The
// polling intervals remain as slow reconciliation.
//
// Usage:
//   startWs()   — call once on app mount (idempotent)
//   stopWs()    — call on unmount (rarely needed)
//   wsConnected — signal, true while the EVENT STREAM is healthy
//                 (either transport)
//   transportMode — "ws" | "poll" | "down" for status displays
// ------------------------------------------------------------

import { signal } from "@preact/signals";
import {
  refreshChartData,
  refreshGraphData,
  refreshBacklogData,
  patchExperiment,
  removeExperiment,
  patchIdea,
  patchProgress,
  _setStreamHealthProbe,
} from "./polling";
import type { Experiment, IdeaNode } from "../lib/types";

// ---------------------------------------------------------------------------
// Public signals
// ---------------------------------------------------------------------------

/** True while the event stream is healthy — over EITHER transport. */
export const wsConnected = signal<boolean>(false);

/** True when auth failed (WS close 1008 / HTTP 401) — prevents reconnect. */
export const wsAuthFailed = signal<boolean>(false);

/** Epoch ms of the last stream event received (for connection stats). */
export const wsLastMessageAt = signal<number | null>(null);

/** Which transport is currently delivering events. */
export const transportMode = signal<"ws" | "poll" | "down">("down");

// ---------------------------------------------------------------------------
// Auth token helper
// ---------------------------------------------------------------------------

/**
 * Returns the WS auth token to use as a query param.
 * Browsers cannot set Authorization headers for WebSocket connections.
 *
 * Looks for a base64-encoded credential string in localStorage under
 * "the-lab:wsToken". If absent, returns an empty string so the
 * connection is attempted without auth (works when server auth is off).
 */
export function getWsToken(): string {
  try {
    return localStorage.getItem("the-lab:wsToken") ?? "";
  } catch {
    return "";
  }
}

// ---------------------------------------------------------------------------
// Raw event subscription — lets the live agent-activity view consume the full
// event stream (experiment_*, message_received, note_added, agent_api_call, …)
// in addition to the signal patching below.
// ---------------------------------------------------------------------------

/** Minimal shape of a server-sent stream event. */
interface WsEvent {
  type: string;
  seq?: number;
  [key: string]: unknown;
}

type WsSubscriber = (event: WsEvent) => void;
const _subscribers = new Set<WsSubscriber>();

/** Subscribe to every raw stream event. Returns an unsubscribe fn. */
export function subscribeWsEvents(fn: WsSubscriber): () => void {
  _subscribers.add(fn);
  return () => { _subscribers.delete(fn); };
}

// ---------------------------------------------------------------------------
// Event → state reducers
// ---------------------------------------------------------------------------

/**
 * Apply one event to the data signals. Prefers patching with the entity
 * payload the event carries; falls back to the coarse refresh when the
 * payload is missing (older server) so behaviour degrades gracefully.
 */
function applyEvent(event: WsEvent): void {
  switch (event.type) {
    case "experiment_queued":
    case "experiment_started":
    case "experiment_finished":
    case "experiment_cancelled": {
      const row = event.experiment as Partial<Experiment> | undefined;
      if (!row || !patchExperiment(row)) refreshChartData();
      // Lifecycle changes flip computed graph flags (has_running/last_finish)
      // that the payload can't carry — the graph fetch is the cheap one.
      if (event.type !== "experiment_queued") refreshGraphData();
      break;
    }
    case "experiment_deleted": {
      const key = (event.label as string) ?? String(event.experiment_id ?? "");
      if (key) removeExperiment(key);
      refreshGraphData();
      break;
    }
    case "experiment_progress_updated": {
      const prog = event.progress as Record<string, unknown> | null | undefined;
      if (prog && typeof event.label === "string") {
        patchProgress(event.label, prog);
      }
      break;
    }
    case "idea_changed": {
      const idea = event.idea as (Partial<IdeaNode> & { id: number }) | undefined;
      if (!idea || !patchIdea(idea)) refreshGraphData();
      refreshBacklogData();
      break;
    }
    case "note_added":
      refreshGraphData();
      break;
    // queue_changed / message_received / agent_changed: consumed by the
    // views that own that data via subscribeWsEvents (queue view, messages
    // view, agent-activity store).
  }
}

// ---------------------------------------------------------------------------
// Shared event pump (both transports feed this)
// ---------------------------------------------------------------------------

/** Sequence number of the last received event. Sent as ?since= on reconnect
 *  so the server can replay any events we missed while disconnected. The
 *  server journal (P4) keeps seq monotonic across restarts. */
let lastSeq = 0;

function handleRawEvent(event: WsEvent): void {
  wsLastMessageAt.value = Date.now();
  if (typeof event.seq === "number" && event.seq > lastSeq) {
    lastSeq = event.seq;
  }
  applyEvent(event);
  // Fan out the raw event to activity subscribers (never let one throw break
  // the stream or the other subscribers).
  for (const sub of _subscribers) {
    try { sub(event); } catch { /* ignore subscriber error */ }
  }
}

function catchUp(): void {
  // Fill any gap accumulated while the stream was down.
  refreshChartData();
  refreshGraphData();
  refreshBacklogData();
}

// ---------------------------------------------------------------------------
// Transport 1: WebSocket
// ---------------------------------------------------------------------------

let socket: WebSocket | null = null;
let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
let started = false;

/** Consecutive failed WS connection attempts — 3 triggers the long-poll
 *  fallback. Reset on any successful open. */
let wsFailureStreak = 0;
const WS_FAILURES_BEFORE_FALLBACK = 3;

/** Backoff delay in ms. Doubles on each failure, caps at BACKOFF_MAX. */
let backoffMs = 1_000;
const BACKOFF_MAX = 30_000;

function buildUrl(): string {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const params = new URLSearchParams();
  if (lastSeq > 0) params.set("since", String(lastSeq));
  const token = getWsToken();
  if (token) params.set("token", token);
  const qs = params.toString();
  return `${proto}//${location.host}/api/v1/ws${qs ? "?" + qs : ""}`;
}

function scheduleReconnect(): void {
  if (!started) return;
  if (wsAuthFailed.value) return; // permanent failure, don't retry
  if (wsFailureStreak >= WS_FAILURES_BEFORE_FALLBACK) {
    startLongPoll();
    return;
  }
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connect();
  }, backoffMs);
  // Exponential backoff with cap
  backoffMs = Math.min(backoffMs * 2, BACKOFF_MAX);
}

function connect(): void {
  if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) {
    return; // already connected or connecting
  }

  let opened = false;
  try {
    socket = new WebSocket(buildUrl());
  } catch {
    wsFailureStreak++;
    scheduleReconnect();
    return;
  }

  socket.onopen = () => {
    opened = true;
    wsFailureStreak = 0;
    backoffMs = 1_000;
    stopLongPoll(); // WS wins; the fallback loop can stop
    wsConnected.value = true;
    transportMode.value = "ws";
    catchUp();
  };

  socket.onmessage = (ev: MessageEvent) => {
    let event: WsEvent;
    try {
      event = JSON.parse(ev.data as string) as WsEvent;
    } catch {
      return; // ignore malformed frames
    }
    handleRawEvent(event);
  };

  socket.onclose = (ev: CloseEvent) => {
    socket = null;
    if (transportMode.value === "ws") {
      wsConnected.value = false;
      transportMode.value = "down";
    }

    if (ev.code === 1008) {
      // Policy violation — auth failed; surface to user and stop retrying
      wsAuthFailed.value = true;
      return;
    }
    if (!opened) wsFailureStreak++;
    scheduleReconnect();
  };

  socket.onerror = () => {
    // onerror is always followed by onclose, which handles reconnect
  };
}

// ---------------------------------------------------------------------------
// Transport 2: long-poll GET /api/v1/events
// ---------------------------------------------------------------------------

let pollAbort: AbortController | null = null;
let pollActive = false;
let upgradeTimer: ReturnType<typeof setInterval> | null = null;
/** How often the fallback re-tries a WS upgrade while long-polling. */
const UPGRADE_RETRY_MS = 60_000;

function startLongPoll(): void {
  if (pollActive || !started) return;
  pollActive = true;
  transportMode.value = "poll";
  pollAbort = new AbortController();
  void longPollLoop(pollAbort.signal);
  // Keep trying to upgrade back to WS in the background.
  upgradeTimer = setInterval(() => {
    if (started && !wsAuthFailed.value) connect();
  }, UPGRADE_RETRY_MS);
}

function stopLongPoll(): void {
  if (!pollActive) return;
  pollActive = false;
  pollAbort?.abort();
  pollAbort = null;
  if (upgradeTimer !== null) {
    clearInterval(upgradeTimer);
    upgradeTimer = null;
  }
}

async function longPollLoop(abort: AbortSignal): Promise<void> {
  let errorStreak = 0;
  catchUp();
  wsConnected.value = true;
  while (pollActive && started && !abort.aborted) {
    try {
      const resp = await fetch(
        `/api/v1/events?since=${lastSeq}&timeout=25`,
        { signal: abort, headers: { "X-The-Lab-Source": "dashboard" } },
      );
      if (resp.status === 401 || resp.status === 403) {
        wsAuthFailed.value = true;
        wsConnected.value = false;
        transportMode.value = "down";
        stopLongPoll();
        return;
      }
      if (!resp.ok) throw new Error(`events ${resp.status}`);
      const data = await resp.json() as { events?: WsEvent[]; seq?: number };
      for (const ev of data.events ?? []) handleRawEvent(ev);
      if (typeof data.seq === "number" && data.seq > lastSeq) lastSeq = data.seq;
      errorStreak = 0;
      wsConnected.value = true;
      if (transportMode.value !== "ws") transportMode.value = "poll";
    } catch {
      if (abort.aborted) return;
      errorStreak++;
      if (errorStreak >= 2 && transportMode.value === "poll") {
        wsConnected.value = false;
        transportMode.value = "down";
      }
      await new Promise((r) => setTimeout(r, Math.min(5_000 * errorStreak, 30_000)));
    }
  }
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/** Start the event-stream connection manager. Safe to call multiple times. */
export function startWs(): void {
  if (started) return;
  started = true;
  // Let the pollers read stream health without an import cycle.
  _setStreamHealthProbe(() => wsConnected.value);
  connect();
}

/** Stop the event stream and cancel any pending reconnect. */
export function stopWs(): void {
  started = false;
  if (reconnectTimer !== null) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  stopLongPoll();
  if (socket) {
    socket.onclose = null; // prevent reconnect from close handler
    socket.close();
    socket = null;
  }
  wsConnected.value = false;
  transportMode.value = "down";
}
