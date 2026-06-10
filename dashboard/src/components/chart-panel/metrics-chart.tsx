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
      const borderWidths = minified
        ? chartData.pointBorderWidths.map(() => 0)
        : chartData.pointBorderWidths;

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
        ds.pointBorderColor    = chartData.pointColors as any;
        ds.pointBorderWidth    = borderWidths as any;
        ds.pointStyle          = (minified ? chartData.pointStyles.map(() => "circle") : chartData.pointStyles) as any;
        ds.pointRadius         = radii as any;
        ds.pointHoverRadius    = minified ? 4 : 10;
        (ds as any)._expData   = chartData.expData;
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
        yScale.title  = { display: !minified, text: metric, color: getCssVar("--text-muted"), font: { size: getCssVarPx("--text-xs") } };
        yScale.min    = yBounds.min;
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

  if (!metric && allKeys.length > 0) {
    const preferred = ["accuracy_per_mtoken", "agent_accuracy", "accuracy"];
    const match = preferred.find((k) => allKeys.includes(k));
    setMetric(match || allKeys[0]);
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <div class="chart-toolbar">
        <span>
          Metric:{" "}
          <select value={metric} onChange={(e) => { setMetric((e.target as HTMLSelectElement).value); }}>
            {grouped.metrics.length > 0 && <optgroup label="Metrics">{grouped.metrics.map((k) => <option key={k} value={k}>{k}</option>)}</optgroup>}
            {grouped.nested.length > 0 && <optgroup label="Nested">{grouped.nested.map((k) => <option key={k} value={k}>{k}</option>)}</optgroup>}
            {grouped.timing.length > 0 && <optgroup label="Timing">{grouped.timing.map((k) => <option key={k} value={k}>{k}</option>)}</optgroup>}
            {grouped.meta.length > 0 && <optgroup label="Meta">{grouped.meta.map((k) => <option key={k} value={k}>{k}</option>)}</optgroup>}
          </select>
        </span>
        <button type="button" class={`chart-toggle-btn${impOnly ? " active" : ""}`} onClick={() => { improvementsOnly.value = !impOnly; }} title="Show only improvements">
          ▲ Improvements
        </button>
        <button type="button" class={`chart-toggle-btn${mean ? " active" : ""}`} onClick={() => { ideaMean.value = !mean; }} title="One point per idea (mean)">
          μ Idea Mean
        </button>
        <button type="button" class={`chart-toggle-btn${clip ? " active" : ""}`} onClick={() => { clipOutliers.value = !clip; }} title="Hide outliers">
          ⤢ Outliers
        </button>
        <button type="button" class={`chart-toggle-btn${logScale ? " active" : ""}`} onClick={() => { setLogScale(!logScale); }} title="Logarithmic Y axis">
          log
        </button>
        <button type="button" class={`chart-toggle-btn${bestLine ? " active" : ""}`} onClick={() => { showBestLine.value = !bestLine; }} title="Show current-best step line">
          ⌇ Best
        </button>
        <button type="button" class={`chart-toggle-btn${minified ? " active" : ""}`} onClick={() => { chartMinified.value = !minified; }} title="Minified: small dots, global overview">
          ⊡ Mini
        </button>
        <button type="button" class="chart-toggle-btn" onClick={() => { if (cloneChartPanel) cloneChartPanel("metrics", metric); }} title="Clone this chart as a new tab">
          + Clone
        </button>
      </div>
      <div id="chart-wrap" style={{ flex: 1, minHeight: 0 }}>
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
  return {
    label: `best ${metricKey}`,
    data,
    borderColor: getCssVar("--purple") || "#a371f7",
    borderWidth: 1.5,
    borderDash: [4, 4],
    pointRadius: 0,
    pointHoverRadius: 0,
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
  const radii = minified ? chartData.pointRadii.map(() => 2) : chartData.pointRadii;
  const borderWidths = minified ? chartData.pointBorderWidths.map(() => 0) : chartData.pointBorderWidths;
  const datasets: any[] = [
    {
      label: metricKey,
      data: chartData.values,
      borderColor: `color-mix(in srgb, ${getCssVar("--text-muted")} 27%, transparent)`,
      pointBackgroundColor: chartData.pointBgColors,
      pointBorderColor: chartData.pointColors,
      pointBorderWidth: borderWidths,
      pointStyle: minified ? chartData.pointStyles.map(() => "circle") : chartData.pointStyles,
      pointRadius: radii,
      pointHoverRadius: minified ? 4 : 10,
      tension: 0,
      fill: false,
      order: 1,
      _expData: chartData.expData,
    },
  ];
  if (bestLineData) {
    datasets.unshift(makeBestLineDataset(bestLineData, metricKey));
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
            color: getCssVar("--text-muted"),
            font: {
              size: 10,
              family: "SF Mono, Fira Code, Consolas, monospace",
            },
            autoSkip: true,
          },
          grid: { display: !minified, color: getCssVar("--border-soft") },
        },
        y: {
          ...(clipOutliers.value ? computeYBounds(chartData.values) : {}),
          title: {
            display: !minified,
            text: metricKey,
            color: getCssVar("--text-muted"),
            font: { size: getCssVarPx("--text-xs") },
          },
          ticks: { color: getCssVar("--text-muted"), font: { size: getCssVarPx("--text-xs") } },
          grid: { color: getCssVar("--border-soft") },
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
              // Show the y-axis value prominently
              const mk = items[0].dataset.label!;
              return mk + " = " + items[0].formattedValue;
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
