/**
 * Hidden experiments: tagged "hidden" on the server (shared by everyone,
 * reversible). They are left out of the charts, the table and the idea-mean
 * aggregation unless "show hidden" is on.
 */
import { computed } from "@preact/signals";
import type { Experiment } from "../lib/types";
import { allExperiments } from "./signals";
import { showHiddenExperiments } from "./settings";
import { updateExperimentTags } from "./api";

export const HIDDEN_TAG = "hidden";

export function isHiddenExperiment(e: Pick<Experiment, "tags">): boolean {
  return !!e.tags?.includes(HIDDEN_TAG);
}

export const hiddenExperimentCount = computed(
  () => allExperiments.value.filter(isHiddenExperiment).length,
);

/** The experiments charts, table and idea means are computed from. */
export const plotExperiments = computed(() =>
  showHiddenExperiments.value
    ? allExperiments.value
    : allExperiments.value.filter((e) => !isHiddenExperiment(e)),
);

/** Hide/unhide one experiment; updates the local list right away. */
export async function setExperimentHidden(label: string, hidden: boolean): Promise<void> {
  const res = await updateExperimentTags(label, hidden ? [HIDDEN_TAG] : [], hidden ? [] : [HIDDEN_TAG]);
  const apply = (tags: string[] | undefined) => {
    const next = new Set(tags ?? []);
    if (hidden) next.add(HIDDEN_TAG); else next.delete(HIDDEN_TAG);
    return [...next];
  };
  allExperiments.value = allExperiments.value.map((e) =>
    String(e.label ?? e.id) === label ? { ...e, tags: res?.tags ?? apply(e.tags) } : e,
  );
}
