// ------------------------------------------------------------
// Chart data building functions extracted from dashboard.html
// (_buildChartData, line ~1636).  Pure function — no DOM access.
// ------------------------------------------------------------

import type { Experiment, IdeaNode, SubwayLayout } from './types';
export type { ChartDataResult } from './types';
import { IDEA_PALETTE, _colorForExp, isLowerBetter } from './colors';

/** Internal meta keys that should not appear in the dropdown. */
const HIDDEN_META_KEYS = new Set(["git_branch", "git_commit", "worktree", "outdir"]);

/**
 * Resolve a chart key to a numeric value from an experiment.
 * Checks metrics first, then meta, then computed timing fields.
 */
export function resolveNumericValue(exp: Experiment, key: string): number | undefined {
  // 1. Metrics
  if (exp.metrics && typeof exp.metrics[key] === 'number') return exp.metrics[key];
  // 2. Meta (numeric values only)
  if (exp.meta && typeof exp.meta[key] === 'number') return exp.meta[key] as number;
  // 3. Timing fields (converted to epoch seconds for charting)
  if (key === 'runtime_seconds' && exp.started_at && exp.finished_at) {
    const ms = new Date(exp.finished_at).getTime() - new Date(exp.started_at).getTime();
    return ms > 0 ? ms / 1000 : undefined;
  }
  if (key === 'started_at' && exp.started_at) return new Date(exp.started_at).getTime() / 1000;
  if (key === 'finished_at' && exp.finished_at) return new Date(exp.finished_at).getTime() / 1000;
  if (key === 'created_at' && exp.created_at) return new Date(exp.created_at).getTime() / 1000;
  return undefined;
}

export interface GroupedKeys {
  metrics: string[];
  meta: string[];
  timing: string[];
}

/** Collect all chartable keys from experiments, grouped by source. */
export function collectChartKeys(experiments: Experiment[]): GroupedKeys {
  const metricSet = new Set<string>();
  const metaSet = new Set<string>();

  for (const exp of experiments) {
    if (exp.metrics) for (const k of Object.keys(exp.metrics)) {
      if (typeof exp.metrics[k] === 'number') metricSet.add(k);
    }
    if (exp.meta) for (const [k, v] of Object.entries(exp.meta)) {
      if (typeof v === 'number' && !HIDDEN_META_KEYS.has(k)) metaSet.add(k);
    }
  }

  // Remove meta keys that collide with metric keys
  for (const k of metricSet) metaSet.delete(k);

  const timing: string[] = [];
  const hasStarted = experiments.some((e) => e.started_at);
  const hasFinished = experiments.some((e) => e.finished_at);
  if (hasStarted && hasFinished) timing.push("runtime_seconds");
  if (hasStarted) timing.push("started_at");
  if (hasFinished) timing.push("finished_at");
  if (experiments.some((e) => e.created_at)) timing.push("created_at");

  return {
    metrics: [...metricSet].sort(),
    meta: [...metaSet].sort(),
    timing,
  };
}

export function filterMetricExperiments(
  metricKey: string,
  allExperiments: Experiment[],
  activeTagFilters: string[],
  tagFilterMode: "or" | "and",
  hiddenStatuses?: Set<string>,
): Experiment[] {
  let filtered = allExperiments.filter(
    (e) => resolveNumericValue(e, metricKey) !== undefined,
  );

  // Filter by idea status visibility
  if (hiddenStatuses && hiddenStatuses.size > 0) {
    filtered = filtered.filter((e) => !hiddenStatuses.has(e.idea_status || "active"));
  }

  if (activeTagFilters.length === 0) return filtered;

  const tagSet = new Set(activeTagFilters);
  filtered = filtered.filter((e) => {
    const expTags = e.tags || [];
    if (expTags.length === 0) return false;
    return tagFilterMode === "and"
      ? activeTagFilters.every((t) => expTags.includes(t))
      : expTags.some((t) => tagSet.has(t));
  });

  return filtered;
}

export function filterVisibleChartExperiments(
  metricKey: string,
  allExperiments: Experiment[],
  activeTagFilters: string[],
  tagFilterMode: "or" | "and",
  improvementsOnly: boolean,
  hiddenStatuses?: Set<string>,
): Experiment[] {
  const filtered = filterMetricExperiments(
    metricKey,
    allExperiments,
    activeTagFilters,
    tagFilterMode,
    hiddenStatuses,
  );

  if (!improvementsOnly) return filtered;

  const lower = isLowerBetter(metricKey);
  let best = lower ? Infinity : -Infinity;
  return filtered.filter((e) => {
    if (e._running) return true;
    const v = resolveNumericValue(e, metricKey) ?? 0;
    if (lower ? v < best : v > best) {
      best = v;
      return true;
    }
    return false;
  });
}

/**
 * Aggregate experiments by idea: one point per idea with the mean metric value.
 * Only completed (non-running) experiments are averaged.
 * Returns synthetic experiment-like objects representing each idea's mean.
 */
