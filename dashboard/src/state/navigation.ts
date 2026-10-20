/** One owner for path, query, history, and reloadable dashboard state. */
import { batch, effect, signal } from "@preact/signals";
import * as settings from "./settings";
import { selectedExperiment } from "./signals";
import { activeSelection, activeFilter } from "./filters";
import { reviewSearchPills } from "./review-filter";
import { overviewPlots } from "./plots";
import { artifactPath, editingPath } from "./artifacts";
import { databaseScope, databaseTab } from "./database";
import { inboxView, conversationAudience, inboxQuery, inboxStatus, inboxSender, type InboxView } from "./inbox";
import { controlsRevision, readPlotControls, restorePlotControls } from "../lib/hooks/use-plot-controls";
import { parseDashboardUrl, formatDashboardUrl, routePath, MAX_INLINE_URL_LENGTH, type DashboardLocation, type DashboardRoute, type DetailView } from "../lib/dashboard-url";

const initial = parseDashboardUrl(location.href);
export const navigation = signal<DashboardRoute>(initial.route);
export const inspectorOpen = signal(!!initial.route.idea);
export const messagesDocked = signal(!!initial.route.dock);
export const threadLocation = signal<{ thread: number | null; message: number | null }>({ thread: initial.route.thread ?? null, message: initial.route.message ?? null });
export const detailLocation = signal<{ idea: number; experiment?: string; view: DetailView } | null>(initial.route.idea && initial.route.view ? { idea: initial.route.idea, experiment: initial.route.experiment, view: initial.route.view } : null);
export const navigationReady = signal(false);
export const navigationError = signal<string | null>(null);
settings.selectedIdea.value = initial.route.idea ?? null;
selectedExperiment.value = initial.route.experiment ?? null;
let generation = 0;
let writeTimer: ReturnType<typeof setTimeout> | undefined;
let lastSemantic = "";
const snapshots = new Map<string, string>();
const headers = { "Content-Type": "application/json", "X-The-Lab-Source": "dashboard" };

