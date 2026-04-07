import { useState, useEffect } from "preact/hooks";
import { getApiStats } from "../state/api";
import type { ApiStatsResponse } from "../state/api";

interface HistoryEntry {
  t: string;
  method: string;
  path: string;
  query: string;
  body: string;
}

export function StatsView() {
  const [stats, setStats] = useState<(ApiStatsResponse & { history?: HistoryEntry[] }) | null>(null);
  const [patternLen, setPatternLen] = useState(2);

  useEffect(() => {
    getApiStats(patternLen).then(setStats).catch(() => {});
  }, [patternLen]);

  useEffect(() => {
    const timer = setInterval(() => {
      getApiStats(patternLen).then(setStats).catch(() => {});
    }, 15_000);
    return () => clearInterval(timer);
  }, [patternLen]);

  if (!stats) return <div style={{ padding: 20, color: "#484f58" }}>Loading stats...</div>;

  return (
    <div class="stats-view">
      <div class="stats-header">
        <h2>API Usage Stats</h2>
        <span class="stats-total">{stats.total_calls.toLocaleString()} total calls</span>
      </div>

      <div class="stats-grid">
        {/* Top Endpoints */}
        <div class="stats-card">
          <div class="stats-card-title">Top Endpoints</div>
          <div class="stats-card-body">
            {stats.calls.slice(0, 20).map((c) => {
              const pct = stats.calls[0] ? (c.count / stats.calls[0].count) * 100 : 0;
              const [method, ...rest] = c.endpoint.split(" ");
              const path = rest.join(" ");
              return (
                <div key={c.endpoint} class="stats-bar-row">
                  <div class="stats-bar-bg" style={{ width: pct + "%" }} />
                  <MethodPill method={method} />
                  <EndpointPath path={path} />
                  <span class="stats-bar-count">{c.count}</span>
                </div>
              );
            })}
          </div>
        </div>

        {/* Patterns */}
        <div class="stats-card">
          <div class="stats-card-title">
            Common Patterns
            <span class="stats-len-selector">
              {[2, 3, 4, 5].map((n) => (
                <span
                  key={n}
                  class={`stats-len-btn${patternLen === n ? " active" : ""}`}
                  onClick={() => setPatternLen(n)}
                  title={`${n}-step sequences`}
                >
                  {n}
                </span>
              ))}
              <span class="stats-len-label">steps</span>
            </span>
          </div>
          <div class="stats-card-body">
            {stats.patterns.length === 0 && (
              <div class="stats-empty">No {patternLen}-step patterns recorded yet</div>
            )}
            {stats.patterns.slice(0, 15).map((p) => {
              const pct = stats.patterns[0] ? (p.count / stats.patterns[0].count) * 100 : 0;
              const steps = p.sequence.split(" → ");
              return (
                <div key={p.sequence} class="stats-bar-row pattern">
                  <div class="stats-bar-bg pattern" style={{ width: pct + "%" }} />
                  <span class="stats-bar-sequence">
                    {steps.map((step, i) => {
                      const [method, ...rest] = step.split(" ");
                      const path = rest.join(" ");
                      return (
                        <span key={i}>
                          {i > 0 && <span class="stats-arrow">→</span>}
                          <MethodPill method={method} small />
                          <EndpointPath path={path} short />
                        </span>
                      );
                    })}
                  </span>
                  <span class="stats-bar-count pattern">{p.count}</span>
                </div>
              );
            })}
          </div>
        </div>
      </div>

      {/* Call History */}
      <div class="stats-card history">
        <div class="stats-card-title">Recent Calls</div>
        <div class="stats-card-body history">
          {(!stats.history || stats.history.length === 0) && (
            <div class="stats-empty">No calls recorded yet (history starts when the server boots)</div>
          )}
          {(stats.history || []).map((h, i) => (
            <HistoryRow key={i} entry={h} />
          ))}
        </div>
      </div>
    </div>
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
  // Strip /api/v1 prefix for compact display
  const display = path.replace(/^\/api\/v1/, "");
  return (
    <span class={`stats-endpoint${short ? " short" : ""}`} title={path}>
      {display}
    </span>
  );
}

function HistoryRow({ entry }: { entry: HistoryEntry }) {
  const [expanded, setExpanded] = useState(false);
  const time = new Date(entry.t);
  const timeStr = time.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
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
