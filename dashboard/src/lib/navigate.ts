/**
 * Shared navigation helpers for bidirectional selection between
 * graph, charts, and detail panel.
 */
import { dismissHoverPreviews } from "./hooks/use-hover-preview";
import { detailLocation } from "../state/navigation";
import { selectedIdea } from "../state/settings";
import { highlightedIdea, scrollToExperiment, selectedExperiment, activatePanel } from "../state/signals";

/**
 * Navigate to an idea from a chart click or graph click.
 * - Sets selectedIdea (detail panel loads it)
 * - Activates the detail panel tab
 * - Scrolls to the graph node
 * - Highlights briefly
 */
export function navigateToIdea(ideaId: number, expLabel?: string, options: { scrollGraph?: boolean } = {}) {
  dismissHoverPreviews();
  if (selectedIdea.value !== ideaId || selectedExperiment.value !== (expLabel ?? null)) detailLocation.value = null;
  selectedExperiment.value = expLabel ?? null;
  selectedIdea.value = ideaId;
  highlightedIdea.value = ideaId;

  // Reveal the shared inspector.
  if (activatePanel) activatePanel("detail");

  // If a specific experiment was clicked, scroll to it in detail
  if (expLabel) {
    scrollToExperiment.value = expLabel;
  }

  // Scroll to graph node
  const station = document.querySelector(
    `.subway-station[data-id="${ideaId}"], .subway-dot[data-id="${ideaId}"]`
  );
  if (station && options.scrollGraph !== false) {
    station.scrollIntoView({ behavior: "smooth", block: "center", inline: "center" });
  }

  // Clear highlight after 3s
  setTimeout(() => {
    if (highlightedIdea.value === ideaId) highlightedIdea.value = null;
  }, 3000);
}

/**
 * Navigate from detail panel experiment header to graph + charts.
 * - Scrolls to graph node
 * - Highlights the idea in charts
 */
export function navigateFromExperiment(ideaId: number) {
  highlightedIdea.value = ideaId;

  // Scroll to graph node
  const station = document.querySelector(
    `.subway-station[data-id="${ideaId}"], .subway-dot[data-id="${ideaId}"]`
  );
  if (station) {
    station.scrollIntoView({ behavior: "smooth", block: "center", inline: "center" });
  }

  // Clear after 3s
  setTimeout(() => {
    if (highlightedIdea.value === ideaId) highlightedIdea.value = null;
  }, 3000);
}
