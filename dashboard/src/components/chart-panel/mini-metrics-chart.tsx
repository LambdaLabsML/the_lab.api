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
 * X axis (xAxis): experiment order, experiments grouped by idea, finish time
 * (finished_at, else started_at/created_at), or cumulative experiment runtime
 * in hours. Every axis gets the same width as experiment order (points × the
 * view's spacing, scrolling past the panel); time and hours place the points
 * proportionally within it.
 *
 * View (chartView): s / m / l draw dots, from tiny tightly packed dots (s) to
 * roomy (l) — see PX_PER_POINT / PT_SIZE_SCALE. x draws no dots and fits any
 * axis to the panel width, with a hover readout on the line.
 *
 * The purple line (timeCurve) is the best-so-far step line, or "avg": a
 * trailing moving average (window from pickAvgWindow) — in every view.
 *
 * Log Y ranges over the positive values; values <= 0 are excluded (× marks on
 * the x axis, skipped by the purple line).
 */

import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "preact/hooks";
import { colorTheme, colorblindMode, chartPointLabels } from "../../state/settings";
import { buildChartData } from "../../lib/chart-data";
import { isLowerBetter } from "../../lib/colors";
import { useEntityNav } from "../../lib/hooks";
import { plotHighlightState } from "../../state/review-filter";
import { runningProgress } from "../../state/signals";
import { fmtMetricName, scoreFormatter } from "../../lib/format";
import { experimentTipContent } from "../ui";
import type { Experiment, IdeaNode, SubwayLayout } from "../../lib/types";

// Horizontal room per experiment (the plot scrolls past the panel width) and
// dot radius multiplier per size: s packs the run tight with tiny dots, l is
// the roomy view. x draws no dots and fits the full width.
const PX_PER_POINT: Record<"s" | "m" | "l", number> = { s: 1.2, m: 2.5, l: 8 };
const PAD = { l: 34, r: 12, t: 10, b: 18 };
const TIP_MARGIN = 8; // min gap from viewport edge (matches shared Tooltip)
const TIP_GAP = 8;    // gap between dot and card

/** Dot-radius multiplier per view. */
const PT_SIZE_SCALE: Record<"s" | "m" | "l", number> = { s: 0.32, m: 0.35, l: 0.75 };

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

/** A running experiment: a small yellow "cake" — a pie slice that grows
 *  clockwise with pct_complete on a faint disc; without progress, a yellow
 *  dot fading in and out. */
function RunningMarker({ x, y, r, pct, faded }: { x: number; y: number; r: number; pct?: number; faded: boolean }) {
  if (typeof pct !== "number" || !Number.isFinite(pct)) {
    return <circle class="mini-run-pulse" cx={x} cy={y} r={r * 0.75} fill="var(--yellow)" opacity={faded ? 0.2 : undefined} />;
  }
  const frac = Math.min(1, Math.max(0, pct / 100));
  let slice: preact.JSX.Element | null = null;
  if (frac >= 0.999) {
    slice = <circle cx={x} cy={y} r={r} fill="var(--yellow)" />;
  } else if (frac > 0) {
    const a = 2 * Math.PI * frac - Math.PI / 2;
    const ex = x + r * Math.cos(a), ey = y + r * Math.sin(a);
    slice = <path d={`M${x},${y} L${x},${y - r} A${r},${r} 0 ${frac > 0.5 ? 1 : 0} 1 ${ex.toFixed(2)},${ey.toFixed(2)} Z`} fill="var(--yellow)" />;
  }
  return (
    <g opacity={faded ? 0.2 : 1}>
      <circle cx={x} cy={y} r={r} fill="var(--yellow)" fill-opacity="0.22" stroke="var(--yellow)" stroke-opacity="0.55" stroke-width="0.8" />
      {slice}
    </g>
  );
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
  exp, x, y, baselineY, color, isMilestone, sizeScale, onHoverChange,
  excluded = false,
}: {
  exp: (Experiment & { _running?: boolean }) | undefined;
  x: number; y: number; baselineY: number;
  color: string; isMilestone: boolean;
  sizeScale: number;
  /** Not on the scale (log Y and value <= 0): a small × on the x axis. */
  excluded?: boolean;
  onHoverChange: (hovered: boolean, rect: DOMRect | null) => void;
}) {
  const groupRef = useRef<SVGGElement>(null);
  const ideaId = exp?.idea_id ?? -1;
  const label = exp?.label ?? (exp?.id != null ? String(exp.id) : undefined);
  const nav = useEntityNav(ideaId, label);

  const { highlighted, faded } = plotHighlightState(ideaId, label);
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
      data-experiment-label={label}
      data-idea-id={ideaId}
      style={exp ? "cursor:pointer;" : undefined}
      onMouseEnter={() => { onHoverChange(true, groupRef.current?.getBoundingClientRect() ?? null); if (exp) nav.bind.onMouseEnter(); }}
      onMouseLeave={() => { onHoverChange(false, null); if (exp) nav.bind.onMouseLeave(); }}
      onClick={exp ? (nav.bind.onClick as any) : undefined}
    >
      {excluded ? (
        <path d={`M${x - 2.5},${baselineY - 6.5} l5,5 m0,-5 l-5,5`}
          stroke={isMilestone ? "var(--yellow)" : "var(--text-faint)"} stroke-width="1.1"
          stroke-linecap="round" opacity={faded ? 0.2 : 0.8} />
      ) : (
      <line x1={x} x2={x} y1={y} y2={baselineY}
        stroke={isMilestone ? "var(--yellow)" : "var(--border)"}
        stroke-width="1" stroke-dasharray="2 3"
        opacity={faded ? 0.1 : isMilestone ? 0.5 : 0.3} />
      )}
      {excluded ? null : isRunning ? (
        <RunningMarker x={x} y={y} r={Math.max(3.2, r * 1.25)} faded={faded}
          pct={runningProgress.value[String(label ?? "")]} />
      ) : (
        <circle cx={x} cy={y} r={r} fill={faded ? "var(--text-faint)" : dotColor}
          stroke={strokeColor} stroke-width={strokeW} opacity={faded ? 0.2 : 1} />
      )}
      {/* Milestone caption — purple + clickable (sits inside this clickable <g>) */}
      <circle cx={x} cy={excluded ? baselineY - 4 : y} r="9" fill="transparent" />
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
export function MiniTooltip({ anchor, children }: { anchor: DOMRect; children: any }) {
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

// Label widths via one cached canvas context (the chart's mono font).
const _labelWidths = new Map<string, number>();
let _labelCtx: CanvasRenderingContext2D | null | undefined;
// Widths measured before the web font loaded are too small: re-measure after.
if (typeof document !== "undefined" && document.fonts) {
  void document.fonts.ready.then(() => _labelWidths.clear());
  document.fonts.addEventListener?.("loadingdone", () => _labelWidths.clear());
}
function textWidth(text: string, size: number): number {
  const key = `${size}|${text}`;
  let w = _labelWidths.get(key);
  if (w === undefined) {
    if (_labelCtx === undefined) _labelCtx = document.createElement("canvas").getContext("2d");
    if (_labelCtx) {
      const fam = getComputedStyle(document.documentElement).getPropertyValue("--font-mono").trim() || "monospace";
      _labelCtx.font = `${size}px ${fam}`;
      w = _labelCtx.measureText(text).width;
    } else {
      w = text.length * size * 0.62;
    }
    _labelWidths.set(key, w);
  }
  return w;
}

// ── component ─────────────────────────────────────────────────────────────────

export function MiniMetricsChart({
  metric, experiments, ideas, layout, hiddenStatuses, hideRunning,
  impOnly, colorMode, tags, tagMode, reversed, mean, logScale = false, clip = false,
  pointSize = "l", timeCurve = "best", xAxis = "experiments",
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
  /** x: line only, fit to the width; s/m/l: dot density. */
  pointSize?: "x" | "s" | "m" | "l";
  /** The purple line: best-so-far steps, or a trailing moving average. */
  timeCurve?: "best" | "avg";
  /** What the x axis shows. */
  xAxis?: "experiments" | "ideas" | "time" | "compute";
}) {
  // hovered dot index + its screen rect (for the HTML overlay tooltip)
  const [hovered, setHovered] = useState<{ idx: number; rect: DOMRect } | null>(null);
  // The line point under the mouse (index into the drawn line) + where.
  const [curveHover, setCurveHover] = useState<{ k: number; x: number; clientX: number; clientY: number } | null>(null);
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

  // Scroll to the newest end (right for oldest → newest, left when reversed)
  // when the chart loads or its metric / axis / view / order changes, and keep
  // following it as experiments arrive — unless the user scrolled away.
  const followNewest = useRef(true);
  const scrollKey = useRef("");
  useLayoutEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const key = `${metric}|${xAxis}|${pointSize}|${reversed}|${impOnly}`;
    if (key !== scrollKey.current) { scrollKey.current = key; followNewest.current = true; }
    if (followNewest.current) el.scrollLeft = reversed ? 0 : el.scrollWidth;
  });
  const onWrapScroll = () => {
    const el = wrapRef.current;
    if (!el) return;
    followNewest.current = reversed
      ? el.scrollLeft <= 4
      : el.scrollLeft + el.clientWidth >= el.scrollWidth - 4;
  };

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
  const showDots = pointSize !== "x";
  const dotSize = pointSize === "x" ? "l" : pointSize;
  const sizeScale = PT_SIZE_SCALE[dotSize] ?? 0.75;
  const avgMode = timeCurve === "avg";
  // Every axis scrolls at the view's spacing; the line-only view (x) fits.
  const continuousAxis = xAxis === "time" || xAxis === "compute";
  const fitWidth = !showDots;

  // ── Milestone (record) detection over the FULL ordered list ──────────────────
  // `chartData` is already in display order (buildChartData reverses when
  // `reversed`). We scan in CHRONOLOGICAL order so records and the running-best
  // are correct, then keep everything indexed by DISPLAY position.
  const fullValues = chartData.values as number[];
  const fullN = fullValues.length;
  const fullExp = chartData.expData as (Experiment & { _running?: boolean })[];
  const fullMilestones = new Set<number>();          // DISPLAY indices that set a record
  const toDisplay = (chrono: number) => (reversed ? fullN - 1 - chrono : chrono);
  let lastRecordChrono = -1;                          // chronological index of most recent record
  {
    let best: number | null = null;
    for (let c = 0; c < fullN; c++) {
      const d = toDisplay(c);
      const v = fullValues[d];
      if (isFinite(v) && (best === null || (lower ? v < best : v > best))) {
        best = v; fullMilestones.add(d); lastRecordChrono = c;
      }
    }
  }

  // ── Step-mode collapse (#79) ─────────────────────────────────────────────────
  // keep(d) = isRecord(d) OR chronologically after the most recent record.
  // Records-only thins the DOTS; the line-only view has none to thin.
  let keepIdx: number[];
  if (impOnly && showDots) {
    keepIdx = [];
    for (let d = 0; d < fullN; d++) {
      const c = reversed ? fullN - 1 - d : d;
      if (fullMilestones.has(d) || c > lastRecordChrono) keepIdx.push(d);
    }
  } else {
    keepIdx = Array.from({ length: fullN }, (_, d) => d);
  }
  const values = keepIdx.map((d) => fullValues[d]);
  const pointColors = keepIdx.map((d) => chartData.pointColors[d]);
  const expData = keepIdx.map((d) => fullExp[d]);
  const labels = keepIdx.map((d) => chartData.labels[d]);
  const milestones = new Set<number>();
  keepIdx.forEach((srcD, dstI) => { if (fullMilestones.has(srcD)) milestones.add(dstI); });
  const n = values.length;

  // ── X positions in axis units (``xu``), per FULL display index ──────────────
  //   experiments: the kept position (Step mode compresses, see #79)
  //   ideas:       experiments grouped per idea (ideas in order of first run),
  //                one unit per experiment, so blocks never overlap
  //   time:        finish time (running: now)
  //   compute:     cumulative experiment runtime (h) at its finish (running: end)
  const chronoOrder = Array.from({ length: fullN }, (_, c) => toDisplay(c));
  const xuFull: number[] = new Array(fullN).fill(NaN);
  const ideaBlocks: { idea: number; start: number; count: number }[] = [];
  let xuMax = 0;
  if (xAxis === "experiments") {
    keepIdx.forEach((d, i) => { xuFull[d] = i; });
    xuMax = Math.max(0, n - 1);
  } else if (xAxis === "ideas") {
    const byIdea = new Map<number, number[]>();
    for (const d of chronoOrder) {
      const id = fullExp[d]?.idea_id ?? -1;
      if (!byIdea.has(id)) byIdea.set(id, []);
      byIdea.get(id)!.push(d);
    }
    let start = 0;
    for (const [idea, ds] of byIdea) {
      ideaBlocks.push({ idea, start, count: ds.length });
      ds.forEach((d, j) => { xuFull[d] = start + j + 0.5; });
      start += ds.length;
    }
    xuMax = start;
  } else if (xAxis === "time") {
    const now = Date.now();
    for (let d = 0; d < fullN; d++) xuFull[d] = fullExp[d]?._running ? now : expTime(fullExp[d]);
  } else {
    // compute: chronological by finish; running ones sit at the end.
    const finished = chronoOrder
      .filter((d) => !fullExp[d]?._running && isFinite(expTime(fullExp[d])))
      .sort((a, b) => expTime(fullExp[a]) - expTime(fullExp[b]));
    let hours = 0;
    for (const d of finished) {
      const e = fullExp[d];
      const t0e = e?.started_at ? Date.parse(e.started_at) : NaN;
      const t1e = e?.finished_at ? Date.parse(e.finished_at) : NaN;
      if (isFinite(t0e) && isFinite(t1e) && t1e > t0e) hours += (t1e - t0e) / HOUR;
      xuFull[d] = hours;
    }
    for (let d = 0; d < fullN; d++) if (fullExp[d]?._running) xuFull[d] = hours;
  }
  if (continuousAxis) {
    const xs = xuFull.filter(isFinite);
    if (!xs.length) {
      return (
        <div ref={wrapRef} style={{ width: "100%", height: "100%", display: "flex", alignItems: "center", justifyContent: "center" }}>
          <span style={{ color: "var(--text-faint)", fontSize: "var(--text-sm)" }}>no timestamps for {metric}</span>
        </div>
      );
    }
  }
  const xuLo = continuousAxis ? Math.min(...xuFull.filter(isFinite)) : 0;
  const xuHi = continuousAxis ? Math.max(...xuFull.filter(isFinite)) : xuMax;

  // ── The purple line: best-so-far or moving average ALONG THE AXIS ────────────
  // From the full run (Step mode only thins the dots). Running experiments carry
  // partial progress, so they never enter the line. ``exp`` on a best point is
  // the experiment that set it; on an average point the latest one in it.
  type CurvePt = { xu: number; y: number; exp?: Experiment };
  const curve: CurvePt[] = [];
  let avgWindow = 0;
  {
    // Experiments axis: chronological, so in Step mode the line still uses the
    // FULL run and steps at the kept positions. Other axes: by axis position.
    const done = chronoOrder.filter((d) => !fullExp[d]?._running && isFinite(fullValues[d]));
    const ordered = xAxis === "experiments"
      ? done
      : done.filter((d) => isFinite(xuFull[d])).sort((a, b) => xuFull[a] - xuFull[b]);
    if (avgMode) avgWindow = pickAvgWindow(ordered.map((d) => fullValues[d]));
    let sum = 0;
    let best: number | null = null;
    let bestExp: Experiment | undefined;
    ordered.forEach((d, k) => {
      const v = fullValues[d];
      let y: number;
      let exp: Experiment | undefined;
      if (avgMode) {
        sum += v;
        if (k >= avgWindow) sum -= fullValues[ordered[k - avgWindow]];
        y = sum / Math.min(k + 1, avgWindow);
        exp = fullExp[d];
      } else {
        if (best === null || (lower ? v < best : v > best)) { best = v; bestExp = fullExp[d]; }
        y = best as number;
        exp = bestExp;
      }
      // Experiments axis: only kept experiments have a position.
      if (isFinite(xuFull[d])) curve.push({ xu: xuFull[d], y, exp });
    });
  }

  // ── Y range ──────────────────────────────────────────────────────────────────
  // Clip outliers (IQR fence) and/or log scale, mirroring the toolbar toggles.
  // With dots the range covers every value; line-only fits the line. Log Y
  // ranges over the POSITIVE values; values <= 0 are excluded (× marks on the x
  // axis, skipped by the line).
  const finite = showDots
    ? [...values.filter(isFinite), ...curve.map((p) => p.y)]
    : curve.map((p) => p.y);
  if (!finite.length) finite.push(0);
  const positives = finite.filter((v) => v > 0);
  const useLog = logScale && positives.length > 0;
  const rangeVals = useLog ? positives : finite;
  const atFloor = useLog && showDots ? values.filter((v) => isFinite(v) && v <= 0).length : 0;
  let dataMin = Math.min(...rangeVals);
  let dataMax = Math.max(...rangeVals);
  const tform = useLog ? Math.log10 : (x: number) => x;
  const untform = useLog ? (t: number) => Math.pow(10, t) : (t: number) => t;
  if (clip && rangeVals.length >= 4) {
    // Fence in the plotted space: on log Y, tiny positive scores (orders of
    // magnitude below the rest) are outliers too.
    const sorted = rangeVals.map(tform).sort((a, b) => a - b);
    const q1 = sorted[Math.floor(sorted.length * 0.25)];
    const q3 = sorted[Math.floor(sorted.length * 0.75)];
    const iqr = q3 - q1;
    if (iqr > 0) {
      dataMin = Math.max(dataMin, untform(q1 - 1.5 * iqr));
      dataMax = Math.min(dataMax, untform(q3 + 1.5 * iqr));
    }
  }
  // Log Y + Outliers: scores below the lower fence join the ≤ 0 ones as × marks.
  const offLog = (v: number) => useLog && isFinite(v) && (v <= 0 || (clip && v < dataMin));
  const belowFence = useLog && clip && showDots ? values.filter((v) => isFinite(v) && v > 0 && v < dataMin).length : 0;
  const tMin = tform(dataMin);
  const tMax = tform(dataMax);
  const tPad = (tMax - tMin) * 0.12 || Math.abs(tMax) * 0.05 || 0.01;
  const yMin = tMin - tPad;
  const yMax = tMax + tPad;
  const clampV = (v: number) => Math.min(dataMax, Math.max(dataMin, v));

  const nTicks = 4;
  const tickStep = (yMax - yMin) / (nTicks - 1);
  const yTicks = Array.from({ length: nTicks }, (_, i) => yMin + i * tickStep);
  // Readout: the final best, or the latest moving average (and its window).
  const bestVal = curve.length
    ? curve.reduce((a, b) => (b.xu >= a.xu ? b : a)).y
    : NaN;
  const yTickVals = yTicks.map((ty) => (useLog ? Math.pow(10, ty) : ty));
  const fmtVal = scoreFormatter([...yTickVals, ...(isFinite(bestVal) ? [bestVal] : [])]);
  const yTickLabels = yTickVals.map(fmtVal);

  // ── Pixel layout (1:1 px; viewBox matches the measured size) ─────────────────
  const padL = Math.max(PAD.l, Math.ceil(Math.max(...yTickLabels.map((l) => l.length)) * font.xs * 0.62) + 8);
  const containerW = size.w;
  const H = Math.max(120, size.h);
  // Width budget: one slot per point (ideas: per experiment in the blocks).
  const units = xAxis === "ideas" ? xuMax : n;
  const minSvgW = fitWidth ? 0 : padL + units * PX_PER_POINT[dotSize] + PAD.r;
  const svgW = Math.max(containerW, minSvgW);
  const needsScroll = svgW > containerW + 0.5;
  const hasXTicks = xAxis !== "experiments";
  const padB = hasXTicks ? PAD.b + font.xs + 6 : PAD.b; // extra row for x ticks
  const plotW = svgW - padL - PAD.r;
  const plotH = H - PAD.t - padB;
  const rightEdge = svgW - PAD.r;
  const baselineY = H - padB;
  // Display order is already reversed for the experiments axis; other axes flip here.
  const flip = reversed && xAxis !== "experiments";
  const toPx = (xu: number) => {
    const span = xuHi - xuLo;
    const f = span > 0 ? (xu - xuLo) / span : 0.5;
    return flip ? rightEdge - f * plotW : padL + f * plotW;
  };
  const toY = (v: number) => PAD.t + (1 - (tform(clampV(v)) - yMin) / (yMax - yMin)) * plotH;
  const dotX = (i: number): number => {
    const xu = xuFull[keepIdx[i]];
    return isFinite(xu) ? toPx(xu) : NaN;
  };

  // Line path (in axis order), skipping values that are off a log scale.
  const onScale = (v: number) => isFinite(v) && !offLog(v);
  const linePts = curve
    .filter((p) => onScale(p.y))
    .sort((a, b) => a.xu - b.xu)
    .map((p) => ({ ...p, px: toPx(p.xu), py: toY(p.y) }));
  const lineEndX = flip ? padL : rightEdge;
  let stepPath = linePts.length ? `M${linePts[0].px.toFixed(1)},${linePts[0].py.toFixed(1)}` : "";
  for (let i = 1; i < linePts.length; i++) {
    stepPath += avgMode
      ? ` L${linePts[i].px.toFixed(1)},${linePts[i].py.toFixed(1)}`  // average: straight segments
      : ` H${linePts[i].px.toFixed(1)} V${linePts[i].py.toFixed(1)}`;
  }
  if (stepPath) stepPath += ` H${lineEndX.toFixed(1)}`;
  const stepStartX = linePts.length ? linePts[0].px : padL;

  // ── X ticks ──────────────────────────────────────────────────────────────────
  const fitsLabel = (x: number, label: string) => {
    const half = (label.length * font.xs * 0.62) / 2;
    return x - half >= 0 && x + half <= svgW;
  };
  let xTicks: { key: string; x: number; label: string }[] = [];
  const maxTicks = Math.max(2, Math.floor(plotW / 90));
  if (xAxis === "time") {
    xTicks = timeTicks(xuLo, xuHi, maxTicks).map((tk) => ({ key: String(tk.t), x: toPx(tk.t), label: tk.label }));
  } else if (xAxis === "compute") {
    const span = Math.max(xuHi - xuLo, 1e-9);
    const step = [0.5, 1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000]
      .find((st) => span / st <= maxTicks) ?? 10000;
    for (let h = Math.ceil(xuLo / step) * step; h <= xuHi; h += step) {
      xTicks.push({ key: String(h), x: toPx(h), label: `${h}h` });
    }
  } else if (xAxis === "ideas") {
    // Label the idea under every ~70px.
    let lastX = -Infinity;
    for (const b of ideaBlocks) {
      const x = toPx(b.start + b.count / 2);
      if (Math.abs(x - lastX) >= 70) {
        xTicks.push({ key: `i${b.idea}`, x, label: `#${b.idea}` });
        lastX = x;
      }
    }
  }
  xTicks = xTicks.filter((tk) => fitsLabel(tk.x, tk.label));
  const ideaSeparators = xAxis === "ideas" && ideaBlocks.length > 1
    ? ideaBlocks.slice(1).map((b) => toPx(b.start)).filter((x, i, a) => i === 0 || Math.abs(x - a[i - 1]) >= 3)
    : [];

  // ── Point labels: short "#idea" tags, records first, then the best scores and
  // the extremes, then anything else (auto); each placed at the first of a few
  // spots around its dot whose box stays in the plot and overlaps no label
  // already placed — dense clusters get none, notable points get theirs.
  const labelMode = chartPointLabels.value;
  const labelH = font.xs + 2;
  const labelPos = (i: number) => {
    const x = dotX(i);
    const v = values[i];
    return isFinite(x) && onScale(v) ? { x, y: toY(v) } : null;
  };
  const labelBoxes = (i: number, text: string) => {
    const p = labelPos(i);
    if (!p) return [];
    const w = textWidth(text, font.xs), g = 4;
    return [
      { x0: p.x + g, y0: p.y - g - labelH, anchor: "start" as const },        // above-right
      { x0: p.x - g - w, y0: p.y - g - labelH, anchor: "end" as const },      // above-left
      { x0: p.x + g, y0: p.y + g, anchor: "start" as const },                 // below-right
      { x0: p.x + g + 2, y0: p.y - labelH / 2, anchor: "start" as const },    // right
    ].map((o) => ({ ...o, x1: o.x0 + w, y1: o.y0 + labelH }));
  };
  const ideaTag = (i: number) => {
    const e = expData?.[i];
    return e && e.idea_id != null ? `#${e.idea_id}` : "";
  };
  type PlacedLabel = { i: number; x: number; y: number; text: string; anchor: "start" | "end"; x0: number; y0: number; x1: number; y1: number };
  const pointLabels: PlacedLabel[] = [];
  if (labelMode !== "off") {
    const usable = (i: number) => {
      const e = expData?.[i];
      if (!e || e._running || !labelPos(i)) return false;
      return !plotHighlightState(e.idea_id, String(e.label ?? e.id)).faded;
    };
    const better = (a: number, b: number) => (lower ? values[a] - values[b] : values[b] - values[a]);
    const all = Array.from({ length: n }, (_, i) => i).filter(usable);
    let order = all.filter((i) => milestones.has(i)).sort(better);
    if (labelMode === "auto" && showDots) {
      const rest = all.filter((i) => !milestones.has(i)).sort(better);
      const top = Math.max(1, Math.ceil(rest.length * 0.05));
      const worst = rest.slice(-Math.max(1, Math.ceil(rest.length * 0.02))).reverse();
      const seen = new Set(order);
      for (const i of [...rest.slice(0, top), ...worst, ...rest.slice(top)]) {
        if (!seen.has(i)) { seen.add(i); order.push(i); }
      }
    }
    order = order.slice(0, 400);
    const M = 1; // keep a hair of space between labels
    const overlaps = (b: { x0: number; y0: number; x1: number; y1: number }) =>
      pointLabels.some((p) => b.x0 - M < p.x1 && b.x1 + M > p.x0 && b.y0 - M < p.y1 && b.y1 + M > p.y0);
    for (const i of order) {
      const text = ideaTag(i);
      if (!text) continue;
      const spot = labelBoxes(i, text).find((b) =>
        b.x0 >= padL && b.x1 <= rightEdge && b.y0 >= 0 && b.y1 <= baselineY && !overlaps(b));
      if (spot) {
        pointLabels.push({ i, text, anchor: spot.anchor, x: spot.anchor === "start" ? spot.x0 : spot.x1,
          y: spot.y1 - 3, x0: spot.x0, y0: spot.y0, x1: spot.x1, y1: spot.y1 });
      }
    }
  }
  // Hovering a point labels and rings every point of the same idea.
  const hoverIdea = hovered !== null ? expData?.[hovered.idx]?.idea_id ?? null : null;
  const hoverPoints = hoverIdea == null ? [] : Array.from({ length: n }, (_, i) => i)
    .filter((i) => expData?.[i]?.idea_id === hoverIdea && labelPos(i));
  const labeledIdx = new Set(pointLabels.map((l) => l.i));
  // The hovered idea's unlabeled points get labels too, where they overlap
  // no other label (other ideas' labels are dimmed meanwhile).
  const hoverLabels: PlacedLabel[] = [];
  for (const i of hoverPoints) {
    if (labeledIdx.has(i)) continue;
    const text = ideaTag(i);
    const spot = labelBoxes(i, text).find((b) =>
      b.x0 >= padL && b.x1 <= rightEdge && b.y0 >= 0 && b.y1 <= baselineY
      && ![...pointLabels, ...hoverLabels].some((p) => b.x0 - 1 < p.x1 && b.x1 + 1 > p.x0 && b.y0 - 1 < p.y1 && b.y1 + 1 > p.y0));
    if (spot) hoverLabels.push({ i, text, anchor: spot.anchor, x: spot.anchor === "start" ? spot.x0 : spot.x1,
      y: spot.y1 - 3, x0: spot.x0, y0: spot.y0, x1: spot.x1, y1: spot.y1 });
  }

  // ── Hover on the line: the value in effect at the mouse position ─────────────
  const onCurveMove = (ev: MouseEvent) => {
    if (!linePts.length) return;
    const svg = ev.currentTarget as SVGSVGElement;
    const r = svg.getBoundingClientRect();
    const x = ((ev.clientX - r.left) / r.width) * svgW;
    if (x < padL || x > rightEdge) { setCurveHover(null); return; }
    let k = -1;
    for (let j = 0; j < linePts.length; j++) {
      if ((flip ? linePts[j].px >= x : linePts[j].px <= x)) k = j; else break;
    }
    if (k < 0) { setCurveHover(null); return; }
    setCurveHover({ k, x, clientX: ev.clientX, clientY: ev.clientY });
  };
  const describeX = (p: CurvePt): string => {
    if (xAxis === "time") {
      return new Date(p.xu).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false });
    }
    if (xAxis === "compute") return `after ${p.xu.toFixed(p.xu < 10 ? 1 : 0)} h of experiments`;
    if (xAxis === "ideas") return p.exp ? `idea #${p.exp.idea_id}` : "idea";
    return p.exp ? `exp/${p.exp.label ?? p.exp.id}` : "experiment";
  };
  const axisLabel = {
    experiments: "experiments",
    ideas: "ideas (grouped)",
    time: "time",
    compute: "experiment hours",
  }[xAxis];
  const arrow = flip || (reversed && xAxis === "experiments") ? `← ${axisLabel}` : `${axisLabel} →`;

  return (
    <div
      ref={wrapRef}
      onScroll={onWrapScroll}
      style={{ width: "100%", height: "100%", overflowX: needsScroll ? "auto" : "hidden", overflowY: "hidden" }}
    >
      <svg
        onMouseMove={onCurveMove}
        onMouseLeave={() => setCurveHover(null)}
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

        {/* Idea block separators */}
        {ideaSeparators.map((x, i) => (
          <line key={`sep${i}`} x1={x} x2={x} y1={PAD.t} y2={baselineY}
            stroke="var(--border-soft)" stroke-width="1" opacity="0.5" />
        ))}

        {/* X baseline */}
        <line x1={padL} x2={rightEdge} y1={baselineY} y2={baselineY} stroke="var(--border)" />

        {/* X ticks */}
        {xTicks.map((tk) => (
          <g key={tk.key}>
            <line x1={tk.x} x2={tk.x} y1={baselineY} y2={baselineY + 3} stroke="var(--border)" />
            <text x={tk.x} y={baselineY + 4 + font.xs} text-anchor="middle"
              fill="var(--text-faint)" font-size={font.xs} font-family="var(--font-mono)">{tk.label}</text>
          </g>
        ))}

        {/* Line: flat tint + stroke (no gradient) */}
        {stepPath && <path d={`${stepPath} V${baselineY} H${stepStartX.toFixed(1)} Z`}
          fill="var(--purple)" fill-opacity={impOnly ? 0.07 : 0.05} />}
        {stepPath && <path d={stepPath} fill="none" stroke="var(--purple)"
          stroke-width={impOnly || !showDots ? 1.8 : 1.4}
          stroke-linejoin="round" stroke-linecap="round" />}

        {/* Hover guide on the line */}
        {curveHover && linePts[curveHover.k] && hovered === null && (
          <g pointer-events="none">
            <line x1={curveHover.x} x2={curveHover.x} y1={PAD.t} y2={baselineY}
              stroke="var(--text-faint)" stroke-dasharray="2 3" stroke-width="1" />
            <circle cx={curveHover.x} cy={linePts[curveHover.k].py} r={3.2}
              fill="var(--purple)" stroke="var(--bg)" stroke-width="1" />
          </g>
        )}

        {/* Dots */}
        {showDots && values.map((v, i) => {
          const x = dotX(i);
          if (!isFinite(x)) return null;
          const e = expData?.[i] as (Experiment & { _running?: boolean }) | undefined;
          const isMs = milestones.has(i);
          return (
            <MiniDot
              key={i}
              exp={e}
              x={x} y={toY(v)} baselineY={baselineY}
              excluded={offLog(v)}
              color={pointColors[i] ?? "var(--text-muted)"}
              isMilestone={isMs}
              sizeScale={sizeScale}
              onHoverChange={(h, rect) =>
                setHovered(h && rect ? { idx: i, rect } : (cur) => (cur?.idx === i ? null : cur))
              }
            />
          );
        })}

        {/* Point labels (#idea) + the hovered idea's points */}
        <g class="mini-point-labels" font-family="var(--font-mono)" font-size={font.xs} pointer-events="none">
          {pointLabels.map((l) => (
            <text key={`l${l.i}`} class="mini-point-label" x={l.x} y={l.y} text-anchor={l.anchor}
              fill={milestones.has(l.i) ? "var(--purple)" : "var(--text-muted)"}
              stroke="var(--bg)" stroke-width="3" paint-order="stroke"
              opacity={hoverIdea != null && expData?.[l.i]?.idea_id !== hoverIdea ? 0.35 : 1}>{l.text}</text>
          ))}
          {hoverPoints.map((i) => {
            const p = labelPos(i)!;
            return <circle key={`h${i}`} cx={p.x} cy={p.y} r={5.5 * sizeScale + 1.5} fill="none" stroke="var(--text)" stroke-width="1.2" />;
          })}
          {hoverLabels.map((l) => (
            <text key={`hl${l.i}`} class="mini-point-label is-hover" x={l.x} y={l.y} text-anchor={l.anchor} fill="var(--text)"
              stroke="var(--bg)" stroke-width="3" paint-order="stroke">{l.text}</text>
          ))}
        </g>

        {/* Bottom labels */}
        <g font-family="var(--font-mono)" font-size={font.xs}>
          <text x={padL} y={H - 5} fill="var(--text-faint)">
            {arrow}
            {atFloor + belowFence > 0
              ? ` · ${[
                  atFloor ? `${atFloor} ≤ 0` : "",
                  belowFence ? `${belowFence} below the outlier fence` : "",
                ].filter(Boolean).join(", ")} off the log scale (×)`
              : ""}
          </text>
          <text x={rightEdge} y={H - 5} text-anchor="end" fill="var(--purple)">
            {avgMode ? `avg of last ${avgWindow}` : "current best"} {isFinite(bestVal) ? fmtVal(bestVal) : "—"}
          </text>
        </g>
      </svg>

      {/* Line hover card */}
      {curveHover && linePts[curveHover.k] && hovered === null && (() => {
        const pt = linePts[curveHover.k];
        const exp = pt.exp;
        const rect = new DOMRect(curveHover.clientX, curveHover.clientY, 0, 0);
        return (
          <MiniTooltip anchor={rect}>
            <span class="ui-tip-head"><span class="ui-tip-mark">{describeX(pt)}</span></span>
            <span class="ui-tip-metric">
              <span class="ui-tip-metric-label">
                {avgMode ? `${metric ? fmtMetricName(metric) : "value"} · avg of last ${avgWindow}` : `best ${metric ? fmtMetricName(metric) : "value"}`}
              </span>
              <span class="ui-tip-metric-value is-record">{fmtVal(pt.y)}</span>
            </span>
            {exp && (
              <span class="ui-tip-idea">
                <span class="ui-tip-idea-id">{avgMode ? "latest" : "set by"} exp/{exp.label ?? exp.id} · idea #{exp.idea_id}</span>
              </span>
            )}
          </MiniTooltip>
        );
      })()}

      {/* Dot hover card — visually identical to timeline/table tooltips */}
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
              pct: runningProgress.value[String(exp.label ?? exp.id)],
            })}
          </MiniTooltip>
        );
      })()}
    </div>
  );
}
