import { useRef, useEffect, useMemo, useState } from "preact/hooks";
import { Chart } from "chart.js/auto";
import { allExperiments, allIdeas, currentLayout, highlightedIdea, cloneChartPanel, updatePanelTitle } from "../../state/signals";
import { navigateToIdea } from "../../lib/navigate";
import {
  selectedMetric,
  selectedIdea,
  colorMode,
  colorTheme,
  improvementsOnly,
  activeTagFilters,
  tagFilterMode,
  showAbandoned,
  showConcluded,
  showRunning,
  clipOutliers,
  ideaMean,
  scatterXMetric,
  scatterYMetric,
} from "../../state/settings";
import { filterMetricExperiments, collectChartKeys, resolveNumericValue } from "../../lib/chart-data";
import { IDEA_PALETTE, _colorForExp, isLowerBetter } from "../../lib/colors";
import { resolveColor } from "../../lib/css-vars";
import { getCssVar } from "../../lib/css-vars";
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

export function ScatterChart({ instanceId, initialXMetric, initialYMetric }: { instanceId?: string; initialXMetric?: string; initialYMetric?: string } = {}) {
  const isClone = !!instanceId;
  const [localX, setLocalX] = useState(initialXMetric || "");
  const [localY, setLocalY] = useState(initialYMetric || "");

  const canvasRef = useRef<HTMLCanvasElement>(null);
  const chartRef = useRef<Chart | null>(null);

  const experiments = allExperiments.value;

  // Compute chartable keys grouped by source
  const grouped = useMemo(() => collectChartKeys(experiments), [experiments]);
  const metricKeys = [...grouped.metrics, ...grouped.meta, ...grouped.timing];
  const ideas = allIdeas.value;
  const layout = currentLayout.value;
  const theme = colorTheme.value;  // subscribe — chart recreates on theme switch
  const mode = colorMode.value;
  const impOnly = improvementsOnly.value;
  const tags = activeTagFilters.value;
  const tagMode = tagFilterMode.value;
  const clip = clipOutliers.value;
  const mean = ideaMean.value;
  const highlighted = highlightedIdea.value;

  const xMetric = isClone ? localX : scatterXMetric.value;
  const yMetric = isClone ? localY : scatterYMetric.value;

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

  // Auto-select metrics if not set
  if (!xMetric && metricKeys.length > 0) {
    setX(metricKeys[0]);
  }
  if (!yMetric && metricKeys.length > 1) {
    setY(metricKeys.length > 1 ? metricKeys[1] : metricKeys[0]);
  }

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

    // Apply improvements-only filter (based on y metric direction)
    if (impOnly && !mean) {
      const lower = isLowerBetter(yMetric);
      let best = lower ? Infinity : -Infinity;
      filtered = filtered.filter((e) => {
        if (e._running) return true;
        const v = resolveNumericValue(e, yMetric) ?? 0;
        if (lower ? v < best : v > best) {
          best = v;
          return true;
        }
        return false;
      });
    }

    // Color computation
    const metricFiltered = filterMetricExperiments(
      selectedMetric.value || yMetric, experiments, tags, tagMode, hiddenStatuses, hideRunning,
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
    const radii = filtered.map((e) => (e._running ? 8 : 6));
    const borderWidths = filtered.map((e) => (e._running ? 2.5 : 1));
    const styles = filtered.map((e) => (e._running ? "triangle" : "circle") as string);

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
      xScale.title = { display: true, text: xMetric, color: getCssVar("--text-muted"), font: { size: 10 } };
      yScale.title = { display: true, text: yMetric, color: getCssVar("--text-muted"), font: { size: 10 } };
      xScale.min = xBounds.min;
      xScale.max = xBounds.max;
      yScale.min = yBounds.min;
      yScale.max = yBounds.max;
      chartRef.current.resize();
      chartRef.current.update("none");
      return;
    }

    chartRef.current = new Chart(canvasRef.current, {
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
            pointHoverRadius: 10,
            _expData: filtered,
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
            ...xBounds,
            title: { display: true, text: xMetric, color: getCssVar("--text-muted"), font: { size: 10 } },
            ticks: { color: getCssVar("--text-muted"), font: { size: 10 } },
            grid: { color: getCssVar("--border") },
          },
          y: {
            ...yBounds,
            title: { display: true, text: yMetric, color: getCssVar("--text-muted"), font: { size: 10 } },
            ticks: { color: getCssVar("--text-muted"), font: { size: 10 } },
            grid: { color: getCssVar("--border") },
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
                const d = (items[0].dataset as any)._expData[items[0].dataIndex];
                const idea = allIdeas.value[d.idea_id];
                const desc = idea?.description || d.idea_description || "";
                const short = desc.length > 60 ? desc.slice(0, 57) + "..." : desc;
                return "idea #" + d.idea_id + ": " + short;
              },
              label(item) {
                const d = (item.dataset as any)._expData[item.dataIndex];
                const xVal = d.metrics?.[scatterXMetric.value] ?? item.parsed.x;
                const yVal = d.metrics?.[scatterYMetric.value] ?? item.parsed.y;
                return [
                  `${scatterXMetric.value} = ${typeof xVal === "number" ? xVal.toPrecision(4) : xVal}`,
                  `${scatterYMetric.value} = ${typeof yVal === "number" ? yVal.toPrecision(4) : yVal}`,
                  d._ideaMean
                    ? `idea/${d.idea_id} mean (${d._meanCount} experiments)`
                    : "exp/" + (d.label || d.id) + ": " + (d.description || "").slice(0, 50),
                  d._running ? "\u25B6 running" : "",
                ].filter(Boolean);
              },
            },
          },
        },
      },
    });
  }, [xMetric, yMetric, mode, impOnly, tags, tagMode, experiments, showAbandoned.value, showConcluded.value, showRunning.value, clip, mean]);

  // Handle highlight changes separately
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    const ds = chart.data.datasets[0] as any;
    const expData = ds._expData as any[];
    if (!expData) return;

    if (highlighted !== null) {
      ds.pointRadius = expData.map((e: any) => (e.idea_id === highlighted ? 12 : 4));
      ds.pointBorderWidth = expData.map((e: any) => (e.idea_id === highlighted ? 3 : 1));
    } else {
      ds.pointRadius = expData.map((e: any) => (e._running ? 8 : 6));
      ds.pointBorderWidth = expData.map((e: any) => (e._running ? 2.5 : 1));
    }
    chart.update("none");
  }, [highlighted]);

  return (
    <div class="chart-col" style={{ height: "100%", display: "flex", flexDirection: "column" }}>
      <div class="chart-col-toolbar">
        X:{" "}
        <select value={xMetric} onChange={(e) => { setX((e.target as HTMLSelectElement).value); }}>
          {grouped.metrics.length > 0 && <optgroup label="Metrics">{grouped.metrics.map((k) => <option key={k} value={k}>{k}</option>)}</optgroup>}
          {grouped.timing.length > 0 && <optgroup label="Timing">{grouped.timing.map((k) => <option key={k} value={k}>{k}</option>)}</optgroup>}
          {grouped.meta.length > 0 && <optgroup label="Meta">{grouped.meta.map((k) => <option key={k} value={k}>{k}</option>)}</optgroup>}
        </select>
        {" "}Y:{" "}
        <select value={yMetric} onChange={(e) => { setY((e.target as HTMLSelectElement).value); }}>
          {grouped.metrics.length > 0 && <optgroup label="Metrics">{grouped.metrics.map((k) => <option key={k} value={k}>{k}</option>)}</optgroup>}
          {grouped.timing.length > 0 && <optgroup label="Timing">{grouped.timing.map((k) => <option key={k} value={k}>{k}</option>)}</optgroup>}
          {grouped.meta.length > 0 && <optgroup label="Meta">{grouped.meta.map((k) => <option key={k} value={k}>{k}</option>)}</optgroup>}
        </select>
        {" "}
        <button type="button" class="chart-toggle-btn" onClick={() => { if (cloneChartPanel) cloneChartPanel("scatter", undefined, xMetric, yMetric); }} title="Clone this chart as a new tab">
          + Clone
        </button>
      </div>
      <div class="chart-col-canvas">
        <canvas ref={canvasRef} />
      </div>
    </div>
  );
}
