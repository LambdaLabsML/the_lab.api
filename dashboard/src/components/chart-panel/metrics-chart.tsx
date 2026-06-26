import { useRef, useEffect, useMemo, useState } from "preact/hooks";
import { Chart } from "chart.js/auto";
import { allExperiments, allIdeas, currentLayout, highlightedIdea, cloneChartPanel, updatePanelTitle } from "../../state/signals";
import {
  selectedMetric,
  colorMode,
  colorTheme,
  fontSize,
  improvementsOnly,
  activeTagFilters,
  tagFilterMode,
  reverseTime,
  showAbandoned,
  showConcluded,
  showRunning,
  clipOutliers,
  ideaMean,
  showBestLine,
  chartMinified,
  colorblindMode,
} from "../../state/settings";
import { buildChartData, collectChartKeys } from "../../lib/chart-data";
import { isLowerBetter } from "../../lib/colors";
import { fmtMetricName } from "../../lib/format";
import { MiniMetricsChart } from "./mini-metrics-chart";
import { navigateToIdea } from "../../lib/navigate";
import { getCssVar, getCssVarPx } from "../../lib/css-vars";
import type { ChartDataResult } from "../../lib/chart-data";

export function MetricsChart({ instanceId, initialMetric }: { instanceId?: string; initialMetric?: string } = {}) {
  // Cloned instances use local state; the original uses the global signal
  const isClone = !!instanceId;
  const [localMetric, setLocalMetric] = useState(initialMetric || "");
  const [logScale, setLogScale] = useState(false);
  const [visibilityTick, setVisibilityTick] = useState(0);
  const bestLine = showBestLine.value;
  const minified = chartMinified.value;
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const chartRef = useRef<Chart | null>(null);
  const innerRef = useRef<HTMLDivElement>(null);

  const experiments = allExperiments.value;
  const ideas = allIdeas.value;
  const layout = currentLayout.value;
  const metric = isClone ? localMetric : selectedMetric.value;
  const mode = colorMode.value;
  const impOnly = improvementsOnly.value;
  const tags = activeTagFilters.value;
  const tagMode = tagFilterMode.value;
  const highlighted = highlightedIdea.value;
  const reversed = reverseTime.value;
  const clip = clipOutliers.value;
  const mean = ideaMean.value;
  const theme = colorTheme.value;  // subscribe — chart recreates on theme/size switch
  const _fz = fontSize.value;      // subscribe — chart recreates on font-size switch
  // Refs must be declared AFTER theme/_fz to avoid TDZ (const not hoisted)
  const prevThemeRef = useRef(theme);
  const prevFzRef = useRef(_fz);

  // Idea-status filters (abandoned/concluded). The "running" toggle is
  // experiment-level — see hideRunning below — so completed experiments
  // under still-active ideas remain visible when running is toggled off.
  const hiddenStatuses = new Set<string>();
  if (!showAbandoned.value) hiddenStatuses.add("abandoned");
  if (!showConcluded.value) hiddenStatuses.add("concluded");
  const hideRunning = !showRunning.value;

  // Destroy chart only on unmount
  useEffect(() => {
    return () => {
      chartRef.current?.destroy();
      chartRef.current = null;
    };
  }, []);

  // When the canvas transitions from hidden (display:none dockview tab) to
  // visible, Chart.js's internal ResizeObserver doesn't always fire. Use an
  // IntersectionObserver to detect visibility and force a resize+redraw.
  // If the chart was never created (canvas had 0 dimensions on first mount —
  // common on mobile before dockview finishes layout), bump visibilityTick
  // to re-run the draw effect now that the canvas is properly sized.
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const observer = new IntersectionObserver((entries) => {
      if (entries[0].isIntersecting) {
        if (chartRef.current) {
          chartRef.current.resize();
          chartRef.current.update("none");
        } else {
          setVisibilityTick((t) => t + 1);
        }
      }
    }, { threshold: 0.01 });
    observer.observe(canvas);
    return () => observer.disconnect();
  }, []);

  // Create or update chart when data/settings change.
  // Heavy work (buildChartData + canvas draw) is deferred to the next
  // animation frame so the browser can repaint the toggled button state
  // first — otherwise useEffect blocks the repaint for 50-200ms and the
  // button appears frozen until the chart finishes updating.
  useEffect(() => {
    if (!metric || !canvasRef.current) return;

    // Sync: update theme/size refs and destroy chart if needed.
    // Must happen before the rAF so createChart gets fresh CSS vars.
    const themeOrSizeChanged = theme !== prevThemeRef.current || _fz !== prevFzRef.current;
    prevThemeRef.current = theme;
    prevFzRef.current = _fz;
    if (chartRef.current && themeOrSizeChanged) {
      chartRef.current.destroy();
      chartRef.current = null;
    }
    // When switching back from mini mode the canvas is a new DOM element —
    // the old Chart.js instance points at the unmounted canvas. Detect the
    // mismatch and recreate so the chart isn't drawn into a detached node.
    if (chartRef.current && (chartRef.current as any).canvas !== canvasRef.current) {
      chartRef.current.destroy();
      chartRef.current = null;
    }

    // Capture mutable refs for the rAF closure
    const canvas = canvasRef.current;
    const inner  = innerRef.current;

    const rafId = requestAnimationFrame(() => {
      if (!canvas.isConnected) return;  // component unmounted

      const chartData = buildChartData(
        metric, experiments, tags, tagMode, impOnly, mode,
        ideas, layout, reversed, hiddenStatuses, mean, hideRunning,
      );
      if (!chartData) return;

      // Minified mode: very small dots for global overview
      const radii = minified
        ? chartData.pointRadii.map(() => 2)
        : chartData.pointRadii;
      // Compute milestone set for gold rings on new global bests
      const lowerM = isLowerBetter(metric);
      const milestoneSetM = new Set<number>();
      let runBestM: number | null = null;
      for (let i = 0; i < chartData.values.length; i++) {
        const v = chartData.values[i];
        if (!isFinite(v)) continue;
        if (runBestM === null || (lowerM ? v < runBestM : v > runBestM)) { runBestM = v; milestoneSetM.add(i); }
      }
      const yellowM = getCssVar("--yellow") || "#d29922";

      const radiiM = chartData.pointRadii.map((r, i) =>
        milestoneSetM.has(i) ? (minified ? 4 : Math.max(r, 7)) : (minified ? 2 : r)
      );
      const borderWidthsM = chartData.pointBorderWidths.map((w, i) =>
        milestoneSetM.has(i) ? 2 : (minified ? 0 : w)
      );
      const borderColorsM = chartData.pointColors.map((c, i) =>
        milestoneSetM.has(i) ? yellowM : c
      );

      // Best-line dataset: step-function of running best value
      const bestLineData = bestLine ? computeBestLine(chartData.values, isLowerBetter(metric)) : null;

      if (inner) {
        const parentW = inner.parentElement?.clientWidth ?? 400;
        const minW = minified
          ? parentW
          : Math.max(parentW, Math.min(chartData.labels.length * 50, 4000));
        inner.style.width = minW + "px";
      }

      if (chartRef.current) {
        const ds = chartRef.current.data.datasets[0];
        ds.data = chartData.values;
        ds.pointBackgroundColor = chartData.pointBgColors as any;
        ds.pointBorderColor    = borderColorsM as any;
        ds.pointBorderWidth    = borderWidthsM as any;
        ds.pointStyle          = (minified ? chartData.pointStyles.map(() => "circle") : chartData.pointStyles) as any;
        ds.pointRadius         = radiiM as any;
        ds.pointHoverRadius    = minified ? 4 : 10;
        (ds as any)._expData   = chartData.expData;
        (ds as any)._milestones = milestoneSetM;
        chartRef.current.data.labels = chartData.labels;
        // Update or add/remove best-line dataset
        if (bestLineData) {
          if (chartRef.current.data.datasets.length < 2) {
            chartRef.current.data.datasets.push(makeBestLineDataset(bestLineData, metric));
          } else {
            chartRef.current.data.datasets[1].data = bestLineData;
          }
        } else {
          chartRef.current.data.datasets.splice(1);
        }
        const yBounds = clip ? computeYBounds(chartData.values) : {};
        const yScale  = chartRef.current.options.scales!.y!;
        yScale.title  = { display: !minified, text: fmtMetricName(metric), color: getCssVar("--text-faint"), font: { size: 8 } };
        yScale.min    = yBounds.min ?? (isLowerBetter(metric) ? undefined : 0);
        yScale.max    = yBounds.max;
        (yScale as any).type = logScale ? "logarithmic" : "linear";
        (chartRef.current.options.scales!.x as any).ticks.display = !minified;
        (chartRef.current.options.scales!.x as any).grid.display = !minified;
        chartRef.current.resize();
        chartRef.current.update("none");
        return;
      }

      chartRef.current = createChart(canvas, metric, chartData, { minified, bestLineData, metricKey: metric });
    }); // end requestAnimationFrame

    return () => cancelAnimationFrame(rafId);
  }, [metric, mode, impOnly, tags, tagMode, experiments, reversed, showAbandoned.value, showConcluded.value, showRunning.value, clip, mean, logScale, theme, _fz, visibilityTick, bestLine, minified, colorblindMode.value]);

  // Handle highlight changes separately (just update point sizes).
  // Mini mode keeps dots small — only the specifically highlighted idea expands.
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    const ds = chart.data.datasets[0] as any;
    const expData = ds._expData as any[];
    if (!expData) return;

    const mini = chartMinified.value;
    if (highlighted !== null) {
      ds.pointRadius = expData.map((e: any) =>
        e.idea_id === highlighted ? (mini ? 5 : 12) : (mini ? 2 : 4)
      );
      ds.pointBorderWidth = expData.map((e: any) =>
        e.idea_id === highlighted ? (mini ? 1.5 : 3) : (mini ? 0 : 1)
      );
    } else {
      ds.pointRadius = expData.map((e: any) =>
        mini ? 2 : (e._running ? 8 : 6)
      );
      ds.pointBorderWidth = expData.map((e: any) =>
        mini ? 0 : (e._running ? 2.5 : 1)
      );
    }
    chart.update("none");
  }, [highlighted, chartMinified.value]);

  // Collect chartable keys grouped by source
  const grouped = useMemo(() => collectChartKeys(experiments), [experiments]);
  const allKeys = [...grouped.metrics, ...grouped.nested, ...grouped.meta, ...grouped.timing];

  const setMetric = isClone
    ? (v: string) => { setLocalMetric(v); if (instanceId && updatePanelTitle) updatePanelTitle(instanceId, `Metrics: ${v}`); }
    : (v: string) => { selectedMetric.value = v; };

  // Auto-select best metric via useEffect. Re-runs when allKeys changes (more experiments load).
  // Also upgrades to preferred metrics if a non-preferred metric was selected initially.
  const allKeysStr = allKeys.join(',');
  useEffect(() => {
    if (isClone) return;
    if (allKeys.length === 0) return;
    const preferred = [
      "accuracy_per_mtoken", "agent_accuracy", "accuracy",
      "score", "total_score", "progress_score", "final_score",
      "f1", "bleu", "rouge", "pass_at_1", "pass_at_10",
    ];
    const current = selectedMetric.value;
    const alreadyPreferred = current && preferred.includes(current);
    if (alreadyPreferred) return; // already on a good metric
    // Pick best preferred metric: prefer non-zero values AND meaningful variance (not constants)
    const nonZeroKeys = allKeys.filter(k => {
      const lower = isLowerBetter(k);
      const vals = experiments
        .filter(e => !e._running && typeof e.metrics?.[k] === "number")
        .map(e => e.metrics![k] as number);
      if (vals.length === 0) return false;
      const hasNonZero = lower ? vals.some(v => v < Infinity) : vals.some(v => v > 0);
      if (!hasNonZero) return false;
      // Exclude constant metrics (all same value) — they're config params, not performance
      const hasVariance = vals.some(v => v !== vals[0]);
      return hasVariance;
    });
    const keyPool = nonZeroKeys.length > 0 ? nonZeroKeys : allKeys;
    const best = preferred.find(k => keyPool.includes(k)) || preferred.find(k => allKeys.includes(k));
    if (best && best !== current) setMetric(best);
    else if (!current) setMetric(keyPool[0] || allKeys[0]);
  }, [allKeysStr, isClone]); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <div class="chart-toolbar">
        <span>
          Metric:{" "}
          <select value={metric} onChange={(e) => { setMetric((e.target as HTMLSelectElement).value); }}>
            {grouped.metrics.length > 0 && <optgroup label="Metrics">{grouped.metrics.map((k) => <option key={k} value={k}>{fmtMetricName(k)}</option>)}</optgroup>}
            {grouped.nested.length > 0 && <optgroup label="Nested">{grouped.nested.map((k) => <option key={k} value={k}>{fmtMetricName(k)}</option>)}</optgroup>}
            {grouped.timing.length > 0 && <optgroup label="Timing">{grouped.timing.map((k) => <option key={k} value={k}>{fmtMetricName(k)}</option>)}</optgroup>}
            {grouped.meta.length > 0 && <optgroup label="Meta">{grouped.meta.map((k) => <option key={k} value={k}>{fmtMetricName(k)}</option>)}</optgroup>}
          </select>
        </span>
        {(() => {
          // Count milestones for the Step button label
          const lower = isLowerBetter(metric);
          let best: number | null = null, mCount = 0;
          for (const e of experiments) {
            if (e._running) continue;
            const v = e.metrics?.[metric];
            if (typeof v !== "number") continue;
            if (best === null || (lower ? v < best : v > best)) { best = v; mCount++; }
          }
          return (
            <button type="button" class={`chart-toggle-btn${impOnly ? " active" : ""}`} onClick={() => { improvementsOnly.value = !impOnly; }} title={impOnly ? `Step-function: ${mCount} new bests (click for all experiments)` : "Show step-function — only new bests"}>
              ▲ {impOnly ? `Step${mCount > 0 ? ` (${mCount})` : ""}` : "All"}
            </button>
          );
        })()}
        <button type="button" class={`chart-toggle-btn${mean ? " active" : ""}`} onClick={() => { ideaMean.value = !mean; }} title="One point per idea (mean)">
          μ Idea Mean
        </button>
        <button type="button" class={`chart-toggle-btn${clip ? " active" : ""}`} onClick={() => { clipOutliers.value = !clip; }} title="Hide outliers">
          ⤢ Outliers
        </button>
        <button type="button" class={`chart-toggle-btn${logScale ? " active" : ""}`} onClick={() => { setLogScale(!logScale); }} title="Logarithmic Y axis">
          Log Y
        </button>
        <button type="button" class={`chart-toggle-btn${bestLine ? " active" : ""}`} onClick={() => { showBestLine.value = !bestLine; }} title="Show current-best step line">
          ⌇ Current Best
        </button>
        <button type="button" class={`chart-toggle-btn${minified ? " active" : ""}`} onClick={() => { chartMinified.value = !minified; }} title="Minified: small dots, global overview">
          ⊡ Mini
        </button>
        <button type="button" class="chart-toggle-btn" onClick={() => { if (cloneChartPanel) cloneChartPanel("metrics", metric); }} title="Clone this chart as a new tab">
          + Clone
        </button>
        {/* Current best badge — quickly visible without needing to hover the chart */}
        {bestLine && (() => {
          const vals = allExperiments.value.filter(e => !e._running && typeof e.metrics?.[metric] === "number").map(e => e.metrics![metric] as number);
          if (vals.length === 0) return null;
          const lower = isLowerBetter(metric);
          const peak = lower ? Math.min(...vals) : Math.max(...vals);
          const fmt = Math.abs(peak) >= 100 ? peak.toFixed(0) : Math.abs(peak) >= 1 ? peak.toFixed(2) : peak.toFixed(3);
          return (
            <span class="chart-best-badge" title={`Current best ${fmtMetricName(metric)}`}>
              ★ {fmt}
            </span>
          );
        })()}
      </div>
      {/* Data distribution hint — shown when most values cluster near bottom */}
      {(() => {
        const vals = allExperiments.value.filter(e => !e._running && typeof e.metrics?.[metric] === "number").map(e => e.metrics![metric] as number);
        if (vals.length < 5) return null;
        const lower = isLowerBetter(metric);
        const peak = lower ? Math.min(...vals) : Math.max(...vals);
        const median = [...vals].sort((a,b) => a-b)[Math.floor(vals.length/2)];
        const pctAtPeak = vals.filter(v => lower ? v === peak : v === peak).length;
        // Show hint only when data is sparse at top (median < 25% of peak)
        if (peak === 0 || median / peak > 0.3) return null;
        return (
          <div class="chart-dist-hint">
            {vals.length} exp
            {impOnly && (
              <span style={{ color: "var(--text-faint)", fontSize: "8px" }}>
                {" "}({milestoneSet.size || pctAtPeak} milestones)
              </span>
            )}
            <span style={{ color: "var(--text-faint)" }}> · </span>
            <span style={{ color: "var(--border-soft)", fontWeight: 600 }}>·</span>
            {" "}median {Math.abs(median) >= 1 ? median.toFixed(1) : median.toFixed(2)}
            <span style={{ color: "var(--text-faint)" }}> · </span>
            <span style={{ color: "var(--purple)", fontWeight: 600 }}>·</span>
            {" "}best {Math.abs(peak) >= 100 ? peak.toFixed(0) : Math.abs(peak) >= 1 ? peak.toFixed(2) : peak.toFixed(3)}
            {impOnly && allExperiments.value.some(e => e._running) && (
              <>
                <span style={{ color: "var(--text-faint)" }}> · </span>
                <span style={{ color: "var(--yellow)", fontSize: "8px" }}>△ running</span>
              </>
            )}
          </div>
        );
      })()}
      {/* Empty state — shown when no metric selected or no data yet */}
      {(!metric || allKeys.length === 0) && (
        <div
          style={{ flex: 1, display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", gap: 8, color: "var(--text-faint)", fontSize: "var(--text-sm)", fontFamily: "var(--font-mono)", cursor: allKeys.length > 0 ? "pointer" : "default" }}
          onClick={allKeys.length > 0 ? () => { innerRef.current?.querySelector("select")?.focus(); (innerRef.current?.querySelector("select") as HTMLElement)?.click?.(); } : undefined}
          title={allKeys.length > 0 ? "Click to select a metric" : undefined}
        >
          {allKeys.length === 0 ? (
            <>
              <span style={{ fontSize: "20px", opacity: 0.3 }}>◈</span>
              <span>loading experiments…</span>
              <span style={{ fontSize: "10px", opacity: 0.4 }}>waiting for data</span>
            </>
          ) : (
            <>
              <span style={{ fontSize: "24px", opacity: 0.4 }}>◇</span>
              <span style={{ fontWeight: 500 }}>select a metric above ↑</span>
              <span style={{ fontSize: "10px", opacity: 0.5 }}>{allKeys.length} metrics available · click here to select</span>
              {/* Show preferred metrics as quick-select options */}
              <div style={{ display: "flex", gap: 6, marginTop: 4, flexWrap: "wrap", justifyContent: "center", maxWidth: 400 }}>
                {["score","n_levels_completed","accuracy","f1","total_score"].filter(k => allKeys.includes(k)).slice(0, 4).map(k => (
                  <span key={k} style={{ fontSize: "9px", padding: "2px 8px", border: "1px solid color-mix(in srgb, var(--accent) 40%, transparent)", borderRadius: 4, color: "var(--accent)", cursor: "pointer", background: "color-mix(in srgb, var(--accent) 8%, transparent)" }}
                    onClick={(ev) => { ev.stopPropagation(); const sel = innerRef.current?.querySelector("select") as HTMLSelectElement | null; if (sel) { sel.value = k; sel.dispatchEvent(new Event("change", {bubbles: true})); } }}>
                    {k.replace(/_/g, " ")}
                  </span>
                ))}
              </div>
            </>
          )}
        </div>
      )}
      <div id="chart-wrap" style={{ flex: 1, minHeight: 0, display: (!metric || allKeys.length === 0) ? "none" : undefined }}>
        {minified ? (
          <div style={{ width: "100%", height: "100%" }}>
            <MiniMetricsChart
              metric={metric}
              experiments={experiments}
              ideas={ideas}
              layout={layout}
              hiddenStatuses={hiddenStatuses}
              hideRunning={hideRunning}
              impOnly={impOnly}
              colorMode={mode}
              tags={tags}
              tagMode={tagMode}
              reversed={reversed}
              mean={mean}
            />
          </div>
        ) : (
          <div id="chart-inner" ref={innerRef}>
            <canvas ref={canvasRef} />
          </div>
        )}
      </div>
    </div>
  );
}

