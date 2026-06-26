/**
 * MiniMetricsChart — landing-page-style SVG chart. This is now the ONLY render
 * mode for the metrics chart (the Chart.js canvas path was removed).
 *
 * Dots = experiments, stepped purple line = current best, gold dots = metric
 * improvements (records), dashed vertical drops to x-axis. Colors and ordering
 * follow buildChartData. Click a dot to navigate to its idea.
 *
 * Design notes (see dashboard/DESIGN.md):
 *  - Renders 1:1 in real pixels (viewBox == measured size) so dots and text are
 *    crisp and small, not blown up by a scaled 200-unit viewBox.
 *  - Font sizes are read from the --text-* tokens, so the chart honours the
 *    user's font-size setting instead of hardcoding font-size="9".
 *  - Flat purple tint under the best-line — no gradient.
 *  - Click/hover routed through the shared useEntityNav hook.
 *
 * Step mode (impOnly): the run list collapses to just the milestone
 * (record-setting) experiments up to the last record, then shows ALL
 * experiments after the last record (the in-progress push toward the next
 * record). Long flat stretches between early records collapse to step points.
 *
 * Point size: dot radii are multiplied by a factor driven by chartPointSize
 * (s | m | l → 0.75 | 1 | 1.4).
 */

import { useEffect, useRef, useState } from "preact/hooks";
import { buildChartData } from "../../lib/chart-data";
import { isLowerBetter } from "../../lib/colors";
import { useEntityNav } from "../../lib/hooks";
import { highlightedIdea } from "../../state/signals";
import { fmtMetricName } from "../../lib/format";
import type { Experiment, IdeaNode, SubwayLayout } from "../../lib/types";

const MIN_PX_PER_POINT = 8;
const PAD = { l: 34, r: 12, t: 10, b: 18 };

/** Dot-radius multiplier per chartPointSize step. */
const PT_SIZE_SCALE: Record<"s" | "m" | "l", number> = { s: 0.75, m: 1, l: 1.4 };

// ── helpers ──────────────────────────────────────────────────────────────────

/** Upward-pointing triangle path centred on (cx, cy) with half-size r. */
function trianglePath(cx: number, cy: number, r: number): string {
  const h = r * 1.8;
  const x0 = cx, y0 = cy - h;           // apex
  const x1 = cx - r, y1 = cy + h * 0.4; // bottom-left
  const x2 = cx + r, y2 = cy + h * 0.4; // bottom-right
  return `M${x0.toFixed(1)},${y0.toFixed(1)} L${x1.toFixed(1)},${y1.toFixed(1)} L${x2.toFixed(1)},${y2.toFixed(1)} Z`;
}

function fmtVal(v: number): string {
  if (Math.abs(v) >= 1000) return v.toFixed(0);
  if (Math.abs(v) >= 10)   return v.toFixed(1);
  if (Math.abs(v) >= 1)    return v.toFixed(2);
  return v.toFixed(3);
}

function wrapText(text: string, maxChars: number, maxRows: number): string[] {
  const clean = (text ?? "").replace(/\s+/g, " ").trim().slice(0, maxChars);
  const words = clean.split(" ");
  const rowChars = Math.ceil(maxChars / maxRows);
  const rows: string[] = [];
  for (const word of words) {
    const cur = rows[rows.length - 1] ?? "";
    if (!cur) { rows.push(word); }
    else if (`${cur} ${word}`.length <= rowChars) { rows[rows.length - 1] = `${cur} ${word}`; }
    else if (rows.length < maxRows) { rows.push(word); }
    else { rows[rows.length - 1] = `${cur}...`; break; }
  }
  return rows.slice(0, maxRows);
}

// ── single dot (its own component so it can use the nav hook) ──────────────────

