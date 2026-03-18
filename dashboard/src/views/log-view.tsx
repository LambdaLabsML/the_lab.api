import { useState } from "preact/hooks";
import { logEntries } from "../state/signals";
import { selectedIdea } from "../state/settings";
import { formatTime, escapeHtml } from "../lib/format";
import type { LogEntry } from "../lib/types";

const FILTERS = [
  { key: "idea", label: "Ideas", color: "#58a6ff", defaultOn: true },
  { key: "experiment", label: "Experiments", color: "#3fb950", defaultOn: true },
  { key: "note-insight", label: "Insight", color: "#58a6ff", defaultOn: true },
  { key: "note-milestone", label: "Milestone", color: "#d29922", defaultOn: true },
  { key: "note-observation", label: "Observation", color: "#8b949e", defaultOn: true },
  { key: "note-debug", label: "Debug", color: "#f85149", defaultOn: false },
];

export function LogView() {
  const entries = logEntries.value;
  const [active, setActive] = useState<Record<string, boolean>>(() => {
    const m: Record<string, boolean> = {};
    for (const f of FILTERS) m[f.key] = f.defaultOn;
    return m;
  });

  function toggle(key: string) {
    setActive({ ...active, [key]: !active[key] });
  }

  const filtered = entries.filter((e) => active[e.type] !== false);

  return (
    <div id="log-container">
      <div id="log-filters">
        {FILTERS.map((f) => (
          <label key={f.key}>
            <input
              type="checkbox"
              checked={!!active[f.key]}
              onChange={() => toggle(f.key)}
            />
            <span class="filter-dot" style={{ background: f.color }} />
            {" "}{f.label}
          </label>
        ))}
      </div>
      <div id="log-entries">
        {filtered.length === 0 && (
          <div style={{ padding: "40px", color: "#484f58", textAlign: "center" }}>
            No log entries yet
          </div>
        )}
        {filtered.map((entry, i) => (
          <LogEntryRow key={i} entry={entry} />
        ))}
      </div>
    </div>
  );
}

function LogEntryRow({ entry }: { entry: LogEntry }) {
  const statusClass = entry.status === "failed" ? " status-failed" : entry.status === "running" ? " status-running" : "";

  function handleClick() {
    if (entry.ideaId) {
      selectedIdea.value = entry.ideaId;
      history.pushState(null, "", `/ideas/${entry.ideaId}`);
    }
  }

  return (
    <div class={`log-entry type-${entry.type}${statusClass}`} onClick={handleClick}>
      <div class="log-header">
        <span class="log-time">{entry.time ? formatTime(entry.time) : ""}</span>
        <span class="log-type">{entry.title}</span>
        {entry.ideaId && <span class="log-idea">idea #{entry.ideaId}</span>}
      </div>
      {entry.body && <div class="log-body">{entry.body}</div>}
      {entry.metrics && Object.keys(entry.metrics).length > 0 && (
        <div class="log-metrics">{JSON.stringify(entry.metrics)}</div>
      )}
      {entry.extra && <div class="log-body" style={{ fontStyle: "italic", color: "#8b949e" }}>{entry.extra}</div>}
      {entry.runtime && <div class="log-body" style={{ color: "#484f58", fontSize: "10px" }}>runtime: {entry.runtime}</div>}
    </div>
  );
}
