import { useState } from "preact/hooks";
import { logEntries } from "../state/signals";
import { selectedIdea } from "../state/settings";
import { formatTime } from "../lib/format";
import type { LogEntry } from "../lib/types";
import { Toggle, EmptyState } from "../components/ui";

const FILTERS = [
  { key: "idea", label: "Ideas", defaultOn: true },
  { key: "experiment", label: "Experiments", defaultOn: true },
  { key: "note-insight", label: "Insight", defaultOn: true },
  { key: "note-milestone", label: "Milestone", defaultOn: true },
  { key: "note-observation", label: "Observation", defaultOn: true },
  { key: "note-debug", label: "Debug", defaultOn: false },
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
      <div class="pane-bar">
        <h2 class="pane-bar-title">Log</h2>
        <span class="pane-bar-count">
          {filtered.length} entries{truncated ? ` (capped at ${MAX_VISIBLE})` : ""}
        </span>
        <div class="pane-bar-actions log-filters">
          {FILTERS.map((f) => (
            <Toggle
              key={f.key}
              active={!!active[f.key]}
              onClick={() => toggle(f.key)}
              class={`log-filter log-filter--${f.key}`}
            >
              <span class="log-filter-dot" />
              {f.label}
            </Toggle>
          ))}
        </div>
      </div>
      <div id="log-entries">
        {filtered.length === 0 && (
          <EmptyState icon="⌗" title="No log entries yet" />
        )}
        {visible.map((entry, i) => (
          <LogEntryRow key={i} entry={entry} />
        ))}
        {truncated && (
          <div class="log-truncated">
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
      {entry.extra && <div class="log-body log-extra">{entry.extra}</div>}
      {entry.runtime && <div class="log-body log-runtime">runtime: {entry.runtime}</div>}
    </div>
  );
}
