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
 *  - Hover card is the shared `.ui-tip` HTML overlay rendering
 *    `experimentTipContent`, positioned by the dot's getBoundingClientRect —
 *    visually identical to the timeline/table tooltips.
 *
 * Step mode (impOnly): the run collapses to ONLY the record-setting experiments
 * (the steps) up to and INCLUDING the most recent record, then shows EVERY
 * experiment after that last record (the current "dry streak") as normal dots.
 * Flat stretches between early records collapse to their step points.
 *
 * Point size: dot radii are multiplied by a factor driven by chartPointSize
 * (s | m | l → 0.75 | 1 | 1.4).
 */

import { useEffect, useLayoutEffect, useRef, useState } from "preact/hooks";
import { buildChartData } from "../../lib/chart-data";
import { isLowerBetter } from "../../lib/colors";
import { useEntityNav } from "../../lib/hooks";
import { highlightedIdea } from "../../state/signals";
import { fmtMetricName } from "../../lib/format";
import { experimentTipContent } from "../ui";
import type { Experiment, IdeaNode, SubwayLayout } from "../../lib/types";

const MIN_PX_PER_POINT = 8;
const PAD = { l: 34, r: 12, t: 10, b: 18 };
const TIP_MARGIN = 8; // min gap from viewport edge (matches shared Tooltip)
const TIP_GAP = 8;    // gap between dot and card

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

// ── single dot (its own component so it can use the nav hook) ──────────────────

