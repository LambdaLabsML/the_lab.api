/**
 * Shared hover-card content for experiments and ideas — the single source of
 * truth for what an entity tooltip shows, so the chart, timeline, tables, agent
 * bar, etc. all render the SAME fields/shape. Drop the return value into a
 * <Tooltip content={…}> or a positioned `.ui-tip` card. See dashboard/DESIGN.md.
 */
import type { ComponentChildren } from "preact";

function fmtVal(v: number): string {
  if (!isFinite(v)) return String(v);
  if (Math.abs(v) >= 1000) return v.toFixed(0);
  if (Math.abs(v) >= 10) return v.toFixed(1);
  if (Math.abs(v) >= 1) return v.toFixed(2);
  return v.toFixed(3);
}

function excerpt(s: string | undefined, n = 88): string {
  return (s ?? "").replace(/\s+/g, " ").trim().slice(0, n);
}

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
  return (
    <>
      <span class="ui-tip-title">
        exp/{o.label}{o.record ? " · ★ record" : ""}{o.running ? " · running" : ""}
      </span>
      {o.value != null && Number.isFinite(o.value as number) && (
        <span class="ui-tip-row"><span>{o.metricName ?? "value"}</span><b>{fmtVal(o.value as number)}</b></span>
      )}
      <span class="ui-tip-row"><span>idea</span><b>#{o.ideaId}{o.status ? ` · ${o.status}` : ""}</b></span>
      {o.ideaTitle && <span class="ui-tip-dim">{excerpt(o.ideaTitle)}</span>}
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
  return (
    <>
      <span class="ui-tip-title">idea #{o.id}{o.status ? ` · ${o.status}` : ""}</span>
      {o.title && <span class="ui-tip-dim">{excerpt(o.title)}</span>}
      {o.best && (
        <span class="ui-tip-row"><span>best {o.best.metricName ?? ""}</span><b>{fmtVal(o.best.value)}</b></span>
      )}
      {o.runs != null && <span class="ui-tip-row"><span>runs</span><b>{o.runs}</b></span>}
    </>
  );
}
