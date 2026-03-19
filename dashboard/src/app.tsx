import { useEffect } from "preact/hooks";
import { effect } from "@preact/signals";
import { Topbar } from "./components/topbar";
import { ChartPanel } from "./components/chart-panel/chart-panel";
import { SuggestPanel } from "./components/suggest-panel";
import { ViewTabs } from "./components/view-tabs";
import { DagView } from "./views/dag-view";
import { TimelineView } from "./views/timeline-view";
import { LogView } from "./views/log-view";
import { ApiView } from "./views/api-view";
import { SandboxView } from "./views/sandbox-view";
import { DetailPanel } from "./components/detail-panel";
import {
  currentView, selectedIdea, selectedMetric, colorMode,
  improvementsOnly, activeTagFilters, tagFilterMode, reverseTime,
  showAbandoned, showConcluded, showRunning,
  applyServerDefaults,
} from "./state/settings";
import { startPolling, stopPolling } from "./state/polling";

// --- URL ↔ filter sync ---

/** Read filter state from URL query params on load. */
function readFiltersFromUrl() {
  const path = window.location.pathname;
  const params = new URLSearchParams(window.location.search);

  // View from path
  const m = path.match(/^\/ideas\/(\d+)/);
  if (m) {
    currentView.value = "dag";
    selectedIdea.value = parseInt(m[1]);
  } else if (path.match(/^\/experiments\/(\d+)/)) {
    currentView.value = "dag";
  } else {
    const viewMap: Record<string, string> = {
      "/": "dag", "/dag": "dag", "/graph": "dag",
      "/timeline": "timeline", "/log": "log", "/api": "api", "/sandbox": "sandbox",
    };
    if (viewMap[path]) currentView.value = viewMap[path];
  }

  // Filters from query params (override localStorage defaults)
  if (params.has("metric")) selectedMetric.value = params.get("metric")!;
  if (params.has("color")) colorMode.value = params.get("color")!;
  if (params.has("improvements")) improvementsOnly.value = params.get("improvements") === "1";
  if (params.has("tags")) activeTagFilters.value = params.get("tags")!.split(",").filter(Boolean);
  if (params.has("tagMode")) tagFilterMode.value = params.get("tagMode") as "or" | "and";
  if (params.has("reverse")) reverseTime.value = params.get("reverse") === "1";
  if (params.has("abandoned")) showAbandoned.value = params.get("abandoned") === "1";
  if (params.has("concluded")) showConcluded.value = params.get("concluded") === "1";
  if (params.has("running")) showRunning.value = params.get("running") === "1";
  if (params.has("idea")) selectedIdea.value = parseInt(params.get("idea")!) || null;
}

/** Build query string from current filter state. Only includes non-default values. */
function buildFilterParams(): string {
  const p = new URLSearchParams();
  if (selectedMetric.value) p.set("metric", selectedMetric.value);
  if (colorMode.value !== "status+improve") p.set("color", colorMode.value);
  if (improvementsOnly.value) p.set("improvements", "1");
  if (activeTagFilters.value.length > 0) p.set("tags", activeTagFilters.value.join(","));
  if (tagFilterMode.value !== "and") p.set("tagMode", tagFilterMode.value);
  if (!reverseTime.value) p.set("reverse", "0");
  if (!showAbandoned.value) p.set("abandoned", "0");
  if (!showConcluded.value) p.set("concluded", "0");
  if (!showRunning.value) p.set("running", "0");
  if (selectedIdea.value !== null) p.set("idea", String(selectedIdea.value));
  const qs = p.toString();
  return qs ? "?" + qs : "";
}

/** Update the URL when any filter changes (without triggering navigation).
 *  Reads all signal values inside the effect so Preact tracks them. */
function syncUrlFromSignals() {
  // Read ALL signals here so the effect re-runs when any changes.
  // (Preact signals only track reads inside the effect callback itself.)
  const _view = currentView.value;
  const _metric = selectedMetric.value;
  const _color = colorMode.value;
  const _imp = improvementsOnly.value;
  const _tags = activeTagFilters.value;
  const _tagMode = tagFilterMode.value;
  const _rev = reverseTime.value;
  const _abn = showAbandoned.value;
  const _con = showConcluded.value;
  const _run = showRunning.value;
  const _idea = selectedIdea.value;

  const viewPaths: Record<string, string> = {
    dag: "/graph", timeline: "/timeline", log: "/log", api: "/api", sandbox: "/sandbox",
  };
  const path = viewPaths[_view] || "/graph";

  const p = new URLSearchParams();
  if (_metric) p.set("metric", _metric);
  if (_color !== "status+improve") p.set("color", _color);
  if (_imp) p.set("improvements", "1");
  if (_tags.length > 0) p.set("tags", _tags.join(","));
  if (_tagMode !== "and") p.set("tagMode", _tagMode);
  if (!_rev) p.set("reverse", "0");
  if (!_abn) p.set("abandoned", "0");
  if (!_con) p.set("concluded", "0");
  if (!_run) p.set("running", "0");
  if (_idea !== null) p.set("idea", String(_idea));
  const qs = p.toString();
  const url = path + (qs ? "?" + qs : "");

  if (url !== window.location.pathname + window.location.search) {
    history.replaceState(null, "", url);
  }
}

export function App() {
  useEffect(() => {
    applyServerDefaults().then(() => readFiltersFromUrl());
    window.addEventListener("popstate", readFiltersFromUrl);
    startPolling();

    // Sync URL whenever any filter signal changes
    const dispose = effect(syncUrlFromSignals);

    return () => {
      window.removeEventListener("popstate", readFiltersFromUrl);
      stopPolling();
      dispose();
    };
  }, []);

  const view = currentView.value;

  return (
    <>
      <Topbar />
      <ChartPanel />
      <SuggestPanel />
      <ViewTabs />
      <div id="main">
        {view === "dag" && <DagView />}
        {view === "timeline" && <TimelineView />}
        {view === "log" && <LogView />}
        {view === "api" && <ApiView />}
        {view === "sandbox" && <SandboxView />}
        {view !== "api" && view !== "sandbox" && <DetailPanel />}
      </div>
    </>
  );
}
