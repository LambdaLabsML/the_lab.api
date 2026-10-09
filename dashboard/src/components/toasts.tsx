// "Hey, I'm here" toasts: another tab joined a collab doc, or explicitly
// invited everyone. Mounted once in the app shell (see app.tsx). Listens on
// the global dashboard event stream — never the per-doc collab socket, which
// only this doc's participants receive.
import { signal } from "@preact/signals";
import { subscribeWsEvents } from "../state/ws";
import { getClientId } from "../state/collab";
import { editingPath, openArtifactForEdit } from "../views/artifacts-view";

interface Toast {
  id: string;
  key: string;
  kind: "presence" | "invite";
  name: string;
  color: string;
  path: string;
  message?: string | null;
}

const TOAST_TTL_MS = 20_000;

const toasts = signal<Toast[]>([]);
/** Dedupe key -> still showing (one toast per person+path at a time). */
const activeKeys = new Set<string>();

function dismiss(id: string, key?: string): void {
  toasts.value = toasts.value.filter((t) => t.id !== id);
  if (key) activeKeys.delete(key);
}

function pushToast(t: Toast, key: string): void {
  if (activeKeys.has(key)) return;
  activeKeys.add(key);
  toasts.value = [...toasts.value, t];
  setTimeout(() => dismiss(t.id, key), TOAST_TTL_MS);
}

/** Subscribe to collab_presence / collab_invite on the global event stream.
 *  Call once (app mount) and dispose on unmount. */
const STALE_AFTER_S = 30;

export function startCollabNotifications(): () => void {
  const selfId = getClientId();
  return subscribeWsEvents((event) => {
    // A fresh tab replays recent events: only toast what is happening now,
    // and not for the document this tab is already editing.
    const ts = typeof event.ts === "number" ? event.ts : null;
    if (ts != null && Date.now() / 1000 - ts > STALE_AFTER_S) return;
    if ((event.type === "collab_presence" || event.type === "collab_invite")
        && editingPath.value === String(event.path ?? "")) return;
    if (event.type === "collab_presence") {
      if (event.action !== "joined") return;
      const client = String(event.client ?? "");
      if (!client || client === selfId) return; // never toast for our own tab
      const path = String(event.path ?? "");
      const key = `presence:${String(event.name ?? client)}:${path}`;
      pushToast({
        id: `${key}:${Date.now()}`, key, kind: "presence",
        name: String(event.name ?? "someone"), color: String(event.color ?? "#58a6ff"), path,
      }, key);
    } else if (event.type === "collab_invite") {
      const from = event.from as { client?: string; name?: string; color?: string } | undefined;
      const client = String(from?.client ?? "");
      if (!client || client === selfId) return;
      const path = String(event.path ?? "");
      const key = `invite:${String(from?.name ?? client)}:${path}`;
      pushToast({
        id: `${key}:${Date.now()}`, key, kind: "invite",
        name: String(from?.name ?? "someone"), color: String(from?.color ?? "#58a6ff"), path,
        message: (event.message as string | null | undefined) ?? null,
      }, key);
    }
  });
}

function join(t: Toast): void {
  openArtifactForEdit(t.path);
  dismiss(t.id, t.key);
}

export function Toasts() {
  const list = toasts.value;
  if (!list.length) return null;
  return (
    <div class="collab-toasts">
      {list.map((t) => (
        <div key={t.id} class="collab-toast" style={{ borderLeftColor: t.color }}>
          <span class="collab-toast-dot" style={{ background: t.color }} />
          <span class="collab-toast-text">
            <b style={{ color: t.color }}>{t.name}</b>{" "}
            {t.kind === "invite"
              ? (t.message ? `invites you: "${t.message}"` : "is inviting everyone to collaborate")
              : "is editing"}{" "}
            {t.path && <code>{t.path}</code>}
          </span>
          <span class="collab-toast-actions">
            <button type="button" class="art-btn is-primary" onClick={() => join(t)}>Join</button>
            <button type="button" class="art-btn is-quiet" onClick={() => dismiss(t.id, t.key)}>Dismiss</button>
          </span>
        </div>
      ))}
    </div>
  );
}
