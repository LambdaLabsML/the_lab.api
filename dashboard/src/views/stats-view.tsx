import { useState, useEffect, useMemo } from "preact/hooks";
import { getApiStats } from "../state/api";
import type { ApiStatsResponse } from "../state/api";
import { useSelection, useDisclosure } from "../lib/hooks";
import { Stat, Toggle, IconButton, EmptyState } from "../components/ui";

interface HistoryEntry {
  t: string;
  method: string;
  path: string;
  query: string;
  body: string;
}

type StatsData = ApiStatsResponse & { history?: HistoryEntry[] };

/** Normalize /api/v1/ideas/42 → /api/v1/ideas/{id} to match pattern keys. */
function normalizePath(path: string): string {
  return path.replace(/\/(\d+)(?=\/|$)/g, "/{id}");
}

/**
 * Find history entries that match a pattern sequence.
 * For n=1: match individual calls.
 * For n>1: find consecutive calls matching the pattern steps.
 */
function findExamples(
  history: HistoryEntry[],
  pattern: string,
  nSteps: number,
  limit = 10,
): HistoryEntry[][] {
  if (nSteps === 1) {
    // pattern is "GET /api/v1/ideas/{id}" — match individual calls
    return history
      .filter((h) => `${h.method} ${normalizePath(h.path)}` === pattern)
      .slice(0, limit)
      .map((h) => [h]);
  }
  const steps = pattern.split(" → ");
  const results: HistoryEntry[][] = [];
  for (let i = 0; i <= history.length - nSteps && results.length < limit; i++) {
    let match = true;
    for (let s = 0; s < nSteps; s++) {
      const h = history[i + s];
      const key = `${h.method} ${normalizePath(h.path)}`;
      if (key !== steps[s]) { match = false; break; }
    }
    if (match) {
      results.push(history.slice(i, i + nSteps));
    }
  }
  return results;
}

