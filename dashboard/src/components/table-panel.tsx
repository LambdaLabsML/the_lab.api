import { useMemo, useState } from "preact/hooks";
import { allExperiments, allIdeas, currentLayout, highlightedIdea, runningProgress } from "../state/signals";
import { RunningBadge } from "./progress-ring";
import {
  selectedMetric, colorMode, activeTagFilters, tagFilterMode,
  showAbandoned, showConcluded, showRunning,
  improvementsOnly, ideaMean, clipOutliers,
} from "../state/settings";
import { collectChartKeys, resolveNumericValue, filterVisibleChartExperiments } from "../lib/chart-data";
import { _colorForExp, isLowerBetter } from "../lib/colors";
import { navigateToIdea } from "../lib/navigate";
import type { Experiment } from "../lib/types";

// ---------------------------------------------------------------------------
// Number formatting
// ---------------------------------------------------------------------------

function fmtNum(n: number): string {
  if (Number.isInteger(n)) return String(n);
  if (Math.abs(n) < 0.0001 && n !== 0) return n.toExponential(2);
  return n.toFixed(4).replace(/0+$/, "").replace(/\.$/, "");
}

// ---------------------------------------------------------------------------
// Status badge color
// ---------------------------------------------------------------------------

const STATUS_BADGE_COLORS: Record<string, string> = {
  active: "#238636",
  running: "#6e4b00",
  concluded: "#1f6feb",
  abandoned: "#da3633",
  suggested: "#2d1a00",
};

// ---------------------------------------------------------------------------
// TablePanel component
// ---------------------------------------------------------------------------

type SortDir = "asc" | "desc";

