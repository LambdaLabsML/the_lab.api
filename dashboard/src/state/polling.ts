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
// Polling is RECONCILIATION, not the data plane: WS events patch the
// signals directly (see patchExperiment/patchIdea below and ws.ts), so
// while the event stream is healthy the pollers stretch to slow
// reconcile intervals; they tighten when the stream is down. All timers
// pause while the tab is hidden and fire immediately on return.
//
//   poller    connected   disconnected
//   backlog     60 s         10 s
//   graph       90 s         15 s
//   chart      120 s         30 s
//   log         60 s         15 s
//   costs       60 s         60 s   (pull-sampled server-side; no events)
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

type Poller = { fn: () => Promise<void>; fastMs: number; slowMs: number };
let pollTimers: ReturnType<typeof setTimeout>[] = [];
let pollersStarted = false;

/** Live view of stream health, set by ws.ts (avoids an import cycle). */
let streamHealthy = () => false;
export function _setStreamHealthProbe(fn: () => boolean): void {
  streamHealthy = fn;
}

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
      merged.sort(compareExps);
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
// Agent cost polling — always-on, server-persisted in .the_lab/agent_costs.json
// ---------------------------------------------------------------------------

function lastReading(e: AgentCostEntry): { cost: number; inTok: number; outTok: number } {
  if (e.live && e.readings?.length) {
    const r = e.readings[e.readings.length - 1];
    return { cost: r.cost, inTok: r.inTok, outTok: r.outTok };
  }
  return { cost: e.cost ?? 0, inTok: e.inTok ?? 0, outTok: e.outTok ?? 0 };
}

function recomputeTotals(map: Record<string, AgentCostEntry>): void {
  const vals = Object.values(map);
  if (!vals.length) return;
  let cost = 0, inTok = 0, outTok = 0;
  for (const e of vals) {
    const r = lastReading(e);
    cost += r.cost; inTok += r.inTok; outTok += r.outTok;
  }
  totalAgentCost.value = cost;
  totalAgentInputTokens.value = inTok;
  totalAgentOutputTokens.value = outTok;
  totalAgentTokens.value = inTok + outTok;
}

function pollAgentCosts(): Promise<void> {
  if (agentCostInflight) return agentCostInflight;
  agentCostInflight = (async () => {
    try {
      const resp = await fetch("/api/v1/agents/costs");
      if (!resp.ok) return;
      const data = await resp.json() as Record<string, AgentCostEntry>;
      agentCostMap.value = data;
      recomputeTotals(data);
    } catch { /* network error — keep stale data */ }
    finally {
      agentCostInflight = null;
    }
  })();
  return agentCostInflight;
}

// Sort: completed by finish time, running at end by start time.
function compareExps(a: Experiment, b: Experiment): number {
  if (a._running && !b._running) return 1;
  if (!a._running && b._running) return -1;
  const ta = a.finished_at || a.started_at || "";
  const tb = b.finished_at || b.started_at || "";
  return ta.localeCompare(tb);
}

// ---------------------------------------------------------------------------
// Event reducers — WS events carry the changed entity (P2.1), so the stream
// can PATCH these signals directly instead of refetching /chart-data and
// /graph wholesale on every doorbell. Pollers above remain the reconcile
// path; each patcher mirrors the corresponding endpoint's row semantics.
// ---------------------------------------------------------------------------

/**
 * Upsert/remove one experiment row from an event payload, mirroring
 * /chart-data composition: running rows and completed-with-metrics rows are
 * present; anything else (queued, failed, cancelled, metric-less) is absent.
 * Returns false when the caller should fall back to a full refresh.
 */
export function patchExperiment(row: Partial<Experiment>): boolean {
  if (!row || (!row.label && row.id == null)) return false;
  const key = row.label ?? String(row.id);
  const list = allExperiments.value;
  const idx = list.findIndex((e) => (e.label || String(e.id)) === key);
  const isRunning = row.status === "running";
  const belongs = isRunning ||
    (row.status === "completed" && row.metrics && Object.keys(row.metrics).length > 0);

  if (!belongs) {
    // Terminal-without-metrics / queued / failed / cancelled / deleted:
    // absent from chart-data, so drop any stale row (e.g. it was running).
    if (idx >= 0) {
      const next = list.filter((_, i) => i !== idx);
      resetGlobalBestBeforeCache();
      allExperiments.value = next;
    }
    clearProgress(key);
    return true;
  }

  const merged = {
    ...(idx >= 0 ? list[idx] : null),
    ...row,
    _running: isRunning || undefined,
  } as Experiment;
  if (!isRunning) delete merged._running;
  const next = idx >= 0 ? [...list] : [...list, merged];
  if (idx >= 0) next[idx] = merged;
  next.sort(compareExps);
  resetGlobalBestBeforeCache();
  allExperiments.value = next;
  if (!isRunning) clearProgress(key);
  return true;
}

