/**
 * AgentSpendChart — consolidated "what was spent WHEN" for all agents.
 * Stacked per-agent bars per day (per hour for the 24h period) with a real
 * date axis, a period filter, a cost⇄tokens toggle, and a legend whose pills
 * toggle agents in/out. Hovering a bar floats the day's per-agent breakdown
 * (FloatCard). Replaces the axis-less cumulative sparklines.
 *
 * Attribution caveat (shown in the footnote): live agents contribute their
 * per-bucket spend deltas; completed agents book their full cost on the day
 * they finished (their record has a single timestamp).
 */
import { useState } from "preact/hooks";
import { FloatCard, type FloatAnchor } from "./ui";
import { agentColor } from "../lib/colors";

export interface CostEntry {
  live?: boolean;
  cost?: number;
  ts?: string;
  inTok?: number;
  outTok?: number;
  readings?: { ts: string; cost: number; inTok: number; outTok: number }[];
}

const PERIODS: { id: string; label: string; hours: number | null }[] = [
  { id: "24h", label: "24h", hours: 24 },
  { id: "7d", label: "7d", hours: 168 },
  { id: "14d", label: "14d", hours: 336 },
  { id: "all", label: "all", hours: null },
];

const W = 760;
const H = 150;
const PLOT_H = 104;
const TOP = 14;
const MAX_AGENTS = 8; // stacked series; the rest lump into "others"

type Series = { ts: number; v: number }[];

function readingsOf(e: CostEntry, mode: "cost" | "tokens"): Series {
  const rs = e.readings ?? (e.cost != null && e.ts
    ? [{ ts: e.ts, cost: e.cost, inTok: e.inTok ?? 0, outTok: e.outTok ?? 0 }]
    : []);
  return rs.map((r) => ({
    ts: Date.parse(r.ts),
    v: mode === "cost" ? r.cost : (r.inTok ?? 0) + (r.outTok ?? 0),
  })).filter((r) => Number.isFinite(r.ts)).sort((a, b) => a.ts - b.ts);
}

/** Spend within [start, end) from a cumulative series. */
function deltaIn(series: Series, start: number, end: number): number {
  let before = 0;
  let last = 0;
  let sawAny = false;
  for (const r of series) {
    if (r.ts < start) before = r.v;
    if (r.ts < end) { last = r.v; sawAny = r.ts >= start || sawAny; }
  }
  return sawAny ? Math.max(0, last - before) : 0;
}

function fmtVal(v: number, mode: "cost" | "tokens"): string {
  if (mode === "cost") return `$${v >= 100 ? v.toFixed(0) : v.toFixed(2)}`;
  return v >= 1_000_000 ? `${(v / 1_000_000).toFixed(1)}M` : v >= 1000 ? `${(v / 1000).toFixed(0)}k` : String(Math.round(v));
}

