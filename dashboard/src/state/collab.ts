// ------------------------------------------------------------
// Live collaborative editing client: one CollabSession per open artifact,
// talking ot.js-style (synced / awaitingConfirm / awaitingWithBuffer) OT
// over `/api/v1/artifacts/collab`. See the shared spec for the protocol
// and transform rules (ported in ../lib/ot.ts).
//
// This module owns the protocol + transform state machine only; it has no
// knowledge of CodeMirror. The editor component feeds it ops derived from
// local transactions (submitLocalOp) and applies the ChangeSpecs it hands
// back (onRemoteChange) to the view.
// ------------------------------------------------------------

import { signal } from "@preact/signals";
import { getWsToken, subscribeWsEvents } from "./ws";
import { messagesMyNames, myName } from "./settings";
import { append, applyOp, transformX, changesFromOps, type Op, type TextChangeSpec } from "../lib/ot";

export interface Participant {
  id: string;
  name: string;
  color: string;
  anchor: number | null;
  head: number | null;
}

export type CollabStatus = "connecting" | "connected" | "reconnecting" | "disconnected";

// ---------------------------------------------------------------------------
// Identity — until real user accounts exist.
// ---------------------------------------------------------------------------

const CLIENT_ID_KEY = "the-lab:collabClientId"; // sessionStorage: per tab
const GUEST_NAME_KEY = "the-lab:collabGuestName"; // localStorage: per browser

function randomSuffix(): string {
  return Math.random().toString(36).slice(2, 6);
}

/** Stable id for this browser tab (sessionStorage — distinct per tab, stable
 *  across reconnects within it, so "another client joined" presence works). */
export function getClientId(): string {
  try {
    let id = sessionStorage.getItem(CLIENT_ID_KEY);
    if (!id) {
      id = "c-" + Date.now().toString(36) + randomSuffix();
      sessionStorage.setItem(CLIENT_ID_KEY, id);
    }
    return id;
  } catch {
    return "c-" + Date.now().toString(36) + randomSuffix();
  }
}

/** Display name: this browser's primary identity (settings.myName), else the
 *  first alias from "Your names" (comma/semicolon separated), else a
 *  persisted "guest-xxxx" (localStorage, per browser). */
export function getDisplayName(): string {
  const primary = myName.value.trim();
  if (primary) return primary;
  const names = messagesMyNames.value.split(/[,;]/).map((s) => s.trim()).filter(Boolean);
  if (names.length) return names[0];
  try {
    let g = localStorage.getItem(GUEST_NAME_KEY);
    if (!g) {
      g = "guest-" + randomSuffix();
      localStorage.setItem(GUEST_NAME_KEY, g);
    }
    return g;
  } catch {
    return "guest-" + randomSuffix();
  }
}

function buildCollabUrl(path: string): string {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const params = new URLSearchParams();
  params.set("path", path);
  const token = getWsToken();
  if (token) params.set("token", token);
  params.set("name", getDisplayName());
  params.set("client", getClientId());
  return `${proto}//${location.host}/api/v1/artifacts/collab?${params.toString()}`;
}

// ---------------------------------------------------------------------------
// Diff helper — common prefix/suffix, used to replay unacknowledged local
// edits as one op after a reconnect loses in-memory server session state.
// ---------------------------------------------------------------------------

function diffOp(oldText: string, newText: string): Op {
  const maxPrefix = Math.min(oldText.length, newText.length);
  let p = 0;
  while (p < maxPrefix && oldText.charCodeAt(p) === newText.charCodeAt(p)) p++;
  const maxSuffix = Math.min(oldText.length - p, newText.length - p);
  let s = 0;
  while (
    s < maxSuffix &&
    oldText.charCodeAt(oldText.length - 1 - s) === newText.charCodeAt(newText.length - 1 - s)
  ) s++;
  const op: Op = [];
  const delText = oldText.slice(p, oldText.length - s);
  const insText = newText.slice(p, newText.length - s);
  if (delText) op.push({ d: delText, p });
  if (insText) op.push({ i: insText, p });
  return op;
}

// ---------------------------------------------------------------------------
// Content hash — sha1 of the git blob of the UTF-8 text (same as the server).
// ---------------------------------------------------------------------------

