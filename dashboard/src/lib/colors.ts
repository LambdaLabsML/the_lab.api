// ------------------------------------------------------------
// Color constants and helper functions extracted from
// dashboard.html.  All functions accept their data dependencies
// as parameters instead of reading globals.
// ------------------------------------------------------------

import type { Experiment, IdeaNode, SubwayLayout } from './types';

/** Per-status background / border / font colors for station nodes. */
export const STATUS_COLORS: Record<string, { bg: string; border: string; font: string }> = {
  active:    { bg: '#238636', border: '#3fb950', font: '#ffffff' },
  running:   { bg: '#6e4b00', border: '#d29922', font: '#ffffff' },
  concluded: { bg: '#1f6feb', border: '#58a6ff', font: '#ffffff' },
  abandoned: { bg: '#da3633', border: '#f85149', font: '#ffffff' },
  suggested: { bg: '#2d1a00', border: '#d29922', font: '#ffffff' },
};

/** Flat status → accent color used for bar borders and dots. */
export const STATUS_BAR_COLORS: Record<string, string> = {
  active: '#3fb950',
  running: '#d29922',
  concluded: '#58a6ff',
  abandoned: '#f85149',
  suggested: '#d29922',
};

/** Sequential palette used for lane/idea coloring. */
export const IDEA_PALETTE: string[] = [
  '#58a6ff', '#3fb950', '#d29922', '#f85149', '#d2a8ff',
  '#79c0ff', '#56d364', '#e3b341', '#ff7b72', '#bc8cff',
  '#a5d6ff', '#7ee787', '#e078f0', '#ffa198', '#d8b4fe',
];

/** Ordering used when sorting lanes by "worst" status. */
export const STATUS_ORDER: Record<string, number> = {
  concluded: 0,
  active: 1,
  abandoned: 2,
  suggested: 3,
};

// Internal flat status-to-color map used by _colorForExp.
const _STATUS_COLORS: Record<string, string> = {
  active: '#3fb950',
  concluded: '#58a6ff',
  abandoned: '#f85149',
  running: '#d29922',
};

// ---------------------------------------------------------------------------
// Metric direction heuristic — mirrors the backend's metric_direction().
// ---------------------------------------------------------------------------

const _LOWER_IS_BETTER_PATTERNS = [
  "loss", "bpb", "perplexity", "error", "mse", "mae", "rmse",
  "cost", "latency", "time", "bytes", "regret", "cer", "wer",
  "fid", "distance", "penalty",
];

/** Infer whether lower values are better for a given metric name. */
export function isLowerBetter(metricKey: string): boolean {
  const k = metricKey.toLowerCase();
  return _LOWER_IS_BETTER_PATTERNS.some((p) => k.includes(p));
}

/** Returns true when `a` is strictly better than `b`. */
function _better(a: number, b: number, lower: boolean): boolean {
  return lower ? a < b : a > b;
}

// ---------------------------------------------------------------------------
// Module-level cache for _computeGlobalBestBefore.
// Keyed by metric + experiment signature so filtered subsets do not collide.
// Call `resetGlobalBestBeforeCache()` whenever experiment data changes.
// ---------------------------------------------------------------------------
let _globalBestBefore: Record<string, Record<number, number | null>> = {};

/** Invalidate the global-best-before cache (call after new chart-data load). */
export function resetGlobalBestBeforeCache(): void {
  _globalBestBefore = {};
}

// ---------------------------------------------------------------------------
// Color computation helpers
// ---------------------------------------------------------------------------

/**
 * For a given idea, find the best metric value among experiments that
 * belong to any of its parent ideas.
 */
export function _parentBestMetric(
  ideaId: number,
  metricKey: string,
  allIdeas: Record<number, IdeaNode>,
  allExperiments: Experiment[],
): number | null {
  const idea = allIdeas[ideaId];
  if (!idea) return null;
  const pids = idea.parent_ids || [];
  const lower = isLowerBetter(metricKey);
  let best: number | null = null;
  for (let i = 0; i < pids.length; i++) {
    for (let j = 0; j < allExperiments.length; j++) {
      const e = allExperiments[j];
      if (e.idea_id === pids[i] && e.metrics && e.metrics[metricKey] !== undefined) {
        const v = e.metrics[metricKey];
        if (best === null || _better(v, best, lower)) best = v;
      }
    }
  }
  return best;
}

/** Best metric value for any experiment belonging to `ideaId`. */
export function _ideaBestMetric(
  ideaId: number,
  metricKey: string,
  allExperiments: Experiment[],
): number | null {
  const lower = isLowerBetter(metricKey);
  let best: number | null = null;
  for (let j = 0; j < allExperiments.length; j++) {
    const e = allExperiments[j];
    if (e.idea_id === ideaId && e.metrics && e.metrics[metricKey] !== undefined) {
      if (best === null || _better(e.metrics[metricKey], best, lower)) best = e.metrics[metricKey];
    }
  }
  return best;
}

/**
 * Precompute the best metric value from all experiments that finished
 * *before* each experiment (keyed by exp id).
 *
 * Results are cached in a module-level map keyed by `metricKey`.
 * Call `resetGlobalBestBeforeCache()` to invalidate.
 */