/** Remove an experiment row entirely (experiment_deleted). */
export function removeExperiment(key: string): void {
  const list = allExperiments.value;
  const next = list.filter((e) => (e.label || String(e.id)) !== key);
  if (next.length !== list.length) {
    resetGlobalBestBeforeCache();
    allExperiments.value = next;
  }
  clearProgress(key);
}

/** Merge an idea payload into allIdeas + the graph node (idea_changed).
 *  Returns false for unknown nodes — new ideas need a full graph fetch
 *  (edges/layout). Computed graph flags (has_running, …) are preserved. */
export function patchIdea(idea: Partial<IdeaNode> & { id: number }): boolean {
  if (!idea || idea.id == null) return false;
  const cur = allIdeas.value[idea.id];
  allIdeas.value = { ...allIdeas.value, [idea.id]: { ...cur, ...idea } as IdeaNode };
  const g = graphData.value;
  const nidx = g ? g.nodes.findIndex((n) => n.id === idea.id) : -1;
  if (!g || nidx < 0) return false;
  const nodes = [...g.nodes];
  nodes[nidx] = { ...nodes[nidx], ...idea };
  graphData.value = { ...g, nodes };
  return true;
}

/** Update one running experiment's pct from experiment_progress_updated. */
export function patchProgress(label: string, pct: number): void {
  if (!label || typeof pct !== "number") return;
  runningProgress.value = { ...runningProgress.value, [label]: pct };
}

function clearProgress(label: string): void {
  if (label in runningProgress.value) {
    const p = { ...runningProgress.value };
    delete p[label];
    runningProgress.value = p;
  }
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
  if (pollersStarted) return; // already running
  pollersStarted = true;

  // Immediate initial fetches — run in parallel, then build log once chart
  // data is available so it can be joined against allExperiments.
  pollBacklog();
  pollGraph();
  const chartPromise = pollChartData();
  chartPromise.then(() => pollLog());
  pollAgentCosts();

  const pollers: Poller[] = [
    { fn: pollBacklog,    fastMs: 10_000, slowMs: 60_000 },
    { fn: pollGraph,      fastMs: 15_000, slowMs: 90_000 },
    { fn: pollChartData,  fastMs: 30_000, slowMs: 120_000 },
    // Log polling reuses the experiments already in `allExperiments`, so it
    // only needs to re-fetch the ideas list.
    { fn: pollLog,        fastMs: 15_000, slowMs: 60_000 },
    // Agent costs are SAMPLED by the GET itself server-side — no push
    // events exist, so this one keeps its cadence in both modes.
    { fn: pollAgentCosts, fastMs: 60_000, slowMs: 60_000 },
  ];

  // Self-scheduling chains: each round re-reads stream health so the
  // cadence adapts without restarting timers. Hidden tabs skip the fetch
  // entirely (cheap re-check loop) — the visibilitychange handler below
  // refreshes everything the moment the tab returns.
  const schedule = (p: Poller): void => {
    const delay = streamHealthy() ? p.slowMs : p.fastMs;
    const t = setTimeout(async () => {
      pollTimers = pollTimers.filter((x) => x !== t);
      if (!document.hidden) {
        try { await p.fn(); } catch { /* poller guards internally */ }
      }
      if (pollersStarted) schedule(p);
    }, document.hidden ? Math.max(delay, 60_000) : delay);
    pollTimers.push(t);
  };
  pollers.forEach(schedule);

  document.addEventListener("visibilitychange", onVisibility);
}

function onVisibility(): void {
  if (!document.hidden) {
    // Tab is back — reconcile everything now rather than waiting a round.
    pollBacklog();
    pollGraph();
    pollChartData().then(() => pollLog());
    pollAgentCosts();
  }
}

/** Stop all polling intervals. */
export function stopPolling(): void {
  pollersStarted = false;
  for (const t of pollTimers) clearTimeout(t);
  pollTimers = [];
  document.removeEventListener("visibilitychange", onVisibility);
}
