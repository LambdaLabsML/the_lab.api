// ------------------------------------------------------------
// Shared application-state signals.
//
// These hold the "live" data fetched from the API as well as
// ephemeral UI state that is NOT persisted to localStorage.
// For user preferences see ./settings.ts.
// ------------------------------------------------------------

import { signal, computed } from "@preact/signals";
import type {
  BacklogResponse,
  GraphResponse,
  Experiment,
  IdeaNode,
  SubwayLayout,
  OpenAPISpec,
  LogEntry,
} from "../lib/types";
import { buildSubwayLayout } from "../lib/subway-layout";

// ---------------------------------------------------------------------------
// Core data populated by polling (see ./polling.ts)
// ---------------------------------------------------------------------------

/** Latest response from GET /api/v1/backlog. */
export const backlogData = signal<BacklogResponse | null>(null);

/** Latest response from GET /api/v1/graph. */
export const graphData = signal<GraphResponse | null>(null);

/** Merged list of completed + running experiments from chart-data. */
export const allExperiments = signal<Experiment[]>([]);

/**
 * Map of idea id -> IdeaNode.
 * Populated from graph data and updated incrementally as nodes arrive.
 */
export const allIdeas = signal<Record<number, IdeaNode>>({});

// ---------------------------------------------------------------------------
// Derived / computed state
// ---------------------------------------------------------------------------

/**
 * Subway layout computed from the current graph data.
 * Automatically recomputes whenever `graphData` changes.
 */
export const currentLayout = computed<SubwayLayout | null>(() => {
  const g = graphData.value;
  if (!g || !g.nodes.length) return null;
  return buildSubwayLayout(g);
});

// ---------------------------------------------------------------------------
// UI state (not persisted)
// ---------------------------------------------------------------------------

/** Idea currently hovered / highlighted in the graph or chart. */
export const highlightedIdea = signal<number | null>(null);

/** Experiment label to scroll to in the detail panel (e.g. "5.2"). */
export const scrollToExperiment = signal<string | null>(null);

/** Global ref to activate dockview panels from any component.
 *  Skips activation when a group is maximized (to avoid exiting fullscreen). */
export let activatePanel: ((panelId: string) => void) | null = null;
export function setActivatePanel(fn: (panelId: string) => void) { activatePanel = fn; }

/** Global ref to clone a chart panel with given settings. */
export let cloneChartPanel: ((type: "metrics" | "scatter", metric?: string, xMetric?: string, yMetric?: string) => void) | null = null;
export function setCloneChartPanel(fn: typeof cloneChartPanel) { cloneChartPanel = fn; }

/** Global ref to update a dockview panel's tab title. */
export let updatePanelTitle: ((panelId: string, title: string) => void) | null = null;
export function setUpdatePanelTitle(fn: typeof updatePanelTitle) { updatePanelTitle = fn; }

/** Parsed OpenAPI spec (loaded lazily when the API view opens). */
export const apiSpec = signal<OpenAPISpec | null>(null);

/** Log entries built from all ideas + experiments + notes. */
export const logEntries = signal<LogEntry[]>([]);
