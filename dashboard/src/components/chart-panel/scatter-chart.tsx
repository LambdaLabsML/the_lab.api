import { usePlotControls, matchesPlotSearch } from "../../lib/hooks/use-plot-controls";
import { inSelection, selectionLabels } from "../../state/filters";
import { registerExperimentSurface } from "../experiment-actions";
import { useRef, useEffect, useMemo, useState } from "preact/hooks";
import { Chart } from "chart.js/auto";
import { allExperiments, allIdeas, currentLayout, highlightedIdea, cloneChartPanel, updatePanelTitle, runningProgress } from "../../state/signals";
import { chartNavClick } from "../../lib/hooks";
import { IconButton, experimentTipContent } from "../ui";
import { CtlIcon, CtlSearch, CtlToggle, MetricPicker } from "./chart-controls";
import { MiniTooltip } from "./mini-metrics-chart";
import {
  selectedMetric, filterText, colorTheme, fontSize, activeTagFilters, tagFilterMode,
  showAbandoned, showConcluded, showRunning, scatterXMetric, scatterYMetric, colorblindMode,
} from "../../state/settings";
import { filterMetricExperiments, collectChartKeys, resolveNumericValue } from "../../lib/chart-data";
import { IDEA_PALETTE, _colorForExp, isLowerBetter } from "../../lib/colors";
import { resolveColor, getCssVar, getCssVarPx } from "../../lib/css-vars";
import { fmtMetricName, fmtScoreShort, scoreFormatter } from "../../lib/format";

// Axis tick labels: one notation per axis, chosen from the ticks on screen
// (scientific for tiny values or when a log axis spans many decades).
function tickLabel(value: number | string, _i: number, ticks: { value: number }[]): string {
  return scoreFormatter(ticks.map((t) => t.value))(Number(value));
}
import type { Experiment, IdeaNode, SubwayLayout } from "../../lib/types";

/**
 * Aggregate experiments by idea mean for scatter: returns one synthetic
 * experiment per idea with mean values for BOTH x and y metrics.
 */
function aggregateScatterByIdeaMean(
  experiments: Experiment[],
  xKey: string,
  yKey: string,
): Experiment[] {
  const groups: Record<number, { exps: Experiment[]; sumX: number; sumY: number; count: number }> = {};
  for (const e of experiments) {
    if (e._running) continue;
    const vx = resolveNumericValue(e, xKey);
    const vy = resolveNumericValue(e, yKey);
    if (vx === undefined || vy === undefined) continue;
    if (!groups[e.idea_id]) groups[e.idea_id] = { exps: [], sumX: 0, sumY: 0, count: 0 };
    groups[e.idea_id].exps.push(e);
    groups[e.idea_id].sumX += vx;
    groups[e.idea_id].sumY += vy;
    groups[e.idea_id].count += 1;
  }

  const result: Experiment[] = [];
  for (const ideaId of Object.keys(groups).map(Number).sort((a, b) => a - b)) {
    const g = groups[ideaId];
    if (g.count === 0) continue;
    const last = g.exps[g.exps.length - 1];
    result.push({
      ...last,
      metrics: {
        ...last.metrics,
        [xKey]: g.sumX / g.count,
        [yKey]: g.sumY / g.count,
      },
      description: `idea/${ideaId} mean (${g.count} exp${g.count > 1 ? "s" : ""})`,
      _ideaMean: true,
      _meanCount: g.count,
    } as Experiment);
  }
  return result;
}

/** IQR-based bounds for clipping outliers on one axis. */
function computeBounds(values: number[]): { min?: number; max?: number } {
  const nums = values.filter((v) => v != null && isFinite(v));
  if (nums.length < 4) return {};
  const sorted = nums.slice().sort((a, b) => a - b);
  const q1 = sorted[Math.floor(sorted.length * 0.25)];
  const q3 = sorted[Math.floor(sorted.length * 0.75)];
  const iqr = q3 - q1;
  if (iqr === 0) return {};
  const lo = q1 - 1.5 * iqr;
  const hi = q3 + 1.5 * iqr;
  const dataMin = sorted[0];
  const dataMax = sorted[sorted.length - 1];
  if (dataMin >= lo && dataMax <= hi) return {};
  const pad = (hi - lo) * 0.05;
  return { min: lo - pad, max: hi + pad };
}

