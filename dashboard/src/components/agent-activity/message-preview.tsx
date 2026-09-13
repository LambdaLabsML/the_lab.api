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
import { fmtScoreShort } from "../../lib/format";

// Prettify a progress key: n_llm_calls → "llm calls", pct_complete → "complete".
const STAT_LABELS: Record<string, string> = {
  pct_complete: "complete", n_turns: "turns", n_llm_calls: "llm calls",
  n_gameplay_actions: "actions", n_levels_completed: "levels done",
  n_levels_touched: "levels touched", budget_remaining_s: "budget (s)",
  fraction_bfs_calls: "bfs fraction", score: "score",
};
function statLabel(k: string): string {
  return STAT_LABELS[k] ?? k.replace(/^n_/, "").replace(/_/g, " ");
}
function statValue(v: unknown): string {
  if (typeof v === "number") {
    return Number.isInteger(v) ? String(v) : fmtScoreShort(v);
  }
  return String(v);
}

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
    const entries = Object.entries(prog).filter(([k]) => !k.startsWith("_"));
    // Numbers + short strings go in the stat grid; the first long string (the
    // script's own "msg" summary) becomes a full-width line under it.
    const rows = entries
      .filter(([, v]) => typeof v === "number" || (typeof v === "string" && v.length <= 16))
      .slice(0, 9);
    const note = entries.find(([, v]) => typeof v === "string" && v.length > 16)?.[1] as string | undefined;
    return (
      <FloatCard anchor={anchor}>
        <div class="msg-head msg-preview-head">
          <span class="shortlog-dot is-running" />
          <span class="msg-preview-fn">exp/{r.label}</span>
          {r.pct != null && <span class="msg-preview-pct">{Math.round(r.pct)}%</span>}
          <span class="msg-time">idea/{r.ideaId}</span>
        </div>
        {rows.length > 0 ? (
          // Metrics as a compact stat grid (label over value — DESIGN <Stat>
          // pattern), not label/value rows: long keys stay on one line.
          <div class="msg-preview-stats">
            {rows.map(([k, v]) => (
              <div class="msg-preview-stat" key={k}>
                <span class="msg-preview-stat-label">{statLabel(k)}</span>
                <span class="msg-preview-stat-value">{statValue(v)}</span>
              </div>
            ))}
          </div>
        ) : (
          <div class="msg-preview-rows">
            <Row label="metrics">{r.loaded ? "none reported yet" : "loading…"}</Row>
          </div>
        )}
        {note && <div class="msg-preview-note">{note}</div>}
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
            <Row label="detail">{call.status == null ? "in flight…" : "not recorded (pre-upgrade event)"}</Row>
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
