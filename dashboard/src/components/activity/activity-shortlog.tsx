/**
 * ActivityShortlog — a condensed "what's happening right now" feed for the left
 * secondary panel (shown alongside Review and Queue), so agent activity is
 * visible at all times. A lean cousin of ActivityPane: driven purely by the
 * global signals (no extra polling), clicking a row jumps to that idea.
 */
import { allExperiments, allIdeas, runningProgress } from "../../state/signals";
import { navigateToIdea } from "../../lib/navigate";
import { ideaTitle } from "../../lib/format";
import { Eyebrow } from "../ui";

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

export function ActivityShortlog() {
  const experiments = allExperiments.value;
  const ideas = allIdeas.value;
  const progress = runningProgress.value;

  const running = experiments.filter((e) => e._running || e.status === "running");
  const recent = experiments
    .filter((e) => !e._running && e.finished_at &&
      (e.status === "completed" || e.status === "failed" || e.status === "cancelled"))
    .sort((a, b) => Date.parse(b.finished_at || "") - Date.parse(a.finished_at || ""))
    .slice(0, 6);

  return (
    <div class="activity-shortlog" aria-label="Live activity">
      <div class="nav-secondary-head"><Eyebrow>Live activity</Eyebrow></div>

      <div class="shortlog-block">
        <div class="shortlog-sub">
          <Eyebrow>Running</Eyebrow>
          <span class="shortlog-count">{running.length}</span>
        </div>
        {running.length === 0 ? (
          <div class="shortlog-idle">— idle —</div>
        ) : (
          running.map((e) => {
            const label = e.label || String(e.id);
            const pct = progress[label];
            return (
              <div class="shortlog-run" key={`r-${e.id}`}>
                <button
                  class="shortlog-row"
                  onClick={() => navigateToIdea(e.idea_id, label)}
                  title={ideas[e.idea_id]?.description}
                >
                  <span class="shortlog-dot is-running" />
                  <span class="shortlog-row-main">#{e.idea_id} {ideaTitle(ideas[e.idea_id]?.description ?? "")}</span>
                  {typeof pct === "number" && <span class="shortlog-pct">{Math.round(pct)}%</span>}
                </button>
                <span class="shortlog-progress">
                  <span
                    class={`shortlog-progress-fill${typeof pct === "number" ? "" : " is-indeterminate"}`}
                    style={typeof pct === "number" ? { width: `${Math.min(100, Math.max(0, pct))}%` } : undefined}
                  />
                </span>
              </div>
            );
          })
        )}
      </div>

      {recent.length > 0 && (
        <div class="shortlog-block">
          <div class="shortlog-sub"><Eyebrow>Recent</Eyebrow></div>
          {recent.map((e) => {
            const ok = e.status === "completed";
            return (
              <button
                key={`f-${e.id}`}
                class="shortlog-row"
                onClick={() => navigateToIdea(e.idea_id, e.label || String(e.id))}
                title={ideas[e.idea_id]?.description}
              >
                <span class={`shortlog-dot ${ok ? "is-done" : "is-bad"}`} />
                <span class="shortlog-row-main">exp {e.label ?? e.id} · #{e.idea_id}</span>
                <span class="shortlog-time">{relTime(e.finished_at)}</span>
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
