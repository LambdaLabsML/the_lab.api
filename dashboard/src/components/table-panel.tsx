import { useMemo, useState } from "preact/hooks";
import { allExperiments, allIdeas, currentLayout, highlightedIdea, runningProgress } from "../state/signals";
import {
  selectedMetric, colorMode, activeTagFilters, tagFilterMode,
  showAbandoned, showConcluded, showRunning,
  improvementsOnly, ideaMean, clipOutliers,
} from "../state/settings";
import { collectChartKeys, resolveNumericValue } from "../lib/chart-data";
import { _colorForExp, isLowerBetter } from "../lib/colors";
import { navigateToIdea } from "../lib/navigate";
import { badgeHtml, fmtMetricName } from "../lib/format";
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

// dynamic — driven by idea status; hex kept for inline background style
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
  // Default: sort by selected metric descending so best experiments are first
  const defaultMetric = selectedMetric.value;
  const [sortKey, setSortKey] = useState<string>(defaultMetric || "label");
  const [sortDir, setSortDir] = useState<SortDir>(defaultMetric ? "desc" : "asc");
  const [hiddenCols, setHiddenCols] = useState<Set<string>>(new Set());
  const [showColMenu, setShowColMenu] = useState(false);
  const [colOrder, setColOrder] = useState<string[] | null>(null);
  const [dragCol, setDragCol] = useState<string | null>(null);
  const [dragOverCol, setDragOverCol] = useState<string | null>(null);

  const experiments = allExperiments.value;
  const ideas = allIdeas.value;
  const layout = currentLayout.value;
  const metric = selectedMetric.value;

  // Compute milestone experiment IDs (new global bests in chronological order)
  const milestoneIds = useMemo(() => {
    if (!metric) return new Set<number>();
    const lower = isLowerBetter(metric);
    const sorted = experiments.filter(e => !e._running && e.metrics && typeof e.metrics[metric] === "number")
      .slice().sort((a, b) => a.id - b.id);
    const set = new Set<number>();
    let best: number | null = null;
    for (const e of sorted) {
      const v = e.metrics![metric] as number;
      if (best === null || (lower ? v < best : v > best)) { best = v; set.add(e.id); }
    }
    return set;
  }, [experiments, metric]);
  const mode = colorMode.value;
  const tags = activeTagFilters.value;
  const tagMode = tagFilterMode.value;
  const highlighted = highlightedIdea.value;
  const impOnly = improvementsOnly.value;
  const mean = ideaMean.value;

  // Idea-status filters (abandoned/concluded). The "running" toggle is
  // experiment-level — completed experiments under active ideas remain
  // visible when running is toggled off.
  const hiddenStatuses = useMemo(() => {
    const s = new Set<string>();
    if (!showAbandoned.value) s.add("abandoned");
    if (!showConcluded.value) s.add("concluded");
    return s;
  }, [showAbandoned.value, showConcluded.value]);
  const hideRunning = !showRunning.value;

  // Filter experiments by tags + status. When improvements-only is on and a
  // metric is selected, further filter to only improvement points.
  const filtered = useMemo(() => {
    // Base filter: tags + status (always applied)
    let result = experiments.filter((e) => {
      if (hiddenStatuses.has(e.idea_status || "active")) return false;
      if (hideRunning && (e._running || e.status === "running")) return false;
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

    // Improvements-only: keep only experiments that beat the previous best
    if (impOnly && metric) {
      const lower = isLowerBetter(metric);
      let best = lower ? Infinity : -Infinity;
      result = result.filter((e) => {
        if (e._running) return true;
        const v = resolveNumericValue(e, metric);
        if (v === undefined) return true; // keep experiments without the metric
        if (lower ? v < best : v > best) {
          best = v;
          return true;
        }
        return false;
      });
    }

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
  }, [experiments, hiddenStatuses, hideRunning, tags, tagMode, metric, impOnly, mean]);

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

  // Column ordering + visibility
  const visibleMetrics = useMemo(() => {
    const effectiveOrder = colOrder
      ? colOrder.filter((k) => allMetricKeys.includes(k))
      : orderedMetrics;
    const finalOrder = colOrder
      ? [...effectiveOrder, ...orderedMetrics.filter((k) => !effectiveOrder.includes(k))]
      : orderedMetrics;
    return finalOrder.filter((k) => !hiddenCols.has(k));
  }, [orderedMetrics, allMetricKeys, colOrder, hiddenCols]);

  // Sort experiments
  const sorted = useMemo(() => {
    const arr = filtered.slice();
    arr.sort((a, b) => {
      let cmp = 0;
      if (sortKey === "label") {
        const la = a.label || String(a.id);
        const lb = b.label || String(b.id);
        // Natural sort: "1.10" > "1.4" (compare idea then seq as integers)
        const pa = la.split(".").map(Number);
        const pb = lb.split(".").map(Number);
        for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
          const va = pa[i] ?? 0, vb = pb[i] ?? 0;
          if (va !== vb) { cmp = va - vb; break; }
        }
        if (cmp === 0) cmp = la.localeCompare(lb);
      } else if (sortKey === "idea") {
        const da = (a.idea_description || "").toLowerCase();
        const db = (b.idea_description || "").toLowerCase();
        cmp = da.localeCompare(db);
      } else if (sortKey === "status") {
        cmp = (a.idea_status || "active").localeCompare(b.idea_status || "active");
      } else if (sortKey === "milestone") {
        // Milestones first, then by label
        const ma = milestoneIds.has(a.id) ? 1 : 0;
        const mb = milestoneIds.has(b.id) ? 1 : 0;
        cmp = mb - ma;
      } else {
        // Metric column — always put missing values at bottom regardless of sort direction
        const va = resolveNumericValue(a, sortKey);
        const vb = resolveNumericValue(b, sortKey);
        if (va === undefined && vb === undefined) cmp = 0;
        else if (va === undefined) return 1;   // a has no value → goes to bottom
        else if (vb === undefined) return -1;  // b has no value → goes to bottom
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
                style="width: 14px; padding: 4px 2px; text-align: center; cursor: pointer;"
                title="New global best at time it ran — click to sort"
                class={thClass("milestone", false)}
                onClick={() => handleSort("milestone")}
              >★{sortArrow("milestone")}</th>
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
              {visibleMetrics.map((mk) => (
                <th
                  key={mk}
                  class={`metric-val${thClass(mk, true)}`}
                  onClick={() => handleSort(mk)}
                  title={mk}
                  draggable
                  onDragStart={(e) => {
                    setDragCol(mk);
                    (e as DragEvent).dataTransfer!.effectAllowed = "move";
                  }}
                  onDragOver={(e) => {
                    e.preventDefault();
                    setDragOverCol(mk);
                  }}
                  onDragEnd={() => {
                    if (dragCol && dragOverCol && dragCol !== dragOverCol) {
                      const arr = [...visibleMetrics];
                      const fromIdx = arr.indexOf(dragCol);
                      const toIdx = arr.indexOf(dragOverCol);
                      if (fromIdx >= 0 && toIdx >= 0) {
                        arr.splice(fromIdx, 1);
                        arr.splice(toIdx, 0, dragCol);
                        // Preserve hidden cols at end so they keep their position
                        setColOrder([...arr, ...[...hiddenCols]]);
                      }
                    }
                    setDragCol(null);
                    setDragOverCol(null);
                  }}
                  onContextMenu={(e) => {
                    e.preventDefault();
                    setHiddenCols(new Set([...hiddenCols, mk]));
                  }}
                  style={`${dragOverCol === mk ? "border-left: 2px solid var(--accent);" : ""}cursor: grab;`}
                >
                  {fmtMetricName(mk)}{sortArrow(mk)}
                </th>
              ))}
              {hiddenCols.size > 0 && (
                <th style="width: 24px; position: relative;">
                  <span
                    style="cursor: pointer; color: var(--accent); font-size: 12px;"
                    onClick={() => setShowColMenu(!showColMenu)}
                    title="Show hidden columns"
                  >+</span>
                  {showColMenu && (
                    <div style="position: absolute; top: 100%; right: 0; background: var(--bg-elev); border: 1px solid var(--border); border-radius: 4px; padding: 4px; z-index: 10; min-width: 120px;">
                      {[...hiddenCols].sort().map((col) => (
                        <div
                          key={col}
                          style="padding: 2px 8px; cursor: pointer; font-size: 10px; color: var(--text-muted); white-space: nowrap;"
                          onClick={() => {
                            const next = new Set(hiddenCols);
                            next.delete(col);
                            setHiddenCols(next);
                            setShowColMenu(false);
                          }}
                          onMouseEnter={(e) => { (e.target as HTMLElement).style.color = 'var(--text)'; }}
                          onMouseLeave={(e) => { (e.target as HTMLElement).style.color = 'var(--text-muted)'; }}
                        >
                          {col}
                        </div>
                      ))}
                      <div
                        style="padding: 2px 8px; cursor: pointer; font-size: 10px; color: var(--accent); border-top: 1px solid var(--border); margin-top: 2px; padding-top: 4px;"
                        onClick={() => { setHiddenCols(new Set()); setShowColMenu(false); }}
                      >
                        Show all
                      </div>
                    </div>
                  )}
                </th>
              )}
            </tr>
          </thead>
          <tbody>
            {sorted.map((exp) => {
              const isHighlighted = highlighted === exp.idea_id;
              return (
                <tr
                  key={exp.id}
                  style={isHighlighted ? "background: var(--bg-hi)" : undefined}
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
                  <td style="text-align:center; padding:3px 2px;">
                    {milestoneIds.has(exp.id) && <span class="exp-milestone">\u2605</span>}
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
                      <span dangerouslySetInnerHTML={{ __html: badgeHtml("running", runningProgress.value[exp.label || String(exp.id)]) }} />
                    ) : (
                      <span class={`badge badge-${exp.idea_status || "active"}`}>
                        {exp.idea_status || "active"}
                      </span>
                    )}
                  </td>
                  {visibleMetrics.map((mk) => {
                    const v = resolveNumericValue(exp, mk);
                    const isSelected = mk === metric;
                    // Color-code selected metric column by value quality
                    let valColor: string | undefined;
                    if (isSelected && v !== undefined) {
                      const lower = isLowerBetter(mk);
                      const allVals = sorted.filter(e => resolveNumericValue(e, mk) !== undefined).map(e => resolveNumericValue(e, mk) as number);
                      if (allVals.length > 1) {
                        const mn = Math.min(...allVals), mx = Math.max(...allVals);
                        const range = mx - mn;
                        if (range > 0) {
                          const frac = lower ? 1 - (v - mn) / range : (v - mn) / range;
                          if (frac >= 0.75) valColor = "var(--green)";
                          else if (frac >= 0.4) valColor = "var(--yellow)";
                          else if (frac > 0) valColor = "var(--text-muted)";
                          else valColor = "var(--text-faint)";
                        }
                      }
                    }
                    return (
                      <td
                        key={mk}
                        class={`metric-val${isSelected ? " metric-selected" : ""}`}
                        style={valColor ? { color: valColor, fontWeight: valColor === "var(--green)" ? "700" : undefined } : undefined}
                      >
                        {v !== undefined ? fmtNum(v) : ""}
                      </td>
                    );
                  })}
                  {hiddenCols.size > 0 && <td />}
                </tr>
              );
            })}
            {sorted.length === 0 && (
              <tr>
                <td
                  colspan={4 + visibleMetrics.length + (hiddenCols.size > 0 ? 1 : 0)}
                  style="text-align: center; color: var(--text-muted); padding: 20px;"
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
