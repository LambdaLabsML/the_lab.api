import { computed } from "@preact/signals";
import type { FilterItem } from "../components/search-filter/search-filter";
import { activeTagFilters, filterText, useSetting } from "./settings";
import { highlightedIdea } from "./signals";

export type ReviewPill = FilterItem & { active?: boolean };
/** Shared with the inspector so highlights appear in the normal search controls. */
export const reviewSearchPills = useSetting<ReviewPill[]>("reviewSearchPills", [
  ...activeTagFilters.value.map(tag => ({ id: `tag:${tag}`, category: "tag", label: tag, value: tag, active: true })),
  ...(filterText.value.trim() ? [{ id: `text:${filterText.value}`, category: "text", label: filterText.value, value: filterText.value, active: true }] : []),
]);
export const activeHighlights = computed(() => reviewSearchPills.value.filter(pill =>
  pill.active !== false && ["idea", "experiment"].includes(pill.category)));

export function isIdeaHighlighted(id: number): boolean {
  return activeHighlights.value.some(pill => pill.category === "idea" && Number(pill.value) === id);
}

export function toggleIdeaHighlight(id: number, description = "") {
  const key = `idea:${id}`;
  const existing = reviewSearchPills.value.find(pill => pill.id === key);
  if (existing?.active !== false && existing) {
    reviewSearchPills.value = reviewSearchPills.value.filter(pill => pill.id !== key);
  } else if (existing) {
    reviewSearchPills.value = reviewSearchPills.value.map(pill => pill.id === key ? { ...pill, active: true } : pill);
  } else {
    reviewSearchPills.value = [...reviewSearchPills.value, {
      id: key, category: "idea", label: `#${id}${description ? ` ${description.split("\n")[0].slice(0, 40)}` : ""}`,
      value: String(id), active: true,
    }];
  }
}

export function plotHighlightState(ideaId: number, label?: string) {
  const selected = activeHighlights.value;
  const hovered = highlightedIdea.value;
  const highlighted = hovered === ideaId || selected.some(pill => pill.category === "idea"
    ? Number(pill.value) === ideaId : pill.label.replace(/^exp\//, "") === label);
  return { highlighted, faded: (hovered !== null || selected.length > 0) && !highlighted };
}