export function AgentSpendChart({ costs }: { costs: Record<string, CostEntry> }) {
  const [period, setPeriod] = useState("7d");
  const [mode, setMode] = useState<"cost" | "tokens">("cost");
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const [hover, setHover] = useState<{ anchor: FloatAnchor; i: number } | null>(null);

  const hours = PERIODS.find((p) => p.id === period)?.hours ?? null;
  const hourly = period === "24h";
  const bucketMs = hourly ? 3_600_000 : 86_400_000;
  const now = Date.now();

  // Bucket range: fixed window for periods; from earliest data for "all".
  const series = Object.entries(costs).map(([id, e]) => ({ id, s: readingsOf(e, mode) }))
    .filter((x) => x.s.length > 0);
  let start: number;
  if (hours != null) {
    start = now - hours * 3_600_000;
  } else {
    const earliest = Math.min(...series.map((x) => x.s[0].ts), now);
    start = earliest;
  }
  // Align to bucket boundaries (local days / whole hours).
  const align = (t: number) => {
    const d = new Date(t);
    if (hourly) d.setMinutes(0, 0, 0);
    else d.setHours(0, 0, 0, 0);
    return d.getTime();
  };
  start = align(start);
  const nBuckets = Math.min(90, Math.ceil((now - start) / bucketMs) + 1);
  start = now - (nBuckets - 1) * bucketMs;
  start = align(start);

  // Per-agent totals in the window (for legend + top-N cut).
  const totals = series
    .map(({ id, s }) => ({ id, total: deltaIn(s, start, now + bucketMs) }))
    .filter((x) => x.total > 0)
    .sort((a, b) => b.total - a.total);
  const topIds = totals.slice(0, MAX_AGENTS).map((x) => x.id);
  const stackIds = [...topIds, ...(totals.length > MAX_AGENTS ? ["…others"] : [])];
  const active = (id: string) => !hidden.has(id);

  // Stacked buckets.
  const buckets = Array.from({ length: nBuckets }, (_, i) => {
    const b0 = start + i * bucketMs;
    const b1 = b0 + bucketMs;
    const per: Record<string, number> = {};
    for (const { id, s } of series) {
      const key = topIds.includes(id) ? id : "…others";
      if (!active(key)) continue;
      const d = deltaIn(s, b0, b1);
      if (d > 0) per[key] = (per[key] ?? 0) + d;
    }
    const total = Object.values(per).reduce((a, b) => a + b, 0);
    return { t0: b0, per, total };
  });
  const yMax = Math.max(...buckets.map((b) => b.total), 1e-9);
  const periodTotal = buckets.reduce((s, b) => s + b.total, 0);

  const bw = W / nBuckets;
  const fmtTick = (t: number) => {
    const d = new Date(t);
    return hourly
      ? `${String(d.getHours()).padStart(2, "0")}:00`
      : `${d.getMonth() + 1}/${d.getDate()}`;
  };
  const tickEvery = Math.max(1, Math.ceil(nBuckets / 8));

  const barColor = (id: string) => (id === "…others" ? "var(--text-faint)" : agentColor(id));

  return (
    <div class="spend-chart">
      <div class="spend-chart-head">
        <span class="ui-eyebrow">Spend over time · {mode}</span>
        <span class="spend-chart-total">{fmtVal(periodTotal, mode)}</span>
        <span class="spend-chart-controls">
          <span class="review-disclosure-switch" role="group" aria-label="Mode">
            {(["cost", "tokens"] as const).map((m) => (
              <button key={m} type="button" class={`rds-opt${mode === m ? " is-on" : ""}`}
                onClick={() => setMode(m)}>{m === "cost" ? "$" : "tok"}</button>
            ))}
          </span>
          <span class="review-disclosure-switch" role="group" aria-label="Period">
            {PERIODS.map((p) => (
              <button key={p.id} type="button" class={`rds-opt${period === p.id ? " is-on" : ""}`}
                onClick={() => setPeriod(p.id)}>{p.label}</button>
            ))}
          </span>
        </span>
      </div>

      <svg class="spend-chart-svg" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none">
        {buckets.map((b, i) => {
          let y = TOP + PLOT_H;
          const segs = stackIds.filter((id) => b.per[id]).map((id) => {
            const h = (b.per[id]! / yMax) * PLOT_H;
            y -= h;
            return <rect key={id} x={i * bw + 1} y={y} width={Math.max(1, bw - 2)} height={h}
              fill={barColor(id)} opacity="0.85" />;
          });
          return <g key={b.t0}>{segs}</g>;
        })}
        {/* x ticks */}
        {buckets.map((b, i) => (i % tickEvery === 0
          ? <text key={`t${b.t0}`} x={i * bw + bw / 2} y={H - 4} text-anchor="middle"
              class="spend-chart-tick">{fmtTick(b.t0)}</text>
          : null))}
        {/* y max */}
        <text x={W - 2} y={TOP - 3} text-anchor="end" class="spend-chart-tick">{fmtVal(yMax, mode)}</text>
        {/* hover targets (full-height columns) */}
        {buckets.map((b, i) => (
          <rect key={`h${b.t0}`} x={i * bw} y={0} width={bw} height={H} fill="transparent"
            onMouseEnter={(e) => {
              const r = (e.currentTarget as SVGRectElement).getBoundingClientRect();
              setHover({ anchor: { top: r.top, bottom: r.bottom, right: r.right }, i });
            }}
            onMouseLeave={() => setHover(null)} />
        ))}
      </svg>

      {/* legend: identity-colored (data series), click toggles */}
      <div class="spend-chart-legend">
        {totals.slice(0, MAX_AGENTS).map(({ id, total }) => (
          <button key={id} type="button"
            class={`spend-chart-key${active(id) ? "" : " is-off"}`}
            onClick={() => setHidden((prev) => {
              const n = new Set(prev);
              if (n.has(id)) n.delete(id); else n.add(id);
              return n;
            })}>
            <span class="spend-chart-swatch" style={{ background: agentColor(id) }} />
            {id} <span class="spend-chart-key-val">{fmtVal(total, mode)}</span>
          </button>
        ))}
        {totals.length > MAX_AGENTS && (
          <span class="spend-chart-key is-static">
            <span class="spend-chart-swatch" style={{ background: "var(--text-faint)" }} />
            +{totals.length - MAX_AGENTS} others
          </span>
        )}
        <span class="spend-chart-note">completed agents book on their finish day</span>
      </div>

      {hover && buckets[hover.i] && (
        <FloatCard anchor={hover.anchor}>
          <div class="msg-head msg-preview-head">
            <span class="msg-preview-fn">{fmtTick(buckets[hover.i].t0)}{hourly ? "" : ` · ${new Date(buckets[hover.i].t0).toLocaleDateString(undefined, { weekday: "short" })}`}</span>
            <span class="msg-time">{fmtVal(buckets[hover.i].total, mode)}</span>
          </div>
          <div class="msg-preview-rows">
            {stackIds.filter((id) => buckets[hover.i].per[id]).map((id) => (
              <div class="msg-preview-row" key={id}>
                <span class="spend-chart-swatch" style={{ background: barColor(id) }} />
                <span class="msg-preview-label" style={{ textTransform: "none", flexBasis: "6em" }}>{id}</span>
                <span class="msg-preview-value">{fmtVal(buckets[hover.i].per[id]!, mode)}</span>
              </div>
            ))}
            {buckets[hover.i].total === 0 && <div class="msg-preview-row"><span class="msg-preview-label">spend</span><span class="msg-preview-value">none</span></div>}
          </div>
        </FloatCard>
      )}
    </div>
  );
}
