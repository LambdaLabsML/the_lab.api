import { useCallback, useEffect, useRef, useState } from "preact/hooks";
import { allExperiments, allIdeas, runningProgress } from "../../state/signals";
import { selectedMetric, useSetting } from "../../state/settings";
import { getQueue } from "../../state/api";
import { useLiveRefresh } from "../../lib/hooks";
import { navigateToIdea } from "../../lib/navigate";
import { fmtScoreShort } from "../../lib/format";
import type { QueueSnapshot } from "../../lib/types";
import { Eyebrow } from "../ui";

const queuedOpen = useSetting("monitorQueuedOpen", false);

/** Running and queued experiments share the queue's authoritative snapshot. */
export function ActivityShortlog() {
  const [snapshot, setSnapshot] = useState<QueueSnapshot | null>(null);
  const [error, setError] = useState(false);
  const alive = useRef(true);
  const load = useCallback(async () => {
    try { const next = await getQueue(); if (alive.current) { setSnapshot(next); setError(false); } }
    catch { if (alive.current) setError(true); }
  }, []);
  useEffect(() => { alive.current = true; void load(); return () => { alive.current = false; }; }, []);
  useLiveRefresh(["queue_changed", "experiment_started", "experiment_finished", "experiment_cancelled"], load, 30_000);
  const experiments = allExperiments.value;
  const running = snapshot?.running ?? experiments.filter(e => e._running || e.status === "running");
  const queued = snapshot?.queued ?? [];
  const ideas = allIdeas.value;
  const progress = runningProgress.value;
  return <div class="activity-shortlog" aria-label="Experiment monitor">
    <div class="shortlog-block">
      <div class="shortlog-sub"><Eyebrow>Running</Eyebrow><span class="shortlog-count">{running.length}</span></div>
      {!running.length && <div class="shortlog-idle">No running experiments</div>}
      {running.map(e => {
        const label = String(e.label || e.id);
        const pct = progress[label];
        const record = experiments.find(x => String(x.label || x.id) === label);
        const score = record?.metrics?.[selectedMetric.value] ?? e.metrics?.[selectedMetric.value];
        return <div class="shortlog-run" key={label}>
          <button class="shortlog-row" data-experiment-label={label} data-idea-id={e.idea_id} onClick={() => navigateToIdea(e.idea_id, label)} title={e.description}>
            <span class="shortlog-dot is-running" /><span class="shortlog-row-main"><strong class="shortlog-run-exp">exp/{label}</strong><span class="monitor-description">{e.description || ideas[e.idea_id]?.description}</span></span>
            <span class="monitor-run-values">{typeof score === "number" && <span class="shortlog-score">{fmtScoreShort(score)}</span>}{typeof pct === "number" && <span class="shortlog-pct">{Math.round(pct)}%</span>}</span>
          </button>
          <span class="shortlog-progress"><span class={`shortlog-progress-fill${typeof pct === "number" ? "" : " is-indeterminate"}`} style={typeof pct === "number" ? { width: `${Math.min(100, Math.max(0, pct))}%` } : undefined} /></span>
        </div>;
      })}
    </div>
    <div class="shortlog-block">
      <button class="shortlog-sub monitor-queued-toggle" aria-expanded={queuedOpen.value} onClick={() => { queuedOpen.value = !queuedOpen.value; }}><Eyebrow>Queued</Eyebrow><span class="shortlog-count">{snapshot ? queued.length : "—"}</span><span class="monitor-queue-chevron" aria-hidden="true">{queuedOpen.value ? "▾" : "▸"}</span></button>
      {queuedOpen.value && <>{!snapshot && !error && <div class="shortlog-idle">Loading queue…</div>}{snapshot && !queued.length && <div class="shortlog-idle">Queue empty</div>}{queued.map((e, index) => <button class="shortlog-row" data-experiment-label={e.label} data-idea-id={e.idea_id} key={e.label} onClick={() => navigateToIdea(e.idea_id, e.label)}><span class="monitor-queue-position">{index + 1}</span><span class="shortlog-row-main"><strong class="shortlog-run-exp">exp/{e.label}</strong><span class="monitor-description">{e.description || ideas[e.idea_id]?.description}</span></span></button>)}</>}
    </div>
    {error && <button class="ui-btn" onClick={load}>Queue unavailable · Retry</button>}
  </div>;
}