function MiniDot({
  exp, x, y, baselineY, color, isMilestone, fontXs, note, sizeScale, onHoverChange,
}: {
  exp: (Experiment & { _running?: boolean }) | undefined;
  x: number; y: number; baselineY: number;
  color: string; isMilestone: boolean; fontXs: number; note?: string;
  sizeScale: number;
  onHoverChange: (hovered: boolean) => void;
}) {
  const ideaId = exp?.idea_id ?? -1;
  const label = exp?.label ?? (exp?.id != null ? String(exp.id) : undefined);
  const nav = useEntityNav(ideaId, label);

  const anyHighlight = highlightedIdea.value !== null;
  const highlighted = nav.highlighted;
  const faded = anyHighlight && !highlighted;
  const isRunning = exp?._running ?? false;

  // small, crisp radii (1:1 px) — milestones a touch larger, highlight larger
  // still — then scaled by the user's point-size setting.
  const r = (highlighted
    ? (isMilestone ? 4 : 3.2)
    : (isMilestone ? 3.2 : 2.4)) * sizeScale;
  const dotColor = isMilestone ? "var(--yellow)" : color;
  const strokeColor = highlighted ? "var(--text)" : isMilestone ? "var(--bg)" : "none";
  const strokeW = highlighted ? 1.3 : isMilestone ? 1 : 0;

  return (
    <g
      style={exp ? "cursor:pointer;" : undefined}
      onMouseEnter={() => { onHoverChange(true); if (exp) nav.bind.onMouseEnter(); }}
      onMouseLeave={() => { onHoverChange(false); if (exp) nav.bind.onMouseLeave(); }}
      onClick={exp ? (nav.bind.onClick as any) : undefined}
    >
      <line x1={x} x2={x} y1={y} y2={baselineY}
        stroke={isMilestone ? "var(--yellow)" : "var(--border)"}
        stroke-width="1" stroke-dasharray="2 3"
        opacity={faded ? 0.1 : isMilestone ? 0.5 : 0.3} />
      {isRunning ? (
        <path d={trianglePath(x, y, r)} fill="transparent" stroke={dotColor}
          stroke-width={Math.max(strokeW, 1.3)} opacity={faded ? 0.2 : 1} />
      ) : (
        <circle cx={x} cy={y} r={r} fill={dotColor}
          stroke={strokeColor} stroke-width={strokeW} opacity={faded ? 0.2 : 1} />
      )}
      {/* Milestone caption — purple + clickable (sits inside this clickable <g>) */}
      {isMilestone && !faded && note && (
        <text x={x + 5} y={y - 5} fill="var(--purple)" font-size={fontXs}
          font-family="var(--font-mono)" opacity="0.95" style="cursor:pointer;">{note}</text>
      )}
      <circle cx={x} cy={y} r="9" fill="transparent" />
    </g>
  );
}

// ── component ─────────────────────────────────────────────────────────────────

