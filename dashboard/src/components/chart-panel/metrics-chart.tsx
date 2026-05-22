import { useRef, useEffect, useMemo, useState } from "preact/hooks";
import { Chart } from "chart.js/auto";
import { allExperiments, allIdeas, currentLayout, highlightedIdea, cloneChartPanel, updatePanelTitle } from "../../state/signals";
import {
  selectedMetric,
  colorMode,
  colorTheme,
  improvementsOnly,
  activeTagFilters,
  tagFilterMode,
  reverseTime,
  showAbandoned,
  showConcluded,
  showRunning,
  clipOutliers,
  ideaMean,
} from "../../state/settings";
import { buildChartData, collectChartKeys } from "../../lib/chart-data";
import { navigateToIdea } from "../../lib/navigate";
import { getCssVar } from "../../lib/css-vars";
import type { ChartDataResult } from "../../lib/chart-data";

export function MetricsChart({ instanceId, initialMetric }: { instanceId?: string; initialMetric?: string } = {}) {
  // Cloned instances use local state; the original uses the global signal
  const isClone = !!instanceId;
  const [localMetric, setLocalMetric] = useState(initialMetric || "");
  const [logScale, setLogScale] = useState(false);
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
  const theme = colorTheme.value;  // subscribe — chart recreates on theme switch

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

  // Create or update chart when data/settings change
  useEffect(() => {
    if (!metric || !canvasRef.current) return;

    const chartData = buildChartData(
      metric,
      experiments,
      tags,
      tagMode,
      impOnly,
      mode,
      ideas,
      layout,
      reversed,
      hiddenStatuses,
      mean,
      hideRunning,
    );

    if (!chartData) return;

    // Size the container for horizontal scroll, capped to avoid
    // exceeding mobile browser canvas size limits (~4096px).
    if (innerRef.current) {
      const parent = innerRef.current.parentElement;
      const parentW = parent ? parent.clientWidth : 400;
      const maxCanvasW = 4000;
      const idealW = chartData.labels.length * 50;
      const minW = Math.max(parentW, Math.min(idealW, maxCanvasW));
      innerRef.current.style.width = minW + "px";
    }

    if (chartRef.current) {
      // Update in-place
      const ds = chartRef.current.data.datasets[0];
      ds.data = chartData.values;
      ds.pointBackgroundColor = chartData.pointBgColors as any;
      ds.pointBorderColor = chartData.pointColors as any;
      ds.pointBorderWidth = chartData.pointBorderWidths as any;
      ds.pointStyle = chartData.pointStyles as any;
      ds.pointRadius = chartData.pointRadii as any;
      (ds as any)._expData = chartData.expData;
      chartRef.current.data.labels = chartData.labels;
      const yBounds = clip ? computeYBounds(chartData.values) : {};
      const yScale = chartRef.current.options.scales!.y!;
      yScale.title = { display: true, text: metric, color: getCssVar("--text-muted"), font: { size: 10 } };
      yScale.min = yBounds.min;
      yScale.max = yBounds.max;
      (yScale as any).type = logScale ? "logarithmic" : "linear";
      chartRef.current.resize();
      chartRef.current.update("none");
      return;
    }

    chartRef.current = createChart(canvasRef.current, metric, chartData);
  }, [metric, mode, impOnly, tags, tagMode, experiments, reversed, showAbandoned.value, showConcluded.value, showRunning.value, clip, mean, logScale]);

  // Handle highlight changes separately (just update point sizes)
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    const ds = chart.data.datasets[0] as any;
    const expData = ds._expData as any[];
    if (!expData) return;

    if (highlighted !== null) {
      ds.pointRadius = expData.map((e: any) =>
        e.idea_id === highlighted ? 12 : 4
      );
      ds.pointBorderWidth = expData.map((e: any) =>
        e.idea_id === highlighted ? 3 : 1
      );
    } else {
      ds.pointRadius = expData.map((e: any) => (e._running ? 8 : 6));
      ds.pointBorderWidth = expData.map((e: any) => (e._running ? 2.5 : 1));
    }
    chart.update("none");
  }, [highlighted]);

  // Collect chartable keys grouped by source
  const grouped = useMemo(() => collectChartKeys(experiments), [experiments]);
  const allKeys = [...grouped.metrics, ...grouped.meta, ...grouped.timing];

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
        <button type="button" class="chart-toggle-btn" onClick={() => { if (cloneChartPanel) cloneChartPanel("metrics", metric); }} title="Clone this chart as a new tab">
          + Clone
        </button>
      </div>
      <div id="chart-wrap" style={{ flex: 1, minHeight: 0 }}>
        <div id="chart-inner" ref={innerRef}>
          <canvas ref={canvasRef} />
        </div>
      </div>
    </div>
  );
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
  chartData: ChartDataResult
): Chart {
  return new Chart(canvas, {
    type: "line",
    data: {
      labels: chartData.labels,
      datasets: [
        {
          label: metricKey,
          data: chartData.values,
          borderColor: "#8b949e44",
          pointBackgroundColor: chartData.pointBgColors,
          pointBorderColor: chartData.pointColors,
          pointBorderWidth: chartData.pointBorderWidths,
          pointStyle: chartData.pointStyles,
          pointRadius: chartData.pointRadii,
          pointHoverRadius: 10,
          tension: 0,
          fill: false,
          _expData: chartData.expData,
        } as any,
      ],
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
            color: getCssVar("--text-muted"),
            font: {
              size: 10,
              family: "SF Mono, Fira Code, Consolas, monospace",
            },
            autoSkip: true,
          },
          grid: { color: getCssVar("--border-soft") },
        },
        y: {
          ...(clipOutliers.value ? computeYBounds(chartData.values) : {}),
          title: {
            display: true,
            text: metricKey,
            color: getCssVar("--text-muted"),
            font: { size: 10 },
          },
          ticks: { color: getCssVar("--text-muted"), font: { size: 10 } },
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
