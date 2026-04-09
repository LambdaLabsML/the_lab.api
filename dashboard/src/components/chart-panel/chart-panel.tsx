import type { Signal } from "@preact/signals";
import { chartOpen, selectedMetric, colorMode, improvementsOnly, showAbandoned, showConcluded, showRunning, clipOutliers } from "../../state/settings";
import { allExperiments } from "../../state/signals";
import { MetricsChart } from "./metrics-chart";
import { TagFilter } from "./tag-filter";
import { useMemo } from "preact/hooks";

export function ChartPanel() {
  const open = chartOpen.value;
  const experiments = allExperiments.value;
  const metric = selectedMetric.value;

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
          <TagFilter />
          <span class="status-filters" style={{ display: "inline-flex", gap: "4px", alignItems: "center", marginLeft: "12px" }}>
            Show:
            <StatusToggle label="concluded" signal={showConcluded} color="#58a6ff" />
            <StatusToggle label="abandoned" signal={showAbandoned} color="#f85149" />
            <StatusToggle label="running" signal={showRunning} color="#d29922" />
          </span>
          <button
            type="button"
            class={`improvements-toggle${!clipOutliers.value ? " active" : ""}`}
            aria-pressed={!clipOutliers.value}
            title="Show all data points including outliers (disables IQR-based y-axis clipping)"
            onClick={() => { clipOutliers.value = !clipOutliers.value; }}
            style={{ marginLeft: "8px" }}
          >
            <span class="improvements-toggle-icon" aria-hidden="true">⤢</span>
            <span class="improvements-toggle-label">Show Outliers</span>
          </button>
        </div>
        <MetricsChart />
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