export function MiniMetricsChart({
  metric, experiments, ideas, layout, hiddenStatuses, hideRunning,
  impOnly, colorMode, tags, tagMode, reversed, mean, logScale = false, clip = false,
  pointSize = "m",
}: {
  metric: string;
  experiments: Experiment[];
  ideas: Record<number, IdeaNode>;
  layout: SubwayLayout | null;
  hiddenStatuses: Set<string>;
  hideRunning: boolean;
  impOnly: boolean;
  colorMode: string;
  tags: string[];
  tagMode: string;
  reversed: boolean;
  mean: boolean;
  logScale?: boolean;
  clip?: boolean;
  pointSize?: "s" | "m" | "l";
}) {
  const [hoveredIdx, setHoveredIdx] = useState<number | null>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ w: 560, h: 180 });
  // font sizes read from the --text-* tokens so the chart honours font-size setting
  const [font, setFont] = useState({ xs: 8, sm: 9 });

  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const read = () => {
      setSize({ w: el.clientWidth || 560, h: el.clientHeight || 180 });
      const cs = getComputedStyle(el);
      const px = (name: string, fallback: number) =>
        parseFloat(cs.getPropertyValue(name)) || fallback;
      setFont({ xs: px("--text-xs", 8), sm: px("--text-sm", 9) });
    };
    read();
    const ro = new ResizeObserver(read);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // Build the FULL run (impOnly handled below by our own step-collapse rule, so
  // buildChartData is always called with improvementsOnly=false). This keeps
  // milestone detection consistent and lets us show post-record experiments.
  const chartData = metric ? buildChartData(
    metric, experiments, tags, tagMode, /* improvementsOnly */ false, colorMode,
    ideas, layout, reversed, hiddenStatuses, mean, hideRunning,
  ) : null;

  if (!chartData || chartData.values.length === 0) {
    return (
      <div ref={wrapRef} style={{ width: "100%", height: "100%", display: "flex", alignItems: "center", justifyContent: "center" }}>
        <span style={{ color: "var(--text-faint)", fontSize: "var(--text-sm)" }}>
          {metric ? `no data for ${metric}` : "select a metric"}
        </span>
      </div>
    );
  }

  const lower = isLowerBetter(metric);
  const sizeScale = PT_SIZE_SCALE[pointSize] ?? 1;

  // ── Milestone (record) detection over the FULL ordered list ──────────────────
  // Records are tracked in display order. The "running best" line is stepped to
  // these. When reversed, the chronological order is the reverse of display
  // order, so we scan accordingly and keep runningBest aligned to display index.
  const fullValues = chartData.values;
  const fullN = fullValues.length;
  const fullMilestones = new Set<number>();   // display indices that set a record
  const runningBestFull: number[] = new Array(fullN);
  let lastRecordIdx = -1;                      // display index of the most recent record
  if (reversed) {
    let best: number | null = null;
    for (let i = fullN - 1; i >= 0; i--) {
      const v = fullValues[i];
      const isBetter = isFinite(v) && (best === null || (lower ? v < best : v > best));
      if (isBetter) { best = v; fullMilestones.add(i); lastRecordIdx = Math.max(lastRecordIdx, i); }
      runningBestFull[i] = best ?? v;
    }
  } else {
    let best: number | null = null;
    for (let i = 0; i < fullN; i++) {
      const v = fullValues[i];
      const isBetter = isFinite(v) && (best === null || (lower ? v < best : v > best));
      if (isBetter) { best = v; fullMilestones.add(i); lastRecordIdx = i; }
      runningBestFull[i] = best ?? v;
    }
  }

  // ── Step-mode collapse (#69) ─────────────────────────────────────────────────
  // Keep: every record up to (and including) the last record, PLUS every
  // experiment AFTER the last record (running optimization toward the next
  // record — these are the only non-record points worth showing). Flat
  // stretches between early records collapse to their step points.
  let keepIdx: number[];
  if (impOnly) {
    keepIdx = [];
    for (let i = 0; i < fullN; i++) {
      if (fullMilestones.has(i) || i > lastRecordIdx) keepIdx.push(i);
    }
  } else {
    keepIdx = fullValues.map((_, i) => i);
  }

  // Project the full arrays onto the kept indices.
  const values = keepIdx.map((i) => fullValues[i]);
  const pointColors = keepIdx.map((i) => chartData.pointColors[i]);
  const expData = keepIdx.map((i) => chartData.expData[i]);
  const labels = keepIdx.map((i) => chartData.labels[i]);
  const runningBest = keepIdx.map((i) => runningBestFull[i]);
  const milestones = new Set<number>();
  keepIdx.forEach((srcI, dstI) => { if (fullMilestones.has(srcI)) milestones.add(dstI); });
  const n = values.length;

  // 1:1 pixel coordinate space — viewBox matches measured size, no scaling.
  const containerW = size.w;
  const H = Math.max(120, size.h);
  const minSvgW = PAD.l + n * MIN_PX_PER_POINT + PAD.r;
  const svgW = Math.max(containerW, minSvgW);
  const needsScroll = svgW > containerW + 0.5;
  const plotW = svgW - PAD.l - PAD.r;
  const plotH = H - PAD.t - PAD.b;
  const rightEdge = svgW - PAD.r;
  const baselineY = H - PAD.b;

  // ── Y range ──────────────────────────────────────────────────────────────────
  // Optionally clip outliers (IQR fence) and/or use a log scale, mirroring the
  // toolbar toggles. Log scale only applies when all finite values are positive.
  const finite = values.filter(isFinite);
  let dataMin = Math.min(...finite);
  let dataMax = Math.max(...finite);
  if (clip && finite.length >= 4) {
    const sorted = [...finite].sort((a, b) => a - b);
    const q1 = sorted[Math.floor(sorted.length * 0.25)];
    const q3 = sorted[Math.floor(sorted.length * 0.75)];
    const iqr = q3 - q1;
    if (iqr > 0) {
      const lo = q1 - 1.5 * iqr;
      const hi = q3 + 1.5 * iqr;
      dataMin = Math.max(dataMin, lo);
      dataMax = Math.min(dataMax, hi);
    }
  }
  const useLog = logScale && finite.every((v) => v > 0);
  const tform = useLog ? Math.log10 : (x: number) => x;
  const tMin = tform(dataMin);
  const tMax = tform(dataMax);
  const tPad = (tMax - tMin) * 0.12 || Math.abs(tMax) * 0.05 || 0.01;
  const yMin = tMin - tPad;
  const yMax = tMax + tPad;
  const clampV = (v: number) => Math.min(dataMax, Math.max(dataMin, v));

  const toX = (i: number) => PAD.l + (n <= 1 ? plotW / 2 : (i / (n - 1)) * plotW);
  const toY = (v: number) => PAD.t + (1 - (tform(clampV(v)) - yMin) / (yMax - yMin)) * plotH;

  // Stepped best-score path
  const stepPoints = values.map((_, i) => [toX(i), toY(runningBest[i])] as const);
  let stepPath = `M${stepPoints[0][0].toFixed(1)},${stepPoints[0][1].toFixed(1)}`;
  for (let i = 1; i < stepPoints.length; i++) {
    stepPath += ` H${stepPoints[i][0].toFixed(1)} V${stepPoints[i][1].toFixed(1)}`;
  }
  stepPath += ` H${rightEdge.toFixed(1)}`;

  const nTicks = 4;
  const tickStep = (yMax - yMin) / (nTicks - 1);
  const yTicks = Array.from({ length: nTicks }, (_, i) => yMin + i * tickStep);
  const bestVal = lower ? Math.min(...finite) : Math.max(...finite);

  return (
    <div
      ref={wrapRef}
      style={{ width: "100%", height: "100%", overflowX: needsScroll ? "auto" : "hidden", overflowY: "hidden" }}
    >
      <svg
        width={needsScroll ? svgW : "100%"}
        height={H}
        viewBox={`0 0 ${svgW} ${H}`}
        preserveAspectRatio="xMidYMid meet"
        style={`display:block;${needsScroll ? `min-width:${svgW}px;` : ""}`}
      >
        {/* Y grid + labels */}
        {yTicks.map((ty, i) => {
          const v = useLog ? Math.pow(10, ty) : ty;
          return (
            <g key={i}>
              <line x1={PAD.l} x2={rightEdge} y1={PAD.t + (1 - (ty - yMin) / (yMax - yMin)) * plotH} y2={PAD.t + (1 - (ty - yMin) / (yMax - yMin)) * plotH}
                stroke="var(--border-soft)" stroke-width="1" />
              <text x={PAD.l - 5} y={PAD.t + (1 - (ty - yMin) / (yMax - yMin)) * plotH + font.xs * 0.34}
                fill="var(--text-faint)" font-size={font.xs} text-anchor="end"
                font-family="var(--font-mono)">{fmtVal(v)}</text>
            </g>
          );
        })}

        {/* X baseline */}
        <line x1={PAD.l} x2={rightEdge} y1={baselineY} y2={baselineY} stroke="var(--border)" />

        {/* Best-line flat tint + stroke (no gradient) */}
        <path d={`${stepPath} V${baselineY} H${PAD.l} Z`}
          fill="var(--purple)" fill-opacity={impOnly ? 0.07 : 0.05} />
        <path d={stepPath} fill="none" stroke="var(--purple)"
          stroke-width={impOnly ? 1.8 : 1.4}
          stroke-linejoin="round" stroke-linecap="round" />

        {/* Dots */}
        {values.map((v, i) => {
          const e = expData?.[i] as (Experiment & { _running?: boolean }) | undefined;
          const isMs = milestones.has(i);
          // milestone caption: "idea/exp · excerpt…" (purple, clickable)
          const note = isMs && e
            ? `${e.idea_id}/${e.label ?? e.id} ${(ideas[e.idea_id]?.description ?? "").replace(/\s+/g, " ").trim().slice(0, 22)}`.trim()
            : undefined;
          return (
            <MiniDot
              key={i}
              exp={e}
              x={toX(i)} y={toY(v)} baselineY={baselineY}
              color={pointColors[i] ?? "var(--text-muted)"}
              isMilestone={isMs}
              fontXs={font.xs}
              note={note}
              sizeScale={sizeScale}
              onHoverChange={(h) => setHoveredIdx(h ? i : (cur) => (cur === i ? null : cur))}
            />
          );
        })}

        {/* Hover tooltip — canonical experiment fields (matches timeline/table):
            exp/label · value (bold), idea #N · status, dim title excerpt, ★ record. */}
        {hoveredIdx !== null && (() => {
          const i = hoveredIdx;
          const x = toX(i);
          const y = toY(values[i]);
          const exp = expData?.[i];
          const label = labels?.[i] ?? String(i);
          const idea = exp ? ideas[exp.idea_id] : null;
          const status = idea?.status ?? "";
          const ideaRows = wrapText(idea?.description ?? "", 92, 2);
          const isMs = milestones.has(i);
          const lh = font.xs + 3;
          const headRows = 4; // metric · exp/value · idea/status · best
          const tooltipW = 214;
          const tooltipH = lh * (headRows + ideaRows.length) + 12;
          const tx = Math.max(PAD.l + 2, Math.min(svgW - tooltipW - PAD.r, x + 10));
          const ty = Math.max(PAD.t + 2, y - tooltipH - 8);
          const ny = (k: number) => ty + lh * k;
          return (
            <g pointer-events="none" font-family="var(--font-mono)">
              <line x1={x} y1={y - 4} x2={tx + 10} y2={ty + tooltipH}
                stroke="var(--border)" stroke-width="1" />
              <rect x={tx} y={ty} width={tooltipW} height={tooltipH} rx="3"
                fill="var(--bg)" stroke="var(--purple)" stroke-width="1"
                style="filter:drop-shadow(0 6px 14px rgba(0,0,0,0.45));" />
              <text x={tx + 8} y={ny(1)} fill="var(--text-faint)" font-size={font.xs}
                style="text-transform:uppercase;letter-spacing:0.08em;">
                {metric ? fmtMetricName(metric) : "metric"}
              </text>
              <text x={tx + 8} y={ny(2)} fill="var(--text)" font-size={font.sm} font-weight="700">
                {label} · {fmtVal(values[i])}{isMs ? "  ★ record" : ""}
              </text>
              <text x={tx + 8} y={ny(3)} fill="var(--purple)" font-size={font.xs}>
                idea #{exp?.idea_id}{status ? ` · ${status}` : ""}
              </text>
              <text x={tx + 8} y={ny(4)} fill={isMs ? "var(--yellow)" : "var(--text-muted)"} font-size={font.xs}>
                best so far {fmtVal(runningBest[i])}{isMs ? " · new record" : ""}
              </text>
              {ideaRows.length > 0 && (
                <text x={tx + 8} y={ny(5)} fill="var(--text-muted)" font-size={font.xs}>
                  {ideaRows.map((row, idx) => (
                    <tspan key={idx} x={tx + 8} dy={idx === 0 ? 0 : lh}>{row}</tspan>
                  ))}
                </text>
              )}
            </g>
          );
        })()}

        {/* Bottom labels */}
        <g font-family="var(--font-mono)" font-size={font.xs}>
          <text x={PAD.l} y={H - 5} fill="var(--text-faint)">experiments left → right</text>
          <text x={rightEdge} y={H - 5} text-anchor="end" fill="var(--purple)">
            current best {fmtVal(bestVal)}
          </text>
        </g>
      </svg>
    </div>
  );
}