// Canvas point images (progress cakes) for running experiments, cached per 5 % step.
const _ringCache = new Map<string, HTMLCanvasElement>();
function runningRing(pct: number | undefined): HTMLCanvasElement {
  const has = typeof pct === "number" && Number.isFinite(pct);
  const step = has ? Math.round(Math.min(100, Math.max(0, pct!)) / 5) * 5 : -1;
  const yellow = getCssVar("--yellow") || "#d29922";
  const key = `${step}|${yellow}`;
  const hit = _ringCache.get(key);
  if (hit) return hit;
  const dpr = window.devicePixelRatio || 1, size = 9;
  const cv = document.createElement("canvas");
  cv.width = cv.height = size * dpr;
  cv.style.width = cv.style.height = `${size}px`;
  const g = cv.getContext("2d")!;
  g.scale(dpr, dpr);
  g.lineWidth = 1.6;
  g.strokeStyle = yellow;
  const c = size / 2, r = 3.2;
  if (has) {
    // A "cake": faint disc, then a solid slice growing clockwise from 12 o'clock.
    g.fillStyle = yellow;
    g.globalAlpha = 0.22; g.beginPath(); g.arc(c, c, r, 0, 2 * Math.PI); g.fill();
    g.globalAlpha = 0.55; g.lineWidth = 0.8; g.stroke();
    g.globalAlpha = 1;
    if (step > 0) {
      g.beginPath(); g.moveTo(c, c);
      g.arc(c, c, r, -Math.PI / 2, -Math.PI / 2 + (2 * Math.PI * step) / 100);
      g.closePath(); g.fill();
    }
  } else {
    // No progress known: a dashed ring (canvas point styles can't pulse cheaply).
    g.setLineDash([2, 2]); g.beginPath(); g.arc(c, c, r, 0, 2 * Math.PI); g.stroke();
  }
  _ringCache.set(key, cv);
  return cv;
}

const HOVER_PX = 24;

