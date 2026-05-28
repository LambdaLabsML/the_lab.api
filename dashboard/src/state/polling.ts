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

import { getBacklog, getGraph, getChartData, getAllIdeas, getExperimentProgress, listAgents } from "./api";
import { resetGlobalBestBeforeCache } from "../lib/colors";
import {
  backlogData,
  graphData,
  allExperiments,
  allIdeas,
  logEntries,
  runningProgress,
  agentCostMap,
  totalAgentCost,
  totalAgentTokens,
  totalAgentInputTokens,
  totalAgentOutputTokens,
} from "./signals";
import type { AgentCostEntry } from "./signals";
// chartOpen and currentView no longer needed — dockview manages panel visibility
import type { IdeaNode, Experiment, LogEntry, IdeaDetail } from "../lib/types";

// ---------------------------------------------------------------------------
// Internal timer handles
// ---------------------------------------------------------------------------

let backlogTimer: ReturnType<typeof setInterval> | null = null;
let graphTimer: ReturnType<typeof setInterval> | null = null;
let chartTimer: ReturnType<typeof setInterval> | null = null;
let logTimer: ReturnType<typeof setInterval> | null = null;
let agentCostTimer: ReturnType<typeof setInterval> | null = null;

// ---------------------------------------------------------------------------
// In-flight promise guards — ensure overlapping callers share one request.
// Each guard holds the Promise for an ongoing fetch and is cleared when it
// settles.  Subsequent calls while a fetch is pending return the same Promise.
// ---------------------------------------------------------------------------

let backlogInflight: Promise<void> | null = null;
let graphInflight: Promise<void> | null = null;
let chartInflight: Promise<void> | null = null;
let logInflight: Promise<void> | null = null;
let agentCostInflight: Promise<void> | null = null;

// Shared list-ideas fetch — used by pollLog. Exposed so callers can await
// the same list-response if one is already in flight.
let ideasListInflight: Promise<IdeaDetail[]> | null = null;

// Fields the log view actually needs — everything else (experiment_summary,
// latest_metrics, tags, branch, parent_ids, …) is fetched on demand when the
// detail panel opens. On a large lab this cuts the /ideas response from
// several MB down to a few KB.
const LOG_IDEA_FIELDS = "id,status,description,created_at,conclusion,notes";

