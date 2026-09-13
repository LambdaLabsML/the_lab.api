/**
 * ExpLink — THE shared way to render an experiment reference ("exp/129.6").
 * Accent-colored mono link with the canonical experiment hover card
 * (experimentTipContent) and click-through to the Overview page (scroll+flash
 * via navigateToIdea). Self-hydrates from the label, so call sites need no
 * data plumbing — dropping this into a view adds the tooltip for free.
 */
import { Tooltip, experimentTipContent } from "./ui";
import { allExperiments, allIdeas } from "../state/signals";
import { selectedMetric } from "../state/settings";
import { fmtMetricName } from "../lib/format";
import { navigateToIdea } from "../lib/navigate";

/** "129.6" → 129 (idea id prefix of an experiment label). */
function ideaFromLabel(label: string): number | null {
  const m = label.match(/^(\d+)\./);
  return m ? Number(m[1]) : null;
}

export function ExpLink({ label, ideaId, class: cls }: {
  label: string;
  ideaId?: number | null;
  class?: string;
}) {
  const exp = allExperiments.value.find((e) => String(e.label ?? e.id) === label);
  const idea = ideaId ?? exp?.idea_id ?? ideaFromLabel(label);
  const metric = selectedMetric.value;
  const score = metric ? exp?.metrics?.[metric] : undefined;

  const tip = experimentTipContent({
    label,
    ideaId: idea ?? 0,
    ideaTitle: idea != null ? allIdeas.value[idea]?.description?.split("\n")[0] : undefined,
    status: exp ? (exp._running ? "running" : exp.status) : undefined,
    statusMsg: exp?.status_msg,
    metricName: metric ? fmtMetricName(metric) : undefined,
    value: typeof score === "number" && isFinite(score) ? score : null,
    running: !!exp?._running,
  });

  return (
    <Tooltip content={tip}>
      <button
        type="button"
        class={`exp-link${cls ? ` ${cls}` : ""}`}
        onClick={(e) => {
          e.stopPropagation();
          if (idea != null) {
            window.location.hash = "#review";   // experiments live on Overview
            navigateToIdea(idea, label);
          }
        }}
      >
        exp/{label}
      </button>
    </Tooltip>
  );
}
