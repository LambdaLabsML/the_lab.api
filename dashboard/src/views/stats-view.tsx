import { useState, useEffect, useMemo } from "preact/hooks";
import { getApiStats } from "../state/api";
import type { ApiStatsResponse } from "../state/api";

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
  const [selected, setSelected] = useState<string | null>(null);

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

  if (!stats) return <div style={{ padding: 20, color: "var(--text-faint)" }}>Loading stats...</div>;

  // Compute quick summary: top 3 endpoints by call count
  const topEndpoints = (stats.calls ?? []).slice(0, 3);
  const totalKB = (stats.response_sizes ?? []).reduce((s, r) => s + r.total_kb, 0);

  return (
    <div class="stats-view">
      <div class="pane-bar">
        <h2 class="pane-bar-title">Stats</h2>
        <span class="pane-bar-count">{stats.total_calls.toLocaleString()} calls</span>
      </div>

      {/* Quick glance summary */}
      <div class="stats-summary-row">
        {topEndpoints.map((e) => (
          <div class="stats-summary-pill" key={e.endpoint} title={e.endpoint}>
            <span class="stats-summary-count">{e.count}</span>
            <span class="stats-summary-label">{e.endpoint.replace(/^(GET|POST|PUT|PATCH|DELETE) \/api\/v1\//, "").replace(/\/\{id\}.*/, "")}</span>
          </div>
        ))}
        {totalKB > 0 && (
          <div class="stats-summary-pill">
            <span class="stats-summary-count">{totalKB > 1000 ? `${(totalKB/1000).toFixed(0)}MB` : `${totalKB.toFixed(0)}KB`}</span>
            <span class="stats-summary-label">total transferred</span>
          </div>
        )}
      </div>

      {/* Response size table */}
      {stats.response_sizes && stats.response_sizes.length > 0 && (
        <div class="stats-card" style={{ marginBottom: "var(--space-3)", flexShrink: 0 }}>
          <div class="stats-card-title">Response sizes · MCP calls only (KB)</div>
          <div class="stats-card-body" style={{ overflowX: "auto" }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "var(--text-sm)", fontFamily: "var(--font-mono)" }}>
              <thead>
                <tr>
                  {["endpoint", "calls", "total KB", "avg KB", "max KB"].map((h) => (
                    <th key={h} style={{ textAlign: h === "endpoint" ? "left" : "right", color: "var(--text-muted)", padding: "2px 8px", borderBottom: "1px solid var(--border-soft)", fontWeight: 600 }}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {stats.response_sizes.slice(0, 20).map((r) => (
                  <tr key={r.endpoint} style={{ borderBottom: "1px solid var(--border-soft)" }}>
                    <td style={{ padding: "3px 8px", color: "var(--text)", maxWidth: 320, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{r.endpoint.replace(/^(GET|POST|PUT|PATCH|DELETE) /, "")}</td>
                    <td style={{ padding: "3px 8px", textAlign: "right", color: "var(--text-muted)" }}>{r.calls}</td>
                    <td style={{ padding: "3px 8px", textAlign: "right", color: r.total_kb > 1000 ? "var(--red)" : r.total_kb > 100 ? "var(--yellow)" : "var(--text)" }}>{r.total_kb.toLocaleString()}</td>
                    <td style={{ padding: "3px 8px", textAlign: "right", color: r.avg_kb > 20 ? "var(--yellow)" : "var(--text-muted)" }}>{r.avg_kb}</td>
                    <td style={{ padding: "3px 8px", textAlign: "right", color: "var(--text-muted)" }}>{r.max_kb}</td>
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
            Common Patterns
            <span class="stats-len-selector">
              {[1, 2, 3, 4, 5].map((n) => (
                <span
                  key={n}
                  class={`stats-len-btn${patternLen === n ? " active" : ""}`}
                  onClick={() => { setPatternLen(n); setSelected(null); }}
                  title={n === 1 ? "Individual endpoints" : `${n}-step sequences`}
                >
                  {n}
                </span>
              ))}
              <span class="stats-len-label">steps</span>
            </span>
          </div>
          <div class="stats-card-body">
            {rows.length === 0 && (
              <div class="stats-empty">No {patternLen}-step patterns recorded yet</div>
            )}
            {rows.slice(0, 20).map((r) => {
              const pct = (r.count / topCount) * 100;
              const isSelected = selected === r.key;
              return (
                <div
                  key={r.key}
                  class={`stats-bar-row${patternLen > 1 ? " pattern" : ""}${isSelected ? " selected" : ""}`}
                  onClick={() => setSelected(isSelected ? null : r.key)}
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
            {selected ? "Recent Examples" : "Recent Calls"}
            {selected && (
              <span class="stats-clear-btn" onClick={() => setSelected(null)}>&times; clear</span>
            )}
          </div>
          <div class="stats-card-body history">
            {selected && examples.length === 0 && (
              <div class="stats-empty">No recent examples in history (history tracks live calls only)</div>
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
  const [expanded, setExpanded] = useState(false);
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
      <div class="stats-history-main" onClick={() => hasArgs && setExpanded(!expanded)}>
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
