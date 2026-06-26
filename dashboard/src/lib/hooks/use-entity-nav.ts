/**
 * useEntityNav — the one shared "clickable idea" interaction.
 *
 * Every node, row, dot, or pill that represents an idea/experiment uses this so
 * click-to-navigate + hover-to-highlight behave identically everywhere. Spread
 * the returned `bind` onto the element; read `highlighted` for reactive styling.
 *
 * Replaces the duplicated onClick/onMouseEnter/onMouseLeave triplets that were
 * copy-pasted across the graph, table, charts, detail panel, and review lists.
 */
import { navigateToIdea } from "../navigate";
import { highlightedIdea } from "../../state/signals";

export interface EntityNav {
  /** True when this idea is the currently highlighted one (reactive). */
  highlighted: boolean;
  /** Spread onto the clickable element. */
  bind: {
    onClick: (e: MouseEvent) => void;
    onMouseEnter: () => void;
    onMouseLeave: () => void;
  };
}

/**
 * @param ideaId  the idea this element points at
 * @param label   optional experiment label, scrolled-to in the detail panel
 * @param opts.preventDefault  call e.preventDefault() before navigating (links)
 */
export function useEntityNav(
  ideaId: number,
  label?: string,
  opts?: { preventDefault?: boolean },
): EntityNav {
  // Reading .value here subscribes the calling component to highlight changes.
  const highlighted = highlightedIdea.value === ideaId;
  return {
    highlighted,
    bind: {
      onClick: (e: MouseEvent) => {
        if (opts?.preventDefault) e.preventDefault();
        navigateToIdea(ideaId, label);
      },
      onMouseEnter: () => {
        highlightedIdea.value = ideaId;
      },
      onMouseLeave: () => {
        // Guard: only clear if *we* are still the highlighted one. Prevents a
        // leave event from wiping a highlight another element just set.
        if (highlightedIdea.value === ideaId) highlightedIdea.value = null;
      },
    },
  };
}

/**
 * Hover-only highlight (no navigation) — for read-only markers like chart
 * milestone dots that should glow on hover but not jump anywhere.
 */
export function useHoverHighlight(ideaId: number) {
  return {
    highlighted: highlightedIdea.value === ideaId,
    onMouseEnter: () => {
      highlightedIdea.value = ideaId;
    },
    onMouseLeave: () => {
      if (highlightedIdea.value === ideaId) highlightedIdea.value = null;
    },
  };
}

/**
 * Drop-in body for a Chart.js dataset `onClick(evt, elements)` handler.
 * Reads `idea_id` + `label`/`id` off the clicked raw datum and navigates.
 */
export function chartNavClick(elements: Array<{ element?: any; raw?: any; index?: number }>): void {
  if (!elements || elements.length === 0) return;
  const raw = (elements[0] as any).raw ?? (elements[0] as any).element?.$context?.raw;
  if (!raw || raw.idea_id == null) return;
  navigateToIdea(raw.idea_id, raw.label ?? (raw.id != null ? String(raw.id) : undefined));
}