async function fetchIdeasList(): Promise<IdeaDetail[]> {
  if (ideasListInflight) return ideasListInflight;
  ideasListInflight = (async () => {
    try {
      return await getAllIdeas(LOG_IDEA_FIELDS);
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
// Agent cost polling — always-on, persists completed costs to localStorage
// ---------------------------------------------------------------------------

const COST_CACHE_KEY = "the_lab_agent_costs_v1";

/** Load completed-agent cost cache from localStorage into the signal on startup. */
function loadCostCache(): Record<string, AgentCostEntry> {
  try {
    const raw = localStorage.getItem(COST_CACHE_KEY);
    if (raw) return JSON.parse(raw) as Record<string, AgentCostEntry>;
  } catch { /* ignore */ }
  return {};
}

function saveCostCache(map: Record<string, AgentCostEntry>): void {
  try {
    // Only persist completed agents (live costs are re-fetched on next load)
    const toSave: Record<string, AgentCostEntry> = {};
    for (const [id, e] of Object.entries(map)) {
      if (!e.live) toSave[id] = e;
    }
    localStorage.setItem(COST_CACHE_KEY, JSON.stringify(toSave));
  } catch { /* ignore */ }
}

function recomputeTotals(map: Record<string, AgentCostEntry>): void {
  const vals = Object.values(map);
  if (!vals.length) return;
  totalAgentCost.value = vals.reduce((s, e) => s + e.cost, 0);
  totalAgentInputTokens.value = vals.reduce((s, e) => s + e.inTok, 0);
  totalAgentOutputTokens.value = vals.reduce((s, e) => s + e.outTok, 0);
  totalAgentTokens.value = vals.reduce((s, e) => s + e.inTok + e.outTok, 0);
}

function pollAgentCosts(): Promise<void> {
  if (agentCostInflight) return agentCostInflight;
  agentCostInflight = (async () => {
    try {
      // Fetch live + past agents in parallel
      const [liveAgents, pastRes] = await Promise.all([
        listAgents().catch(() => []),
        fetch("/api/v1/agents/past").then((r) => r.ok ? r.json() : []).catch(() => []) as Promise<Array<{agent_id: string; completed_at?: string; created_at?: string}>>,
      ]);

      const current = { ...agentCostMap.value };
      const liveIds = new Set(liveAgents.map((a) => a.agent_id));

      // Build list of agents to fetch:
      //  - All live agents (always re-fetch — cost grows)
      //  - Past agents not yet in cache
      const toFetch: Array<{ id: string; ts: string; live: boolean }> = [];

      for (const a of liveAgents) {
        toFetch.push({ id: a.agent_id, ts: a.created_at || new Date().toISOString(), live: true });
      }
      for (const a of pastRes) {
        if (!current[a.agent_id] || current[a.agent_id].live) {
          // Not yet cached, or was previously marked live (now completed — re-fetch once)
          toFetch.push({
            id: a.agent_id,
            ts: a.completed_at || a.created_at || new Date().toISOString(),
            live: false,
          });
        }
      }

      if (!toFetch.length) return;

      const results = await Promise.allSettled(
        toFetch.map(({ id }) =>
          fetch(`/api/v1/agents/${id}/history`)
            .then((r) => (r.ok ? r.json() : null))
            .catch(() => null)
        )
      );

      let changed = false;
      results.forEach((r, i) => {
        if (r.status === "fulfilled" && r.value?.totals) {
          const t = r.value.totals;
          const { id, ts, live } = toFetch[i];
          // Only update if live (cost grows) or not yet cached
          if (live || !current[id]) {
            current[id] = {
              cost: t.cost_usd ?? 0,
              inTok: t.input_tokens ?? 0,
              outTok: t.output_tokens ?? 0,
              ts,
              live,
            };
            changed = true;
          }
        }
      });

      // Mark any agent that was live but is no longer in liveIds as completed
      for (const [id, entry] of Object.entries(current)) {
        if (entry.live && !liveIds.has(id)) {
          current[id] = { ...entry, live: false };
          changed = true;
        }
      }

      if (changed) {
        agentCostMap.value = current;
        recomputeTotals(current);
        saveCostCache(current);
      }
    } catch { /* network error — keep stale data */ }
    finally {
      agentCostInflight = null;
    }
  })();
  return agentCostInflight;
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

/** Re-fetch backlog data immediately. */
export function refreshBacklogData(): Promise<void> {
  return pollBacklog();
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

  // Hydrate agent costs from localStorage immediately so the topbar shows
  // cost before the first network fetch completes.
  const cached = loadCostCache();
  if (Object.keys(cached).length) {
    agentCostMap.value = cached;
    recomputeTotals(cached);
  }

  // Immediate initial fetches — run in parallel, then build log once chart
  // data is available so it can be joined against allExperiments.
  pollBacklog();
  pollGraph();
  const chartPromise = pollChartData();
  chartPromise.then(() => pollLog());
  pollAgentCosts();

  backlogTimer = setInterval(pollBacklog, 10_000);
  graphTimer = setInterval(pollGraph, 15_000);
  chartTimer = setInterval(pollChartData, 30_000);
  // Log polling reuses the experiments already in `allExperiments` (kept
  // fresh by chartTimer), so it only needs to re-fetch the ideas list.
  logTimer = setInterval(pollLog, 15_000);
  // Agent costs: poll every 60 s. Live agents re-fetch on every tick so the
  // topbar/sparkline update as cost accumulates.
  agentCostTimer = setInterval(pollAgentCosts, 60_000);
}

/** Stop all polling intervals. */
export function stopPolling(): void {
  if (backlogTimer !== null) { clearInterval(backlogTimer); backlogTimer = null; }
  if (graphTimer !== null) { clearInterval(graphTimer); graphTimer = null; }
  if (chartTimer !== null) { clearInterval(chartTimer); chartTimer = null; }
  if (logTimer !== null) { clearInterval(logTimer); logTimer = null; }
  if (agentCostTimer !== null) { clearInterval(agentCostTimer); agentCostTimer = null; }
}
