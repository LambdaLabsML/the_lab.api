// ------------------------------------------------------------
// Chart data building functions extracted from dashboard.html
// (_buildChartData, line ~1636).  Pure function — no DOM access.
// ------------------------------------------------------------

import type { Experiment, IdeaNode, SubwayLayout } from './types';
export type { ChartDataResult } from './types';
import { IDEA_PALETTE, _colorForExp } from './colors';

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
): ChartDataResult {
  let filtered = allExperiments.filter(
    (e) => e.metrics && e.metrics[metricKey] !== undefined,
  );

  if (activeTagFilters.length > 0) {
    const tagSet = new Set(activeTagFilters);
    if (tagFilterMode === "and") {
      filtered = filtered.filter(
        (e) => e.tags && activeTagFilters.every((t) => e.tags!.includes(t)),
      );
    } else {
      filtered = filtered.filter(
        (e) => e.tags && e.tags.some((t) => tagSet.has(t)),
      );
    }
  }

  if (improvementsOnly) {
    let best = -Infinity;
    filtered = filtered.filter((e) => {
      const v = e.metrics![metricKey];
      if (v > best) {
        best = v;
        return true;
      }
      return false;
    });
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
    const c = _colorForExp(exp, metricKey, colorMode, currentLayout, allIdeas, allExperiments);
    return c || ideaColorMap[exp.idea_id] || '#8b949e';
  }

  return {
    labels: filtered.map((e) => 'exp/' + e.id + (e._running ? ' \u25B6' : '')),
    pointColors: filtered.map((e) => getColor(e)),
    pointBgColors: filtered.map((e) => (e._running ? 'transparent' : getColor(e))),
    pointStyles: filtered.map((e) => (e._running ? 'triangle' : 'circle')),
    pointRadii: filtered.map((e) => (e._running ? 8 : 6)),
    pointBorderWidths: filtered.map((e) => (e._running ? 2.5 : 1)),
    values: filtered.map((e) => e.metrics![metricKey]),
    expData: filtered,
  };
}
