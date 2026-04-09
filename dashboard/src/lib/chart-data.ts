// ------------------------------------------------------------
// Chart data building functions extracted from dashboard.html
// (_buildChartData, line ~1636).  Pure function — no DOM access.
// ------------------------------------------------------------

import type { Experiment, IdeaNode, SubwayLayout } from './types';
export type { ChartDataResult } from './types';
import { IDEA_PALETTE, _colorForExp, isLowerBetter } from './colors';

export function filterMetricExperiments(
  metricKey: string,
  allExperiments: Experiment[],
  activeTagFilters: string[],
  tagFilterMode: "or" | "and",
  hiddenStatuses?: Set<string>,
): Experiment[] {
  let filtered = allExperiments.filter(
    (e) => e.metrics && typeof e.metrics[metricKey] === 'number',
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
    const v = e.metrics![metricKey];
    if (lower ? v < best : v > best) {
      best = v;
      return true;
    }
    return false;
  });
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
): ChartDataResult {
  const metricFiltered = filterMetricExperiments(
    metricKey,
    allExperiments,
    activeTagFilters,
    tagFilterMode,
    hiddenStatuses,
  );
  let filtered = filterVisibleChartExperiments(
    metricKey,
    allExperiments,
    activeTagFilters,
    tagFilterMode,
    improvementsOnly,
    hiddenStatuses,
  );

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
    const c = _colorForExp(exp, metricKey, colorMode, currentLayout, allIdeas, metricFiltered);
    return c || ideaColorMap[exp.idea_id] || '#8b949e';
  }

  if (reversed) filtered = filtered.slice().reverse();

  return {
    labels: filtered.map((e) => 'exp/' + (e.label || e.id) + (e._running ? ' \u25B6' : '')),
    pointColors: filtered.map((e) => getColor(e)),
    pointBgColors: filtered.map((e) => (e._running ? 'transparent' : getColor(e))),
    pointStyles: filtered.map((e) => (e._running ? 'triangle' : 'circle')),
    pointRadii: filtered.map((e) => (e._running ? 8 : 6)),
    pointBorderWidths: filtered.map((e) => (e._running ? 2.5 : 1)),
    values: filtered.map((e) => e.metrics![metricKey]),
    expData: filtered,
  };
}
