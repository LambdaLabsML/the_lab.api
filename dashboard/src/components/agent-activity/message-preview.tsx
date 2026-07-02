/**
 * Hover-preview overlay — one floating card over the main area, fed by either
 * hover source in the sidebar: a message row (full message text) or a lab-call
 * row (when / duration / status / response size ≈ tokens / returned keys).
 * Fades in, lives for the duration of the hover. Mounted once in the shell.
 */
import type { ComponentChildren } from "preact";
import { messagePreview, callPreview, runPreview, previewAnchor } from "../../state/agent-activity";
import { AgentPill } from "../agent-pill";
import { FloatCard } from "../ui";
import { ApiIcon } from "./icons";
import { RichText, stripRouting } from "../rich-text";
import { fnCall } from "./fn-call";

function clock(ts: number): string {
  if (!ts) return "";
  const d = new Date(ts);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function fmtBytes(n: number): string {
  if (n >= 1_048_576) return `${(n / 1_048_576).toFixed(1)} MB`;
  if (n >= 1024) return `${(n / 1024).toFixed(1)} kB`;
  return `${n} B`;
}

function Row({ label, children }: { label: string; children: ComponentChildren }) {
  return (
    <div class="msg-preview-row">
      <span class="msg-preview-label">{label}</span>
      <span class="msg-preview-value">{children}</span>
    </div>
  );
}

export function MessagePreviewOverlay() {
  const m = messagePreview.value;
  const c = callPreview.value;
  const r = runPreview.value;
  if (!m && !c && !r) return null;

  const anchor = previewAnchor.value;

  // Running-experiment card: live counters on top, log tail below.
  if (r) {
    const prog = r.progress ?? {};
    const rows = Object.entries(prog)
      .filter(([, v]) => typeof v === "number" || typeof v === "string")
      .slice(0, 8);
    return (
      <FloatCard anchor={anchor}>
        <div class="msg-head msg-preview-head">
          <span class="shortlog-dot is-running" />
          <span class="msg-preview-fn">exp/{r.label}</span>
          {r.pct != null && <span class="msg-preview-pct">{Math.round(r.pct)}%</span>}
          <span class="msg-time">idea/{r.ideaId}</span>
        </div>
        <div class="msg-preview-rows">
          {rows.map(([k, v]) => (
            <Row key={k} label={k.replace(/_/g, " ")}>{String(v)}</Row>
          ))}
          {r.loaded && rows.length === 0 && <Row label="metrics">none reported yet</Row>}
          {!r.loaded && <Row label="metrics">loading…</Row>}
        </div>
        {r.logTail && (
          <pre class="msg-preview-log">{r.logTail}</pre>
        )}
      </FloatCard>
    );
  }

  // Lab-call card: when / took / status / size ≈ tokens / result shape.
  if (c) {
    const call = c.call;
    return (
      <FloatCard anchor={anchor}>
        <div class="msg-head msg-preview-head">
          <AgentPill id={c.agentId} />
          <span class="msg-preview-icon msg-preview-icon--call"><ApiIcon /></span>
          <span class="msg-preview-fn">{fnCall(call)}</span>
          <span class="msg-preview-time">{clock(call.ts)}</span>
        </div>
        <div class="msg-preview-rows">
          {call.status != null && <Row label="status">{call.status}</Row>}
          {call.durationMs != null && <Row label="took">{call.durationMs} ms</Row>}
          {call.respBytes != null && (
            <Row label="returned">
              {fmtBytes(call.respBytes)} · ≈{Math.max(1, Math.round(call.respBytes / 4))} tok
            </Row>
          )}
          {call.respKeys && call.respKeys.length > 0 && (
            <Row label="shape">{`{ ${call.respKeys.join(", ")} }`}</Row>
          )}
          {call.durationMs == null && call.respBytes == null && (
            <Row label="detail">not recorded (server predates the ticker upgrade)</Row>
          )}
        </div>
      </FloatCard>
    );
  }

  const p = m!;
  // Consolidated with the Messages tab: same classes (msg-head/arrow/time/body)
  // + the shared AgentPill/RichText, so the card renders identically.
  return (
    <FloatCard anchor={anchor}>
      <div class="msg-head msg-preview-head">
        {p.from && <AgentPill id={p.from} />}
        <span class="msg-arrow">→</span>
        <AgentPill id={p.to} />
        <span class="msg-time">{clock(p.ts)}</span>
      </div>
      <div class="msg-body msg-preview-body">{p.body ? <RichText text={stripRouting(p.body)} /> : "…"}</div>
      <div class="msg-preview-hint">
        {p.full ? "click to open in Messages" : "loading full text…"}
      </div>
    </FloatCard>
  );
}
