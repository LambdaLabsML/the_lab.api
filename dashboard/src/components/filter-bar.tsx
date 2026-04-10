/**
 * FilterBar — Tags and status visibility toggles.
 * Metric/color selectors and chart toggles moved to MetricsChart.
 */

import type { Signal } from "@preact/signals";
import {
  showAbandoned,
  showConcluded,
  showRunning,
  colorMode,
} from "../state/settings";
import { TagFilter } from "./chart-panel/tag-filter";

export function FilterBar() {
  return (
    <div class="filter-bar-standalone">
      <span>
        Color:{" "}
        <select
          value={colorMode.value}
          onChange={(e) => { colorMode.value = (e.target as HTMLSelectElement).value; }}
          style={{ background: "#161b22", color: "#c9d1d9", border: "1px solid #30363d", borderRadius: "3px", padding: "1px 4px", fontFamily: "inherit", fontSize: "10px" }}
        >
          <option value="status+improve">status + improve</option>
          <option value="lane">by lane</option>
          <option value="status">by status</option>
          <option value="improvement">by improvement</option>
          <option value="idea">by idea</option>
        </select>
      </span>
      <span style={{ width: "1px", height: "18px", background: "#30363d", margin: "0 4px" }} />
      <TagFilter />
      <span style={{ width: "1px", height: "18px", background: "#30363d", margin: "0 4px" }} />
      <span class="status-filters" style={{ display: "inline-flex", gap: "4px", alignItems: "center" }}>
        Show:
        <StatusToggle label="concluded" signal={showConcluded} color="#58a6ff" />
        <StatusToggle label="abandoned" signal={showAbandoned} color="#f85149" />
        <StatusToggle label="running" signal={showRunning} color="#d29922" />
      </span>
    </div>
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