export function StatsView() {
  const [stats, setStats] = useState<StatsData | null>(null);
  const [patternLen, setPatternLen] = useState(1);
  // single-select pattern row (click again to clear)
  const { selected, select, clear: clearSelected } = useSelection<string>();

  useEffect(() => {
    getApiStats(Math.max(patternLen, 2)).then(setStats).catch(() => {});
  }, [patternLen]);

  useEffect(() => {
    const timer = setInterval(() => {
      getApiStats(Math.max(patternLen, 2)).then(setStats).catch(() => {});
    }, 15_000);
    return () => clearInterval(timer);
  }, [patternLen]);

  // Unified list: n=1 uses calls, n>1 uses patterns
  const rows = useMemo(() => {
    if (!stats) return [];
    if (patternLen === 1) {
      return stats.calls.map((c) => ({ key: c.endpoint, count: c.count }));
    }
    return stats.patterns.map((p) => ({ key: p.sequence, count: p.count }));
  }, [stats, patternLen]);

  const topCount = rows[0]?.count || 1;

  // Examples for selected pattern from history (reversed = oldest first for sequence matching)
  const examples = useMemo(() => {
    if (!selected || !stats?.history) return [];
    const hist = [...stats.history].reverse(); // oldest first
    return findExamples(hist, selected, patternLen);
  }, [selected, stats?.history, patternLen]);

  if (!stats) return <div class="stats-loading">Loading stats…</div>;

  // Compute quick summary: top 3 endpoints by call count
  const topEndpoints = (stats.calls ?? []).slice(0, 3);
  const totalKB = (stats.response_sizes ?? []).reduce((s, r) => s + r.total_kb, 0);

  return (
    <div class="stats-view">
      <div class="pane-bar">
        <span class="ui-eyebrow pane-bar-title">Stats</span>
        <span class="pane-bar-count">{stats.total_calls.toLocaleString()} calls</span>
      </div>

      {/* Quick glance summary */}
      <div class="stats-summary-row">
        {topEndpoints.map((e) => (
          <div class="stats-summary-pill" key={e.endpoint} title={e.endpoint}>
            <Stat
              size="sm"
              tone="accent"
              value={e.count}
              sub={e.endpoint.replace(/^(GET|POST|PUT|PATCH|DELETE) \/api\/v1\//, "").replace(/\/\{id\}.*/, "")}
            />
          </div>
        ))}
        {totalKB > 0 && (
          <div class="stats-summary-pill">
            <Stat
              size="sm"
              tone="accent"
              value={totalKB > 1000 ? `${(totalKB/1000).toFixed(0)}MB` : `${totalKB.toFixed(0)}KB`}
              sub="total transferred"
            />
          </div>
        )}
      </div>

      {/* Response size table */}
      {stats.response_sizes && stats.response_sizes.length > 0 && (
        <div class="stats-card stats-card-sizes">
          <div class="stats-card-title">
            <span class="ui-eyebrow">Response sizes · MCP calls only (KB)</span>
          </div>
          <div class="stats-card-body stats-sizes-body">
            <table class="stats-size-table">
              <thead>
                <tr>
                  {["endpoint", "calls", "total KB", "avg KB", "max KB"].map((h) => (
                    <th key={h} class={h === "endpoint" ? "" : "num"}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {stats.response_sizes.slice(0, 20).map((r) => (
                  <tr key={r.endpoint}>
                    <td class="stats-size-endpoint">{r.endpoint.replace(/^(GET|POST|PUT|PATCH|DELETE) /, "")}</td>
                    <td class="num muted">{r.calls}</td>
                    <td class="num" style={{ color: r.total_kb > 1000 ? "var(--red)" : r.total_kb > 100 ? "var(--yellow)" : "var(--text)" }}>{r.total_kb.toLocaleString()}</td>
                    <td class="num" style={{ color: r.avg_kb > 20 ? "var(--yellow)" : "var(--text-muted)" }}>{r.avg_kb}</td>
                    <td class="num muted">{r.max_kb}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      <div class="stats-columns">
        {/* Left: patterns list */}
        <div class="stats-card">
          <div class="stats-card-title">
            <span class="ui-eyebrow">Common Patterns</span>
            <span class="stats-len-selector">
              {[1, 2, 3, 4, 5].map((n) => (
                <Toggle
                  key={n}
                  active={patternLen === n}
                  onClick={() => { setPatternLen(n); clearSelected(); }}
                  title={n === 1 ? "Individual endpoints" : `${n}-step sequences`}
                  class="stats-len-btn"
                >
                  {n}
                </Toggle>
              ))}
              <span class="stats-len-label">steps</span>
            </span>
          </div>
          <div class="stats-card-body">
            {rows.length === 0 && (
              <EmptyState title="No patterns yet" body={`No ${patternLen}-step patterns recorded yet.`} />
            )}
            {rows.slice(0, 20).map((r) => {
              const pct = (r.count / topCount) * 100;
              const isSelected = selected === r.key;
              return (
                <div
                  key={r.key}
                  class={`stats-bar-row${patternLen > 1 ? " pattern" : ""}${isSelected ? " selected" : ""}`}
                  onClick={() => select(r.key)}
                >
                  <div class={`stats-bar-bg${patternLen > 1 ? " pattern" : ""}`} style={{ width: pct + "%" }} />
                  <PatternLabel pattern={r.key} nSteps={patternLen} />
                  <span class={`stats-bar-count${patternLen > 1 ? " pattern" : ""}`}>{r.count}</span>
                </div>
              );
            })}
          </div>
        </div>

        {/* Right: examples detail */}
        <div class="stats-card">
          <div class="stats-card-title">
            <span class="ui-eyebrow">{selected ? "Recent Examples" : "Recent Calls"}</span>
            {selected && (
              <IconButton class="stats-clear-btn" onClick={() => clearSelected()} title="Clear selection">
                &times; clear
              </IconButton>
            )}
          </div>
          <div class="stats-card-body history">
            {selected && examples.length === 0 && (
              <EmptyState title="No examples" body="No recent examples in history (history tracks live calls only)." />
            )}
            {selected && examples.map((group, gi) => (
              <div key={gi} class="stats-example-group">
                {group.map((h, i) => (
                  <HistoryRow key={i} entry={h} showDate={i === 0} />
                ))}
              </div>
            ))}
            {!selected && (stats.history || []).map((h, i) => (
              <HistoryRow key={i} entry={h} showDate />
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}

function PatternLabel({ pattern, nSteps }: { pattern: string; nSteps: number }) {
  if (nSteps === 1) {
    const [method, ...rest] = pattern.split(" ");
    return (
      <span class="stats-bar-sequence">
        <MethodPill method={method} />
        <EndpointPath path={rest.join(" ")} />
      </span>
    );
  }
  const steps = pattern.split(" → ");
  return (
    <span class="stats-bar-sequence">
      {steps.map((step, i) => {
        const [method, ...rest] = step.split(" ");
        return (
          <span key={i}>
            {i > 0 && <span class="stats-arrow">→</span>}
            <MethodPill method={method} small />
            <EndpointPath path={rest.join(" ")} short />
          </span>
        );
      })}
    </span>
  );
}

function MethodPill({ method, small }: { method: string; small?: boolean }) {
  return (
    <span class={`api-method ${method.toLowerCase()}${small ? " small" : ""}`}>
      {method}
    </span>
  );
}

function EndpointPath({ path, short }: { path: string; short?: boolean }) {
  const display = path.replace(/^\/api\/v1/, "");
  return (
    <span class={`stats-endpoint${short ? " short" : ""}`} title={path}>
      {display}
    </span>
  );
}

function HistoryRow({ entry, showDate }: { entry: HistoryEntry; showDate?: boolean }) {
  const { open: expanded, toggle: toggleExpanded } = useDisclosure(false);
  const time = new Date(entry.t);
  const timeStr = showDate
    ? time.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })
    : time.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  const hasArgs = !!(entry.query || entry.body);
  const argsPreview = entry.query
    ? "?" + entry.query
    : entry.body
      ? entry.body.slice(0, 60) + (entry.body.length > 60 ? "…" : "")
      : "";

  return (
    <div class={`stats-history-row${expanded ? " expanded" : ""}`}>
      <div class="stats-history-main" onClick={() => { if (hasArgs) toggleExpanded(); }}>
        <span class="stats-history-time">{timeStr}</span>
        <MethodPill method={entry.method} small />
        <EndpointPath path={entry.path} />
        {hasArgs && !expanded && (
          <span class="stats-history-args-preview">{argsPreview}</span>
        )}
        {hasArgs && (
          <span class="stats-history-expand">{expanded ? "▾" : "▸"}</span>
        )}
      </div>
      {expanded && hasArgs && (
        <div class="stats-history-detail">
          {entry.query && <div><span class="stats-detail-label">Query:</span> ?{entry.query}</div>}
          {entry.body && <div><span class="stats-detail-label">Body:</span> {entry.body}</div>}
        </div>
      )}
    </div>
  );
}