export function TablePanel() {
  const [sortKey, setSortKey] = useState<string>("label");
  const [sortDir, setSortDir] = useState<SortDir>("asc");

  const experiments = allExperiments.value;
  const ideas = allIdeas.value;
  const layout = currentLayout.value;
  const metric = selectedMetric.value;
  const mode = colorMode.value;
  const tags = activeTagFilters.value;
  const tagMode = tagFilterMode.value;
  const highlighted = highlightedIdea.value;
  const impOnly = improvementsOnly.value;
  const mean = ideaMean.value;

  // Build hidden statuses set (same logic as metrics chart)
  const hiddenStatuses = useMemo(() => {
    const s = new Set<string>();
    if (!showAbandoned.value) s.add("abandoned");
    if (!showConcluded.value) s.add("concluded");
    if (!showRunning.value) { s.add("active"); s.add("suggested"); }
    return s;
  }, [showAbandoned.value, showConcluded.value, showRunning.value]);

  // Filter experiments — use the same filterVisibleChartExperiments logic
  // so improvements-only and idea-mean match the chart exactly.
  const filtered = useMemo(() => {
    // When no metric is selected, fall back to tag+status filtering only
    if (!metric) {
      return experiments.filter((e) => {
        if (hiddenStatuses.has(e.idea_status || "active")) return false;
        if (tags.length > 0) {
          const expTags = e.tags || [];
          if (expTags.length === 0) return false;
          const tagSet = new Set(tags);
          if (tagMode === "and") {
            if (!tags.every((t) => expTags.includes(t))) return false;
          } else {
            if (!expTags.some((t) => tagSet.has(t))) return false;
          }
        }
        return true;
      });
    }

    // Use the chart's filtering (respects improvements-only)
    let result = filterVisibleChartExperiments(
      metric, experiments, tags, tagMode, impOnly, hiddenStatuses,
    );

    // Idea mean: aggregate to one row per idea
    if (mean) {
      const groups: Record<number, { exps: Experiment[]; sums: Record<string, number>; counts: Record<string, number> }> = {};
      for (const e of result) {
        if (e._running) continue;
        if (!groups[e.idea_id]) groups[e.idea_id] = { exps: [], sums: {}, counts: {} };
        const g = groups[e.idea_id];
        g.exps.push(e);
        if (e.metrics) {
          for (const [k, v] of Object.entries(e.metrics)) {
            if (typeof v === "number") {
              g.sums[k] = (g.sums[k] || 0) + v;
              g.counts[k] = (g.counts[k] || 0) + 1;
            }
          }
        }
      }
      result = Object.keys(groups).map(Number).sort((a, b) => a - b).map((ideaId) => {
        const g = groups[ideaId];
        const last = g.exps[g.exps.length - 1];
        const meanMetrics: Record<string, number> = {};
        for (const k of Object.keys(g.sums)) {
          meanMetrics[k] = g.sums[k] / g.counts[k];
        }
        return {
          ...last,
          metrics: meanMetrics,
          label: `idea/${ideaId}`,
          description: `mean of ${g.exps.length} exp${g.exps.length > 1 ? "s" : ""}`,
          _ideaMean: true,
          _meanCount: g.exps.length,
        } as Experiment;
      });
    }

    return result;
  }, [experiments, hiddenStatuses, tags, tagMode, metric, impOnly, mean]);

  // Collect all metric keys from the filtered set
  const allMetricKeys = useMemo(() => {
    const keys = collectChartKeys(filtered);
    return keys.metrics;
  }, [filtered]);

  // Order columns: selected metric first, then the rest alphabetically
  const orderedMetrics = useMemo(() => {
    if (!metric || !allMetricKeys.includes(metric)) return allMetricKeys;
    return [metric, ...allMetricKeys.filter((k) => k !== metric)];
  }, [allMetricKeys, metric]);

  // Sort experiments
  const sorted = useMemo(() => {
    const arr = filtered.slice();
    arr.sort((a, b) => {
      let cmp = 0;
      if (sortKey === "label") {
        const la = a.label || String(a.id);
        const lb = b.label || String(b.id);
        // Try numeric comparison on labels like "5.3"
        const na = parseFloat(la);
        const nb = parseFloat(lb);
        if (!isNaN(na) && !isNaN(nb)) {
          cmp = na - nb;
        } else {
          cmp = la.localeCompare(lb);
        }
      } else if (sortKey === "idea") {
        const da = (a.idea_description || "").toLowerCase();
        const db = (b.idea_description || "").toLowerCase();
        cmp = da.localeCompare(db);
      } else if (sortKey === "status") {
        cmp = (a.idea_status || "active").localeCompare(b.idea_status || "active");
      } else {
        // Metric column
        const va = resolveNumericValue(a, sortKey);
        const vb = resolveNumericValue(b, sortKey);
        if (va === undefined && vb === undefined) cmp = 0;
        else if (va === undefined) cmp = 1;
        else if (vb === undefined) cmp = -1;
        else cmp = va - vb;
      }
      return sortDir === "asc" ? cmp : -cmp;
    });
    return arr;
  }, [filtered, sortKey, sortDir]);

  // Toggle sort on column header click
  function handleSort(key: string) {
    if (sortKey === key) {
      setSortDir(sortDir === "asc" ? "desc" : "asc");
    } else {
      setSortKey(key);
      setSortDir("asc");
    }
  }

  // Sort indicator
  function sortArrow(key: string): string {
    if (sortKey !== key) return "";
    return sortDir === "asc" ? " \u25B2" : " \u25BC";
  }

  // Header class builder
  function thClass(key: string, isMetric?: boolean): string {
    let cls = "";
    if (sortKey === key) cls += " sorted";
    if (isMetric && key === metric) cls += " metric-selected";
    return cls;
  }

  // Get experiment color dot
  function dotColor(exp: Experiment): string {
    const c = _colorForExp(exp, metric || "", mode, layout, ideas, filtered);
    return c || "#8b949e";
  }

  return (
    <div class="table-panel">
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th style="width: 16px"></th>
              <th
                class={thClass("label")}
                onClick={() => handleSort("label")}
              >
                Exp{sortArrow("label")}
              </th>
              <th
                class={thClass("idea")}
                onClick={() => handleSort("idea")}
              >
                Idea{sortArrow("idea")}
              </th>
              <th
                class={thClass("status")}
                onClick={() => handleSort("status")}
              >
                Status{sortArrow("status")}
              </th>
              {orderedMetrics.map((mk) => (
                <th
                  key={mk}
                  class={`metric-val${thClass(mk, true)}`}
                  onClick={() => handleSort(mk)}
                  title={mk}
                >
                  {mk}{sortArrow(mk)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {sorted.map((exp) => {
              const isHighlighted = highlighted === exp.idea_id;
              return (
                <tr
                  key={exp.id}
                  style={isHighlighted ? "background: #30363d" : undefined}
                  onMouseEnter={() => { highlightedIdea.value = exp.idea_id; }}
                  onMouseLeave={() => { if (highlightedIdea.value === exp.idea_id) highlightedIdea.value = null; }}
                  onClick={() => navigateToIdea(exp.idea_id, exp.label)}
                >
                  <td>
                    <span
                      class="color-dot"
                      style={`background: ${dotColor(exp)}`}
                    />
                  </td>
                  <td>
                    <span class="exp-link">
                      exp/{exp.label || exp.id}{exp._running ? " \u25B6" : ""}
                    </span>
                  </td>
                  <td>
                    <span class="idea-desc" title={exp.idea_description || ""}>
                      {exp.idea_description || `idea/${exp.idea_id}`}
                    </span>
                  </td>
                  <td>
                    {exp._running ? (
                      <RunningBadge pct={runningProgress.value[exp.label || String(exp.id)]} />
                    ) : (
                      <span
                        class="status-badge"
                        style={`background: ${STATUS_BADGE_COLORS[exp.idea_status || "active"] || "#333"}`}
                      >
                        {exp.idea_status || "active"}
                      </span>
                    )}
                  </td>
                  {orderedMetrics.map((mk) => {
                    const v = resolveNumericValue(exp, mk);
                    const isSelected = mk === metric;
                    return (
                      <td
                        key={mk}
                        class={`metric-val${isSelected ? " metric-selected" : ""}`}
                      >
                        {v !== undefined ? fmtNum(v) : ""}
                      </td>
                    );
                  })}
                </tr>
              );
            })}
            {sorted.length === 0 && (
              <tr>
                <td
                  colspan={4 + orderedMetrics.length}
                  style="text-align: center; color: #8b949e; padding: 20px;"
                >
                  No experiments match current filters
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