/** Build the running-best step-function values for the best-line dataset. */
function computeBestLine(values: number[], lowerIsBetter: boolean): number[] {
  let best: number | null = null;
  return values.map((v) => {
    if (best === null || (lowerIsBetter ? v < best : v > best)) best = v;
    return best!;
  });
}

/** Chart.js dataset config for the best-line. */
function makeBestLineDataset(data: number[], metricKey: string) {
  const peak = Math.max(...data.filter(isFinite));
  return {
    label: `best ${fmtMetricName(metricKey)}: ${peak.toFixed ? peak.toFixed(3) : peak}`,
    data,
    borderColor: getCssVar("--purple") || "#a371f7",
    borderWidth: 1.5,
    borderDash: [4, 4],
    pointRadius: 0,
    pointHoverRadius: 3,
    tension: 0,
    fill: false,
    stepped: "before",
    order: 0,
  } as any;
}

/** IQR-based y-axis bounds that clip extreme outliers. */
function computeYBounds(values: number[]): { min?: number; max?: number } {
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

function createChart(
  canvas: HTMLCanvasElement,
  metricKey: string,
  chartData: ChartDataResult,
  opts: { minified?: boolean; bestLineData?: number[] | null; metricKey?: string } = {}
): Chart {
  const { minified = false, bestLineData = null } = opts;

  // Mark experiments that set a new global best — shown with a gold ring
  const lower = isLowerBetter(metricKey);
  const milestoneSet = new Set<number>();
  let runBest: number | null = null;
  for (let i = 0; i < chartData.values.length; i++) {
    const v = chartData.values[i];
    if (!isFinite(v)) continue;
    if (runBest === null || (lower ? v < runBest : v > runBest)) { runBest = v; milestoneSet.add(i); }
  }
  const yellow = getCssVar("--yellow") || "#d29922";

  const radii = chartData.pointRadii.map((r, i) =>
    milestoneSet.has(i) ? (minified ? 4 : Math.max(r, 7)) : (minified ? 2 : r)
  );
  const borderWidths = chartData.pointBorderWidths.map((w, i) =>
    milestoneSet.has(i) ? 2 : (minified ? 0 : w)
  );
  const borderColors = chartData.pointColors.map((c, i) =>
    milestoneSet.has(i) ? yellow : c
  );

  const datasets: any[] = [
    {
      label: metricKey,
      data: chartData.values,
      spanGaps: false,  // break line at null values (e.g. running exps in improvements mode)
      borderColor: `color-mix(in srgb, ${getCssVar("--text-muted")} 27%, transparent)`,
      pointBackgroundColor: chartData.pointBgColors,
      pointBorderColor: borderColors,
      pointBorderWidth: borderWidths,
      pointStyle: minified ? chartData.pointStyles.map(() => "circle") : chartData.pointStyles,
      pointRadius: radii,
      pointHoverRadius: minified ? 4 : 10,
      tension: 0,
      fill: false,
      order: 1,
      _expData: chartData.expData,
      _milestones: milestoneSet,
    },
  ];
  if (bestLineData) {
    datasets.unshift(makeBestLineDataset(bestLineData, metricKey));
  }

  // Median reference line — subtle gray horizontal showing typical performance
  const finiteVals = chartData.values.filter(isFinite);
  if (finiteVals.length >= 5) {
    const sorted = [...finiteVals].sort((a, b) => a - b);
    const median = sorted[Math.floor(sorted.length / 2)];
    const peak = lower ? Math.min(...finiteVals) : Math.max(...finiteVals);
    // Only show median line when there's meaningful gap (median < 60% of peak)
    if (peak !== 0 && Math.abs(median / peak) < 0.6) {
      datasets.unshift({
        label: `median ${fmtMetricName(metricKey)}`,
        data: chartData.labels.map(() => median),
        borderColor: getCssVar("--border"),
        borderWidth: 1,
        borderDash: [2, 6],
        pointRadius: 0,
        pointHoverRadius: 0,
        tension: 0,
        fill: false,
        stepped: false,
        order: -1,
      } as any);
    }
  }
  return new Chart(canvas, {
    type: "line",
    data: {
      labels: chartData.labels,
      datasets,
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: 400 },
      onClick(_evt, elements) {
        if (elements.length > 0) {
          const idx = elements[0].index;
          const ds = this.data.datasets[0] as any;
          if (ds?._expData?.[idx]) {
            const d = ds._expData[idx];
            navigateToIdea(d.idea_id, d.label || d.id);
          }
        }
      },
      scales: {
        x: {
          display: true,
          ticks: {
            display: !minified,
            color: getCssVar("--text-faint"),
            font: { size: 8, family: "var(--font-mono, SF Mono, monospace)" },
            autoSkip: true,
            autoSkipPadding: 6,
            maxRotation: 45,
          },
          grid: { display: !minified, color: getCssVar("--border-soft") },
        },
        y: {
          ...(clipOutliers.value ? computeYBounds(chartData.values) : {}),
          title: {
            display: !minified,
            text: fmtMetricName(metricKey),
            color: getCssVar("--text-faint"),
            font: { size: 8 },
          },
          ticks: { color: getCssVar("--text-faint"), font: { size: 8 } },
          grid: { color: getCssVar("--border-soft") },
          // Ensure chart uses at least 70% of the y range so dots aren't all crammed at bottom
          suggestedMin: isLowerBetter(metricKey) ? undefined : 0,
        },
      },
      onHover(_evt, elements) {
        if (elements.length > 0) {
          const ds = this.data.datasets[0] as any;
          if (ds?._expData?.[elements[0].index]) {
            highlightedIdea.value = ds._expData[elements[0].index].idea_id;
          }
        } else {
          highlightedIdea.value = null;
        }
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: getCssVar("--bg-elev"),
          titleColor: getCssVar("--text"),
          bodyColor: getCssVar("--text-muted"),
          borderColor: getCssVar("--border"),
          borderWidth: 1,
          maxWidth: 350,
          titleFont: {
            family: "SF Mono, Fira Code, Consolas, monospace",
            size: 11,
          },
          bodyFont: {
            family: "SF Mono, Fira Code, Consolas, monospace",
            size: 11,
          },
          callbacks: {
            title(items) {
              const d = (items[0].dataset as any)._expData[
                items[0].dataIndex
              ];
              const idea = allIdeas.value[d.idea_id];
              const desc = idea?.description || d.idea_description || "";
              // Truncate long descriptions
              const short = desc.length > 60 ? desc.slice(0, 57) + "..." : desc;
              return "idea #" + d.idea_id + ": " + short;
            },
            afterTitle(items) {
              const mk = items[0].dataset.label!;
              const isMilestone = (items[0].dataset as any)._milestones?.has(items[0].dataIndex);
              return (isMilestone ? "⭐ new best · " : "") + mk + " = " + items[0].formattedValue;
            },
            label(item) {
              const d = (item.dataset as any)._expData[item.dataIndex];
              return [
                d._ideaMean
                  ? `idea/${d.idea_id} mean (${d._meanCount} experiments)`
                  : "exp/" + (d.label || d.id) + ": " + (d.description || "").slice(0, 50),
                d._running ? "\u25B6 running (from progress)" : "",
                d.runtime ? "runtime: " + d.runtime : "",
                d.finished_at
                  ? "at " + new Date(d.finished_at).toLocaleString()
                  : "",
              ].filter(Boolean);
            },
          },
        },
      },
    },
  });
}
