import { useCallback, useEffect, useRef, useState } from "preact/hooks";
import { allExperiments, allIdeas, runningProgress } from "../../state/signals";
import { selectedMetric, useSetting } from "../../state/settings";
import { getQueue } from "../../state/api";
import { useLiveRefresh } from "../../lib/hooks";
import { navigateToIdea } from "../../lib/navigate";
import { fmtScoreShort, ideaTitle } from "../../lib/format";
import { showRunPreview, hideRunPreview } from "../../state/agent-activity";
import { ExpLink } from "../exp-link";
import { IdeaLink } from "../idea-link";
import type { QueueSnapshot } from "../../lib/types";
import { Eyebrow } from "../ui";

const queuedOpen = useSetting("monitorQueuedOpen", false);
/** Show runs that are started but still waiting for a Slurm allocation. */
const waitingOpen = useSetting("monitorSlurmWaitingOpen", false);

function relTime(iso?: string | null): string {
  if (!iso) return "";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "";
  const sec = Math.max(0, Math.floor((Date.now() - t) / 1000));
  if (sec < 60) return `${sec}s`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}m`;
  const hr = Math.floor(min / 60);
  if (hr < 48) return `${hr}h`;
  return `${Math.floor(hr / 24)}d`;
}

/** Running and queued experiments share the queue's authoritative snapshot.
 *  Running rows are one line each (number, idea title, live score · %) over
 *  a thin progress bar; Recent lists the last finished runs. */
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
  // Started by the lab but still waiting for a Slurm allocation: listed after
  // the runs that are actually working, collapsed by default.
  // (The queue snapshot carries slurm_state; until it loads, nothing counts as waiting.)
  const isWaiting = (e: object) => (e as { slurm_state?: string | null }).slurm_state === "PENDING";
  const working = running.filter(e => !isWaiting(e));
  const waiting = running.filter(e => isWaiting(e));
  const waitingSeparator = <button class="shortlog-separator" aria-expanded={waitingOpen.value}
    onClick={() => { waitingOpen.value = !waitingOpen.value; }}
    title={waitingOpen.value ? "Hide the runs still waiting for a Slurm allocation" : "Show the runs still waiting for a Slurm allocation"}>
    <span class="shortlog-separator-rule" />
    <span class="shortlog-separator-text">{waitingOpen.value ? "▾" : "▸"} {waiting.length} waiting in Slurm{waitingOpen.value ? "" : " — show"}</span>
    <span class="shortlog-separator-rule" />
  </button>;
  const recent = experiments
    .filter(e => !e._running && e.finished_at && ["completed", "failed", "cancelled"].includes(e.status))
    .sort((a, b) => Date.parse(b.finished_at || "") - Date.parse(a.finished_at || ""))
    .slice(0, 6);
  return <div class="activity-shortlog" aria-label="Experiment monitor">
    <div class="shortlog-block">
      <div class="shortlog-sub"><Eyebrow>Running</Eyebrow><span class="shortlog-count" title={waiting.length ? `${working.length} working, ${waiting.length} waiting in Slurm` : undefined}>{waiting.length ? `${working.length} + ${waiting.length}` : running.length}</span></div>
      {!running.length && <div class="shortlog-idle">No running experiments</div>}
      {(waitingOpen.value ? [...working, ...waiting] : working).map((e, i) => {
        const separator = i === working.length && waiting.length > 0;
        const label = String(e.label || e.id);
        const pct = progress[label];
        const record = experiments.find(x => String(x.label || x.id) === label);
        const score = record?.metrics?.[selectedMetric.value] ?? e.metrics?.[selectedMetric.value];
        const hasScore = typeof score === "number" && Number.isFinite(score);
        return <>{separator && waitingSeparator}<div class="shortlog-run" key={label}>
          <button class="shortlog-row" data-experiment-label={label} data-idea-id={e.idea_id}
            onClick={() => navigateToIdea(e.idea_id, label)}
            onMouseEnter={(ev) => showRunPreview({ label, ideaId: e.idea_id, pct: typeof pct === "number" ? pct : undefined }, ev.currentTarget as Element)}
            onMouseLeave={hideRunPreview}
            title={e.description || undefined}>
            <span class="shortlog-dot is-running" />
            <span class="shortlog-row-main">
              <span class="shortlog-run-exp">{label}</span>{" "}
              {ideaTitle(ideas[e.idea_id]?.description ?? "") || e.description}
            </span>
            {(hasScore || typeof pct === "number") && <span class="shortlog-vals">
              {hasScore && <span class="shortlog-score" title={`live ${selectedMetric.value}`}>{fmtScoreShort(score as number)}</span>}
              {typeof pct === "number" && <span class="shortlog-pct">{Math.round(pct)}%</span>}
            </span>}
          </button>
          <span class="shortlog-progress"><span class={`shortlog-progress-fill${typeof pct === "number" ? "" : " is-indeterminate"}`} style={typeof pct === "number" ? { width: `${Math.min(100, Math.max(0, pct))}%` } : undefined} /></span>
        </div></>;
      })}
      {!waitingOpen.value && waiting.length > 0 && waitingSeparator}
    </div>
    <div class="shortlog-block">
      <button class="shortlog-sub monitor-queued-toggle" aria-expanded={queuedOpen.value} onClick={() => { queuedOpen.value = !queuedOpen.value; }}><Eyebrow>Queued</Eyebrow><span class="shortlog-count">{snapshot ? queued.length : "—"}</span><span class="monitor-queue-chevron" aria-hidden="true">{queuedOpen.value ? "▾" : "▸"}</span></button>
      {queuedOpen.value && <>{!snapshot && !error && <div class="shortlog-idle">Loading queue…</div>}{snapshot && !queued.length && <div class="shortlog-idle">Queue empty</div>}{queued.map((e, index) => <button class="shortlog-row" data-experiment-label={e.label} data-idea-id={e.idea_id} key={e.label} onClick={() => navigateToIdea(e.idea_id, e.label)}><span class="monitor-queue-position">{index + 1}</span><span class="shortlog-row-main"><strong class="shortlog-run-exp">exp/{e.label}</strong><span class="monitor-description">{e.description || ideas[e.idea_id]?.description}</span></span></button>)}</>}
    </div>
    {recent.length > 0 && <div class="shortlog-block">
      <div class="shortlog-sub"><Eyebrow>Recent</Eyebrow></div>
      {recent.map(e => {
        const ok = e.status === "completed";
        const score = e.metrics?.[selectedMetric.value];
        return <div key={`f-${e.id}`} class="shortlog-row" role="button" data-experiment-label={String(e.label ?? e.id)} data-idea-id={e.idea_id}
          onClick={() => navigateToIdea(e.idea_id, e.label || String(e.id))} title={ideas[e.idea_id]?.description}>
          <span class={`shortlog-dot ${ok ? "is-done" : "is-bad"}`} />
          <ExpLink label={String(e.label ?? e.id)} ideaId={e.idea_id} class="shortlog-exp" />
          <IdeaLink id={e.idea_id} />
          {typeof score === "number" && isFinite(score) && <span class="shortlog-score">{fmtScoreShort(score)}</span>}
          <span class="shortlog-time">{relTime(e.finished_at)}</span>
        </div>;
      })}
    </div>}
    {error && <button class="ui-btn" onClick={load}>Queue unavailable · Retry</button>}
  </div>;
}
