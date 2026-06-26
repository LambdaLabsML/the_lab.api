// ------------------------------------------------------------
// WebSocket connection manager.
//
// Provides event-driven cache invalidation on top of the
// existing polling intervals (which remain as fallback).
//
// Usage:
//   startWs()  — call once on app mount (idempotent)
//   stopWs()   — call on unmount (rarely needed)
//   wsConnected — Preact signal, true when socket is open
// ------------------------------------------------------------

import { signal } from "@preact/signals";
import {
  refreshChartData,
  refreshGraphData,
  refreshBacklogData,
} from "./polling";

// ---------------------------------------------------------------------------
// Public signal
// ---------------------------------------------------------------------------

/** True when the WebSocket connection is open and healthy. */
export const wsConnected = signal<boolean>(false);

/** True when auth failed (close code 1008) — prevents reconnect. */
export const wsAuthFailed = signal<boolean>(false);

/** Epoch ms of the last websocket message received (for connection stats). */
export const wsLastMessageAt = signal<number | null>(null);

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
// Event → handler map
// ---------------------------------------------------------------------------

/** Minimal shape of a server-sent WS event. */
interface WsEvent {
  type: string;
  seq?: number;
  [key: string]: unknown;
}

// Maps each server event type to a list of refresh functions to call.
// Keep handlers fine-grained — only trigger the minimum required fetches.
const HANDLERS: Record<string, Array<() => void>> = {
  experiment_queued:    [refreshChartData],
  experiment_started:  [refreshChartData],
  experiment_finished: [refreshChartData, refreshGraphData],
  experiment_cancelled:[refreshChartData],
  // queue_changed: queue pane self-polls at 3 s, no extra refresh needed
  idea_changed:        [refreshGraphData, refreshBacklogData],
  note_added:          [refreshGraphData],
  graph_changed:       [refreshGraphData, refreshBacklogData],
  // message_received: notifications appear via next backlog/graph fetch
};

// ---------------------------------------------------------------------------
// Internal state
// ---------------------------------------------------------------------------

let socket: WebSocket | null = null;
let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
let started = false;

/** Sequence number of the last received event. Sent as ?since= on reconnect
 *  so the server can replay any events we missed while disconnected. */
let lastSeq = 0;

/** Backoff delay in ms. Doubles on each failure, caps at BACKOFF_MAX. */
let backoffMs = 1_000;
const BACKOFF_MAX = 30_000;

// ---------------------------------------------------------------------------
// Connection logic
// ---------------------------------------------------------------------------

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

  try {
    socket = new WebSocket(buildUrl());
  } catch {
    // URL construction or WebSocket constructor failed — retry later
    scheduleReconnect();
    return;
  }

  socket.onopen = () => {
    wsConnected.value = true;
    backoffMs = 1_000; // reset backoff on success

    // Catch-up: fire all pollers immediately to fill any gap while we were offline
    refreshChartData();
    refreshGraphData();
    refreshBacklogData();
  };

  socket.onmessage = (ev: MessageEvent) => {
    let event: WsEvent;
    try {
      event = JSON.parse(ev.data as string) as WsEvent;
    } catch {
      return; // ignore malformed frames
    }
    wsLastMessageAt.value = Date.now();

    // Track the sequence number for gap-recovery on reconnect
    if (typeof event.seq === "number" && event.seq > lastSeq) {
      lastSeq = event.seq;
    }

    const handlers = HANDLERS[event.type];
    if (handlers) {
      for (const fn of handlers) {
        fn();
      }
    }
  };

  socket.onclose = (ev: CloseEvent) => {
    wsConnected.value = false;
    socket = null;

    if (ev.code === 1008) {
      // Policy violation — auth failed; surface to user and stop retrying
      wsAuthFailed.value = true;
      return;
    }

    scheduleReconnect();
  };

  socket.onerror = () => {
    // onerror is always followed by onclose, which handles reconnect
  };
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/** Start the WebSocket connection manager. Safe to call multiple times. */
export function startWs(): void {
  if (started) return;
  started = true;
  connect();
}

/** Stop the WebSocket connection and cancel any pending reconnect. */
export function stopWs(): void {
  started = false;
  if (reconnectTimer !== null) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  if (socket) {
    socket.onclose = null; // prevent reconnect from close handler
    socket.close();
    socket = null;
  }
  wsConnected.value = false;
}