export function navigateSection(section: DashboardRoute["section"]) {
  generation++;
  navigationReady.value = true;
  navigationError.value = null;
  navigation.value = { ...navigation.value, section, settings: false, tool: undefined };
}
export function setDetailLocation(params: { idea?: string | number | null; exp?: string | number | null; view?: string | number | null }) {
  if (params.view == null) { detailLocation.value = null; return; }
  const idea = Number(params.idea ?? settings.selectedIdea.value);
  if (idea > 0 && ["log", "output", "script", "diff"].includes(String(params.view))) {
    const next = { idea, experiment: params.exp == null ? undefined : String(params.exp), view: params.view as DetailView };
    if (JSON.stringify(detailLocation.value) !== JSON.stringify(next)) detailLocation.value = next;
  }
}
function capture(): DashboardLocation {
  const current = navigation.value;
  const route: DashboardRoute = { section: current.section, settings: current.settings, tool: current.tool };
  if (route.section === "artifacts") { route.artifact = artifactPath.value; route.edit = !!editingPath.value; }
  if (route.section === "database") { route.database = databaseTab.value; route.scope = databaseScope.value ?? undefined; }
  if (route.section === "messages" || (route.section === "review" && messagesDocked.value)) {
    route.thread = threadLocation.value.thread ?? undefined; route.message = threadLocation.value.message ?? undefined;
    route.dock = route.section === "review" || undefined;
  }
  if (inspectorOpen.value && settings.selectedIdea.value != null && route.section !== "artifacts" && !route.settings) {
    route.idea = settings.selectedIdea.value; route.experiment = selectedExperiment.value ?? undefined;
    const detail = detailLocation.value;
    if (detail?.idea === route.idea && (!detail.experiment || detail.experiment === route.experiment)) route.view = detail.view;
  }
  const state: DashboardLocation = { route };
  if (route.section === "review" && !route.settings) {
    void controlsRevision.value;
    state.overview = {
      metric: settings.selectedMetric.value, main: readPlotControls(), height: settings.reviewChartHeight.value, labels: settings.chartPointLabels.value,
      plots: overviewPlots.value.map((plot, index) => ({ id: `url-${index}`, type: plot.type, metric: plot.metric || "", x: plot.x || "", y: plot.y || "", width: plot.width ?? "full", height: plot.height ?? 360, controls: readPlotControls(plot.id) })),
      filter: activeFilter.value?.name || activeSelection.value.filter, where: activeSelection.value.where,
      tags: settings.activeTagFilters.value, tagMode: settings.tagFilterMode.value as "and" | "or", query: settings.filterText.value,
      hidden: [!settings.showAbandoned.value && "abandoned", !settings.showConcluded.value && "concluded", !settings.showRunning.value && "running"].filter(Boolean) as string[],
      highlights: reviewSearchPills.value.filter(item => item.active !== false && ["idea", "experiment"].includes(item.category)).map(item => ({ category: item.category as "idea" | "experiment", value: item.value, label: item.category === "idea" ? `#${item.value}` : item.label })),
    };
  }
  if (route.section === "messages" || route.dock) state.inbox = { view: inboxView.value, audience: conversationAudience.value, query: inboxQuery.value, status: inboxStatus.value, sender: inboxSender.value };
  return state;
}
function apply(state: DashboardLocation, keepOverview = false) {
  const route = state.route;
  batch(() => {
    navigation.value = route;
    settings.selectedIdea.value = route.idea ?? null;
    selectedExperiment.value = route.experiment ?? null;
    inspectorOpen.value = !!route.idea;
    detailLocation.value = route.idea && route.view ? { idea: route.idea, experiment: route.experiment, view: route.view } : null;
    messagesDocked.value = !!route.dock;
    threadLocation.value = { thread: route.thread ?? null, message: route.message ?? null };
    if (route.section === "artifacts") { artifactPath.value = route.artifact || ""; editingPath.value = route.edit ? route.artifact || null : null; }
    if (route.section === "database") { databaseTab.value = route.database ?? "experiments"; databaseScope.value = route.scope ?? null; }
    if (state.overview && !keepOverview) {
      const view = state.overview;
      restorePlotControls(view.main);
      settings.selectedMetric.value = view.metric;
      settings.reviewChartHeight.value = view.height;
      settings.chartPointLabels.value = view.labels;
      settings.activeTagFilters.value = view.tags;
      settings.tagFilterMode.value = view.tagMode;
      settings.filterText.value = view.query;
      settings.showAbandoned.value = !view.hidden.includes("abandoned");
      settings.showConcluded.value = !view.hidden.includes("concluded");
      settings.showRunning.value = !view.hidden.includes("running");
      activeSelection.value = { filter: view.filter, where: view.where };
      reviewSearchPills.value = view.highlights.map(item => ({ ...item, id: `${item.category}:${item.category === "idea" ? item.value : item.label}`, active: true }));
      // Stable ids let Back/Forward update already-mounted plot controls too.
      for (const [index, plot] of view.plots.entries()) restorePlotControls(plot.controls, `url-${index}`);
      overviewPlots.value = view.plots.map((plot, index) => { const { controls, ...metadata } = plot; return { ...metadata, id: `url-${index}` }; });
    }
    if (state.inbox) {
      inboxView.value = ["for_you", "mine", "all", "saved", "replies"].includes(state.inbox.view) ? state.inbox.view as InboxView : "for_you";
      conversationAudience.value = state.inbox.audience;
      inboxQuery.value = state.inbox.query;
      inboxStatus.value = ["all", "open", "closed"].includes(state.inbox.status) ? state.inbox.status as "all" | "open" | "closed" : "all";
      inboxSender.value = state.inbox.sender;
    }
  });
}
async function api(path: string, body?: unknown) {
  const response = await fetch(`/api/v1/dashboard/views${path}`, { headers, ...(body ? { method: "POST", body: JSON.stringify(body) } : {}) });
  if (!response.ok) throw new Error(`Shared view ${response.status === 404 ? "not found" : "could not be loaded or saved"}.`);
  return response.json();
}
export async function restoreLocation() {
  const mine = ++generation;
  clearTimeout(writeTimer); navigationReady.value = false; navigationError.value = null;
  try {
    const parsed = parseDashboardUrl(location.href);
    let state: DashboardLocation = parsed;
    if (parsed.viewRef) {
      const result = await api("/" + encodeURIComponent(parsed.viewRef));
      if (!result.state?.route || !["review", "messages", "queue", "database", "artifacts"].includes(result.state.route.section)) throw new Error("Shared view is invalid.");
      state = result.state;
    }
    if (mine !== generation) return;
    apply(state, parsed.home);
    lastSemantic = semantic(capture());
  } catch (error) {
    if (mine === generation) navigationError.value = error instanceof Error ? error.message : String(error);
  } finally {
    if (mine === generation) navigationReady.value = true;
  }
}
function semantic(state: DashboardLocation) {
  return routePath(state.route) + (state.route.dock ? `|${state.route.thread ?? ""}/${state.route.message ?? ""}` : "");
}
async function write(state: DashboardLocation, mine: number) {
  try {
    const inline = formatDashboardUrl(state);
    let url = inline;
    if (inline.length > MAX_INLINE_URL_LENGTH) {
      // Snapshot ids and highlight labels are normalized in capture().
      const snapshot = JSON.stringify(state);
      let ident = snapshots.get(snapshot);
      if (!ident) {
        const name = state.overview ? `${state.overview.metric || "overview"}-${state.overview.plots.length + 1}-graphs` : state.route.thread ? `thread-${state.route.thread}` : state.route.section;
        ident = (await api("", { name, state })).id;
        snapshots.set(snapshot, ident!);
      }
      url = `/${state.route.section === "review" ? "overview" : state.route.section}/views/${ident}`;
    }
    if (mine !== generation || navigationError.value) return;
    const key = semantic(state), method = key === lastSemantic ? "replaceState" : "pushState";
    if (url !== location.pathname + location.search + location.hash) history[method](null, "", url);
    lastSemantic = key;
  } catch (error) {
    if (mine === generation) navigationError.value = `${error instanceof Error ? error.message : error} The current URL has not been updated.`;
  }
}
export function startUrlSync() {
  const read = () => { void restoreLocation(); };
  window.addEventListener("popstate", read); window.addEventListener("hashchange", read);
  const dispose = effect(() => {
    if (!navigationReady.value || navigationError.value) return;
    const state = capture();
    const mine = ++generation;
    clearTimeout(writeTimer);
    writeTimer = setTimeout(() => { void write(state, mine); }, 180);
  });
  void restoreLocation();
  return () => { generation++; clearTimeout(writeTimer); dispose(); window.removeEventListener("popstate", read); window.removeEventListener("hashchange", read); };
}