function MiniDot({
  exp, x, y, baselineY, color, isMilestone, fontXs, note, sizeScale, onHoverChange,
}: {
  exp: (Experiment & { _running?: boolean }) | undefined;
  x: number; y: number; baselineY: number;
  color: string; isMilestone: boolean; fontXs: number; note?: string;
  sizeScale: number;
  onHoverChange: (hovered: boolean, rect: DOMRect | null) => void;
}) {
  const groupRef = useRef<SVGGElement>(null);
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
      ref={groupRef}
      style={exp ? "cursor:pointer;" : undefined}
      onMouseEnter={() => { onHoverChange(true, groupRef.current?.getBoundingClientRect() ?? null); if (exp) nav.bind.onMouseEnter(); }}
      onMouseLeave={() => { onHoverChange(false, null); if (exp) nav.bind.onMouseLeave(); }}
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

// ── HTML overlay tooltip (shared .ui-tip card) ─────────────────────────────────

/**
 * Renders the shared `.ui-tip` card at a hovered dot's screen rect, clamped to
 * the viewport the same way the shared <Tooltip> does (two-pass measure, flip
 * top↔bottom, nudge horizontally). Keeps the chart tooltip pixel-identical to
 * the timeline/table tooltips.
 */
function MiniTooltip({ anchor, children }: { anchor: DOMRect; children: any }) {
  const cardRef = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState<{ left: number; top: number } | null>(null);

  useLayoutEffect(() => {
    if (!cardRef.current) return;
    const card = cardRef.current.getBoundingClientRect();
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const cx = anchor.left + anchor.width / 2;
    // Prefer above the dot; flip below if it would clip.
    let top = anchor.top - card.height - TIP_GAP;
    if (top < TIP_MARGIN) top = anchor.bottom + TIP_GAP;
    top = Math.max(TIP_MARGIN, Math.min(top, vh - card.height - TIP_MARGIN));
    let left = cx - card.width / 2;
    left = Math.max(TIP_MARGIN, Math.min(left, vw - card.width - TIP_MARGIN));
    setPos({ left, top });
  }, [anchor]);

  return (
    <div
      ref={cardRef}
      class="ui-tip"
      role="tooltip"
      style={{
        left: `${pos ? pos.left : anchor.left}px`,
        top: `${pos ? pos.top : anchor.top}px`,
        visibility: pos ? "visible" : "hidden",
      }}
    >
      {children}
    </div>
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
  // hovered dot index + its screen rect (for the HTML overlay tooltip)
  const [hovered, setHovered] = useState<{ idx: number; rect: DOMRect } | null>(null);
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
  // `chartData` is already in display order (buildChartData reverses when
  // `reversed`). We scan in CHRONOLOGICAL order so records and the running-best
  // are correct, then keep everything indexed by DISPLAY position.
  //
  //   chrono i = reversed ? (n-1 - displayIdx) : displayIdx
  //
  // The "most recent record" is the last record in chronological order; the
  // "dry streak" is every experiment chronologically AFTER it. We translate both
  // back to display indices so the kept set is direction-agnostic.
  const fullValues = chartData.values;
  const fullN = fullValues.length;
  const fullMilestones = new Set<number>();          // DISPLAY indices that set a record
  const runningBestFull: number[] = new Array(fullN); // by DISPLAY index
  const toDisplay = (chrono: number) => (reversed ? fullN - 1 - chrono : chrono);
  let lastRecordChrono = -1;                          // chronological index of most recent record
  {
    let best: number | null = null;
    for (let c = 0; c < fullN; c++) {
      const d = toDisplay(c);
      const v = fullValues[d];
      const isBetter = isFinite(v) && (best === null || (lower ? v < best : v > best));
      if (isBetter) { best = v; fullMilestones.add(d); lastRecordChrono = c; }
      runningBestFull[d] = best ?? v;
    }
  }

  // ── Step-mode collapse (#79) ─────────────────────────────────────────────────
  // KEEP rule (display index d, chronological index c = reversed ? n-1-d : d):
  //   keep(d) = isRecord(d)  OR  c > lastRecordChrono
  // i.e. every record up to & including the most recent record (steps; flat
  // stretches between early records collapse), PLUS every experiment after the
  // most recent record (the current dry streak) as normal dots. Works in both
  // time directions because the dry-streak test is chronological.
  let keepIdx: number[];
  if (impOnly) {
    keepIdx = [];
    for (let d = 0; d < fullN; d++) {
      const c = reversed ? fullN - 1 - d : d;
      if (fullMilestones.has(d) || c > lastRecordChrono) keepIdx.push(d);
    }
  } else {
    keepIdx = Array.from({ length: fullN }, (_, d) => d);
  }

  // Project the full arrays onto the kept indices (preserving display order).
  const values = keepIdx.map((d) => fullValues[d]);
  const pointColors = keepIdx.map((d) => chartData.pointColors[d]);
  const expData = keepIdx.map((d) => chartData.expData[d]);
  const labels = keepIdx.map((d) => chartData.labels[d]);
  const runningBest = keepIdx.map((d) => runningBestFull[d]);
  const milestones = new Set<number>();
  keepIdx.forEach((srcD, dstI) => { if (fullMilestones.has(srcD)) milestones.add(dstI); });
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
          const gy = PAD.t + (1 - (ty - yMin) / (yMax - yMin)) * plotH;
          return (
            <g key={i}>
              <line x1={PAD.l} x2={rightEdge} y1={gy} y2={gy}
                stroke="var(--border-soft)" stroke-width="1" />
              <text x={PAD.l - 5} y={gy + font.xs * 0.34}
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
              onHoverChange={(h, rect) =>
                setHovered(h && rect ? { idx: i, rect } : (cur) => (cur?.idx === i ? null : cur))
              }
            />
          );
        })}

        {/* Bottom labels */}
        <g font-family="var(--font-mono)" font-size={font.xs}>
          <text x={PAD.l} y={H - 5} fill="var(--text-faint)">experiments left → right</text>
          <text x={rightEdge} y={H - 5} text-anchor="end" fill="var(--purple)">
            current best {fmtVal(bestVal)}
          </text>
        </g>
      </svg>

      {/* Shared HTML hover card — visually identical to timeline/table tooltips */}
      {hovered !== null && (() => {
        const i = hovered.idx;
        if (i >= n) return null;
        const exp = expData?.[i];
        if (!exp) return null;
        const label = (exp.label ?? exp.id ?? labels?.[i] ?? String(i)) as string;
        const idea = ideas[exp.idea_id];
        return (
          <MiniTooltip anchor={hovered.rect}>
            {experimentTipContent({
              label: String(label),
              ideaId: exp.idea_id,
              ideaTitle: idea?.description,
              status: idea?.status,
              metricName: metric ? fmtMetricName(metric) : undefined,
              value: values[i],
              record: milestones.has(i),
              running: (exp as any)._running ?? false,
            })}
          </MiniTooltip>
        );
      })()}
    </div>
  );
}