export function _computeGlobalBestBefore(
  metricKey: string,
  allExperiments: Experiment[],
): Record<number, number | null> {
  const sorted = allExperiments
    .filter((e) => e.metrics && e.metrics[metricKey] !== undefined && !e._running)
    .slice()
    .sort((a, b) =>
      (a.finished_at || a.started_at || '').localeCompare(b.finished_at || b.started_at || ''),
    );

  const cacheKey = metricKey + "::" + sorted
    .map((e) => `${e.id}:${e.finished_at || e.started_at || ''}:${e.metrics![metricKey]}`)
    .join("|");
  if (_globalBestBefore[cacheKey]) return _globalBestBefore[cacheKey];

  const lower = isLowerBetter(metricKey);
  let best: number | null = null;
  const map: Record<number, number | null> = {};
  for (let i = 0; i < sorted.length; i++) {
    map[sorted[i].id] = best; // best BEFORE this experiment
    const v = sorted[i].metrics![metricKey];
    if (best === null || _better(v, best, lower)) best = v;
  }
  _globalBestBefore[cacheKey] = map;
  return map;
}

/**
 * Did any experiment in `ideaId` beat the global best at the time
 * it ran?
 */
export function _ideaHasGlobalImprovement(
  ideaId: number,
  metricKey: string,
  allExperiments: Experiment[],
): boolean {
  const lower = isLowerBetter(metricKey);
  const bestBefore = _computeGlobalBestBefore(metricKey, allExperiments);
  for (let j = 0; j < allExperiments.length; j++) {
    const e = allExperiments[j];
    if (e.idea_id !== ideaId || !e.metrics || e.metrics[metricKey] === undefined || e._running)
      continue;
    const prev = bestBefore[e.id];
    if (prev === null || prev === undefined || _better(e.metrics[metricKey], prev, lower)) return true;
  }
  return false;
}

/**
 * Determine the dot/border color for a single experiment point
 * in the chart, given the current `colorMode`.
 *
 * Returns `null` for 'idea' mode — the caller is expected to
 * fall back to an idea-palette color map.
 */
export function _colorForExp(
  exp: Experiment,
  metricKey: string,
  mode: string,
  currentLayout: SubwayLayout | null,
  allIdeas: Record<number, IdeaNode>,
  allExperiments: Experiment[],
): string | null {
  if (mode === 'lane') {
    if (currentLayout && currentLayout.ideaLane[exp.idea_id] !== undefined)
      return IDEA_PALETTE[currentLayout.ideaLane[exp.idea_id] % IDEA_PALETTE.length];
    return '#8b949e';
  }
  if (mode === 'status') {
    return _STATUS_COLORS[exp.idea_status || 'active'] || '#8b949e';
  }
  if (mode === 'status+improve') {
    const base = _STATUS_COLORS[exp.idea_status || 'active'] || '#8b949e';
    if (!exp.metrics || exp.metrics[metricKey] === undefined || exp._running) return base;
    const lower = isLowerBetter(metricKey);
    const bestBefore = _computeGlobalBestBefore(metricKey, allExperiments);
    const prev = bestBefore[exp.id];
    if (prev === null || prev === undefined || _better(exp.metrics[metricKey], prev, lower))
      return '#e078f0'; // purple — new global best at time of running
    return base;
  }
  if (mode === 'improvement') {
    if (!exp.metrics || exp.metrics[metricKey] === undefined) return '#8b949e';
    const parentBest = _parentBestMetric(exp.idea_id, metricKey, allIdeas, allExperiments);
    if (parentBest === null) return '#d29922';
    return _better(exp.metrics[metricKey], parentBest, isLowerBetter(metricKey)) ? '#3fb950' : '#f85149';
  }
  return null; // 'idea' — handled by ideaColorMap in caller
}

/**
 * Determine the dot/border color for an idea node in the DAG,
 * given the current `colorMode`.
 */
export function _colorForIdea(
  ideaId: number,
  mode: string,
  metricKey: string,
  currentLayout: SubwayLayout | null,
  allIdeas: Record<number, IdeaNode>,
  allExperiments: Experiment[],
): string {
  if (mode === 'lane') {
    if (currentLayout && currentLayout.ideaLane[ideaId] !== undefined)
      return IDEA_PALETTE[currentLayout.ideaLane[ideaId] % IDEA_PALETTE.length];
    return '#8b949e';
  }
  if (mode === 'status') {
    const idea = allIdeas[ideaId];
    const st = idea ? (idea.has_running ? 'running' : idea.status) : 'active';
    return STATUS_BAR_COLORS[st] || '#8b949e';
  }
  if (mode === 'status+improve') {
    const idea = allIdeas[ideaId];
    const st = idea ? (idea.has_running ? 'running' : idea.status) : 'active';
    const base = STATUS_BAR_COLORS[st] || '#8b949e';
    if (!metricKey) return base;
    if (_ideaHasGlobalImprovement(ideaId, metricKey, allExperiments))
      return '#e078f0'; // purple — set a new global best
    return base;
  }
  if (mode === 'improvement') {
    if (!metricKey) return '#8b949e';
    const myBest = _ideaBestMetric(ideaId, metricKey, allExperiments);
    if (myBest === null) return '#8b949e';
    const parentBest = _parentBestMetric(ideaId, metricKey, allIdeas, allExperiments);
    if (parentBest === null) return '#d29922';
    return _better(myBest, parentBest, isLowerBetter(metricKey)) ? '#3fb950' : '#f85149';
  }
  // 'idea' — palette by ID
  return IDEA_PALETTE[(ideaId - 1) % IDEA_PALETTE.length];
}
