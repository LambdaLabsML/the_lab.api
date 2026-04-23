// ------------------------------------------------------------
// Centralized polling setup.
//
// Call `startPolling()` once (e.g. in the App component's
// useEffect) to kick off periodic fetches.  Each interval
// writes fresh data into the corresponding signal.
//
// Concurrent-call coalescing: each pollFoo() shares a single
// in-flight Promise, so overlapping callers (e.g. the initial
// startPolling fire + a manual refresh triggered around the
// same time) all piggy-back on one network request instead of
// firing the API multiple times.
//
// Polling intervals (matching the original dashboard.html):
//   Backlog   — every 10 s
//   Graph     — every 15 s
//   Chart     — every 30 s  (only when the chart panel is open)
//   Log       — every 15 s  (only when the log view is active)
// ------------------------------------------------------------

import { getBacklog, getGraph, getChartData, getAllIdeas, getExperimentProgress } from "./api";
import { resetGlobalBestBeforeCache } from "../lib/colors";
import {
  backlogData,
  graphData,
  allExperiments,
  allIdeas,
  logEntries,
  runningProgress,
} from "./signals";
// chartOpen and currentView no longer needed — dockview manages panel visibility
import type { IdeaNode, Experiment, LogEntry, IdeaDetail } from "../lib/types";

// ---------------------------------------------------------------------------
// Internal timer handles
// ---------------------------------------------------------------------------

let backlogTimer: ReturnType<typeof setInterval> | null = null;
let graphTimer: ReturnType<typeof setInterval> | null = null;
let chartTimer: ReturnType<typeof setInterval> | null = null;
let logTimer: ReturnType<typeof setInterval> | null = null;

// ---------------------------------------------------------------------------
// In-flight promise guards — ensure overlapping callers share one request.
// Each guard holds the Promise for an ongoing fetch and is cleared when it
// settles.  Subsequent calls while a fetch is pending return the same Promise.
// ---------------------------------------------------------------------------

let backlogInflight: Promise<void> | null = null;
let graphInflight: Promise<void> | null = null;
let chartInflight: Promise<void> | null = null;
let logInflight: Promise<void> | null = null;

// Shared list-ideas fetch — used by pollLog. Exposed so callers can await
// the same list-response if one is already in flight.
let ideasListInflight: Promise<IdeaDetail[]> | null = null;

async function fetchIdeasList(): Promise<IdeaDetail[]> {
  if (ideasListInflight) return ideasListInflight;
  ideasListInflight = (async () => {
    try {
      return await getAllIdeas();
    } finally {
      ideasListInflight = null;
    }
  })();
  return ideasListInflight;
}

// ---------------------------------------------------------------------------
// Individual fetch-and-update helpers
// ---------------------------------------------------------------------------

function pollBacklog(): Promise<void> {
  if (backlogInflight) return backlogInflight;
  backlogInflight = (async () => {
    try {
      backlogData.value = await getBacklog();
    } catch {
      // Network error — keep stale data.
    } finally {
      backlogInflight = null;
    }
  })();
  return backlogInflight;
}

function pollGraph(): Promise<void> {
  if (graphInflight) return graphInflight;
  graphInflight = (async () => {
    try {
      const data = await getGraph();
      graphData.value = data;

      // Merge nodes into the allIdeas map.
      const updated: Record<number, IdeaNode> = { ...allIdeas.value };
      for (const n of data.nodes) {
        updated[n.id] = n;
      }
      allIdeas.value = updated;
    } catch {
      // Network error — keep stale data.
    } finally {
      graphInflight = null;
    }
  })();
  return graphInflight;
}

function pollChartData(): Promise<void> {
  if (chartInflight) return chartInflight;
  chartInflight = (async () => {
    try {
      const data = await getChartData();
      // Mark running experiments with a synthetic flag (matches dashboard.html behaviour).
      const running: Experiment[] = data.running.map((e) => ({
        ...e,
        _running: true,
      }));
      const merged = [...data.experiments, ...running];
      // Sort: completed by finish time, running at end by start time.
      merged.sort((a, b) => {
        if (a._running && !b._running) return 1;
        if (!a._running && b._running) return -1;
        const ta = a.finished_at || a.started_at || "";
        const tb = b.finished_at || b.started_at || "";
        return ta.localeCompare(tb);
      });
      resetGlobalBestBeforeCache();
      allExperiments.value = merged;

      // Fetch progress for running experiments
      const runningLabels = running.map((e) => e.label || String(e.id)).filter(Boolean);
      if (runningLabels.length > 0) {
        const progress: Record<string, number> = {};
        await Promise.all(
          runningLabels.map(async (label) => {
            try {
              const resp = await getExperimentProgress(label);
              const pct = resp.progress?.pct_complete ?? resp.progress?.pct;
              if (typeof pct === "number") progress[label] = pct;
            } catch { /* ignore */ }
          }),
        );
        runningProgress.value = progress;
      } else {
        runningProgress.value = {};
      }
    } catch {
      // Network error — keep stale data.
    } finally {
      chartInflight = null;
    }
  })();
  return chartInflight;
}

