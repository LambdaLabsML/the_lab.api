// ------------------------------------------------------------
// Color constants and helper functions extracted from
// dashboard.html.  All functions accept their data dependencies
// as parameters instead of reading globals.
// ------------------------------------------------------------

import type { Experiment, IdeaNode, SubwayLayout } from './types';
import { getCssVar } from './css-vars';

/** Per-status background / border / font colors for station nodes. */
export const STATUS_COLORS: Record<string, { bg: string; border: string; font: string }> = {
  active:    { bg: '#238636', border: '#3fb950', font: '#ffffff' },
  running:   { bg: '#6e4b00', border: '#d29922', font: '#ffffff' },
  queued:    { bg: '#1c2a3a', border: '#6e7681', font: '#c9d1d9' },
  concluded: { bg: '#1f6feb', border: '#58a6ff', font: '#ffffff' },
  abandoned: { bg: '#da3633', border: '#f85149', font: '#ffffff' },
  suggested: { bg: '#2d1a00', border: '#d29922', font: '#ffffff' },
};

/**
 * Flat status → accent color used for bar borders and dots.
 * @deprecated Use getStatusColor(status) instead for theme-aware runtime resolution.
 */
export const STATUS_BAR_COLORS: Record<string, string> = {
  active: '#3fb950',
  running: '#d29922',
  queued: '#6e7681',
  concluded: '#58a6ff',
  abandoned: '#f85149',
  suggested: '#d29922',
};

/** Maps status → CSS var() reference strings.
 *  Returning var(--token) instead of a resolved hex lets the browser
 *  re-evaluate the colour automatically when [data-theme] changes,
 *  with no JS re-render required. */
const STATUS_VAR_MAP: Record<string, string> = {
  active:    'var(--green)',
  running:   'var(--yellow)',
  concluded: 'var(--accent)',
  abandoned: 'var(--red)',
  suggested: 'var(--text-muted)',
  pending:   'var(--text-faint)',
  queued:    'var(--text-muted)',
};

/** Returns a CSS var() reference for the given status.
 *  Use this in inline style= attributes so the browser resolves the
 *  colour live; use getCssVar() when you need a resolved hex (Chart.js). */
export function getStatusColor(status: string): string {
  return STATUS_VAR_MAP[status] ?? 'var(--text-faint)';
}

/** Sequential palette used for lane/idea coloring. */
export const IDEA_PALETTE: string[] = [
  '#58a6ff', '#3fb950', '#d29922', '#f85149', '#d2a8ff',
  '#79c0ff', '#56d364', '#e3b341', '#ff7b72', '#bc8cff',
  '#a5d6ff', '#7ee787', '#e078f0', '#ffa198', '#d8b4fe',
];

/** Stable per-agent color: hash the agent id into the shared palette so an
 *  agent keeps the same color across renders and components. */
export function agentColor(id: string | null | undefined): string {
  if (!id) return 'var(--text-faint)';
  let h = 0;
  for (let i = 0; i < id.length; i++) h = (h * 31 + id.charCodeAt(i)) >>> 0;
  return IDEA_PALETTE[h % IDEA_PALETTE.length];
}

/** 1–2 char initials for an agent avatar. */
export function agentInitials(id: string | null | undefined): string {
  if (!id) return '··';
  const clean = id.replace(/[^a-zA-Z0-9]/g, '');
  return (clean || id).slice(0, 2).toUpperCase();
}

/** Ordering used when sorting lanes by "worst" status. */
export const STATUS_ORDER: Record<string, number> = {
  concluded: 0,
  active: 1,
  abandoned: 2,
  suggested: 3,
};

// Internal helper — resolves experiment/idea status to a theme-aware color.
function _expStatusColor(status: string): string {
  return getStatusColor(status);
}

// ---------------------------------------------------------------------------
// Metric direction heuristic — mirrors the backend's metric_direction().
// ---------------------------------------------------------------------------

