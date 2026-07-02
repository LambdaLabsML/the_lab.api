/**
 * IdeaLink — THE shared way to render an idea reference ("#129"). Muted mono
 * link with the canonical idea hover card (ideaTipContent — status, best
 * score, runs, title) and click-through to the Overview page. Self-hydrates
 * from the id, mirroring ExpLink.
 */
import { Tooltip, ideaTipContent } from "./ui";
import { allExperiments, allIdeas } from "../state/signals";
import { selectedMetric } from "../state/settings";
import { fmtMetricName } from "../lib/format";
import { isLowerBetter } from "../lib/colors";
import { navigateToIdea } from "../lib/navigate";

export function IdeaLink({ id, class: cls }: { id: number; class?: string }) {
  const idea = allIdeas.value[id];
  const metric = selectedMetric.value;

  let best: number | null = null;
  let runs = 0;
  if (metric) {
    const lower = isLowerBetter(metric);
    for (const e of allExperiments.value) {
      if (e.idea_id !== id) continue;
      runs++;
      const v = e.metrics?.[metric];
      if (typeof v === "number" && isFinite(v)) {
        best = best == null ? v : lower ? Math.min(best, v) : Math.max(best, v);
      }
    }
  }

  const tip = ideaTipContent({
    id,
    status: idea?.status,
    title: idea?.description?.split("\n")[0],
    best: best != null ? { metricName: fmtMetricName(metric), value: best } : null,
    runs: runs || undefined,
  });

  return (
    <Tooltip content={tip}>
      <button
        type="button"
        class={`idea-link${cls ? ` ${cls}` : ""}`}
        onClick={(e) => {
          e.stopPropagation();
          window.location.hash = "#review";
          navigateToIdea(id);
        }}
      >
        #{id}
      </button>
    </Tooltip>
  );
}