function aggregateByIdeaMean(
  experiments: Experiment[],
  metricKey: string,
): Experiment[] {
  const groups: Record<number, { exps: Experiment[]; sum: number; count: number }> = {};
  for (const e of experiments) {
    if (e._running) continue;
    const v = resolveNumericValue(e, metricKey);
    if (v === undefined) continue;
    if (!groups[e.idea_id]) groups[e.idea_id] = { exps: [], sum: 0, count: 0 };
    groups[e.idea_id].exps.push(e);
    groups[e.idea_id].sum += v;
    groups[e.idea_id].count += 1;
  }

  const result: Experiment[] = [];
  for (const ideaId of Object.keys(groups).map(Number).sort((a, b) => a - b)) {
    const g = groups[ideaId];
    if (g.count === 0) continue;
    const mean = g.sum / g.count;
    // Use the last experiment as a template for metadata
    const last = g.exps[g.exps.length - 1];
    result.push({
      ...last,
      metrics: { ...last.metrics, [metricKey]: mean },
      description: `idea/${ideaId} mean (${g.count} exp${g.count > 1 ? 's' : ''})`,
      _ideaMean: true,
      _meanCount: g.count,
    } as Experiment);
  }
  return result;
}

/**
 * Build the data arrays needed to render the Chart.js metrics chart.
 *
 * @param metricKey         - The metric key to plot (e.g. 'accuracy')
 * @param allExperiments    - All experiments (completed + running), pre-sorted
 * @param activeTagFilters  - Currently active tag filter set
 * @param improvementsOnly  - Whether to show only improvement-over-previous points
 * @param colorMode         - Current color mode string
 * @param allIdeas          - Map of idea id -> IdeaNode
 * @param currentLayout     - Current subway layout (may be null)
 * @returns ChartDataResult with labels, colors, values, etc.
 */
export function buildChartData(
  metricKey: string,
  allExperiments: Experiment[],
  activeTagFilters: string[],
  tagFilterMode: "or" | "and",
  improvementsOnly: boolean,
  colorMode: string,
  allIdeas: Record<number, IdeaNode>,
  currentLayout: SubwayLayout | null,
  reversed: boolean = false,
  hiddenStatuses?: Set<string>,
  useIdeaMean: boolean = false,
): ChartDataResult {
  const metricFiltered = filterMetricExperiments(
    metricKey,
    allExperiments,
    activeTagFilters,
    tagFilterMode,
    hiddenStatuses,
  );
  let filtered: Experiment[];

  // For idea mean: aggregate from ALL matching experiments (no improvements-only),
  // compute milestones on the full aggregated list, then apply improvements-only.
  // This ensures milestones don't change when toggling improvements-only.
  const meanMilestones = new Set<number>();
  let allMeans: Experiment[] = [];

  if (useIdeaMean) {
    // Get all matching experiments (without improvements-only filter)
    const allMatching = filterMetricExperiments(
      metricKey, allExperiments, activeTagFilters, tagFilterMode, hiddenStatuses,
    );
    allMeans = aggregateByIdeaMean(allMatching, metricKey);

    // Compute milestones on the FULL aggregated list (stable across toggles)
    if (colorMode === 'status+improve' || colorMode === 'improvement') {
      const lower = isLowerBetter(metricKey);
      let runBest = lower ? Infinity : -Infinity;
      for (const e of allMeans) {
        const v = e.metrics?.[metricKey];
        if (typeof v === 'number' && (lower ? v < runBest : v > runBest)) {
          runBest = v;
          meanMilestones.add(e.idea_id);
        }
      }
    }

    // Now apply improvements-only on the aggregated data
    if (improvementsOnly) {
      const lower = isLowerBetter(metricKey);
      let best = lower ? Infinity : -Infinity;
      filtered = allMeans.filter((e) => {
        const v = resolveNumericValue(e, metricKey) ?? 0;
        if (lower ? v < best : v > best) {
          best = v;
          return true;
        }
        return false;
      });
    } else {
      filtered = allMeans;
    }
  } else {
    filtered = filterVisibleChartExperiments(
      metricKey, allExperiments, activeTagFilters, tagFilterMode, improvementsOnly, hiddenStatuses,
    );
  }

  // Fallback sequential palette for 'idea' mode
  const ideaColorMap: Record<number, string> = {};
  let colorIdx = 0;
  for (const exp of filtered) {
    if (!(exp.idea_id in ideaColorMap)) {
      ideaColorMap[exp.idea_id] = IDEA_PALETTE[colorIdx % IDEA_PALETTE.length];
      colorIdx++;
    }
  }

  function getColor(exp: Experiment): string {
    // For mean mode, use milestone tracking instead of per-experiment lookup
    if ((exp as any)._ideaMean && (colorMode === 'status+improve' || colorMode === 'improvement')) {
      const base = '#8b949e';
      const statusBase = ({'concluded': '#58a6ff', 'abandoned': '#f85149', 'active': '#d29922'} as Record<string, string>)[exp.idea_status || 'active'] || base;
      return meanMilestones.has(exp.idea_id) ? '#e078f0' : statusBase;
    }
    const c = _colorForExp(exp, metricKey, colorMode, currentLayout, allIdeas, metricFiltered);
    return c || ideaColorMap[exp.idea_id] || '#8b949e';
  }

  if (reversed) filtered = filtered.slice().reverse();

  return {
    labels: filtered.map((e) => (e as any)._ideaMean
      ? `idea/${e.idea_id} (μ${(e as any)._meanCount})`
      : 'exp/' + (e.label || e.id) + (e._running ? ' \u25B6' : '')),
    pointColors: filtered.map((e) => getColor(e)),
    pointBgColors: filtered.map((e) => (e._running ? 'transparent' : getColor(e))),
    pointStyles: filtered.map((e) => (e._running ? 'triangle' : 'circle')),
    pointRadii: filtered.map((e) => (e._running ? 8 : 6)),
    pointBorderWidths: filtered.map((e) => (e._running ? 2.5 : 1)),
    values: filtered.map((e) => resolveNumericValue(e, metricKey) ?? 0),
    expData: filtered,
  };
}