export function ScatterChart({ instanceId, initialXMetric, initialYMetric, hideClone = false, onAxesChange }: { instanceId?: string; initialXMetric?: string; initialYMetric?: string; hideClone?: boolean; onAxesChange?: (x: string, y: string) => void } = {}) {
  const isClone = !!instanceId;
  const { ideaMean, clipOutliers, colorMode, plotFilter } = usePlotControls(instanceId);
  const [localX, setLocalX] = useState(initialXMetric || "");
  const [localY, setLocalY] = useState(initialYMetric || "");
  // Hovered point (within HOVER_PX of the cursor) for the shared hover card.
  const [hover, setHover] = useState<{ d: any; rect: DOMRect; x: number | null; y: number | null } | null>(null);
  const setHoverRef = useRef(setHover);
  setHoverRef.current = setHover;
  // After a click the details panel slides in, the chart redraws, and
  // Chart.js replays the last (unmoved) mouse event — which would put the
  // card right back. Ignore hovers until the pointer really moves again.
  const suppressHover = useRef<{ x: number; y: number } | null>(null);
  useEffect(() => {
    if (!hover) return;
    const dismiss = () => { setHover(null); if (highlightedIdea.value !== null) highlightedIdea.value = null; };
    const onTouch = (e: Event) => { if (e.target !== canvasRef.current) dismiss(); };
    window.addEventListener("scroll", dismiss, { capture: true, passive: true });
    document.addEventListener("touchstart", onTouch, { capture: true, passive: true });
    document.addEventListener("pointercancel", dismiss, true);
    return () => {
      window.removeEventListener("scroll", dismiss, { capture: true } as any);
      document.removeEventListener("touchstart", onTouch, { capture: true } as any);
      document.removeEventListener("pointercancel", dismiss, true);
    };
  }, [hover !== null]);

  const canvasRef = useRef<HTMLCanvasElement>(null);
  const chartRef = useRef<Chart | null>(null);
  const prevThemeRef = useRef(colorTheme.value);
  const prevFzRef = useRef(fontSize.value);

  const allRows = allExperiments.value;
  const selection = selectionLabels.value;
  const localQuery = plotFilter.value;
  const globalQuery = filterText.value;
  const ideaMap = allIdeas.value;
  const experiments = useMemo(() => allRows.filter(e => inSelection(e) && matchesPlotSearch(e, ideaMap[e.idea_id]?.description ?? "", localQuery)), [allRows, selection, localQuery, globalQuery, ideaMap]);

  // Compute chartable keys grouped by source
  const grouped = useMemo(() => collectChartKeys(allRows), [allRows]);
  const metricKeys = [...grouped.metrics, ...grouped.nested, ...grouped.meta, ...grouped.timing];
  const metricGroups = [
    { label: "Metrics", keys: grouped.metrics }, { label: "Nested", keys: grouped.nested },
    { label: "Timing", keys: grouped.timing }, { label: "Meta", keys: grouped.meta },
  ];
  const ideas = allIdeas.value;
  const layout = currentLayout.value;
  const theme = colorTheme.value;  // subscribe — chart recreates on theme/size switch
  const _fz = fontSize.value;  // subscribe — chart recreates on font-size switch
  const mode = colorMode.value;
  const tags = activeTagFilters.value;
  const tagMode = tagFilterMode.value;
  const clip = clipOutliers.value;
  const mean = ideaMean.value;
  const highlighted = highlightedIdea.value;

  const xMetric = isClone ? localX : scatterXMetric.value;
  const yMetric = isClone ? localY : scatterYMetric.value;

  useEffect(() => { if (xMetric && yMetric) onAxesChange?.(xMetric, yMetric); }, [xMetric, yMetric]);

  const setX = isClone
    ? (v: string) => { setLocalX(v); if (instanceId && updatePanelTitle) updatePanelTitle(instanceId, `Scatter: ${v} vs ${yMetric || "?"}`); }
    : (v: string) => { scatterXMetric.value = v; };
  const setY = isClone
    ? (v: string) => { setLocalY(v); if (instanceId && updatePanelTitle) updatePanelTitle(instanceId, `Scatter: ${xMetric || "?"} vs ${v}`); }
    : (v: string) => { scatterYMetric.value = v; };

  // Idea-status filters (abandoned/concluded). The "running" toggle is
  // experiment-level — see hideRunning below.
  const hiddenStatuses = new Set<string>();
  if (!showAbandoned.value) hiddenStatuses.add("abandoned");
  if (!showConcluded.value) hiddenStatuses.add("concluded");
  const hideRunning = !showRunning.value;

  // Auto-select metrics — prefer meaningful pairs
  if (!xMetric && metricKeys.length > 0) {
    const preferredX = ["score", "total_score", "progress_score", "accuracy", "f1"];
    setX(preferredX.find(k => metricKeys.includes(k)) ?? metricKeys[0]);
  }
  if (!yMetric && metricKeys.length > 1) {
    const preferredY = ["elapsed_s", "n_levels_completed", "n_environments_completed", "duration_s"];
    const px = preferredY.find(k => metricKeys.includes(k) && k !== xMetric);
    setY(px ?? (metricKeys.find(k => k !== xMetric) ?? metricKeys[0]));
  }

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    return registerExperimentSurface(canvas, event => {
      const chart = chartRef.current;
      if (!chart) return null;
      const hit = chart.getElementsAtEventForMode(event, "nearest", { intersect: false }, false)[0];
      if (!hit) return null;
      // Ring markers are larger than Chart.js's point radius. Accept their
      // visible edge, while keeping clicks on empty chart space unchanged.
      const box = canvas.getBoundingClientRect();
      const point = hit.element;
      const distance = Math.hypot(event.clientX - box.left - point.x * box.width / chart.width,
        event.clientY - box.top - point.y * box.height / chart.height);
      if (distance > 14) return null;
      const exp = hit && (chart.data.datasets[hit.datasetIndex] as any)._expData?.[hit.index];
      return exp && exp.idea_id != null ? { ideaId: exp.idea_id, label: String(exp.label ?? exp.id) } : null;
    });
  }, []);

  // Destroy chart on unmount
  useEffect(() => {
    return () => {
      chartRef.current?.destroy();
      chartRef.current = null;
    };
  }, []);

  // Create or update chart
  useEffect(() => {
    if (!xMetric || !yMetric || !canvasRef.current) return;

    // Sync: update refs and destroy if needed — must run before rAF
    const themeOrSizeChanged = theme !== prevThemeRef.current || _fz !== prevFzRef.current;
    prevThemeRef.current = theme;
    prevFzRef.current = _fz;
    if (chartRef.current && themeOrSizeChanged) {
      chartRef.current.destroy();
      chartRef.current = null;
    }

    const canvas = canvasRef.current;
    const rafId = requestAnimationFrame(() => {
    if (!canvas.offsetParent || !canvas.isConnected) return;

    // Get experiments that have BOTH metrics
    let filtered: Experiment[];
    if (mean) {
      // Filter for x metric, then aggregate both
      const xFiltered = filterMetricExperiments(xMetric, experiments, tags, tagMode, hiddenStatuses, hideRunning);
      // Further filter to only those that also have y metric
      const bothFiltered = xFiltered.filter(
        (e) => resolveNumericValue(e, yMetric) !== undefined,
      );
      filtered = aggregateScatterByIdeaMean(bothFiltered, xMetric, yMetric);
    } else {
      // Filter for x metric
      const xFiltered = filterMetricExperiments(xMetric, experiments, tags, tagMode, hiddenStatuses, hideRunning);
      // Further filter to only those that also have y metric
      filtered = xFiltered.filter(
        (e) => resolveNumericValue(e, yMetric) !== undefined,
      );
    }

    // Color computation
    const metricFiltered = filterMetricExperiments(
      (isClone ? yMetric : selectedMetric.value || yMetric), experiments, tags, tagMode, hiddenStatuses, hideRunning,
    );
    const ideaColorMap: Record<number, string> = {};
    let colorIdx = 0;
    for (const exp of filtered) {
      if (!(exp.idea_id in ideaColorMap)) {
        ideaColorMap[exp.idea_id] = IDEA_PALETTE[colorIdx % IDEA_PALETTE.length];
        colorIdx++;
      }
    }

    function getColor(exp: Experiment): string {
      const colorMetric = selectedMetric.value || yMetric;
      const c = resolveColor(_colorForExp(exp, colorMetric, mode, layout, ideas, metricFiltered));
      return c || ideaColorMap[exp.idea_id] || "#8b949e";
    }

    const scatterData = filtered.map((e) => ({
      x: resolveNumericValue(e, xMetric) ?? 0,
      y: resolveNumericValue(e, yMetric) ?? 0,
    }));
    const bgColors = filtered.map((e) => (e._running ? "transparent" : getColor(e)));
    const borderColors = filtered.map((e) => getColor(e));
    // Small, crisp dots — consistent with the mini exemplar's quiet language.
    const radii = filtered.map((e) => (e._running ? 3.5 : 2.4));
    const borderWidths = filtered.map((e) => (e._running ? 1.5 : 1));
    // Running: a yellow progress cake (pct_complete), like the metrics chart.
    const progress = runningProgress.value;
    const styles = filtered.map((e) => (e._running
      ? runningRing(progress[String(e.label ?? e.id)])
      : "circle") as any);

    const xBounds = clip ? computeBounds(scatterData.map((d) => d.x)) : {};
    const yBounds = clip ? computeBounds(scatterData.map((d) => d.y)) : {};

    if (chartRef.current) {
      const ds = chartRef.current.data.datasets[0] as any;
      ds.data = scatterData;
      ds.pointBackgroundColor = bgColors;
      ds.pointBorderColor = borderColors;
      ds.pointRadius = radii;
      ds.pointBorderWidth = borderWidths;
      ds.pointStyle = styles;
      ds._expData = filtered;
      const xScale = chartRef.current.options.scales!.x!;
      const yScale = chartRef.current.options.scales!.y!;
      xScale.title = { display: true, text: fmtMetricName(xMetric), color: getCssVar("--text-faint"), font: { size: getCssVarPx("--text-xs") } };
      yScale.title = { display: true, text: fmtMetricName(yMetric), color: getCssVar("--text-faint"), font: { size: getCssVarPx("--text-xs") } };
      xScale.min = xBounds.min;
      xScale.max = xBounds.max;
      yScale.min = yBounds.min;
      yScale.max = yBounds.max;
      chartRef.current.resize();
      chartRef.current.update("none");
      return;
    }

    chartRef.current = new Chart(canvas, {
      type: "scatter",
      data: {
        datasets: [
          {
            label: `${xMetric} vs ${yMetric}`,
            data: scatterData,
            pointBackgroundColor: bgColors,
            pointBorderColor: borderColors,
            pointRadius: radii,
            pointBorderWidth: borderWidths,
            pointStyle: styles,
            pointHoverRadius: 5,
            _expData: filtered,
          } as any,
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: { duration: 400 },
        onClick(_evt, elements) {
          // Route through the shared chart-nav helper. Scatter points are
          // {x,y} objects; the matching experiments live in _expData, so map
          // each clicked element back to its experiment before delegating.
          const mapped = elements
            .map((el) => ({ raw: (this.data.datasets[el.datasetIndex] as any)?._expData?.[el.index], index: el.index }))
            .filter((m) => m.raw?.idea_id != null);
          chartNavClick(mapped);
          // The click opens the details: drop the hover card, and keep it
          // down until the pointer moves (see suppressHover).
          setHoverRef.current(null);
          highlightedIdea.value = null;
          const ne = (_evt as any).native as MouseEvent | undefined;
          suppressHover.current = { x: ne?.clientX ?? -1, y: ne?.clientY ?? -1 };
        },
        scales: {
          x: {
            ...xBounds,
            title: { display: true, text: fmtMetricName(xMetric), color: getCssVar("--text-faint"), font: { size: getCssVarPx("--text-xs") } },
            ticks: { color: getCssVar("--text-faint"), font: { size: getCssVarPx("--text-xs") }, callback: tickLabel },
            grid: { color: getCssVar("--border-soft") },
            border: { color: getCssVar("--border-soft") },
          },
          y: {
            ...yBounds,
            title: { display: true, text: fmtMetricName(yMetric), color: getCssVar("--text-faint"), font: { size: getCssVarPx("--text-xs") } },
            ticks: { color: getCssVar("--text-faint"), font: { size: getCssVarPx("--text-xs") }, callback: tickLabel },
            grid: { color: getCssVar("--border-soft") },
            border: { color: getCssVar("--border-soft") },
          },
        },
        onHover(evt, _elements, chart) {
          const native = (evt as any).native as MouseEvent | undefined;
          const sup = suppressHover.current;
          if (sup) {
            // Replays (and the click's own position) don't count as moving.
            if (!native || (Math.abs(native.clientX - sup.x) < 4 && Math.abs(native.clientY - sup.y) < 4)) {
              setHoverRef.current(null);
              return;
            }
            suppressHover.current = null;
          }
          // Like the main graph: the nearest point within HOVER_PX gets the
          // hover card and highlights its idea's points.
          const near = chart.getElementsAtEventForMode(evt as any, "nearest", { intersect: false }, false)[0];
          const pt = near ? chart.getDatasetMeta(near.datasetIndex).data[near.index] : null;
          const ex = (evt as any).x, ey = (evt as any).y;
          const d = near ? (chart.data.datasets[near.datasetIndex] as any)?._expData?.[near.index] : null;
          if (pt && d && Math.hypot(pt.x - ex, pt.y - ey) <= HOVER_PX) {
            highlightedIdea.value = d.idea_id;
            const box = chart.canvas.getBoundingClientRect();
            const raw = (chart.data.datasets[near.datasetIndex].data as any[])[near.index] as { x: number; y: number };
            setHoverRef.current({ d, rect: new DOMRect(box.left + pt.x - 5, box.top + pt.y - 5, 10, 10), x: raw?.x ?? null, y: raw?.y ?? null });
            chart.canvas.style.cursor = "pointer";
          } else {
            highlightedIdea.value = null;
            setHoverRef.current(null);
            chart.canvas.style.cursor = "";
          }
        },
        plugins: {
          legend: { display: false },
          tooltip: { enabled: false },
        },
      },
    });
    }); // end requestAnimationFrame

    return () => cancelAnimationFrame(rafId);
  }, [xMetric, yMetric, mode, tags, tagMode, experiments, showAbandoned.value, showConcluded.value, showRunning.value, clip, mean, theme, _fz, colorblindMode.value]);

  // Handle highlight changes separately
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    const ds = chart.data.datasets[0] as any;
    const expData = ds._expData as any[];
    if (!expData) return;

    if (highlighted !== null) {
      ds.pointRadius = expData.map((e: any) => (e.idea_id === highlighted ? 6 : 2.4));
      ds.pointBorderWidth = expData.map((e: any) => (e.idea_id === highlighted ? 1.5 : 1));
    } else {
      ds.pointRadius = expData.map((e: any) => (e._running ? 3.5 : 2.4));
      ds.pointBorderWidth = expData.map((e: any) => (e._running ? 1.5 : 1));
    }
    chart.update("none");
  }, [highlighted]);

  return (
    <div class="chart-col" style={{ height: "100%", display: "flex", flexDirection: "column" }}>
      <div class="chart-col-toolbar ctl-toolbar">
        {isClone && <CtlSearch value={plotFilter.value} onInput={v => { plotFilter.value = v; }} placeholder="Filter this graph…" />}
        <MetricPicker icon="x" title="X axis" value={xMetric} onChange={setX} format={fmtMetricName} wide groups={metricGroups} />
        <button type="button" class="ctl-btn ctl-swap" title="Swap X and Y" aria-label="Swap X and Y axes"
          onClick={() => { const x = xMetric, y = yMetric; setX(y); setY(x); }}><CtlIcon name="swap" /></button>
        <MetricPicker icon="y" title="Y axis" value={yMetric} onChange={setY} format={fmtMetricName} wide groups={metricGroups} />
        <CtlToggle active={mean} icon="mean" onClick={() => { ideaMean.value = !mean; }} title="Idea mean: one point per idea (mean of its runs)">mean</CtlToggle>
        <CtlToggle active={clip} icon="outliers" onClick={() => { clipOutliers.value = !clip; }} title="Hide outliers (values far outside the rest)" />
        {!hideClone && <span class="chart-toolbar-end"><IconButton onClick={() => { if (cloneChartPanel) cloneChartPanel("scatter", undefined, xMetric, yMetric); }} title="Clone this chart as a new tab">
          + Clone
        </IconButton></span>}
      </div>
      <div class="chart-col-canvas">
        <canvas ref={canvasRef} onMouseLeave={() => { setHover(null); }} />
      </div>
      {/* Same hover card as the main graph (the Chart.js tooltip is off). */}
      {hover && (() => {
        const d = hover.d;
        const idea = allIdeas.value[d.idea_id];
        const label = String(d.label ?? d.id);
        return <MiniTooltip anchor={hover.rect}>{experimentTipContent({
          label, ideaId: d.idea_id, ideaTitle: idea?.description, status: idea?.status,
          mark: d._ideaMean ? `idea/${d.idea_id} · mean of ${d._meanCount}` : undefined,
          metricName: fmtMetricName(yMetric), value: hover.y,
          extraMetrics: [{ name: fmtMetricName(xMetric), value: hover.x }],
          running: !!d._running, pct: runningProgress.value[label],
        })}</MiniTooltip>;
      })()}
    </div>
  );
}
