import { useRef, useEffect } from "preact/hooks";
import { Chart } from "chart.js/auto";
import { allExperiments, allIdeas, currentLayout, highlightedIdea } from "../../state/signals";
import {
  selectedMetric,
  selectedIdea,
  colorMode,
  improvementsOnly,
  activeTagFilters,
  tagFilterMode,
  reverseTime,
  showAbandoned,
  showConcluded,
  showRunning,
} from "../../state/settings";
import { buildChartData } from "../../lib/chart-data";
import type { ChartDataResult } from "../../lib/chart-data";

export function MetricsChart() {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const chartRef = useRef<Chart | null>(null);
  const innerRef = useRef<HTMLDivElement>(null);

  const experiments = allExperiments.value;
  const ideas = allIdeas.value;
  const layout = currentLayout.value;
  const metric = selectedMetric.value;
  const mode = colorMode.value;
  const impOnly = improvementsOnly.value;
  const tags = activeTagFilters.value;
  const tagMode = tagFilterMode.value;
  const highlighted = highlightedIdea.value;
  const reversed = reverseTime.value;

  // Build set of hidden idea statuses
  const hiddenStatuses = new Set<string>();
  if (!showAbandoned.value) hiddenStatuses.add("abandoned");
  if (!showConcluded.value) hiddenStatuses.add("concluded");
  if (!showRunning.value) { hiddenStatuses.add("active"); hiddenStatuses.add("suggested"); }

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
      chartRef.current.options.scales!.y!.title = {
        display: true,
        text: metric,
        color: "#8b949e",
        font: { size: 10 },
      };
      chartRef.current.resize();
      chartRef.current.update("none");
      return;
    }

    chartRef.current = createChart(canvasRef.current, metric, chartData);
  }, [metric, mode, impOnly, tags, tagMode, experiments, reversed, showAbandoned.value, showConcluded.value, showRunning.value]);

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

  return (
    <div id="chart-wrap">
      <div id="chart-inner" ref={innerRef}>
        <canvas ref={canvasRef} />
      </div>
    </div>
  );
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
            const ideaId = ds._expData[idx].idea_id;
            // Open the idea detail panel
            selectedIdea.value = ideaId;
            history.pushState(null, "", "/ideas/" + ideaId);
            // Highlight and scroll to the idea in the graph
            highlightedIdea.value = ideaId;
            const station = document.querySelector(
              `.subway-station[data-id="${ideaId}"], .subway-dot[data-id="${ideaId}"]`
            );
            if (station) {
              station.scrollIntoView({ behavior: "smooth", block: "center", inline: "center" });
            }
            setTimeout(() => {
              highlightedIdea.value = null;
            }, 3000);
          }
        }
      },
      scales: {
        x: {
          display: true,
          ticks: {
            color: "#8b949e",
            font: {
              size: 10,
              family: "SF Mono, Fira Code, Consolas, monospace",
            },
            autoSkip: true,
          },
          grid: { color: "#21262d" },
        },
        y: {
          title: {
            display: true,
            text: metricKey,
            color: "#8b949e",
            font: { size: 10 },
          },
          ticks: { color: "#484f58", font: { size: 10 } },
          grid: { color: "#21262d" },
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
          backgroundColor: "#161b22",
          titleColor: "#58a6ff",
          bodyColor: "#c9d1d9",
          borderColor: "#30363d",
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
                "exp/" + d.id + ": " + (d.description || "").slice(0, 50),
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
