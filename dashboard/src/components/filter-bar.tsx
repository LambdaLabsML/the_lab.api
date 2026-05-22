/**
 * FilterBar — Tags and status visibility toggles.
 * Metric/color selectors and chart toggles moved to MetricsChart.
 */

import { useRef, useState } from "preact/hooks";
import type { Signal } from "@preact/signals";
import {
  showAbandoned,
  showConcluded,
  showRunning,
  colorMode,
  filterText,
} from "../state/settings";
import { TagFilter } from "./chart-panel/tag-filter";

/** Isolated input — uses useState to avoid signal reads in render path. */
function FilterInput() {
  const inputRef = useRef<HTMLInputElement>(null);
  const [hasText, setHasText] = useState(!!filterText.value);
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: "2px" }}>
      <input
        ref={inputRef}
        type="text"
        placeholder="Filter..."
        defaultValue={filterText.value}
        onInput={(e) => {
          const v = (e.target as HTMLInputElement).value;
          filterText.value = v;
          setHasText(!!v);
        }}
        style={{ background: "var(--bg-elev)", color: "var(--text)", border: "1px solid var(--border)", borderRadius: "3px", padding: "2px 6px", fontSize: "10px", fontFamily: "inherit", width: "120px", outline: "none" }}
      />
      {hasText && (
        <span
          style={{ color: "var(--text-muted)", cursor: "pointer", fontSize: "12px", lineHeight: "1" }}
          onClick={() => {
            filterText.value = "";
            if (inputRef.current) inputRef.current.value = "";
            setHasText(false);
          }}
          title="Clear filter"
        >&times;</span>
      )}
    </span>
  );
}

export function FilterBar() {
  return (
    <div class="filter-bar-standalone">
      <FilterInput />
      <span style={{ width: "1px", height: "18px", background: "var(--border)", margin: "0 4px" }} />
      <span>
        Color:{" "}
        <select
          value={colorMode.value}
          onChange={(e) => { colorMode.value = (e.target as HTMLSelectElement).value; }}
          style={{ background: "var(--bg-elev)", color: "var(--text)", border: "1px solid var(--border)", borderRadius: "3px", padding: "1px 4px", fontFamily: "inherit", fontSize: "10px" }}
        >
          <option value="status+improve">status + improve</option>
          <option value="lane">by lane</option>
          <option value="status">by status</option>
          <option value="improvement">by improvement</option>
          <option value="idea">by idea</option>
        </select>
      </span>
      <span style={{ width: "1px", height: "18px", background: "var(--border)", margin: "0 4px" }} />
      <TagFilter />
      <span style={{ width: "1px", height: "18px", background: "var(--border)", margin: "0 4px" }} />
      <span class="status-filters" style={{ display: "inline-flex", gap: "4px", alignItems: "center" }}>
        Show:
        <StatusToggle label="concluded" signal={showConcluded} color="var(--accent)" />
        <StatusToggle label="abandoned" signal={showAbandoned} color="var(--red)" />
        <StatusToggle label="running" signal={showRunning} color="var(--yellow)" />
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