async function gitBlobSha1(text: string): Promise<string> {
  const utf8 = new TextEncoder().encode(text);
  const header = new TextEncoder().encode(`blob ${utf8.length}\0`);
  const full = new Uint8Array(header.length + utf8.length);
  full.set(header, 0);
  full.set(utf8, header.length);
  const digest = await crypto.subtle.digest("SHA-1", full);
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

// ---------------------------------------------------------------------------
// CollabSession
// ---------------------------------------------------------------------------

export class CollabSession {
  readonly path: string;
  readonly selfId = getClientId();
  readonly selfName = getDisplayName();

  readonly status = signal<CollabStatus>("connecting");
  readonly participants = signal<Participant[]>([]);
  readonly selfColor = signal<string>("#58a6ff");
  readonly lastSavedAt = signal<number | null>(null);
  readonly fatalError = signal<string | null>(null);

  /** Called with CodeMirror-style ChangeSpecs that must be applied to the
   *  editor (remote ops, transformed past any pending local edits). */
  onRemoteChange: ((changes: TextChangeSpec[]) => void) | null = null;
  /** Called once with the full text the editor should be (re)seeded with
   *  (first join, or a rejoin where we had no pending edits of our own). */
  onResync: ((text: string) => void) | null = null;

  private ws: WebSocket | null = null;
  private version = 0;
  /** Full local mirror, including any not-yet-acked local edits. Kept in
   *  sync with the editor's doc via submitLocalOp / onRemoteChange. */
  private text = "";
  /** Mirror of the server's last known COMMITTED text (no pending local
   *  edits layered on) — used only to verify `hash` against `confirmedText`. */
  private confirmedText = "";
  private outstanding: Op | null = null;
  private buffer: Op | null = null;
  private firstJoin = true;
  private hadPendingAtDisconnect = false;
  private closedByUser = false;

  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private backoffMs = 1_000;
  private readonly BACKOFF_MAX = 15_000;
  private pingTimer: ReturnType<typeof setInterval> | null = null;
  private cursorThrottle: ReturnType<typeof setTimeout> | null = null;
  private pendingCursor: { anchor: number; head: number } | null = null;

  constructor(path: string) {
    this.path = path;
    this.connect();
  }

  // -- outgoing -----------------------------------------------------------

  private send(msg: Record<string, unknown>): void {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(msg));
  }

  /** Feed a local edit (already applied optimistically to the editor).
   *  `op` is the component list for this transaction; `newFullText` is the
   *  editor's full text after it. */
  submitLocalOp(op: Op, newFullText: string): void {
    this.text = newFullText;
    if (!op.length) return;
    if (!this.outstanding) {
      this.outstanding = op;
      this.send({ type: "op", v: this.version, ops: op });
    } else if (!this.buffer) {
      this.buffer = op;
    } else {
      for (const c of op) append(this.buffer, c);
    }
  }

  /** Throttled (~100ms) cursor broadcast. */
  sendCursor(anchor: number, head: number): void {
    this.pendingCursor = { anchor, head };
    if (this.cursorThrottle) return;
    this.cursorThrottle = setTimeout(() => {
      this.cursorThrottle = null;
      if (this.pendingCursor) this.send({ type: "cursor", ...this.pendingCursor });
    }, 100);
  }

  invite(message?: string | null): void {
    this.send({ type: "invite", message: message ?? null });
  }

  dispose(): void {
    this.closedByUser = true;
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    if (this.pingTimer) clearInterval(this.pingTimer);
    if (this.cursorThrottle) clearTimeout(this.cursorThrottle);
    this.ws?.close();
    this.ws = null;
  }

  // -- connection lifecycle -------------------------------------------------

  private connect(): void {
    this.status.value = this.firstJoin ? "connecting" : "reconnecting";
    let ws: WebSocket;
    try {
      ws = new WebSocket(buildCollabUrl(this.path));
    } catch {
      this.scheduleReconnect();
      return;
    }
    this.ws = ws;
    ws.onopen = () => {
      this.backoffMs = 1_000;
      this.pingTimer = setInterval(() => this.send({ type: "ping" }), 20_000);
    };
    ws.onmessage = (ev: MessageEvent) => {
      let msg: Record<string, any>;
      try {
        msg = JSON.parse(ev.data as string);
      } catch {
        return; // ignore malformed frames
      }
      try {
        this.handleMessage(msg);
      } catch (e) {
        // Applying the frame failed (local state out of sync): log it and
        // rejoin, which resyncs the text from the server.
        console.error("collab: could not apply", msg?.type, e);
        ws.close();
      }
    };
    ws.onclose = (ev: CloseEvent) => {
      this.ws = null;
      if (this.pingTimer) { clearInterval(this.pingTimer); this.pingTimer = null; }
      if (this.closedByUser) return;
      if (ev.code === 1008) {
        this.status.value = "disconnected";
        this.fatalError.value = "Not authorized to edit this file.";
        return;
      }
      this.hadPendingAtDisconnect = this.hadPendingAtDisconnect || !!this.outstanding || !!this.buffer;
      this.status.value = "reconnecting";
      this.scheduleReconnect();
    };
    ws.onerror = () => { /* onerror is always followed by onclose */ };
  }

  private scheduleReconnect(): void {
    if (this.closedByUser || this.reconnectTimer) return;
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.connect();
    }, this.backoffMs);
    this.backoffMs = Math.min(this.backoffMs * 2, this.BACKOFF_MAX);
  }

  // -- incoming -------------------------------------------------------------

  private handleMessage(msg: Record<string, any>): void {
    switch (msg.type) {
      case "joined": this.handleJoined(msg); break;
      case "op": this.handleRemoteOp(msg.ops as Op, msg.v as number); break;
      case "ack": this.handleAck(msg.v as number, msg.hash as string); break;
      case "cursor": this.updateParticipantCursor(msg.client, msg.anchor, msg.head); break;
      case "presence": this.participants.value = msg.clients ?? []; break;
      case "saved": this.lastSavedAt.value = Date.parse(msg.at) || Date.now(); break;
      case "error":
        this.fatalError.value = typeof msg.message === "string" ? msg.message : "collab error";
        this.ws?.close(); // reconnect + rejoin, per spec
        break;
      // "pong": no-op keep-alive reply
    }
  }

  /** Joined at least once: ``text`` holds the document (incl. remote ops
   *  that arrived before the editor view existed). */
  get hasJoined(): boolean { return !this.firstJoin; }
  get currentText(): string { return this.text; }

  private handleJoined(msg: Record<string, any>): void {
    const serverText: string = msg.text ?? "";
    const serverVersion: number = msg.v ?? 0;
    this.selfColor.value = msg.color || this.selfColor.value;
    this.participants.value = msg.clients ?? [];

    if (this.firstJoin) {
      this.firstJoin = false;
      this.text = serverText;
      this.confirmedText = serverText;
      this.version = serverVersion;
      this.outstanding = null;
      this.buffer = null;
      this.status.value = "connected";
      this.onResync?.(serverText);
    } else {
      // Reconnect: the server session (and our outstanding/buffer there) is
      // gone. Replay any unacknowledged local edits as a single diff op so
      // they aren't lost; otherwise just accept the server's text.
      const hadPending = this.hadPendingAtDisconnect;
      this.hadPendingAtDisconnect = false;
      this.version = serverVersion;
      this.confirmedText = serverText;
      this.outstanding = null;
      this.buffer = null;
      if (hadPending && this.text !== serverText) {
        const op = diffOp(serverText, this.text);
        if (op.length) {
          this.outstanding = op;
          this.send({ type: "op", v: this.version, ops: op });
        }
        // this.text already holds our edits — leave the editor as-is.
      } else if (this.text !== serverText) {
        const op = diffOp(this.text, serverText);
        this.text = serverText;
        if (op.length) this.onRemoteChange?.(changesFromOps(op));
      }
      this.status.value = "connected";
    }
    void this.verifyHash(msg.hash);
  }

  private handleRemoteOp(serverOp: Op, v: number): void {
    this.confirmedText = applyOp(this.confirmedText, serverOp);
    let toApply: Op = serverOp;
    if (this.outstanding) {
      const [newOutstanding, opPrime] = transformX(this.outstanding, toApply);
      this.outstanding = newOutstanding.length ? newOutstanding : null;
      toApply = opPrime;
      if (this.buffer) {
        const [newBuffer, opPrime2] = transformX(this.buffer, toApply);
        this.buffer = newBuffer.length ? newBuffer : null;
        toApply = opPrime2;
      }
    }
    if (toApply.length) {
      this.text = applyOp(this.text, toApply);
      this.onRemoteChange?.(changesFromOps(toApply));
    }
    this.version = v + 1;
  }

  private handleAck(v: number, hash: string): void {
    const acked = this.outstanding;
    if (acked) this.confirmedText = applyOp(this.confirmedText, acked);
    this.version = v + 1;
    if (this.buffer) {
      const toSend = this.buffer;
      this.outstanding = toSend;
      this.buffer = null;
      this.send({ type: "op", v: this.version, ops: toSend });
    } else {
      this.outstanding = null;
    }
    void this.verifyHash(hash);
  }

  private async verifyHash(expected: string | undefined): Promise<void> {
    if (!expected) return;
    const text = this.confirmedText;
    let actual: string;
    try {
      actual = await gitBlobSha1(text);
    } catch {
      return; // SubtleCrypto unavailable (non-HTTPS/non-localhost) — skip the check
    }
    if (actual !== expected) {
      // Integrity mismatch — reconnect and rejoin to resync.
      this.ws?.close();
    }
  }

  private updateParticipantCursor(clientId: string, anchor: number | null, head: number | null): void {
    const list = this.participants.value;
    const idx = list.findIndex((p) => p.id === clientId);
    if (idx === -1) return;
    const next = list.slice();
    next[idx] = { ...next[idx], anchor, head };
    this.participants.value = next;
  }
}

// ---------------------------------------------------------------------------
// Active-collaborator counts per path — fed by the global dashboard event
// stream's `collab_presence` events (count is given directly), so the
// artifacts sidebar can show a "● N editing" badge without opening a socket
// per listed file.
// ---------------------------------------------------------------------------

export const collabActiveCounts = signal<Record<string, number>>({});

subscribeWsEvents((event) => {
  if (event.type !== "collab_presence") return;
  const path = String((event as Record<string, unknown>).path ?? "");
  if (!path) return;
  const count = Number((event as Record<string, unknown>).count ?? 0);
  const next = { ...collabActiveCounts.value };
  if (count > 0) next[path] = count; else delete next[path];
  collabActiveCounts.value = next;
});