function pollLog(): Promise<void> {
  if (logInflight) return logInflight;
  logInflight = (async () => {
    try {
      // Use the list endpoint only — it already returns `experiment_summary`
      // plus insight/milestone notes per idea. We then pull full experiment
      // rows from the `allExperiments` signal (populated by pollChartData),
      // so we don't need to fetch /ideas/{id} per idea.
      const ideas = await fetchIdeasList();
      const entries: LogEntry[] = [];

      // Index experiments by idea_id so we can join them with ideas in O(n).
      const expByIdea: Record<number, Experiment[]> = {};
      for (const e of allExperiments.value) {
        const arr = expByIdea[e.idea_id] || (expByIdea[e.idea_id] = []);
        arr.push(e);
      }

      for (const idea of ideas) {
        // Idea created
        entries.push({
          type: "idea",
          time: idea.created_at,
          ideaId: idea.id,
          title: `Idea #${idea.id} created`,
          body: idea.description,
          status: idea.status,
          extra: idea.conclusion ? `Conclusion: ${idea.conclusion}` : undefined,
        });

        // Experiments (joined from the chart-data experiments signal)
        for (const e of expByIdea[idea.id] || []) {
          const t = e.finished_at || e.started_at || e.created_at || "";
          let suffix = ` ${e.status}`;
          if (e.status === "completed") suffix = " completed";
          else if (e.status === "failed") suffix = " failed";
          else if (e.status === "running") suffix = " running";
          entries.push({
            type: "experiment",
            time: t,
            ideaId: idea.id,
            title: `exp/${e.label || e.id}${suffix}`,
            body: e.description,
            status: e.status,
            metrics: e.metrics,
            runtime: e.runtime,
          });
        }

        // Notes from the list endpoint (insight + milestone levels only;
        // observation/debug levels are only loaded on the detail panel to
        // keep the list response small).
        for (const n of idea.notes || []) {
          entries.push({
            type: `note-${n.level || "observation"}`,
            time: n.created_at || "",
            ideaId: idea.id,
            title: n.level || "observation",
            body: n.text,
          });
        }
      }

      entries.sort((a, b) => (b.time || "").localeCompare(a.time || ""));
      logEntries.value = entries;
    } catch {
      // Network error — keep stale data.
    } finally {
      logInflight = null;
    }
  })();
  return logInflight;
}

// ---------------------------------------------------------------------------
// Public: one-shot refresh (useful after mutations like tag rename)
// ---------------------------------------------------------------------------

/** Re-fetch graph data immediately (e.g. after suggesting an idea). */
export function refreshGraphData(): Promise<void> {
  return pollGraph();
}

/** Re-fetch chart data immediately regardless of the chartOpen setting. */
export function refreshChartData(): Promise<void> {
  return pollChartData();
}

// ---------------------------------------------------------------------------
// Public: start / stop
// ---------------------------------------------------------------------------

/**
 * Start all polling intervals and fire the initial fetch immediately.
 * Safe to call multiple times — subsequent calls are no-ops.
 *
 * Log polling waits for the initial chart-data fetch so the log view
 * has experiments to join against on first paint (avoids a second
 * refresh once chart-data arrives).
 */
export function startPolling(): void {
  if (backlogTimer !== null) return; // already running

  // Immediate initial fetches — run in parallel, then build log once chart
  // data is available so it can be joined against allExperiments.
  pollBacklog();
  pollGraph();
  const chartPromise = pollChartData();
  chartPromise.then(() => pollLog());

  backlogTimer = setInterval(pollBacklog, 10_000);
  graphTimer = setInterval(pollGraph, 15_000);
  chartTimer = setInterval(pollChartData, 30_000);
  // Log polling reuses the experiments already in `allExperiments` (kept
  // fresh by chartTimer), so it only needs to re-fetch the ideas list.
  logTimer = setInterval(pollLog, 15_000);
}

/** Stop all polling intervals. */
export function stopPolling(): void {
  if (backlogTimer !== null) {
    clearInterval(backlogTimer);
    backlogTimer = null;
  }
  if (graphTimer !== null) {
    clearInterval(graphTimer);
    graphTimer = null;
  }
  if (chartTimer !== null) {
    clearInterval(chartTimer);
    chartTimer = null;
  }
  if (logTimer !== null) {
    clearInterval(logTimer);
    logTimer = null;
  }
}
