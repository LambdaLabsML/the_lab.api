import { useState } from "preact/hooks";
import { logEntries } from "../state/signals";
import { selectedIdea } from "../state/settings";
import { formatTime, escapeHtml } from "../lib/format";
import type { LogEntry } from "../lib/types";

const FILTERS = [
  { key: "idea", label: "Ideas", color: "var(--accent)", defaultOn: true },
  { key: "experiment", label: "Experiments", color: "var(--green)", defaultOn: true },
  { key: "note-insight", label: "Insight", color: "var(--accent)", defaultOn: true },
  { key: "note-milestone", label: "Milestone", color: "var(--yellow)", defaultOn: true },
  { key: "note-observation", label: "Observation", color: "var(--text-muted)", defaultOn: true },
  { key: "note-debug", label: "Debug", color: "var(--red)", defaultOn: false },
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
  const MAX_VISIBLE = 200;
  const visible = filtered.slice(0, MAX_VISIBLE);
  const truncated = filtered.length > MAX_VISIBLE;

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
        <span style={{ color: "var(--text-faint)", marginLeft: "auto", fontSize: "10px" }}>
          {filtered.length} entries{truncated ? ` (showing ${MAX_VISIBLE})` : ""}
        </span>
      </div>
      <div id="log-entries">
        {filtered.length === 0 && (
          <div style={{ padding: "40px", color: "var(--text-faint)", textAlign: "center" }}>
            No log entries yet
          </div>
        )}
        {visible.map((entry, i) => (
          <LogEntryRow key={i} entry={entry} />
        ))}
        {truncated && (
          <div style={{ padding: "12px", color: "var(--text-faint)", textAlign: "center", fontSize: "11px" }}>
            Showing {MAX_VISIBLE} of {filtered.length} entries
          </div>
        )}
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
      {entry.extra && <div class="log-body" style={{ fontStyle: "italic", color: "var(--text-muted)" }}>{entry.extra}</div>}
      {entry.runtime && <div class="log-body" style={{ color: "var(--text-faint)", fontSize: "10px" }}>runtime: {entry.runtime}</div>}
    </div>
  );
}
