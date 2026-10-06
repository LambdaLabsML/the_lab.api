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
 *
 * Time mode (chartPointSize "x"): no dots. X is wall-clock time (finished_at,
 * else started_at/created_at), squeezed to the panel width so the whole
 * history is visible without scrolling. Only one curve is drawn (raw or
 * idea-mean), with time ticks on the x axis: the best-so-far step line, or
 * (timeCurve "avg") a trailing moving average, window from pickAvgWindow.
 */

import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "preact/hooks";
import { colorTheme, colorblindMode } from "../../state/settings";
import { buildChartData } from "../../lib/chart-data";
import { isLowerBetter } from "../../lib/colors";
import { useEntityNav } from "../../lib/hooks";
import { highlightedIdea } from "../../state/signals";
import { fmtMetricName, scoreFormatter } from "../../lib/format";
import { experimentTipContent } from "../ui";
import type { Experiment, IdeaNode, SubwayLayout } from "../../lib/types";

const MIN_PX_PER_POINT = 8;
const PAD = { l: 34, r: 12, t: 10, b: 18 };
const TIP_MARGIN = 8; // min gap from viewport edge (matches shared Tooltip)
const TIP_GAP = 8;    // gap between dot and card

/** Dot-radius multiplier per chartPointSize step. */
const PT_SIZE_SCALE: Record<"s" | "m" | "l" | "x", number> = { s: 0.75, m: 1, l: 1.4, x: 1 };

/**
 * Moving-average window for time mode, sized from the data: the k whose
 * trailing mean best predicts the NEXT result (lowest mean absolute one-step
 * error). Noisy, flat series pick a wide window; trending series a narrow one.
 * k ∈ [3, min(256, n/2)] (at least half the results remain as targets);
 * absolute error so one failed run doesn't dominate. The error curve is treated
 * as unimodal in k and bisected on its slope: O(n·log kMax) via prefix sums.
 */
export function pickAvgWindow(vals: number[]): number {
  const n = vals.length;
  const kMax = Math.min(256, Math.floor(n / 2));
  if (kMax <= 3) return Math.max(1, Math.min(3, n));
  const prefix = [0];
  for (const v of vals) prefix.push(prefix[prefix.length - 1] + v);
  // Score every k on the same targets (i ≥ kMax) so errors are comparable.
  const memo = new Map<number, number>();
  const err = (k: number): number => {
    let e = memo.get(k);
    if (e === undefined) {
      e = 0;
      for (let i = kMax; i < n; i++) e += Math.abs(vals[i] - (prefix[i] - prefix[i - k]) / k);
      memo.set(k, e);
    }
    return e;
  };
  // Bisect for the first k where the error stops decreasing.
  let lo = 3, hi = kMax;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (err(mid) <= err(mid + 1)) hi = mid; else lo = mid + 1;
  }
  return lo;
}

const MIN = 60_000, HOUR = 60 * MIN, DAY = 24 * HOUR;
const TIME_STEPS = [
  MIN, 5 * MIN, 15 * MIN, 30 * MIN, HOUR, 3 * HOUR, 6 * HOUR, 12 * HOUR,
  DAY, 2 * DAY, 7 * DAY, 14 * DAY, 30 * DAY, 90 * DAY, 365 * DAY,
];

// ── helpers ──────────────────────────────────────────────────────────────────

/** Experiment timestamp (ms) for time mode. */
function expTime(e: Experiment | undefined): number {
  if (!e) return NaN;
  const iso = e.finished_at || e.started_at || e.created_at;
  return iso ? Date.parse(iso) : NaN;
}

/** Evenly spaced, locally aligned time ticks: at most `maxTicks` over [t0, t1]. */
function timeTicks(t0: number, t1: number, maxTicks: number): { t: number; label: string }[] {
  const span = Math.max(t1 - t0, 1);
  const step = TIME_STEPS.find((s) => span / s <= maxTicks) ?? TIME_STEPS[TIME_STEPS.length - 1];
  // Align to local midnight/hour boundaries, not UTC.
  const tz = -new Date(t0).getTimezoneOffset() * MIN;
  const out: { t: number; label: string }[] = [];
  let prevDay = "";
  for (let t = Math.ceil((t0 + tz) / step) * step - tz; t <= t1; t += step) {
    const d = new Date(t);
    const day = d.toLocaleDateString([], { month: "short", day: "numeric" });
    const hm = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
    const label = step >= DAY || hm === "00:00" ? day : day !== prevDay ? `${day} ${hm}` : hm;
    out.push({ t, label });
    prevDay = day;
  }
  return out;
}

