import type { Signal } from "@preact/signals";
import {
  chartOpen,
  selectedMetric,
  colorMode,
  improvementsOnly,
  showAbandoned,
  showConcluded,
  showRunning,
  clipOutliers,
  ideaMean,
  scatterOpen,
  filterBarOpen,
} from "../../state/settings";
import { allExperiments } from "../../state/signals";
import { MetricsChart } from "./metrics-chart";
import { ScatterChart } from "./scatter-chart";
import { TagFilter } from "./tag-filter";
import { useMemo } from "preact/hooks";

export function ChartPanel() {
  const open = chartOpen.value;
  const experiments = allExperiments.value;
  const metric = selectedMetric.value;
  const scatter = scatterOpen.value;
  const filterOpen = filterBarOpen.value;

  // Collect all metric keys from experiments
  const metricKeys = useMemo(() => {
    const keys = new Set<string>();
    for (const exp of experiments) {
      if (exp.metrics) {
        for (const k of Object.keys(exp.metrics)) keys.add(k);
      }
    }
    return [...keys].sort();
  }, [experiments]);

  // Auto-select a preferred metric if none selected
  if (!metric && metricKeys.length > 0) {
    const preferred = ["accuracy_per_mtoken", "agent_accuracy", "accuracy"];
    const match = preferred.find((k) => metricKeys.includes(k));
    selectedMetric.value = match || metricKeys[0];
  }

  function toggle() {
    chartOpen.value = !chartOpen.value;
  }

  return (
    <>
      <div id="chart-toggle" onClick={toggle}>
        <span class={`arrow${open ? " open" : ""}`}>&#9654;</span> Metrics over
        time
      </div>
      <div id="chart-panel" class={open ? "" : "collapsed"}>
        {/* ---- Shared toolbar: metric selector + toggles ---- */}
        <div id="metric-selector">
          Metric:{" "}
          <select
            value={metric}
            onChange={(e) => {
              selectedMetric.value = (e.target as HTMLSelectElement).value;
            }}
          >
            {metricKeys.map((k) => (
              <option key={k} value={k}>
                {k}
              </option>
            ))}
          </select>
          {" "}Color:{" "}
          <select
            value={colorMode.value}
            onChange={(e) => {
              colorMode.value = (e.target as HTMLSelectElement).value;
            }}
          >
            <option value="status+improve">status + metric improvement</option>
            <option value="lane">by lane</option>
            <option value="status">by status</option>
            <option value="improvement">by improvement</option>
            <option value="idea">by idea sequence</option>
          </select>
          <button
            type="button"
            class={`improvements-toggle${improvementsOnly.value ? " active" : ""}`}
            aria-pressed={improvementsOnly.value}
            title="Collapse to metric-setting ideas while keeping live runs visible."
            onClick={() => {
              improvementsOnly.value = !improvementsOnly.value;
            }}
          >
            <span class="improvements-toggle-icon" aria-hidden="true">
              ▲
            </span>
            <span class="improvements-toggle-label">Improvements Only</span>
          </button>
          <button
            type="button"
            class={`improvements-toggle${ideaMean.value ? " active" : ""}`}
            aria-pressed={ideaMean.value}
            title="Show one point per idea (mean of completed experiments)"
            onClick={() => { ideaMean.value = !ideaMean.value; }}
          >
            <span class="improvements-toggle-icon" aria-hidden="true">μ</span>
            <span class="improvements-toggle-label">Idea Mean</span>
          </button>
          <button
            type="button"
            class={`improvements-toggle${clipOutliers.value ? " active" : ""}`}
            aria-pressed={clipOutliers.value}
            title="Hide outliers via IQR-based y-axis clipping"
            onClick={() => { clipOutliers.value = !clipOutliers.value; }}
          >
            <span class="improvements-toggle-icon" aria-hidden="true">⤢</span>
            <span class="improvements-toggle-label">Hide Outliers</span>
          </button>
          <button
            type="button"
            class={`improvements-toggle${scatter ? " active" : ""}`}
            aria-pressed={scatter}
            title="Toggle 2D scatter chart"
            onClick={() => { scatterOpen.value = !scatter; }}
          >
            <span class="improvements-toggle-icon" aria-hidden="true">⊞</span>
            <span class="improvements-toggle-label">Scatter</span>
          </button>
        </div>

        {/* ---- Collapsible filter bar (tags + status) ---- */}
        <div class="filter-bar-toggle" onClick={() => { filterBarOpen.value = !filterOpen; }}>
          <span class={`arrow${filterOpen ? " open" : ""}`}>&#9654;</span> Filters
        </div>
        <div class={`filter-bar${filterOpen ? "" : " collapsed"}`}>
          <TagFilter />
          <span class="status-filters" style={{ display: "inline-flex", gap: "4px", alignItems: "center", marginLeft: "12px" }}>
            Show:
            <StatusToggle label="concluded" signal={showConcluded} color="#58a6ff" />
            <StatusToggle label="abandoned" signal={showAbandoned} color="#f85149" />
            <StatusToggle label="running" signal={showRunning} color="#d29922" />
          </span>
        </div>

        {/* ---- Charts: side by side ---- */}
        <div class={`chart-row${scatter ? "" : " single"}`}>
          <div class={`chart-col${scatter ? "" : " full"}`}>
            <MetricsChart />
          </div>
          {scatter && <ScatterChart metricKeys={metricKeys} />}
        </div>
      </div>
    </>
  );
}

function StatusToggle({ label, signal: s, color }: { label: string; signal: Signal<boolean>; color: string }) {
  const active = s.value;
  return (
    <span
      class={`tag-toggle${active ? " active" : ""}`}
      style={active ? { borderColor: color, color } : undefined}
      onClick={() => { s.value = !active; }}
      title={active ? `Hide ${label} ideas` : `Show ${label} ideas`}
    >
      {label}
    </span>
  );
}
