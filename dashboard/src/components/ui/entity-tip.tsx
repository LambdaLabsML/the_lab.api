/**
 * Shared hover-card content for experiments and ideas — the single source of
 * truth for what an entity tooltip shows, so the chart, timeline, tables, agent
 * bar, etc. all render the SAME structured card. Drop the return value into a
 * <Tooltip content={…}> or a positioned `.ui-tip` card (which is a flex column;
 * these blocks stack with the card's gap). See dashboard/DESIGN.md.
 *
 * Layout: a header row (mark + record/running flag), a confident metric value
 * with an eyebrow label, an idea line with a status badge, then a clamped
 * description — all on the purple-outlined card.
 */
import type { ComponentChildren } from "preact";

function fmtVal(v: number): string {
  if (!isFinite(v)) return String(v);
  if (Math.abs(v) >= 1000) return v.toFixed(0);
  if (Math.abs(v) >= 10) return v.toFixed(1);
  if (Math.abs(v) >= 1) return v.toFixed(2);
  return v.toFixed(3);
}

function excerpt(s: string | undefined, n = 96): string {
  return (s ?? "").replace(/\s+/g, " ").trim().slice(0, n);
}

// status → .ui-badge tone
const STATUS_TONE: Record<string, string> = {
  active: "active", running: "running", completed: "good", concluded: "concluded",
  abandoned: "abandoned", failed: "bad", cancelled: "neutral",
  queued: "neutral", pending: "neutral", suggested: "warn",
};

/** Canonical experiment hover content. */
export function experimentTipContent(o: {
  label: string;
  ideaId: number;
  ideaTitle?: string;
  status?: string;
  metricName?: string;
  value?: number | null;
  record?: boolean;
  running?: boolean;
}): ComponentChildren {
  const tone = o.status ? (STATUS_TONE[o.status] ?? "neutral") : null;
  return (
    <>
      <span class="ui-tip-head">
        <span class="ui-tip-mark">exp/{o.label}</span>
        {o.record ? (
          <span class="ui-tip-flag ui-tip-flag--record">★ record</span>
        ) : o.running ? (
          <span class="ui-tip-flag ui-tip-flag--running">running</span>
        ) : null}
      </span>
      {o.value != null && Number.isFinite(o.value as number) && (
        <span class="ui-tip-metric">
          <span class="ui-tip-metric-label">{o.metricName ?? "value"}</span>
          <span class={`ui-tip-metric-value${o.record ? " is-record" : ""}`}>{fmtVal(o.value as number)}</span>
        </span>
      )}
      <span class="ui-tip-idea">
        <span class="ui-tip-idea-id">idea #{o.ideaId}</span>
        {tone && <span class={`ui-badge ui-badge--${tone}`}>{o.status}</span>}
      </span>
      {o.ideaTitle && <span class="ui-tip-desc">{excerpt(o.ideaTitle)}</span>}
    </>
  );
}

/** Canonical idea hover content. */
export function ideaTipContent(o: {
  id: number;
  status?: string;
  title?: string;
  best?: { metricName?: string; value: number } | null;
  runs?: number;
}): ComponentChildren {
  const tone = o.status ? (STATUS_TONE[o.status] ?? "neutral") : null;
  return (
    <>
      <span class="ui-tip-head">
        <span class="ui-tip-mark">idea #{o.id}</span>
        {tone && <span class={`ui-badge ui-badge--${tone}`}>{o.status}</span>}
      </span>
      {o.best && (
        <span class="ui-tip-metric">
          <span class="ui-tip-metric-label">best {o.best.metricName ?? ""}</span>
          <span class="ui-tip-metric-value is-record">{fmtVal(o.best.value)}</span>
        </span>
      )}
      {o.runs != null && (
        <span class="ui-tip-idea"><span class="ui-tip-idea-id">{o.runs} runs</span></span>
      )}
      {o.title && <span class="ui-tip-desc">{excerpt(o.title)}</span>}
    </>
  );
}