const _LOWER_IS_BETTER_PATTERNS = [
  "loss", "bpb", "perplexity", "error", "mse", "mae", "rmse",
  "cost", "latency", "time", "bytes", "regret", "cer", "wer",
  "fid", "distance", "penalty", "ttft", "_ms",
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

// Memo layer for _computeGlobalBestBefore: the filter+sort+key-build is O(n log n)
// and was running 149 times per graph render (once per idea). With reference
// equality on allExperiments + metricKey, the preparation runs only once per
// render, dropping 149 × O(n log n) → 1 × O(n log n).
let _sortedMemo: { exps: Experiment[]; metric: string; sorted: Experiment[]; key: string } | null = null;

/** Invalidate the global-best-before cache (call after new chart-data load). */
export function resetGlobalBestBeforeCache(): void {
  _globalBestBefore = {};
  _sortedMemo = null;  // also clear the sort memo
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
      if (e.idea_id === pids[i] && e.metrics && typeof e.metrics[metricKey] === 'number') {
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
    if (e.idea_id === ideaId && e.metrics && typeof e.metrics[metricKey] === 'number') {
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
  // Use memoised sorted array + cacheKey when called repeatedly with the
  // same experiments array (same render cycle). 149 idea-colour calls in
  // the graph would otherwise each run an O(n log n) sort + O(n) key build.
  let sorted: Experiment[];
  let cacheKey: string;

  if (_sortedMemo && _sortedMemo.exps === allExperiments && _sortedMemo.metric === metricKey) {
    sorted   = _sortedMemo.sorted;
    cacheKey = _sortedMemo.key;
  } else {
    sorted = allExperiments
      .filter((e) => e.metrics && typeof e.metrics[metricKey] === 'number' && !e._running)
      .slice()
      .sort((a, b) =>
        (a.finished_at || a.started_at || '').localeCompare(b.finished_at || b.started_at || ''),
      );
    cacheKey = metricKey + "::" + sorted
      .map((e) => `${e.id}:${e.finished_at || e.started_at || ''}:${e.metrics![metricKey]}`)
      .join("|");
    _sortedMemo = { exps: allExperiments, metric: metricKey, sorted, key: cacheKey };
  }

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
    if (e.idea_id !== ideaId || !e.metrics || typeof e.metrics[metricKey] !== 'number' || e._running)
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
    return 'var(--text-muted)';
  }
  if (mode === 'status') {
    return _expStatusColor(exp.idea_status || 'active');
  }
  if (mode === 'status+improve') {
    const base = _expStatusColor(exp.idea_status || 'active');
    if (!exp.metrics || typeof exp.metrics[metricKey] !== 'number' || exp._running) return base;
    const lower = isLowerBetter(metricKey);
    const bestBefore = _computeGlobalBestBefore(metricKey, allExperiments);
    const prev = bestBefore[exp.id];
    if (prev === null || prev === undefined || _better(exp.metrics[metricKey], prev, lower))
      return 'var(--purple)'; // new global best
    return base;
  }
  if (mode === 'improvement') {
    if (!exp.metrics || typeof exp.metrics[metricKey] !== 'number') return 'var(--text-muted)';
    const parentBest = _parentBestMetric(exp.idea_id, metricKey, allIdeas, allExperiments);
    if (parentBest === null) return 'var(--yellow)';
    return _better(exp.metrics[metricKey], parentBest, isLowerBetter(metricKey)) ? 'var(--green)' : 'var(--red)';
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
    return 'var(--text-muted)';
  }
  if (mode === 'status') {
    const idea = allIdeas[ideaId];
    const st = idea ? (idea.has_running ? 'running' : (idea.has_queued ? 'queued' : idea.status)) : 'active';
    return getStatusColor(st);
  }
  if (mode === 'status+improve') {
    const idea = allIdeas[ideaId];
    const st = idea ? (idea.has_running ? 'running' : (idea.has_queued ? 'queued' : idea.status)) : 'active';
    const base = getStatusColor(st);
    if (!metricKey) return base;
    if (_ideaHasGlobalImprovement(ideaId, metricKey, allExperiments))
      return 'var(--purple)'; // new global best
    return base;
  }
  if (mode === 'improvement') {
    if (!metricKey) return 'var(--text-muted)';
    const myBest = _ideaBestMetric(ideaId, metricKey, allExperiments);
    if (myBest === null) return 'var(--text-muted)';
    const parentBest = _parentBestMetric(ideaId, metricKey, allIdeas, allExperiments);
    if (parentBest === null) return 'var(--yellow)';
    return _better(myBest, parentBest, isLowerBetter(metricKey)) ? 'var(--green)' : 'var(--red)';
  }
  // 'idea' — palette by ID
  return IDEA_PALETTE[(ideaId - 1) % IDEA_PALETTE.length];
}