/** Upward-pointing triangle path centred on (cx, cy) with half-size r. */
function trianglePath(cx: number, cy: number, r: number): string {
  const h = r * 1.8;
  const x0 = cx, y0 = cy - h;           // apex
  const x1 = cx - r, y1 = cy + h * 0.4; // bottom-left
  const x2 = cx + r, y2 = cy + h * 0.4; // bottom-right
  return `M${x0.toFixed(1)},${y0.toFixed(1)} L${x1.toFixed(1)},${y1.toFixed(1)} L${x2.toFixed(1)},${y2.toFixed(1)} Z`;
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
  pointSize = "m", timeCurve = "best",
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
  pointSize?: "s" | "m" | "l" | "x";
  /** Time mode only: best-so-far step line, or trailing moving average. */
  timeCurve?: "best" | "avg";
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
  // Memoized: the parent re-renders on unrelated changes (hover, cost/progress
  // ticks, size); hiddenStatuses is a fresh Set each render, so key on its
  // contents. Theme/colorblind feed resolved CSS colours, so key on them too.
  const hsKey = [...hiddenStatuses].sort().join(",");
  const themeKey = `${colorTheme.value}|${colorblindMode.value}`;
  const chartData = useMemo(() => metric ? buildChartData(
    metric, experiments, tags, tagMode, /* improvementsOnly */ false, colorMode,
    ideas, layout, reversed, hiddenStatuses, mean, hideRunning,
  ) : null,
  // eslint-disable-next-line react-hooks/exhaustive-deps
  [metric, experiments, tags, tagMode, colorMode, ideas, layout, reversed, hsKey, mean, hideRunning, themeKey]);

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
  const timeMode = pointSize === "x";

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
  if (impOnly && !timeMode) {
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

  // Time mode: chronological (t, y) points of ONE curve — best-so-far, or the
  // trailing moving average — and the y range fits that curve rather than
  // every value. Works on raw experiments and idea means alike (a mean point
  // sits at its idea's latest finish). Running experiments carry partial
  // progress, so they never enter the curve; they only extend the axis to "now".
  const avgMode = timeMode && timeCurve === "avg";
  const timed: { t: number; y: number }[] = [];
  let avgWindow = 0;
  const isRunning = (i: number) => !!(expData?.[i] as { _running?: boolean } | undefined)?._running;
  const anyRunning = timeMode && values.some((_, i) => isRunning(i));
  if (timeMode) {
    const pts = values
      .map((v, i) => ({ t: isRunning(i) ? NaN : expTime(expData?.[i] as Experiment | undefined), v }))
      .filter((p) => isFinite(p.t) && isFinite(p.v))
      .sort((a, b) => a.t - b.t);
    if (avgMode) avgWindow = pickAvgWindow(pts.map((p) => p.v));
    let windowSum = 0;
    pts.forEach((p, k) => {
      if (avgMode) {
        windowSum += p.v;
        if (k >= avgWindow) windowSum -= pts[k - avgWindow].v;
        timed.push({ t: p.t, y: windowSum / Math.min(k + 1, avgWindow) });
      } else {
        const prev = timed.length ? timed[timed.length - 1].y : null;
        timed.push({ t: p.t, y: prev === null || (lower ? p.v < prev : p.v > prev) ? p.v : prev });
      }
    });
    if (timed.length === 0) {
      return (
        <div ref={wrapRef} style={{ width: "100%", height: "100%", display: "flex", alignItems: "center", justifyContent: "center" }}>
          <span style={{ color: "var(--text-faint)", fontSize: "var(--text-sm)" }}>no timestamps for {metric}</span>
        </div>
      );
    }
  }

  // ── Y range ──────────────────────────────────────────────────────────────────
  // Optionally clip outliers (IQR fence) and/or use a log scale, mirroring the
  // toolbar toggles. Log scale only applies when all finite values are positive.
  const finite = timeMode ? timed.map((p) => p.y) : values.filter(isFinite);
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

  const nTicks = 4;
  const tickStep = (yMax - yMin) / (nTicks - 1);
  const yTicks = Array.from({ length: nTicks }, (_, i) => yMin + i * tickStep);
  // Bottom-right readout: the best value, or the latest moving average.
  const bestVal = avgMode ? timed[timed.length - 1].y : lower ? Math.min(...finite) : Math.max(...finite);
  // One notation for the axis + best label, chosen from what is on screen.
  const yTickVals = yTicks.map((ty) => (useLog ? Math.pow(10, ty) : ty));
  const fmtVal = scoreFormatter([...yTickVals, bestVal]);
  const yTickLabels = yTickVals.map(fmtVal);

  // 1:1 pixel coordinate space — viewBox matches measured size, no scaling.
  // Left pad grows to fit the widest tick label (e.g. "-3.84e-4").
  const padL = Math.max(PAD.l, Math.ceil(Math.max(...yTickLabels.map((l) => l.length)) * font.xs * 0.62) + 8);
  const containerW = size.w;
  const H = Math.max(120, size.h);
  // Time mode never scrolls: the full time range is squeezed into the panel.
  const minSvgW = timeMode ? 0 : padL + n * MIN_PX_PER_POINT + PAD.r;
  const svgW = Math.max(containerW, minSvgW);
  const needsScroll = svgW > containerW + 0.5;
  const padB = timeMode ? PAD.b + font.xs + 6 : PAD.b; // extra row for time ticks
  const plotW = svgW - padL - PAD.r;
  const plotH = H - PAD.t - padB;
  const rightEdge = svgW - PAD.r;
  const baselineY = H - padB;

  const toX = (i: number) => padL + (n <= 1 ? plotW / 2 : (i / (n - 1)) * plotW);
  const toY = (v: number) => PAD.t + (1 - (tform(clampV(v)) - yMin) / (yMax - yMin)) * plotH;

  const t0 = timed.length ? timed[0].t : 0;
  const t1 = timed.length ? Math.max(timed[timed.length - 1].t, anyRunning ? Date.now() : -Infinity) : 1;
  const toXT = (t: number) => {
    const f = t1 > t0 ? (t - t0) / (t1 - t0) : 0.5;
    return reversed ? rightEdge - f * plotW : padL + f * plotW;
  };

  // Stepped best-score path: (x, running best) pairs, drawn as H/V segments.
  let stepPoints: (readonly [number, number])[];
  if (timeMode) {
    stepPoints = timed.map((p) => [toXT(p.t), toY(p.y)] as const);
  } else {
    stepPoints = values.map((_, i) => [toX(i), toY(runningBest[i])] as const);
  }
  const stepStartX = stepPoints.length ? stepPoints[0][0] : padL;
  const stepEndX = timeMode ? (reversed ? padL : rightEdge) : rightEdge;
  let stepPath = stepPoints.length ? `M${stepPoints[0][0].toFixed(1)},${stepPoints[0][1].toFixed(1)}` : "";
  for (let i = 1; i < stepPoints.length; i++) {
    stepPath += avgMode
      ? ` L${stepPoints[i][0].toFixed(1)},${stepPoints[i][1].toFixed(1)}`  // average: straight segments
      : ` H${stepPoints[i][0].toFixed(1)} V${stepPoints[i][1].toFixed(1)}`;
  }
  if (stepPath) stepPath += ` H${stepEndX.toFixed(1)}`;

  // Time-axis ticks, spaced so labels don't collide (~90px each).
  const xTicks = timeMode && timed.length
    ? timeTicks(t0, t1, Math.max(2, Math.floor(plotW / 90)))
        .map((tk) => ({ ...tk, x: toXT(tk.t) }))
        .filter((tk) => {
          const half = (tk.label.length * font.xs * 0.62) / 2;
          return tk.x - half >= 0 && tk.x + half <= svgW;
        })
    : [];


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
          const gy = PAD.t + (1 - (ty - yMin) / (yMax - yMin)) * plotH;
          return (
            <g key={i}>
              <line x1={padL} x2={rightEdge} y1={gy} y2={gy}
                stroke="var(--border-soft)" stroke-width="1" />
              <text x={padL - 5} y={gy + font.xs * 0.34}
                fill="var(--text-faint)" font-size={font.xs} text-anchor="end"
                font-family="var(--font-mono)">{yTickLabels[i]}</text>
            </g>
          );
        })}

        {/* X baseline */}
        <line x1={padL} x2={rightEdge} y1={baselineY} y2={baselineY} stroke="var(--border)" />

        {/* Time ticks (time mode) */}
        {xTicks.map((tk) => (
          <g key={tk.t}>
            <line x1={tk.x} x2={tk.x} y1={baselineY} y2={baselineY + 3} stroke="var(--border)" />
            <text x={tk.x} y={baselineY + 4 + font.xs} text-anchor="middle"
              fill="var(--text-faint)" font-size={font.xs} font-family="var(--font-mono)">{tk.label}</text>
          </g>
        ))}

        {/* Best-line flat tint + stroke (no gradient) */}
        {stepPath && <path d={`${stepPath} V${baselineY} H${stepStartX.toFixed(1)} Z`}
          fill="var(--purple)" fill-opacity={impOnly ? 0.07 : 0.05} />}
        {stepPath && <path d={stepPath} fill="none" stroke="var(--purple)"
          stroke-width={impOnly || timeMode ? 1.8 : 1.4}
          stroke-linejoin="round" stroke-linecap="round" />}

        {/* Dots (none in time mode) */}
        {!timeMode && values.map((v, i) => {
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
          <text x={padL} y={H - 5} fill="var(--text-faint)">
            {timeMode ? (reversed ? "← time" : "time →") : "experiments left → right"}
          </text>
          <text x={rightEdge} y={H - 5} text-anchor="end" fill="var(--purple)">
            {avgMode ? `avg of last ${avgWindow}` : "current best"} {fmtVal(bestVal)}
          </text>
        </g>
      </svg>

      {/* Shared HTML hover card — visually identical to timeline/table tooltips */}
      {hovered !== null && !timeMode && (() => {
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
