import { useEffect } from "preact/hooks";
import { Topbar } from "./components/topbar";
import { ChartPanel } from "./components/chart-panel/chart-panel";
import { SuggestPanel } from "./components/suggest-panel";
import { ViewTabs } from "./components/view-tabs";
import { DagView } from "./views/dag-view";
import { TimelineView } from "./views/timeline-view";
import { LogView } from "./views/log-view";
import { ApiView } from "./views/api-view";
import { DetailPanel } from "./components/detail-panel";
import { currentView, selectedIdea } from "./state/settings";
import { startPolling, stopPolling } from "./state/polling";

function syncViewFromUrl() {
  const path = window.location.pathname;
  const m = path.match(/^\/ideas\/(\d+)/);
  if (m) {
    currentView.value = "dag";
    selectedIdea.value = parseInt(m[1]);
    return;
  }
  const em = path.match(/^\/experiments\/(\d+)/);
  if (em) {
    // For experiments, we open the DAG view — the detail panel will resolve the idea
    currentView.value = "dag";
    // Store experiment ID temporarily; detail panel will handle lookup
    (window as any).__pendingExpId = parseInt(em[1]);
    return;
  }
  const viewMap: Record<string, string> = {
    "/": "dag",
    "/dag": "dag",
    "/graph": "dag",
    "/timeline": "timeline",
    "/log": "log",
    "/api": "api",
  };
  if (viewMap[path]) {
    currentView.value = viewMap[path];
  }
}

export function App() {
  useEffect(() => {
    syncViewFromUrl();
    window.addEventListener("popstate", syncViewFromUrl);
    startPolling();
    return () => {
      window.removeEventListener("popstate", syncViewFromUrl);
      stopPolling();
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
        {view !== "api" && <DetailPanel />}
      </div>
    </>
  );
}
