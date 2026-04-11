// ------------------------------------------------------------
// Centralized polling setup.
//
// Call `startPolling()` once (e.g. in the App component's
// useEffect) to kick off periodic fetches.  Each interval
// writes fresh data into the corresponding signal.
//
// Polling intervals (matching the original dashboard.html):
//   Backlog   — every 10 s
//   Graph     — every 15 s
//   Chart     — every 30 s  (only when the chart panel is open)
//   Log       — every 15 s  (only when the log view is active)
// ------------------------------------------------------------

import { getBacklog, getGraph, getChartData, getAllIdeas, getIdea } from "./api";
import { resetGlobalBestBeforeCache } from "../lib/colors";
import {
  backlogData,
  graphData,
  allExperiments,
  allIdeas,
  logEntries,
} from "./signals";
// chartOpen and currentView no longer needed — dockview manages panel visibility
import type { IdeaNode, Experiment, LogEntry } from "../lib/types";

// ---------------------------------------------------------------------------
// Internal timer handles
// ---------------------------------------------------------------------------

let backlogTimer: ReturnType<typeof setInterval> | null = null;
let graphTimer: ReturnType<typeof setInterval> | null = null;
let chartTimer: ReturnType<typeof setInterval> | null = null;
let logTimer: ReturnType<typeof setInterval> | null = null;

// ---------------------------------------------------------------------------
// Individual fetch-and-update helpers
// ---------------------------------------------------------------------------

async function pollBacklog(): Promise<void> {
  try {
    backlogData.value = await getBacklog();
  } catch {
    // Network error — keep stale data.
  }
}

async function pollGraph(): Promise<void> {
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
  }
}

async function pollChartData(): Promise<void> {
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
  } catch {
    // Network error — keep stale data.
  }
}

async function pollLog(): Promise<void> {
  try {
    const ideas = await getAllIdeas();
    const entries: LogEntry[] = [];

    // Limit to the most recent 50 ideas to avoid N+1 explosion on large projects
    const recentIdeas = ideas
      .sort((a, b) => (b.created_at || "").localeCompare(a.created_at || ""))
      .slice(0, 50);

    const details = await Promise.all(
      recentIdeas.map((idea) =>
        getIdea(idea.id, true).catch(() => null),
      ),
    );

    for (const full of details) {
      if (!full) continue;
      const experiments = full.experiments || [];
      const notes = full.notes || [];

      // Idea created
      entries.push({
        type: "idea",
        time: full.created_at,
        ideaId: full.id,
        title: `Idea #${full.id} created`,
        body: full.description,
        status: full.status,
        extra: full.conclusion ? `Conclusion: ${full.conclusion}` : undefined,
      });

      // Experiments
      for (const e of experiments) {
        const t = e.finished_at || e.started_at || e.created_at || "";
        let suffix = ` ${e.status}`;
        if (e.status === "completed") suffix = " completed";
        else if (e.status === "failed") suffix = " failed";
        else if (e.status === "running") suffix = " running";
        entries.push({
          type: "experiment",
          time: t,
          ideaId: full.id,
          title: `exp/${e.label || e.id}${suffix}`,
          body: e.description,
          status: e.status,
          metrics: e.metrics,
          runtime: e.runtime,
        });
      }

      // Notes
      for (const n of notes) {
        entries.push({
          type: `note-${n.level || "observation"}`,
          time: n.created_at || "",
          ideaId: full.id,
          title: n.level || "observation",
          body: n.text,
        });
      }
    }

    entries.sort((a, b) => (b.time || "").localeCompare(a.time || ""));
    logEntries.value = entries;
  } catch {
    // Network error — keep stale data.
  }
}

// ---------------------------------------------------------------------------
// Public: one-shot refresh (useful after mutations like tag rename)
// ---------------------------------------------------------------------------

/** Re-fetch graph data immediately (e.g. after suggesting an idea). */
export async function refreshGraphData(): Promise<void> {
  try {
    const data = await getGraph();
    graphData.value = data;

    const updated: Record<number, IdeaNode> = { ...allIdeas.value };
    for (const n of data.nodes) {
      updated[n.id] = n;
    }
    allIdeas.value = updated;
  } catch {
    // ignore
  }
}

/** Re-fetch chart data immediately regardless of the chartOpen setting. */
export async function refreshChartData(): Promise<void> {
  try {
    const data = await getChartData();
    const running: Experiment[] = data.running.map((e) => ({
      ...e,
      _running: true,
    }));
    const merged = [...data.experiments, ...running];
    merged.sort((a, b) => {
      if (a._running && !b._running) return 1;
      if (!a._running && b._running) return -1;
      const ta = a.finished_at || a.started_at || "";
      const tb = b.finished_at || b.started_at || "";
      return ta.localeCompare(tb);
    });
    resetGlobalBestBeforeCache();
    allExperiments.value = merged;
  } catch {
    // ignore
  }
}

// ---------------------------------------------------------------------------
// Public: start / stop
// ---------------------------------------------------------------------------

/**
 * Start all polling intervals and fire the initial fetch immediately.
 * Safe to call multiple times — subsequent calls are no-ops.
 */
export function startPolling(): void {
  if (backlogTimer !== null) return; // already running

  // Immediate initial fetches
  pollBacklog();
  pollGraph();
  pollChartData();
  pollLog();

  backlogTimer = setInterval(pollBacklog, 10_000);
  graphTimer = setInterval(pollGraph, 15_000);
  chartTimer = setInterval(pollChartData, 30_000);
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
