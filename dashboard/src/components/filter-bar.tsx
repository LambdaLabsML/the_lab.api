/**
 * FilterBar — Tags and status visibility toggles.
 * Metric/color selectors and chart toggles moved to MetricsChart.
 *
 * Design language (see dashboard/DESIGN.md): status toggles are .ui-toggle
 * chips, dividers are hairline .ui-sep--v separators, type is token-scaled
 * (never hardcoded px), inputs sit on elevation with hairline borders.
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
import { Toggle, Separator, Eyebrow } from "./ui";

/** Isolated input — uses useState to avoid signal reads in render path. */
function FilterInput() {
  const inputRef = useRef<HTMLInputElement>(null);
  const [hasText, setHasText] = useState(!!filterText.value);
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: "2px" }}>
      <input
        ref={inputRef}
        type="text"
        placeholder="Filter…"
        defaultValue={filterText.value}
        onInput={(e) => {
          const v = (e.target as HTMLInputElement).value;
          filterText.value = v;
          setHasText(!!v);
        }}
        style={{
          background: "var(--bg)",
          color: "var(--text)",
          border: "1px solid var(--border-soft)",
          borderRadius: "var(--radius-sm)",
          padding: "3px 7px",
          fontSize: "var(--text-xs)",
          fontFamily: "var(--font-mono)",
          width: "120px",
          outline: "none",
        }}
      />
      {hasText && (
        <span
          style={{ color: "var(--text-faint)", cursor: "pointer", fontSize: "var(--text-lg)", lineHeight: "1" }}
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
      <Separator vertical />
      <span style={{ display: "inline-flex", alignItems: "center", gap: "5px" }}>
        <Eyebrow>Color</Eyebrow>
        <select
          value={colorMode.value}
          onChange={(e) => { colorMode.value = (e.target as HTMLSelectElement).value; }}
          style={{
            background: "var(--bg)",
            color: "var(--text)",
            border: "1px solid var(--border-soft)",
            borderRadius: "var(--radius-sm)",
            padding: "2px 5px",
            fontFamily: "var(--font-mono)",
            fontSize: "var(--text-xs)",
            outline: "none",
          }}
        >
          <option value="status+improve">status + improve</option>
          <option value="lane">by lane</option>
          <option value="status">by status</option>
          <option value="improvement">by improvement</option>
          <option value="idea">by idea</option>
        </select>
      </span>
      <Separator vertical />
      <TagFilter />
      <Separator vertical />
      <span class="status-filters" style={{ display: "inline-flex", gap: "5px", alignItems: "center" }}>
        <Eyebrow>Show</Eyebrow>
        <StatusToggle label="concluded" signal={showConcluded} />
        <StatusToggle label="abandoned" signal={showAbandoned} />
        <StatusToggle label="running" signal={showRunning} />
      </span>
    </div>
  );
}

function StatusToggle({ label, signal: s }: { label: string; signal: Signal<boolean> }) {
  const active = s.value;
  return (
    <Toggle
      active={active}
      onClick={() => { s.value = !active; }}
      title={active ? `Hide ${label} ideas` : `Show ${label} ideas`}
    >
      {label}
    </Toggle>
  );
}
