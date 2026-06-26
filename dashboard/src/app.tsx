import { useEffect, useRef, useCallback, useState } from "preact/hooks";
import { effect, signal } from "@preact/signals";
import { render } from "preact";

// Tracks which panels are available to add — closed panels + overlay candidates when maximized
const availablePanels = signal<string[]>([]);
const isMaximized = signal(false);

// Tray: panels that live in a bottom bar and pop up as dismissable floating lightboxes
const DEFAULT_TRAY_IDS = ["api", "stats", "sandbox", "prompts", "agents", "messages", "queue", "task", "suggest"];
// Which panels are currently in the tray (can grow when user sends panels to tray)
const trayPanels = signal<string[]>([...DEFAULT_TRAY_IDS]);
// Set of panel IDs currently shown as transient floats (auto-dismiss on click-outside)
const trayOpen = signal<Set<string>>(new Set());
// Tracks open panel IDs reactively — drives tray re-renders when panels open/close
const openPanelIds = signal<Set<string>>(new Set());
// Remembered float positions per panel ID
const trayPositions: Record<string, { x: number; y: number; width: number; height: number }> = {};
import {
  DockviewComponent,
  themeDark,
} from "dockview-core";
import type {
  IContentRenderer,
  IHeaderActionsRenderer,
  IGroupHeaderProps,
  GroupPanelPartInitParameters,
  SerializedDockview,
  DockviewGroupPanel,
} from "dockview-core";

import { Topbar } from "./components/topbar";
import { TaskBanner } from "./components/task-banner";
import { SuggestPanel } from "./components/suggest-panel";
import { ChatPanel } from "./components/chat-panel";
import { DagView } from "./views/dag-view";
import { TimelineView } from "./views/timeline-view";
import { LogView } from "./views/log-view";
import { ApiView } from "./views/api-view";
import { StatsView } from "./views/stats-view";
import { SandboxView } from "./views/sandbox-view";
import { PromptsView } from "./views/prompts-view";
import { AgentsView } from "./views/agents-view";
import { MessagesView } from "./views/messages-view";
import { QueueView } from "./views/queue-view";
import { TablePanel } from "./components/table-panel";
import { DetailPanel } from "./components/detail-panel";
import { MetricsChart } from "./components/chart-panel/metrics-chart";
import { ScatterChart } from "./components/chart-panel/scatter-chart";
import { FilterBar } from "./components/filter-bar";
import {
  currentView, selectedIdea, selectedMetric, colorMode,
  improvementsOnly, activeTagFilters, tagFilterMode, reverseTime,
  showAbandoned, showConcluded, showRunning,
  clipOutliers, ideaMean,
  scatterXMetric, scatterYMetric,
  applyServerDefaults,
  dashboardLayout,
} from "./state/settings";
import { startPolling, stopPolling } from "./state/polling";
import { startWs, stopWs } from "./state/ws";
import {
  allExperiments,
  allIdeas,
  backlogData,
  currentLayout,
  graphData,
  highlightedIdea,
  logEntries,
  runningProgress,
  setActivatePanel,
  setCloneChartPanel,
  setUpdatePanelTitle,
  setSendToTray,
  totalAgentCost,
} from "./state/signals";
import { initTouchMoveMenu } from "./lib/touch-move-menu";
import { getStatusColor, isLowerBetter } from "./lib/colors";
import { navigateToIdea } from "./lib/navigate";
import { fmtMetricName } from "./lib/format";

// ---------------------------------------------------------------------------
// Panel component map — maps panel ID to Preact component
// ---------------------------------------------------------------------------

const PANEL_NAMES: Record<string, string> = {
  graph: "Graph", timeline: "Timeline", log: "Log",
  api: "API", stats: "Stats", sandbox: "Sandbox",
  prompts: "Prompts", agents: "Agents", messages: "Messages", queue: "Queue",
  metrics: "Metrics", scatter: "Scatter", detail: "Detail",
  filters: "Filters", suggest: "Suggest", task: "Task",
  table: "Table",
};

const ALL_PANEL_IDS = Object.keys(PANEL_NAMES);

const PANEL_MAP: Record<string, (params?: any) => preact.JSX.Element> = {
  graph: () => <DagView />,
  timeline: () => <TimelineView />,
  log: () => <LogView />,
  api: () => <ApiView />,
  stats: () => <StatsView />,
  sandbox: () => <SandboxView />,
  prompts: () => <PromptsView />,
  agents: () => <AgentsView />,
  messages: () => <MessagesView />,
  queue: () => <QueueView />,
  metrics: (p?: any) => <MetricsChart instanceId={p?.instanceId} initialMetric={p?.metric} />,
  scatter: (p?: any) => <ScatterChart instanceId={p?.instanceId} initialXMetric={p?.xMetric} initialYMetric={p?.yMetric} />,
  detail: () => <DetailPanel />,
  filters: () => <FilterBar />,
  suggest: () => <SuggestPanel />,
  task: () => <TaskBanner />,
  table: () => <TablePanel />,
};

// ---------------------------------------------------------------------------
// PreactPanelRenderer — bridges dockview panels to Preact components
// ---------------------------------------------------------------------------

class PreactPanelRenderer implements IContentRenderer {
  element: HTMLElement;
  private _componentId: string;

  constructor(id: string) {
    this._componentId = id;
    this.element = document.createElement("div");
    this.element.className = "dv-panel";
    this.element.style.cssText = "width:100%;height:100%;overflow:auto;";
  }

  init(params: GroupPanelPartInitParameters) {
    // Resolve component type: "metrics-3" → "metrics", "scatter-2" → "scatter"
    const baseType = this._componentId.replace(/-\d+$/, "");
    const factory = PANEL_MAP[baseType] || PANEL_MAP[this._componentId];
    const extraParams = params?.params as Record<string, any> | undefined;
    if (factory) {
      render(factory(extraParams), this.element);
    } else {
      this.element.textContent = `Unknown panel: ${this._componentId}`;
    }
  }

  dispose() {
    render(null, this.element);
  }
}

// ---------------------------------------------------------------------------
// Header action buttons — maximize + float on each pane's top-right
// ---------------------------------------------------------------------------

// Layout snapshot taken before entering maximize — restored on exit
let _preMaximizeLayout: any = null;
// Per-panel fullscreen workspaces: saves the maximize+floating layout
// so re-entering fullscreen on the same panel restores it
const FULLSCREEN_LAYOUTS_KEY = "the-lab:fullscreenLayouts";


class PanelHeaderActions implements IHeaderActionsRenderer {
  element: HTMLElement;
  private _group: DockviewGroupPanel;

  constructor(group: DockviewGroupPanel) {
    this._group = group;
    this.element = document.createElement("div");
    this.element.className = "dv-header-actions";
  }

  init(_params: IGroupHeaderProps) {
    const group = this._group;

    // Float/dock toggle button
    const floatBtn = document.createElement("button");
    floatBtn.className = "dv-header-action-btn";
    floatBtn.title = "Float panel";
    floatBtn.textContent = "⬚";

    const updateFloatBtn = () => {
      const isFloat = group.api.location.type === "floating";
      floatBtn.textContent = isFloat ? "⬓" : "⬚";
      floatBtn.title = isFloat ? "Dock panel" : "Float panel";
      floatBtn.className = `dv-header-action-btn${isFloat ? " active" : ""}`;
    };

    floatBtn.addEventListener("click", () => {
      const dv = (group.api as any).accessor as DockviewComponent;
      if (group.api.location.type === "floating") {
        // Dock back: move ALL panels from floating group to a grid group
        const gridGroup = dv.groups.find(
          (g) => g.api.location.type === "grid" && g.id !== group.id
        );
        if (gridGroup) {
          // Copy panel list — iterating while moving mutates the array
          const panels = [...group.panels];
          for (const panel of panels) {
            dv.moveGroupOrPanel({
              from: { groupId: group.id, panelId: panel.id },
              to: { group: gridGroup, position: "center" },
            });
          }
        }
      } else {
        // Float
        const rect = group.element.getBoundingClientRect();
        dv.addFloatingGroup(group, {
          x: rect.left + 30,
          y: rect.top + 30,
          width: Math.max(rect.width, 300),
          height: Math.max(rect.height, 200),
        });
      }
      setTimeout(updateFloatBtn, 50);
    });
    this.element.appendChild(floatBtn);

    // Listen for location changes
    group.api.onDidLocationChange(() => setTimeout(updateFloatBtn, 50));

    // Maximize button
    const maxBtn = document.createElement("button");
    maxBtn.className = "dv-header-action-btn";
    maxBtn.title = "Focus panel";
    maxBtn.textContent = "⤢";

    const updateMaxBtn = () => {
      const dv = (group.api as any).accessor as DockviewComponent;
      const isMax = dv.isMaximizedGroup(group);
      maxBtn.textContent = isMax ? "⤡" : "⤢";
      maxBtn.title = isMax ? "Restore workspace" : "Focus panel";
      maxBtn.className = `dv-header-action-btn${isMax ? " active" : ""}`;
    };

    maxBtn.addEventListener("click", () => {
      const dv = (group.api as any).accessor as DockviewComponent;
      // If floating, dock all panels first before maximizing
      if (group.api.location.type === "floating") {
        const gridGroup = dv.groups.find(
          (g) => g.api.location.type === "grid" && g.id !== group.id
        );
        if (gridGroup) {
          const panels = [...group.panels];
          for (const panel of panels) {
            dv.moveGroupOrPanel({
              from: { groupId: group.id, panelId: panel.id },
              to: { group: gridGroup, position: "center" },
            });
          }
          try { _preMaximizeLayout = dv.toJSON(); } catch { /* ignore */ }
          setTimeout(() => dv.maximizeGroup(gridGroup), 50);
          setTimeout(updateMaxBtn, 100);
          return;
        }
      }
      if (dv.isMaximizedGroup(group)) {
        // Save the current fullscreen workspace (maximize + floats) for this panel
        const panelId = group.activePanel?.id || group.id;
        try {
          const layouts = JSON.parse(localStorage.getItem(FULLSCREEN_LAYOUTS_KEY) || "{}");
          layouts[panelId] = dv.toJSON();
          localStorage.setItem(FULLSCREEN_LAYOUTS_KEY, JSON.stringify(layouts));
        } catch { /* ignore */ }

        (dv as any)._userExitingMaximize = true;
        dv.exitMaximizedGroup();
        // Restore pre-maximize layout
        if (_preMaximizeLayout) {
          const saved = _preMaximizeLayout;
          _preMaximizeLayout = null;
          requestAnimationFrame(() => {
            try { dv.fromJSON(saved); } catch { /* ignore */ }
          });
        }
      } else {
        // Save current layout before entering maximize
        try { _preMaximizeLayout = dv.toJSON(); } catch { /* ignore */ }

        // Check if we have a saved fullscreen workspace for this panel
        const panelId = group.activePanel?.id || group.id;
        let restored = false;
        try {
          const layouts = JSON.parse(localStorage.getItem(FULLSCREEN_LAYOUTS_KEY) || "{}");
          const savedLayout = layouts[panelId];
          if (savedLayout) {
            dv.fromJSON(savedLayout);
            restored = true;
            // fromJSON might not restore maximize state — find the group
            // containing the panel and re-maximize it
            requestAnimationFrame(() => {
              const restoredPanel = dv.panels.find((p) => p.id === panelId);
              if (restoredPanel && !dv.hasMaximizedGroup()) {
                const g = restoredPanel.group;
                if (g && g.api.location.type === "grid") {
                  dv.maximizeGroup(g);
                }
              }
            });
          }
        } catch { /* ignore */ }

        if (!restored) {
          dv.maximizeGroup(group);
        }
      }
      setTimeout(updateMaxBtn, 50);
    });
    this.element.appendChild(maxBtn);
  }

  dispose() {}
}

// ---------------------------------------------------------------------------
// Default layout builder
// ---------------------------------------------------------------------------

// Default layout:
// Row 1: Filters (full width, compact)
// Row 2: Metrics (75% left) | Scatter (25% right)
// Row 3: Table/Graph/Timeline/Log (50% left) | Detail (50% right)
// Task, Suggest, API, Stats, Sandbox, Prompts → tray
const DEFAULT_LAYOUT: SerializedDockview = {"grid":{"root":{"type":"branch","data":[{"type":"leaf","data":{"views":["filters"],"activeView":"filters","id":"1"},"size":80},{"type":"branch","data":[{"type":"leaf","data":{"views":["metrics"],"activeView":"metrics","id":"2"},"size":1120},{"type":"leaf","data":{"views":["scatter"],"activeView":"scatter","id":"3"},"size":480}],"size":330},{"type":"branch","data":[{"type":"leaf","data":{"views":["table","graph","timeline","queue","log"],"activeView":"table","id":"4"},"size":800},{"type":"leaf","data":{"views":["detail"],"activeView":"detail","id":"5"},"size":800}],"size":590}],"size":1600},"width":1600,"height":1000,"orientation":"VERTICAL"},"panels":{"filters":{"id":"filters","contentComponent":"default","title":"Filters"},"metrics":{"id":"metrics","contentComponent":"default","title":"Metrics"},"scatter":{"id":"scatter","contentComponent":"default","title":"Scatter"},"table":{"id":"table","contentComponent":"default","title":"Table"},"graph":{"id":"graph","contentComponent":"default","title":"Graph"},"timeline":{"id":"timeline","contentComponent":"default","title":"Timeline"},"queue":{"id":"queue","contentComponent":"default","title":"Queue"},"log":{"id":"log","contentComponent":"default","title":"Log"},"detail":{"id":"detail","contentComponent":"default","title":"Detail"}},"activeGroup":"4"} as any;

// Mobile/narrow layout: start with the essentials; specialist panes stay in
// the pane bar so the workbench does not become a wall of tabs on first open.
function buildMobileLayout(dv: DockviewComponent) {
  const top = dv.addPanel({ id: "metrics", component: "default", title: "Metrics" });
  dv.addPanel({ id: "table", component: "default", title: "Table", position: { referencePanel: top } });
  const bottom = dv.addPanel({
    id: "detail", component: "default", title: "Detail",
    position: { referencePanel: top, direction: "below" },
  });
  dv.addPanel({ id: "queue", component: "default", title: "Queue", position: { referencePanel: bottom } });
}

const NARROW_BREAKPOINT = 800;
type DashboardMode = "review" | "workbench";

function buildDefaultLayout(dv: DockviewComponent) {
  if (typeof window !== "undefined" && window.innerWidth <= NARROW_BREAKPOINT) {
    buildMobileLayout(dv);
    return;
  }
  try {
    dv.fromJSON(DEFAULT_LAYOUT);
  } catch {
    // Fallback: add panels programmatically if JSON fails
    const graphPanel = dv.addPanel({ id: "graph", component: "default", title: "Graph" });
    for (const [id, title] of Object.entries(PANEL_NAMES)) {
      if (id === "graph") continue;
      dv.addPanel({ id, component: "default", title, position: { referencePanel: graphPanel } });
    }
  }
}

// ---------------------------------------------------------------------------
// URL <-> filter sync (preserved from original)
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// URL <-> state sync — compact, shareable URLs
//
// Path:   /ideas/13  or  /
// Params: only non-default values appear (keeps URLs short)
//   m=score         metric
//   t=hybrid,poly   tag filters
//   tm=or           tag filter mode (omit when "and")
//   cm=lane         color mode (omit when "status+improve")
//   imp             improvements only (presence = on)
//   mean            idea mean (presence = on)
//   hide=ac         hidden statuses: a=abandoned c=concluded r=running
//   norev           reverse time OFF (omit when ON, which is default)
//   noclip          clip outliers OFF (omit when ON)
//   sx=score        scatter X metric
//   sy=cost         scatter Y metric
//
// Also supports legacy params (metric=, tags=, color=, etc.) for back-compat.
// ---------------------------------------------------------------------------

function readFiltersFromUrl() {
  const path = window.location.pathname;
  const p = new URLSearchParams(window.location.search);

  // Path: /ideas/13
  const ideaMatch = path.match(/^\/ideas\/(\d+)/);
  if (ideaMatch) {
    selectedIdea.value = parseInt(ideaMatch[1]);
  } else if (p.has("idea")) {
    // Legacy: ?idea=13
    selectedIdea.value = parseInt(p.get("idea")!) || null;
  }

  // Metric (short: m, legacy: metric)
  const metric = p.get("m") || p.get("metric");
  if (metric) selectedMetric.value = metric;

  // Tag filters (short: t, legacy: tags)
  const tags = p.get("t") || p.get("tags");
  if (tags) activeTagFilters.value = tags.split(",").filter(Boolean);

  // Tag filter mode (short: tm, legacy: tagMode)
  const tm = p.get("tm") || p.get("tagMode");
  if (tm === "or" || tm === "and") tagFilterMode.value = tm;

  // Color mode (short: cm, legacy: color)
  const cm = p.get("cm") || p.get("color");
  if (cm) colorMode.value = cm;

  // Boolean toggles — presence means ON (for imp, mean) or OFF (for norev, noclip)
  if (p.has("imp") || p.get("improvements") === "1") improvementsOnly.value = true;
  if (p.has("mean")) ideaMean.value = true;
  if (p.has("norev") || p.get("reverse") === "0") reverseTime.value = false;
  if (p.has("noclip")) clipOutliers.value = false;

  // Hidden statuses (short: hide=acr, legacy: abandoned=0 etc.)
  const hide = p.get("hide") || "";
  if (hide) {
    if (hide.includes("a")) showAbandoned.value = false;
    if (hide.includes("c")) showConcluded.value = false;
    if (hide.includes("r")) showRunning.value = false;
  } else {
    // Legacy format
    if (p.get("abandoned") === "0") showAbandoned.value = false;
    if (p.get("concluded") === "0") showConcluded.value = false;
    if (p.get("running") === "0") showRunning.value = false;
  }

  // Scatter metrics
  if (p.has("sx")) scatterXMetric.value = p.get("sx")!;
  if (p.has("sy")) scatterYMetric.value = p.get("sy")!;
}

function syncUrlFromSignals() {
  const _idea = selectedIdea.value;
  const _metric = selectedMetric.value;
  const _tags = activeTagFilters.value;
  const _tagMode = tagFilterMode.value;
  const _color = colorMode.value;
  const _imp = improvementsOnly.value;
  const _mean = ideaMean.value;
  const _rev = reverseTime.value;
  const _clip = clipOutliers.value;
  const _abn = showAbandoned.value;
  const _con = showConcluded.value;
  const _run = showRunning.value;
  const _sx = scatterXMetric.value;
  const _sy = scatterYMetric.value;

  // Path: /ideas/13 or /
  const path = _idea !== null ? `/ideas/${_idea}` : "/";

  // Only encode non-default values
  const p = new URLSearchParams();
  if (_metric) p.set("m", _metric);
  if (_tags.length > 0) p.set("t", _tags.join(","));
  if (_tagMode !== "and") p.set("tm", _tagMode);
  if (_color !== "status+improve") p.set("cm", _color);
  if (_imp) p.set("imp", "");
  if (_mean) p.set("mean", "");
  if (!_rev) p.set("norev", "");
  if (!_clip) p.set("noclip", "");

  // Hidden statuses: compact "hide=ac" format
  let hide = "";
  if (!_abn) hide += "a";
  if (!_con) hide += "c";
  if (!_run) hide += "r";
  if (hide) p.set("hide", hide);

  if (_sx) p.set("sx", _sx);
  if (_sy) p.set("sy", _sy);

  const qs = p.toString()
    .replace(/=(&|$)/g, "$1")  // strip trailing = for valueless params (imp, mean, norev, noclip)
    .replace(/&$/, "");
  const url = path + (qs ? "?" + qs : "");

  if (url !== window.location.pathname + window.location.search) {
    history.replaceState(null, "", url);
  }
}

// ---------------------------------------------------------------------------
// App component
// ---------------------------------------------------------------------------

export function App() {
  const containerRef = useRef<HTMLDivElement>(null);
  const dockviewRef = useRef<DockviewComponent | null>(null);
  const [dashboardMode, setDashboardMode] = useState<DashboardMode>("review");

  // Initialize dockview
  useEffect(() => {
    if (dashboardMode !== "workbench") return;
    const container = containerRef.current;
    if (!container) return;

    const dv = new DockviewComponent(container, {
      createComponent: (options) => {
        return new PreactPanelRenderer(options.id);
      },
      createRightHeaderActionComponent: (group) => {
        return new PanelHeaderActions(group);
      },
      theme: themeDark,
      floatingGroupBounds: "boundedWithinViewport",
      disableFloatingGroups: false,
    });
    dockviewRef.current = dv;

    // Long-press / right-click on tabs shows a "Move panel" menu (touch-friendly)
    initTouchMoveMenu(dv, container);

    // Expose panel activation globally — skips when maximized to avoid exiting fullscreen
    setActivatePanel((panelId: string) => {
      // Don't activate panels in other groups when maximized
      const hasMax = dv.groups.some((g) => dv.isMaximizedGroup(g));
      if (hasMax) return;
      const panel = dv.panels.find((p) => p.id === panelId);
      if (panel) {
        panel.api.setActive();
      }
    });

    // Expose chart cloning globally
    let cloneCounter = 0;
    setCloneChartPanel((type, metric, xMetric, yMetric) => {
      cloneCounter++;
      const id = `${type}-${cloneCounter}`;
      const title = type === "metrics"
        ? `Metrics: ${metric || "?"}`
        : `Scatter: ${xMetric || "?"} vs ${yMetric || "?"}`;
      const params = type === "metrics"
        ? { instanceId: id, metric }
        : { instanceId: id, xMetric, yMetric };
      // Find the active group of the source panel to add as a sibling tab
      const sourcePanel = dv.panels.find((p) => p.id === type || p.id.startsWith(type + "-"));
      const position = sourcePanel
        ? { referencePanel: sourcePanel }
        : undefined;
      dv.addPanel({ id, component: "default", title, params, position });
    });

    // Expose panel title updates globally
    setUpdatePanelTitle((panelId, title) => {
      const panel = dv.panels.find((p) => p.id === panelId);
      if (panel) {
        panel.api.setTitle(title);
      }
    });

    // Expose "send to tray" globally for the tab context menu
    setSendToTray((panelId) => {
      const panel = dv.panels.find((p) => p.id === panelId);
      if (panel) dv.removePanel(panel);
      // Add to tray if not already there
      if (!trayPanels.value.includes(panelId)) {
        trayPanels.value = [...trayPanels.value, panelId];
      }
    });

    // On narrow screens, always use mobile layout (ignore saved)
    const isNarrow = typeof window !== "undefined" && window.innerWidth <= NARROW_BREAKPOINT;
    if (isNarrow) {
      buildMobileLayout(dv);
    } else {
      // Try to restore saved layout
      const saved = dashboardLayout.value;
      let restored = false;
      if (saved) {
        try {
          const serialized = saved as SerializedDockview;
          if (serialized.grid && serialized.panels) {
            dv.fromJSON(serialized);
            restored = true;
          }
        } catch {
          // Corrupt layout — fall through to default
        }
      }
      if (!restored) {
        buildDefaultLayout(dv);
      }
    }

    // Deep-link restore: if the URL hash specifies an idea, activate the detail
    // panel so dockview mounts it (lazy-mount means it won't mount otherwise on
    // fresh sessions where the detail tab was never clicked).
    const _initHash = new URLSearchParams(window.location.hash.slice(1));
    const _initIdea = _initHash.get("idea");
    if (_initIdea) {
      const _ideaId = parseInt(_initIdea);
      if (_ideaId) {
        selectedIdea.value = _ideaId;
        requestAnimationFrame(() => {
          const panel = dv.panels.find((p) => p.id === "detail");
          if (panel) panel.api.setActive();
        });
      }
    }

    // Save layout on changes — debounced so rapid in-flight events during
    // fromJSON() initialization (or CSS-injection reflows) don't write a
    // partially-initialized layout to localStorage.
    let _layoutSaveTimer: ReturnType<typeof setTimeout> | null = null;
    const layoutDisposable = dv.onDidLayoutChange(() => {
      if (_layoutSaveTimer) clearTimeout(_layoutSaveTimer);
      _layoutSaveTimer = setTimeout(() => {
        try { dashboardLayout.value = dv.toJSON(); } catch { /* ignore */ }
      }, 400);
    });

    function updateAvailablePanels() {
      const openIds = new Set(dv.panels.map((p) => p.id));
      const closed = ALL_PANEL_IDS.filter((id) => !openIds.has(id));
      const maxGroup = dv.groups.find((g) => dv.isMaximizedGroup(g));
      isMaximized.value = !!maxGroup;

      if (maxGroup) {
        // In maximize mode: show all panels not in the maximized group as addable
        const maxPanelIds = new Set(maxGroup.panels.map((p) => p.id));
        const floatable = ALL_PANEL_IDS.filter((id) => !maxPanelIds.has(id));
        availablePanels.value = floatable;
      } else {
        availablePanels.value = closed;
      }
    }

    function syncOpenPanelIds() {
      openPanelIds.value = new Set(dv.panels.map((p) => p.id));
    }

    const addDisposable = dv.onDidAddPanel(() => {
      try { dashboardLayout.value = dv.toJSON(); } catch { /* ignore */ }
      updateAvailablePanels();
      syncOpenPanelIds();
    });

    const removeDisposable = dv.onDidRemovePanel(() => {
      try { dashboardLayout.value = dv.toJSON(); } catch { /* ignore */ }
      updateAvailablePanels();
      syncOpenPanelIds();
    });

    // Track the user's intended maximized group so we can restore it
    // when dockview unexpectedly exits maximize (e.g. during panel moves)
    let _intendedMaxGroup: string | null = null;

    const maxDisposable = dv.onDidMaximizedGroupChange((e) => {
      if (e.group && dv.isMaximizedGroup(e.group)) {
        _intendedMaxGroup = e.group.id;
      } else if (!dv.hasMaximizedGroup() && _intendedMaxGroup) {
        // Check if user explicitly exited via the button
        if ((dv as any)._userExitingMaximize) {
          (dv as any)._userExitingMaximize = false;
          _intendedMaxGroup = null;
          // _preMaximizeLayout restore is handled in the button click handler
          updateAvailablePanels();
          return;
        }
        // Maximize was lost unexpectedly — if floating groups exist (user is
        // in fullscreen+floating mode), re-maximize automatically.
        const hasFloating = dv.groups.some((g) => g.api.location.type === "floating");
        if (hasFloating) {
          const group = dv.groups.find((g) => g.id === _intendedMaxGroup && g.api.location.type === "grid");
          if (group) {
            requestAnimationFrame(() => {
              if (!dv.hasMaximizedGroup()) {
                dv.maximizeGroup(group);
              }
            });
          }
        } else {
          _intendedMaxGroup = null;
        }
      }
      updateAvailablePanels();
    });

    updateAvailablePanels();
    syncOpenPanelIds();

    // ── Feature: double-click tab bar to collapse/expand group ──
    // Dockview enforces 100px minimum per group by default. We override
    // minimumHeight when collapsing (to allow 28px) and restore it on expand.
    // A guarded layoutChange enforcer re-applies collapsed size if dockview
    // tries to redistribute space into collapsed groups.
    const collapsedGroups = new Map<string, number>(); // groupId → original height
    const TAB_BAR_HEIGHT = 28; // matches --dv-tabs-and-actions-container-height
    let _collapseGuard = false;

    // After any layout change, re-enforce collapsed heights
    const collapseEnforcer = dv.onDidLayoutChange(() => {
      if (_collapseGuard || collapsedGroups.size === 0) return;
      _collapseGuard = true;
      for (const [gid] of collapsedGroups) {
        const g = dv.groups.find((gg) => gg.id === gid);
        if (g && g.height > TAB_BAR_HEIGHT + 2) {
          g.api.setSize({ height: TAB_BAR_HEIGHT });
        }
      }
      _collapseGuard = false;
    });

    function onTabBarDblClick(e: MouseEvent) {
      const header = (e.target as HTMLElement).closest(".dv-tabs-and-actions-container");
      if (!header) return;
      if ((e.target as HTMLElement).closest(".dv-header-actions")) return;
      const groupEl = header.closest(".dv-groupview") as HTMLElement | null;
      if (!groupEl) return;
      const group = dv.groups.find((g) => g.element === groupEl || g.element.contains(groupEl));
      if (!group) return;

      _collapseGuard = true;

      if (collapsedGroups.has(group.id)) {
        // ── Expand ──
        const origHeight = collapsedGroups.get(group.id)!;
        const top = Math.round(group.element.getBoundingClientRect().top);
        const siblings = dv.groups.filter((g) =>
          Math.abs(Math.round(g.element.getBoundingClientRect().top) - top) < 5);
        for (const g of siblings) {
          collapsedGroups.delete(g.id);
          g.api.setConstraints({ minimumHeight: undefined as any });
        }
        group.api.setSize({ height: origHeight });
      } else {
        // ── Collapse ──
        const currentHeight = group.height;
        if (currentHeight > TAB_BAR_HEIGHT + 10) {
          const top = Math.round(group.element.getBoundingClientRect().top);
          for (const g of dv.groups) {
            if (Math.abs(Math.round(g.element.getBoundingClientRect().top) - top) < 5) {
              collapsedGroups.set(g.id, currentHeight);
              // Override dockview's 100px default minimum
              g.api.setConstraints({ minimumHeight: TAB_BAR_HEIGHT });
            }
          }
          group.api.setSize({ height: TAB_BAR_HEIGHT });
        }
      }

      requestAnimationFrame(() => { _collapseGuard = false; });
    }
    container.addEventListener("dblclick", onTabBarDblClick);

    // ── Feature: drag tab to tray to hide it ──
    let _draggedPanelId: string | null = null;
    const dragPanelDisposable = dv.onWillDragPanel((e) => {
      _draggedPanelId = e.panel.id;
    });
    // Clear on dragend (fires on the document when any drag ends)
    function onDragEnd() { _draggedPanelId = null; }
    document.addEventListener("dragend", onDragEnd);

    // The tray element gets dragover/drop listeners after mount
    requestAnimationFrame(() => {
      const trayEl = document.querySelector(".panel-tray");
      if (!trayEl) return;
      trayEl.addEventListener("dragover", (e: Event) => {
        if (!_draggedPanelId) return;
        (e as DragEvent).preventDefault();
        (e as DragEvent).dataTransfer!.dropEffect = "move";
        (trayEl as HTMLElement).classList.add("drag-over");
      });
      trayEl.addEventListener("dragleave", () => {
        (trayEl as HTMLElement).classList.remove("drag-over");
      });
      trayEl.addEventListener("drop", (e: Event) => {
        (e as DragEvent).preventDefault();
        (trayEl as HTMLElement).classList.remove("drag-over");
        if (_draggedPanelId) {
          const panelId = _draggedPanelId;
          _draggedPanelId = null;
          // Remove from dockview and add to tray
          const panel = dv.panels.find((p) => p.id === panelId);
          if (panel) dv.removePanel(panel);
          if (!trayPanels.value.includes(panelId)) {
            trayPanels.value = [...trayPanels.value, panelId];
          }
        }
      });
    });

    // When a tray panel is docked (moved from floating to grid), remove
    // it from transient tracking so it becomes permanent.
    const trayDockCheck = dv.onDidLayoutChange(() => {
      if (trayOpen.value.size === 0) return;
      const nextOpen = new Set(trayOpen.value);
      let changed = false;
      for (const panelId of trayOpen.value) {
        const panel = dv.panels.find((p) => p.id === panelId);
        if (!panel || panel.group.api.location.type !== "floating") {
          nextOpen.delete(panelId);
          changed = true;
          // Non-default panels leave the tray entirely when docked
          if (!DEFAULT_TRAY_IDS.includes(panelId)) {
            trayPanels.value = trayPanels.value.filter((id) => id !== panelId);
          }
        }
      }
      if (changed) trayOpen.value = nextOpen;
    });

    return () => {
      document.removeEventListener("dragend", onDragEnd);
      dragPanelDisposable.dispose();
      container.removeEventListener("dblclick", onTabBarDblClick);
      trayDockCheck.dispose();
      collapseEnforcer.dispose();
      layoutDisposable.dispose();
      addDisposable.dispose();
      removeDisposable.dispose();
      maxDisposable.dispose();
      dv.dispose();
      dockviewRef.current = null;
    };
  }, [dashboardMode]);

  // Polling, URL sync, server defaults
  useEffect(() => {
    applyServerDefaults().then(() => readFiltersFromUrl());
    window.addEventListener("popstate", readFiltersFromUrl);
    startPolling();
    startWs();
    const dispose = effect(syncUrlFromSignals);
    return () => {
      window.removeEventListener("popstate", readFiltersFromUrl);
      stopPolling();
      stopWs();
      dispose();
    };
  }, []);

  // --- Layout management ---

  const SAVED_LAYOUTS_KEY = "the-lab:savedLayouts";

  const handleResetLayout = useCallback(() => {
    if (!confirm("Reset to default layout and clear all dashboard preferences (selected metric, tag filters, status toggles, etc.)? Saved named layouts will be kept.")) return;
    const dv = dockviewRef.current;
    // Wipe every `the-lab:` localStorage key except saved named layouts.
    // Reload afterwards so signals re-initialise from defaults — they read
    // localStorage at module load, so an in-memory reset would leave stale
    // values until the next refresh.
    try {
      const keep = new Set([SAVED_LAYOUTS_KEY]);
      const toRemove: string[] = [];
      for (let i = 0; i < localStorage.length; i++) {
        const k = localStorage.key(i);
        if (k && k.startsWith("the-lab:") && !keep.has(k)) toRemove.push(k);
      }
      for (const k of toRemove) localStorage.removeItem(k);
    } catch { /* ignore */ }
    if (dv) {
      dv.clear();
      buildDefaultLayout(dv);
    }
    dashboardLayout.value = null;
    location.reload();
  }, []);

  const handleSaveLayout = useCallback((name: string) => {
    const dv = dockviewRef.current;
    if (!dv) return;
    try {
      const layouts = JSON.parse(localStorage.getItem(SAVED_LAYOUTS_KEY) || "{}");
      layouts[name] = dv.toJSON();
      localStorage.setItem(SAVED_LAYOUTS_KEY, JSON.stringify(layouts));
    } catch { /* ignore */ }
  }, []);

  const handleLoadLayout = useCallback((name: string) => {
    const dv = dockviewRef.current;
    if (!dv) return;
    try {
      const layouts = JSON.parse(localStorage.getItem(SAVED_LAYOUTS_KEY) || "{}");
      if (layouts[name]) {
        dv.fromJSON(layouts[name]);
        dashboardLayout.value = dv.toJSON();
      }
    } catch { /* ignore */ }
  }, []);

  const handleDeleteLayout = useCallback((name: string) => {
    try {
      const layouts = JSON.parse(localStorage.getItem(SAVED_LAYOUTS_KEY) || "{}");
      delete layouts[name];
      localStorage.setItem(SAVED_LAYOUTS_KEY, JSON.stringify(layouts));
    } catch { /* ignore */ }
  }, []);

  const getSavedLayouts = useCallback((): string[] => {
    try {
      return Object.keys(JSON.parse(localStorage.getItem(SAVED_LAYOUTS_KEY) || "{}"));
    } catch { return []; }
  }, []);

  // Read tray signals at component level so Preact subscribes to changes
  const _open = openPanelIds.value;
  const _floating = trayOpen.value;
  const _tray = trayPanels.value;
  // Panels in the tray: explicit tray panels + closed panels not docked in grid
  const _dockedInGrid = ALL_PANEL_IDS.filter((id) => _open.has(id) && !_floating.has(id));
  const _closed = ALL_PANEL_IDS.filter((id) => !_open.has(id) && !_tray.includes(id));
  const trayItems = [..._tray, ..._closed].filter((id) => !_dockedInGrid.includes(id));

  const handleToggleTrayPanel = useCallback((id: string) => {
    const dv = dockviewRef.current;
    if (!dv) return;
    const existing = dv.panels.find((p) => p.id === id);
    if (existing) {
      // Save float position relative to the dockview container.
      // Read from the floatingGroup wrapper (parent of group element) which
      // is what dockview positions — avoids border offset accumulation.
      if (existing.group.api.location.type === "floating") {
        const floatWrapper = existing.group.element.parentElement;
        const el = floatWrapper || existing.group.element;
        const container = dv.element.getBoundingClientRect();
        const rect = el.getBoundingClientRect();
        trayPositions[id] = {
          x: Math.round(rect.left - container.left),
          y: Math.round(rect.top - container.top),
          width: Math.round(rect.width),
          height: Math.round(rect.height),
        };
      }
      dv.removePanel(existing);
      const next = new Set(trayOpen.value);
      next.delete(id);
      trayOpen.value = next;
    } else {
      // Open as floating — restore saved position or center
      const title = PANEL_NAMES[id] || id;
      const saved = trayPositions[id];
      const pos = saved || {
        x: (window.innerWidth - Math.min(600, window.innerWidth * 0.5)) / 2,
        y: (window.innerHeight - Math.min(500, window.innerHeight * 0.5)) / 2 - 30,
        width: Math.min(600, window.innerWidth * 0.5),
        height: Math.min(500, window.innerHeight * 0.5),
      };
      // Ensure panel is tracked in trayPanels so it stays visible while floating
      if (!trayPanels.value.includes(id)) {
        trayPanels.value = [...trayPanels.value, id];
      }
      // Use addPanel with floating option — avoids creating an intermediate
      // grid group which would redistribute space and reset docked pane sizes.
      dv.addPanel({ id, component: "default", title, floating: pos } as any);
      const next = new Set(trayOpen.value);
      next.add(id);
      trayOpen.value = next;
    }
  }, []);

  const handleAddPanel = useCallback((id: string, mode: "dock" | "float" = "dock") => {
    const dv = dockviewRef.current;
    if (!dv) return;

    const title = PANEL_NAMES[id] || id;
    const shouldFloat = mode === "float" || dv.hasMaximizedGroup();
    const existing = dv.panels.find((p) => p.id === id);
    if (existing) {
      if (shouldFloat && existing.group.api.location.type !== "floating") {
        dv.removePanel(existing);
      } else {
        existing.api.setActive();
        return;
      }
    }

    if (shouldFloat) {
      const width = Math.min(620, Math.max(320, window.innerWidth * 0.44));
      const height = Math.min(520, Math.max(260, window.innerHeight * 0.48));
      const offset = 32 + (trayOpen.value.size % 4) * 26;
      dv.addPanel({
        id,
        component: "default",
        title,
        floating: {
          x: Math.max(16, (window.innerWidth - width) / 2 + offset),
          y: Math.max(52, (window.innerHeight - height) / 2 + offset - 40),
          width,
          height,
        },
      } as any);
      const next = new Set(trayOpen.value);
      next.add(id);
      trayOpen.value = next;
    } else {
      const active = dv.activePanel;
      dv.addPanel({
        id,
        component: "default",
        title,
        position: active ? { referencePanel: active } : undefined,
      });
    }

    if (!trayPanels.value.includes(id) && DEFAULT_TRAY_IDS.includes(id)) {
      trayPanels.value = [...trayPanels.value, id];
    }
  }, []);

  const focusPanelIds = availablePanels.value;
  const inFocusMode = isMaximized.value;
  const isWorkbench = dashboardMode === "workbench";
  const openReviewSection = useCallback((id: string) => {
    setDashboardMode("review");
    requestAnimationFrame(() => {
      const el = document.getElementById(id);
      if (el instanceof HTMLDetailsElement) el.open = true;
      el?.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  }, []);

  return (
    <>
      <Topbar
        onResetLayout={handleResetLayout}
        onSaveLayout={handleSaveLayout}
        onLoadLayout={handleLoadLayout}
        onDeleteLayout={handleDeleteLayout}
        getSavedLayouts={getSavedLayouts}
        onOpenReview={() => setDashboardMode("review")}
        onOpenReviewSection={openReviewSection}
        onOpenWorkbench={() => setDashboardMode("workbench")}
      />
      <main class="dashboard-page">
        <div class="dashboard-modebar" aria-label="Dashboard mode">
          <button class={dashboardMode === "review" ? "active" : ""} onClick={() => setDashboardMode("review")}>Review</button>
          <button class={dashboardMode === "workbench" ? "active" : ""} onClick={() => setDashboardMode("workbench")}>Workbench</button>
        </div>

        {!isWorkbench && <ReviewDashboard onOpenWorkbench={() => setDashboardMode("workbench")} />}

        {isWorkbench && (
          <>
            <section class="workspace-shell" aria-label="Dashboard workspace">
              <div class="workspace-toolbar">
                <button class="workspace-review-btn" onClick={() => setDashboardMode("review")}>
                  ← Review
                </button>
                <div class="workspace-actions" aria-label="Workspace actions">
                  {ALL_PANEL_IDS.filter((id) => !_open.has(id)).map((id) => (
                    <button
                      key={id}
                      class="workspace-panel-chip"
                      onClick={() => handleAddPanel(id)}
                      title={`Open ${PANEL_NAMES[id]}`}
                    >
                      {PANEL_NAMES[id]}
                    </button>
                  ))}
                  {ALL_PANEL_IDS.filter((id) => !_open.has(id)).length === 0 && (
                    <span class="workspace-all-open">all panels open</span>
                  )}
                </div>
              </div>

              {inFocusMode && focusPanelIds.length > 0 && (
                <div class="focus-strip">
                  <span class="focus-strip-label">Focus context</span>
                  {focusPanelIds.map((id) => (
                    <button
                      key={id}
                      class="focus-strip-btn"
                      onClick={() => handleAddPanel(id, "float")}
                      title={`Open ${PANEL_NAMES[id]} as floating context`}
                    >
                      + {PANEL_NAMES[id]}
                    </button>
                  ))}
                </div>
              )}

              <div
                id="dockview-container"
                ref={containerRef}
              />
            </section>

            <div class="panel-tray" aria-label="Panel tray">
              {trayItems.map((id) => (
                <button
                  key={id}
                  class={_floating.has(id) ? "active" : ""}
                  onClick={() => handleToggleTrayPanel(id)}
                  title={PANEL_NAMES[id] || id}
                >
                  {PANEL_NAMES[id] || id}
                </button>
              ))}
            </div>
          </>
        )}
      </main>
      <ChatPanel />
    </>
  );
}

// ── Idea exploration ring ─────────────────────────────────────────────────────

function IdeaRing({ active, concluded, abandoned }: { active: number; concluded: number; abandoned: number }) {
  const total = active + concluded + abandoned;
  if (total === 0) return null;
  const size = 36, sw = 6, r = (size - sw) / 2;
  const c = 2 * Math.PI * r;
  const concludedFrac = concluded / total;
  const activeFrac = active / total;
  const concludedLen = c * concludedFrac;
  const activeLen = c * activeFrac;
  const abandonedLen = c * (abandoned / total);
  // concluded arc starts at top (-90°), active follows, abandoned last
  const concludedOffset = 0;
  const activeOffset = c - concludedLen;
  const abandonedOffset = c - concludedLen - activeLen;

  return (
    <svg width={size} height={size} style={{ flexShrink: 0 }} title={`${concluded} concluded · ${active} active · ${abandoned} abandoned`}>
      {/* Background track */}
      <circle cx={size/2} cy={size/2} r={r} fill="none" stroke="var(--border)" strokeWidth={sw} />
      {/* Concluded arc (blue) */}
      {concludedLen > 0 && (
        <circle cx={size/2} cy={size/2} r={r} fill="none"
          stroke="var(--accent)" strokeWidth={sw} strokeOpacity={0.7}
          strokeDasharray={`${concludedLen} ${c - concludedLen}`}
          strokeDashoffset={c / 4}
          strokeLinecap="butt"
          transform={`rotate(-90 ${size/2} ${size/2})`}
        />
      )}
      {/* Active arc (green) */}
      {activeLen > 0 && (
        <circle cx={size/2} cy={size/2} r={r} fill="none"
          stroke="var(--green)" strokeWidth={sw} strokeOpacity={0.8}
          strokeDasharray={`${activeLen} ${c - activeLen}`}
          strokeDashoffset={c / 4 + concludedLen}
          strokeLinecap="butt"
          transform={`rotate(-90 ${size/2} ${size/2})`}
        />
      )}
      {/* Abandoned arc (red) */}
      {abandonedLen > 0 && (
        <circle cx={size/2} cy={size/2} r={r} fill="none"
          stroke="var(--red)" strokeWidth={sw} strokeOpacity={0.6}
          strokeDasharray={`${abandonedLen} ${c - abandonedLen}`}
          strokeDashoffset={c / 4 + concludedLen + activeLen}
          strokeLinecap="butt"
          transform={`rotate(-90 ${size/2} ${size/2})`}
        />
      )}
      {/* Center label: total count */}
      <text x={size/2} y={size/2 + 3} textAnchor="middle" fontSize="9" fontWeight="600" fill="var(--text)" fontFamily="var(--font-mono, monospace)">
        {total}
      </text>
    </svg>
  );
}

// ── Idea mini leaderboard ─────────────────────────────────────────────────────

function IdeaMiniLeaderboard({ experiments, ideas, metric, lower }: {
  experiments: import("./lib/types").Experiment[];
  ideas: Record<number, import("./lib/types").IdeaNode>;
  metric: string;
  lower: boolean;
}) {
  if (!metric) return null;

  // Compute best score and experiment count per idea
  const ideaBest: Record<number, { best: number; expLabel: string; expId: number; count: number }> = {};
  for (const e of experiments) {
    if (e._running) continue;
    if (typeof e.metrics?.[metric] !== "number") continue;
    const v = e.metrics![metric] as number;
    const cur = ideaBest[e.idea_id];
    if (!cur) {
      ideaBest[e.idea_id] = { best: v, expLabel: e.label ?? String(e.id), expId: e.id, count: 1 };
    } else {
      cur.count++;
      if (lower ? v < cur.best : v > cur.best) {
        cur.best = v; cur.expLabel = e.label ?? String(e.id); cur.expId = e.id;
      }
    }
  }

  // Compute chronological score history per idea (for sparklines)
  const ideaHistory: Record<number, number[]> = {};
  for (const e of experiments.slice().sort((a, b) => a.id - b.id)) {
    if (e._running) continue;
    if (typeof e.metrics?.[metric] !== "number") continue;
    const v = e.metrics![metric] as number;
    if (!ideaHistory[e.idea_id]) ideaHistory[e.idea_id] = [];
    ideaHistory[e.idea_id].push(v);
  }

  const ranked = Object.entries(ideaBest)
    .map(([id, d]) => ({ ideaId: Number(id), ...d }))
    .sort((a, b) => lower ? a.best - b.best : b.best - a.best)
    .slice(0, 5);

  if (ranked.length === 0) return null;

  function fmtV(v: number) {
    return Math.abs(v) >= 100 ? v.toFixed(0) : Math.abs(v) >= 1 ? v.toFixed(2) : v.toFixed(3);
  }

  // Tiny sparkline for an idea's score history
  function IdeaSparkline({ vals, lower: lo }: { vals: number[]; lower: boolean }) {
    if (vals.length === 0) return null;
    const W = 28, H = 10;
    if (vals.length === 1) {
      return (
        <svg width={W} height={H} style={{ flexShrink: 0, opacity: 0.5 }}>
          <circle cx={W/2} cy={H/2} r={2} fill="var(--text-faint)" />
        </svg>
      );
    }
    const lo_ = Math.min(...vals), hi_ = Math.max(...vals);
    const range = hi_ - lo_;
    if (range < 1e-9) {
      // flat line
      return (
        <svg width={W} height={H} style={{ flexShrink: 0, opacity: 0.5 }}>
          <line x1={2} y1={H/2} x2={W-2} y2={H/2} stroke="var(--text-faint)" strokeWidth="1" strokeDasharray="2 1.5" />
        </svg>
      );
    }
    const px = (i: number) => 2 + (i / (vals.length - 1)) * (W - 4);
    const py = (v: number) => H - 2 - ((v - lo_) / range) * (H - 4);
    const d = vals.map((v, i) => `${i === 0 ? "M" : "L"}${px(i).toFixed(1)},${py(v).toFixed(1)}`).join(" ");
    const trend = lo ? vals[0] - vals[vals.length - 1] : vals[vals.length - 1] - vals[0];
    const color = trend > range * 0.1 ? "var(--green)" : trend < -range * 0.1 ? "var(--red)" : "var(--text-faint)";
    return (
      <svg width={W} height={H} style={{ flexShrink: 0, opacity: 0.75 }}>
        <path d={d} fill="none" stroke={color} strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    );
  }

  return (
    <div class="emr-section">
      <span class="emr-label">top ideas</span>
      <div class="emr-rows">
        {ranked.map((r, i) => {
          const idea = ideas[r.ideaId];
          const title = idea?.description?.split("\n")[0].slice(0, 40) ?? `idea #${r.ideaId}`;
          const history = ideaHistory[r.ideaId] ?? [];
          return (
            <div key={r.ideaId} class={`emr-row${i === 0 ? " emr-milestone" : ""}`}
              style={{ cursor: "pointer" }}
              onClick={() => navigateToIdea(r.ideaId)}
              title={`idea #${r.ideaId}: ${idea?.description?.split("\n")[0] ?? ""}`}
            >
              <span class="emr-rank">{i + 1}</span>
              <span class="emr-idea">#{r.ideaId}</span>
              <span style={{ fontSize: "7px", opacity: 0.7, flexShrink: 0, color: idea?.status === "active" ? "var(--green)" : idea?.status === "concluded" ? "var(--accent)" : "var(--red)" }}>●</span>
              <span class="emr-exp" style={{ color: "var(--text-muted)", maxWidth: 100, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{title}</span>
              <span class="emr-count" style={{ color: r.count < 5 ? "var(--yellow)" : r.count > 30 ? "var(--text-faint)" : "var(--text-muted)" }}>{r.count}</span>
              <IdeaSparkline vals={history} lower={lower} />
              <span class="emr-val">{fmtV(r.best)}</span>
              {i > 0 && ranked[0].best !== r.best && (
                <span class="emr-gap">
                  {lower
                    ? `+${(r.best - ranked[0].best).toFixed(1)}`
                    : `-${(ranked[0].best - r.best).toFixed(1)}`}
                </span>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ── Glanceable experiment grid ────────────────────────────────────────────────

import { IDEA_PALETTE } from "./lib/colors";

const STATUS_SQ_CLASS: Record<string, string> = {
  running:   "sq-running",
  completed: "sq-done",
  active:    "sq-done",
  failed:    "sq-failed",
  abandoned: "sq-failed",
  concluded: "sq-concluded",
  queued:    "sq-queued",
  pending:   "sq-queued",
  cancelled: "sq-cancelled",
};

function ExperimentGrid({ experiments }: { experiments: import("./lib/types").Experiment[] }) {
  if (experiments.length === 0) return null;

  // Group by idea in chronological order
  const ideaOrder: number[] = [];
  const byIdea = new Map<number, typeof experiments[0][]>();
  const chrono = experiments.slice().sort((a, b) => a.id - b.id);
  for (const e of chrono) {
    if (!byIdea.has(e.idea_id)) { byIdea.set(e.idea_id, []); ideaOrder.push(e.idea_id); }
    byIdea.get(e.idea_id)!.push(e);
  }

  // Build idea → palette color index
  const ideaColor: Record<number, string> = {};
  ideaOrder.forEach((id, i) => { ideaColor[id] = IDEA_PALETTE[i % IDEA_PALETTE.length]; });

  const hasRunning = experiments.some((e) => e._running || e.status === "running");
  const hasFailed  = experiments.some((e) => e.status === "failed" || e.status === "abandoned");

  return (
    <div class="exp-grid-wrap">
      <div class="exp-grid">
        {ideaOrder.map((ideaId, gi) => {
          const exps = byIdea.get(ideaId)!;
          const ideaCol = ideaColor[ideaId];
          return (
            <span
              key={ideaId}
              class="exp-idea-group"
              style={gi > 0 ? { marginLeft: 5, cursor: "pointer" } : { cursor: "pointer" }}
              title={`idea #${ideaId} — click to view`}
              onClick={() => navigateToIdea(ideaId)}
            >
              {exps.map((e) => {
                const status = e._running ? "running" : (e.status ?? "unknown");
                const cls = `exp-grid-sq ${STATUS_SQ_CLASS[status] ?? "sq-cancelled"}`;
                return (
                  <span
                    key={e.id}
                    class={cls}
                    style={{ "--idea-color": ideaCol } as any}
                    title={`${e.label ?? e.id} · idea #${e.idea_id} · ${status}`}
                  />
                );
              })}
            </span>
          );
        })}
      </div>
      <div class="exp-grid-legend">
        {hasRunning && <span class="sq-legend sq-running">running</span>}
        <span class="sq-legend sq-done">done</span>
        {hasFailed && <span class="sq-legend sq-failed">failed</span>}
        <span class="sq-legend sq-concluded">concluded</span>
        <span class="sq-legend sq-queued">queued</span>
        <span class="exp-grid-legend-note">← each square = one experiment, grouped by idea →</span>
      </div>
    </div>
  );
}

/** Mini sparkline of running-best values — shows improvement trajectory */
function BestSparkline({ experiments, metric, lower }: {
  experiments: import("./lib/types").Experiment[];
  metric: string;
  lower: boolean;
}) {
  const W = 48, H = 18;
  const vals: number[] = [];
  let best: number | null = null;
  const sorted = experiments.slice().sort((a, b) => a.id - b.id);
  for (const e of sorted) {
    const v = e.metrics?.[metric];
    if (typeof v !== "number" || !isFinite(v)) continue;
    if (best === null || (lower ? v < best : v > best)) best = v;
    vals.push(best);
  }
  if (vals.length < 2) return null;
  const recent = vals.slice(-20);
  const lo = Math.min(...recent), hi = Math.max(...recent);
  const range = hi - lo;

  // If variance is < 0.1% of the value, show a "plateau" dash instead
  if (range === 0 || (Math.abs(lo) > 0 && range / Math.abs(lo) < 0.001)) {
    return (
      <svg width={W} height={H} style={{ display: "block", flexShrink: 0, opacity: 0.5 }}>
        <line x1={4} y1={H/2} x2={W-4} y2={H/2} stroke="var(--text-faint)" strokeWidth="1.5" strokeDasharray="3 2" strokeLinecap="round" />
      </svg>
    );
  }

  const px = (i: number) => (i / (recent.length - 1)) * W;
  const py = (v: number) => H - 2 - ((v - lo) / range) * (H - 4);
  const d = recent.map((v, i) => `${i === 0 ? "M" : "L"}${px(i).toFixed(1)},${py(v).toFixed(1)}`).join(" ");
  return (
    <svg width={W} height={H} style={{ display: "block", flexShrink: 0, opacity: 0.85 }}>
      <path d={d} fill="none" stroke="var(--purple)" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
      <circle cx={px(recent.length - 1)} cy={py(recent[recent.length - 1])} r="2" fill="var(--purple)" />
    </svg>
  );
}


// ── Score distribution histogram ─────────────────────────────────────────────

function ScoreDistBar({ experiments, metric, lower }: {
  experiments: import("./lib/types").Experiment[];
  metric: string;
  lower: boolean;
}) {
  const values = experiments
    .filter(e => !e._running && typeof e.metrics?.[metric] === "number")
    .map(e => e.metrics![metric] as number);
  if (values.length < 3) return null;
  const lo = Math.min(...values), hi = Math.max(...values);
  if (hi === lo) return null;

  const BUCKETS = 10;
  const size = (hi - lo) / BUCKETS;
  const counts = new Array(BUCKETS).fill(0);
  for (const v of values) counts[Math.min(Math.floor((v - lo) / size), BUCKETS - 1)]++;
  const maxC = Math.max(...counts);

  function fmtN(v: number) {
    return Math.abs(v) >= 10 ? v.toFixed(0) : v.toFixed(1);
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 2, alignItems: "stretch" }}>
      <div class="score-dist-bar" title={`Score distribution: red=low scores, green=high scores. Range ${fmtN(lo)}–${fmtN(hi)}`}>
        {counts.map((c, i) => {
          const frac = lower ? 1 - i / (BUCKETS - 1) : i / (BUCKETS - 1);
          const h = c > 0 ? Math.max(3, Math.round((c / maxC) * 22)) : 1;
          const clr = `hsl(${Math.round(120 * frac)},70%,52%)`;
          return (
            <span key={i} class="score-dist-tick"
              style={{ height: `${h}px`, background: c > 0 ? clr : "var(--border-soft)", opacity: c > 0 ? 0.8 : 0.25 }}
              title={`${c} exp @ ${(lo + i * size).toFixed(1)}–${(lo + (i+1) * size).toFixed(1)}`}
            />
          );
        })}
      </div>
      {/* Range label */}
      <div style={{ display: "flex", justifyContent: "space-between", width: "90px", fontSize: "9px", color: "var(--text-muted)", fontFamily: "var(--font-mono, monospace)" }}>
        <span>{fmtN(lo)}</span>
        <span>{fmtN(hi)}</span>
      </div>
    </div>
  );
}

// ── Mini results list for collapsed Experiments disclosure ───────────────────

function ExpMiniResults({ experiments, metric, lower, milestoneIds, ideas }: {
  experiments: import("./lib/types").Experiment[];
  metric: string;
  lower: boolean;
  milestoneIds: Set<number>;
  ideas: Record<number, import("./lib/types").IdeaNode>;
}) {
  const withMetric = experiments.filter(
    (e) => !e._running && typeof e.metrics?.[metric] === "number",
  );
  if (withMetric.length === 0 && experiments.filter(e => e._running).length === 0) return null;

  // Top 5 by metric
  const top5 = [...withMetric]
    .sort((a, b) => {
      const va = a.metrics![metric] as number;
      const vb = b.metrics![metric] as number;
      return lower ? va - vb : vb - va;
    })
    .slice(0, 5);

  // Last 3 by id (most recent runs — show what just happened)
  const recent3 = [...experiments]
    .sort((a, b) => b.id - a.id)
    .slice(0, 3);

  function fmtVal(v: number) {
    return Math.abs(v) >= 100 ? v.toFixed(0) : Math.abs(v) >= 1 ? v.toFixed(2) : v.toFixed(3);
  }

  const statusDot: Record<string, string> = {
    running: "var(--yellow)", completed: "var(--green)", failed: "var(--red)",
    cancelled: "var(--text-faint)", queued: "var(--text-faint)",
  };

  return (
    <div class="exp-mini-results">
      {/* Top 5 by metric */}
      {top5.length > 0 && (
        <div class="emr-section">
          <span class="emr-label">top {top5.length}</span>
          <div class="emr-rows">
            {top5.map((e, i) => {
              const v = e.metrics![metric] as number;
              const isMilestone = milestoneIds.has(e.id);
              const idea = ideas[e.idea_id];
              return (
                <div key={e.id} class={`emr-row${isMilestone ? " emr-milestone" : ""}`}
                  style={{ cursor: "pointer" }}
                  onClick={() => navigateToIdea(e.idea_id, e.label ?? String(e.id))}
                  title={`${e.description?.slice(0, 100) ?? ""}  ·  exp/${e.label ?? e.id} · idea #${e.idea_id}`}
                >
                  <span class="emr-rank">{i + 1}</span>
                  {isMilestone && <span class="emr-star">★</span>}
                  <code class="emr-exp">exp/{e.label ?? e.id}</code>
                  <span class="emr-idea">#{e.idea_id}</span>
                  <span class="emr-val">{fmtVal(v)}</span>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* Recent 3 — highlight stagnation when all score zero */}
      {(() => {
        const recentScores = recent3.filter(e => !e._running && typeof e.metrics?.[metric] === "number")
          .map(e => e.metrics![metric] as number);
        const allZero = recentScores.length >= 2 && recentScores.every(v => v === 0);
        return (
      <div class="emr-section">
        <span class="emr-label" style={allZero ? { color: "var(--red)", fontWeight: 600 } : undefined}>
          {allZero ? "recent ↓ 0s" : "recent"}
        </span>
        <div class="emr-rows">
          {recent3.map((e) => {
            const status = e._running ? "running" : (e.status ?? "unknown");
            const v = typeof e.metrics?.[metric] === "number" ? e.metrics![metric] as number : null;
            return (
              <div key={e.id} class="emr-row"
                style={{ cursor: "pointer" }}
                onClick={() => navigateToIdea(e.idea_id, e.label ?? String(e.id))}
                title={`exp/${e.label ?? e.id} · ${status}`}
              >
                <span class="emr-dot" style={{ background: statusDot[status] ?? "var(--border)" }} />
                <code class="emr-exp">exp/{e.label ?? e.id}</code>
                <span class="emr-idea">#{e.idea_id}</span>
                <span class="emr-val" style={v === 0 ? { color: "var(--text-faint)" } : undefined}>
                  {v !== null ? fmtVal(v) : status}
                </span>
              </div>
            );
          })}
        </div>
      </div>
        );
      })()}
    </div>
  );
}

function ProgressRing({ pct }: { pct: number }) {
  const size = 14, sw = 2, r = (size - sw) / 2;
  const c = 2 * Math.PI * r;
  const off = c * (1 - Math.min(Math.max(pct, 0), 100) / 100);
  return (
    <svg width={size} height={size} style={{ display: "inline-block", verticalAlign: "middle", marginRight: 4, flexShrink: 0 }}>
      <circle cx={size/2} cy={size/2} r={r} fill="none" stroke="var(--border)" strokeWidth={sw} />
      <circle cx={size/2} cy={size/2} r={r} fill="none" stroke="var(--yellow)" strokeWidth={sw}
        strokeDasharray={c} strokeDashoffset={off} strokeLinecap="round"
        transform={`rotate(-90 ${size/2} ${size/2})`} />
    </svg>
  );
}

function ReviewDashboard({ onOpenWorkbench }: { onOpenWorkbench: () => void }) {
  const data = backlogData.value;
  const experiments = allExperiments.value;
  const ideas = allIdeas.value;
  const cost = totalAgentCost.value;
  const logs = logEntries.value;
  const progress = runningProgress.value;
  const activeIdeas = data?.active_ideas.length ?? 0;
  const totalRunning = data?.total_running ?? 0;
  const branch = data?.current_branch ?? "--";
  const done = experiments.filter((e) => !e._running && e.status !== "running");
  const finished = done.length;
  const running = experiments.filter((e) => e._running || e.status === "running").length;
  const queued = data?.total_pending ?? 0;
  const failed = experiments.filter((e) => e.status === "failed").length;
  const activeRuns = experiments.filter((e) => e._running || e.status === "running");
  const selected = selectedIdea.value;
  const metric = selectedMetric.value || "metric";
  const latestFinished = done[done.length - 1];
  const avgProgress = activeRuns.length
    ? Math.round(activeRuns.reduce((sum, e) => sum + (progress[e.label || String(e.id)] ?? 0), 0) / activeRuns.length)
    : 0;

  // Compute best score for the selected metric
  const lower = isLowerBetter(metric);
  let bestExp: typeof experiments[0] | null = null;
  for (const e of done) {
    const v = e.metrics?.[metric];
    if (typeof v !== "number") continue;
    if (!bestExp || (lower ? v < bestExp.metrics![metric]! : v > bestExp.metrics![metric]!)) bestExp = e;
  }
  const bestVal = bestExp?.metrics?.[metric];
  const bestIdea = bestExp ? ideas[bestExp.idea_id] : null;

  const liveCount = totalRunning || running;
  const isLive = liveCount > 0;

  // Last-activity: most recent finished_at across all experiments
  const lastFinishedAt = done.reduce<string | null>((latest, e) => {
    if (!e.finished_at) return latest;
    return !latest || e.finished_at > latest ? e.finished_at : latest;
  }, null);
  function timeAgo(iso: string | null): string {
    if (!iso) return "";
    const sec = Math.max(0, Math.floor((Date.now() - Date.parse(iso)) / 1000));
    if (sec < 60) return `${sec}s ago`;
    const min = Math.floor(sec / 60);
    if (min < 60) return `${min}m ago`;
    const hr = Math.floor(min / 60);
    if (hr < 48) return `${hr}h ago`;
    return `${Math.floor(hr / 24)}d ago`;
  }

  // Experiments run since last breakthrough (new global best)
  const expsSinceBest = (() => {
    if (!bestExp) return null;
    return done.filter(e => e.id > bestExp!.id).length;
  })();

  // Campaign age: days since first experiment created
  const campaignAgeDays = (() => {
    const earliest = experiments.reduce<string | null>((e, x) => {
      if (!x.created_at) return e;
      return !e || x.created_at < e ? x.created_at : e;
    }, null);
    if (!earliest) return null;
    return Math.floor((Date.now() - Date.parse(earliest)) / (24 * 3600 * 1000));
  })();

  // Campaign velocity: experiments finished in last 7 days
  const velocityPerDay = (() => {
    const sevenDaysAgo = Date.now() - 7 * 24 * 3600 * 1000;
    const recent = done.filter(e => e.finished_at && Date.parse(e.finished_at) > sevenDaysAgo);
    return recent.length > 0 ? (recent.length / 7).toFixed(1) : null;
  })();

  // 14-day activity sparkline: count experiments finished each day
  const activityBars = (() => {
    const DAYS = 14;
    const now = Date.now();
    const buckets = new Array(DAYS).fill(0);
    for (const e of done) {
      if (!e.finished_at) continue;
      const daysAgo = (now - Date.parse(e.finished_at)) / (24 * 3600 * 1000);
      if (daysAgo < DAYS) buckets[DAYS - 1 - Math.floor(daysAgo)]++;
    }
    const maxB = Math.max(...buckets, 1);
    return buckets.map(c => c / maxB); // normalized 0-1
  })();

  // Experiment success rate: % that scored above zero for the primary metric
  const successRate = (() => {
    if (!metric) return null;
    const withMetric = done.filter(e => typeof e.metrics?.[metric] === "number");
    if (withMetric.length < 5) return null;
    const scored = withMetric.filter(e => {
      const v = e.metrics![metric] as number;
      return lower ? v < Infinity : v > 0;
    });
    return Math.round((scored.length / withMetric.length) * 100);
  })();

  // Trend: compare last-10 avg vs overall avg — ↑ improving, → flat, ↓ declining
  const scoreTrend = (() => {
    if (!metric || !bestVal || typeof bestVal !== "number") return null;
    const withMetric = done.filter(e => typeof e.metrics?.[metric] === "number")
      .slice().sort((a, b) => a.id - b.id);
    if (withMetric.length < 20) return null;
    const all = withMetric.map(e => e.metrics![metric] as number);
    const recent10 = all.slice(-10);
    const prev10 = all.slice(-20, -10);
    const avgRecent = recent10.reduce((s, v) => s + v, 0) / 10;
    const avgPrev = prev10.reduce((s, v) => s + v, 0) / 10;
    const diff = lower ? avgPrev - avgRecent : avgRecent - avgPrev;
    if (diff > avgPrev * 0.05) return "↑";
    if (diff < -avgPrev * 0.05) return "↓";
    return "→";
  })();

  // Best score for the selected idea (for Idea detail disclosure preview)
  const selectedIdeaBest = selected != null ? done
    .filter(e => e.idea_id === selected && e.metrics && typeof e.metrics[metric] === "number")
    .reduce<number | null>((best, e) => {
      const v = e.metrics![metric] as number;
      return best === null || (lower ? v < best : v > best) ? v : best;
    }, null) : null;

  // Milestone set AND count (new global bests, chronologically)
  const milestoneIdsSet = (() => {
    if (!metric) return new Set<number>();
    const sorted = done.filter(e => e.metrics && typeof e.metrics[metric] === "number")
      .slice().sort((a, b) => a.id - b.id);
    let best: number | null = null;
    const set = new Set<number>();
    for (const e of sorted) {
      const v = e.metrics![metric] as number;
      if (best === null || (lower ? v < best : v > best)) { best = v; set.add(e.id); }
    }
    return set;
  })();

  // Count milestone experiments (new global bests, chronologically)
  const milestonesCount = (() => {
    if (!metric) return 0;
    const sorted = done.filter(e => e.metrics && typeof e.metrics[metric] === "number")
      .slice().sort((a, b) => a.id - b.id);
    let best: number | null = null;
    let count = 0;
    for (const e of sorted) {
      const v = e.metrics![metric] as number;
      if (best === null || (lower ? v < best : v > best)) { best = v; count++; }
    }
    return count;
  })();

  // Stagnation: if last 10 experiments all score <= 20% of best, flag it
  const isStagnant = (() => {
    if (!metric || !bestVal || typeof bestVal !== "number" || bestVal <= 0) return false;
    const recent10 = done.filter(e => typeof e.metrics?.[metric] === "number")
      .slice(-10).map(e => e.metrics![metric] as number);
    if (recent10.length < 3) return false;
    const recentMax = lower ? Math.min(...recent10) : Math.max(...recent10);
    return lower ? recentMax > bestVal * 5 : recentMax < bestVal * 0.2;
  })();

  // Idea health breakdown for the Ideas disclosure mini-bar
  const ideaList = Object.values(ideas);
  const ideasConcluded = ideaList.filter((i) => i.status === "concluded").length;
  const ideasAbandoned = ideaList.filter((i) => i.status === "abandoned").length;
  const ideasActive    = ideaList.length - ideasConcluded - ideasAbandoned;

  return (
    <div class="review-page">
      {/* ── Status strip ─────────────────────────────────────────────── */}
      <div class="review-status-strip">
        <div class="review-status-left">
          <span class={`review-status-dot ${isLive ? "live" : "idle"}`} />
          <span class="review-status-primary">
            {isLive ? (
              <>
                <ProgressRing pct={avgProgress} />
                <strong>{liveCount}</strong> running{avgProgress > 0 ? ` · ${avgProgress}%` : ""}
                {activeRuns.length > 0 && activeRuns.length <= 4 && (
                  <span class="review-running-list">
                    {activeRuns.map((e) => `exp/${e.label ?? e.id}`).join("  ")}
                  </span>
                )}
              </>
            ) : (
              <span class="review-status-idle">
                idle
                {lastFinishedAt && <span class="review-idle-hint"> · last {timeAgo(lastFinishedAt)}</span>}
                {lastFinishedAt && Date.now() - Date.parse(lastFinishedAt) > 3600_000 && (
                  <span class="review-idle-cta"> — run <code>the-lab-agent</code> to continue</span>
                )}
              </span>
            )}
          </span>
          {queued > 0 && <span class="review-status-item"><strong>{queued}</strong> queued</span>}
          <span class="review-status-item"><strong>{finished}</strong> done</span>
          {failed > 0 && <span class="review-status-item review-status-item--warn"><strong>{failed}</strong> failed</span>}
          <span class="review-status-item"><strong>{activeIdeas}</strong> ideas</span>
          {velocityPerDay && (
            <span class="review-status-item review-status-velocity" title={`Avg experiments per day over last 7 days`}>
              <strong>{velocityPerDay}</strong>/day
            </span>
          )}
          <span class="review-status-sep" />
          <code class="review-status-branch">{branch}</code>
          {cost != null && <span class="review-status-item review-status-item--cost">${cost.toFixed(0)}</span>}
          {campaignAgeDays !== null && campaignAgeDays > 0 && (
            <span class="review-status-item review-campaign-age" title={`Campaign started ${campaignAgeDays} days ago`}>
              {campaignAgeDays}d campaign
            </span>
          )}
        </div>
        {/* 14-day activity sparkline — hidden on mobile */}
        {activityBars.some(h => h > 0) ? (
          <div class="activity-spark" title="Experiment activity over last 14 days (each bar = 1 day)">
            {activityBars.map((h, i) => (
              <span key={i} class="activity-bar" style={{
                height: `${h > 0 ? Math.max(4, Math.round(h * 16)) : 2}px`,
                background: h > 0 ? "var(--accent)" : "var(--border)",
                opacity: h > 0 ? 0.7 + h * 0.3 : 0.25,
              }} />
            ))}
          </div>
        ) : (
          <span class="activity-idle-label">quiet 14d</span>
        )}
        <button class="review-primary-action" onClick={onOpenWorkbench}>Workbench →</button>
      </div>

      {/* ── Experiment grid — glanceable status tiles ────────────────── */}
      <ExperimentGrid experiments={experiments} />

      {/* ── Campaign narrative — one-sentence plain-English state ──────── */}
      {bestVal != null && typeof bestVal === "number" && (
        <div class="review-narrative">
          {(() => {
            const stagnant = expsSinceBest != null && expsSinceBest > 20;
            const stagnantEl = stagnant ? (
              <span style={{ color: expsSinceBest! > 50 ? "var(--red)" : "var(--yellow)", fontWeight: 600 }}>
                no improvement in last {expsSinceBest}
              </span>
            ) : null;
            // Stagnation goes FIRST so it's visible even when truncated on narrow screens
            const parts: preact.ComponentChildren[] = [];
            if (stagnantEl) parts.push(stagnantEl);
            if (isLive) parts.push(`${liveCount} running`);
            else if (lastFinishedAt) parts.push(`idle ${timeAgo(lastFinishedAt)}`);
            if (finished > 0) parts.push(`${finished} exp`);
            if (successRate !== null) parts.push(`${successRate}% scored`);
            if (!stagnantEl && bestExp?.finished_at) parts.push(`best set ${timeAgo(bestExp.finished_at)}`);
            return parts.reduce((acc: preact.ComponentChildren[], p, i) => (
              i === 0 ? [p] : [...acc, <span style={{ opacity: 0.4 }}> · </span>, p]
            ), []);
          })()}
        </div>
      )}

      {/* ── Best score callout ───────────────────────────────────────── */}
      {bestExp != null && bestVal != null && (
        <div class="review-best-bar">
          <span class="review-best-label">best {fmtMetricName(metric)}</span>
          <BestSparkline experiments={done} metric={metric} lower={lower} />
          <strong class="review-best-value">{typeof bestVal === "number" ? bestVal.toFixed(3) : bestVal}</strong>
          <span class="review-best-direction" title={lower ? "lower is better" : "higher is better"}>
            {lower ? "↓ lower better" : "↑ higher better"}
          </span>
          <span class="review-best-meta">
            {(() => {
              const d = bestIdea ? (bestIdea.description?.split("\n")[0] ?? `idea #${bestExp.idea_id}`) : `idea #${bestExp.idea_id}`;
              return d.length > 52 ? d.slice(0, 52).replace(/\s+\S*$/, "") + "…" : d;
            })()}
            {" · "}<code>{bestExp.label ?? `exp/${bestExp.id}`}</code>
            {bestExp.finished_at && (
              <span class="review-best-age"> · {timeAgo(bestExp.finished_at)}</span>
            )}
          </span>
          {expsSinceBest != null && expsSinceBest > 5 && (
            <span class="review-best-since"
              title={`${expsSinceBest} experiments run since the last new best — consider a new approach`}
              style={{ color: expsSinceBest > 50 ? "var(--red)" : expsSinceBest > 20 ? "var(--yellow)" : "var(--text-faint)", fontSize: "var(--text-xs)", fontFamily: "var(--font-mono)" }}>
              +{expsSinceBest} since
            </span>
          )}
        </div>
      )}

      {/* ── Pivot hint — when deeply stagnant ────────────────────────── */}
      {expsSinceBest != null && expsSinceBest > 80 && (
        <div class="review-pivot-hint" title="Consider branching a new idea or exploring under-tried directions">
          <span style={{ color: "var(--red)" }}>⚠</span>
          {" "}stuck for {expsSinceBest} experiments
          {" · "}
          <a href="#review-ops" class="review-pivot-link"
            onClick={(e) => { e.preventDefault(); document.getElementById("review-ops")?.scrollIntoView({ behavior: "smooth" }); }}>
            open queue
          </a>
          {" · "}
          <a href="#review-ideas" class="review-pivot-link"
            onClick={(e) => { e.preventDefault(); document.getElementById("review-ideas")?.scrollIntoView({ behavior: "smooth" }); }}>
            explore ideas
          </a>
        </div>
      )}

      {/* ── Milestone timeline — shows when breakthroughs happened ─────── */}
      {bestExp && milestoneIdsSet.size > 1 && campaignAgeDays && campaignAgeDays > 0 && (
        <div class="review-milestone-timeline" title="Each ★ marks a new best score — shows when progress was made vs. now">
          {(() => {
            const milestones = done
              .filter(e => milestoneIdsSet.has(e.id) && e.finished_at)
              .sort((a, b) => a.id - b.id);
            if (milestones.length < 2) return null;
            const tStart = Date.parse(milestones[0].finished_at!);
            const tNow = Date.now();
            const totalMs = tNow - tStart;
            const lastMs = Date.parse(milestones[milestones.length - 1].finished_at!) - tStart;
            const lastPct = (lastMs / totalMs) * 100;
            return (
              <>
                <div class="rmt-bar">
                  <div class="rmt-active" style={{ width: `${lastPct.toFixed(1)}%` }} />
                  <div class="rmt-idle"   style={{ width: `${(100 - lastPct).toFixed(1)}%` }} />
                  {milestones.map((e) => {
                    const pct = ((Date.parse(e.finished_at!) - tStart) / totalMs) * 100;
                    const v = typeof e.metrics?.[metric] === "number" ? (e.metrics![metric] as number).toFixed(2) : "";
                    return (
                      <span key={e.id} class="rmt-dot" style={{ left: `${pct.toFixed(1)}%` }} title={`${e.label ?? `exp/${e.id}`}: ${v}`} />
                    );
                  })}
                </div>
                <div class="rmt-labels">
                  <span>campaign start</span>
                  <span style={{ marginLeft: `${lastPct.toFixed(1)}%`, transform: "translateX(-50%)" }} class="rmt-last-label">
                    last record · {timeAgo(milestones[milestones.length - 1].finished_at!)}
                  </span>
                  <span style={{ marginLeft: "auto" }}>now</span>
                </div>
              </>
            );
          })()}
        </div>
      )}

      {/* ── Chart — the main signal ──────────────────────────────────── */}
      <div class="review-chart-wrap" id="review-progress">
        <MetricsChart />
        <a class="review-chart-skip" href="#review-runs" onClick={(e) => {
          e.preventDefault();
          document.getElementById("review-runs")?.scrollIntoView({ behavior: "smooth", block: "start" });
        }}>↓ experiments & ideas</a>
      </div>

      {/* ── Collapsible detail sections — Experiments first (results > structure) ── */}
      <div class="review-stack">
        <ReviewDisclosure
          id="review-runs"
          title="Experiments"
          action={[
            `${finished} done`,
            running > 0 ? `${running} running` : null,
            failed > 0 ? `${failed} failed` : null,
            successRate !== null ? `${successRate}% scored` : null,
            milestonesCount > 0 ? `${milestonesCount} records` : null,
            scoreTrend && velocityPerDay ? `trend ${scoreTrend}` : null,
            isStagnant ? "⚠ stagnant" : null,
          ].filter(Boolean).join(" · ")}
          preview={
            <div class="emr-preview">
              {/* Score distribution histogram — how are scores spread? */}
              <ScoreDistBar experiments={experiments} metric={metric} lower={lower} />
              <ExpMiniResults
                experiments={experiments}
                metric={metric}
                lower={lower}
                milestoneIds={milestoneIdsSet}
                ideas={ideas}
              />
            </div>
          }
        >
          <div class="review-panel review-table-panel">
            <TablePanel />
          </div>
        </ReviewDisclosure>

        <ReviewDisclosure
          id="review-ideas"
          title="Ideas"
          action={(() => {
            const avgDepth = activeIdeas > 0 ? (finished / activeIdeas).toFixed(1) : null;
            return `${activeIdeas} active · ${ideasConcluded} concluded${ideasAbandoned > 0 ? ` · ${ideasAbandoned} abandoned` : ""}`;
          })()}
          preview={
            <div class="emr-preview">
              {/* Exploration ring + health bar row */}
              <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
                <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 2 }}>
                  <IdeaRing active={ideasActive} concluded={ideasConcluded} abandoned={ideasAbandoned} />
                  {ideasConcluded + ideasActive + ideasAbandoned > 0 && (
                    <span style={{ fontSize: "8px", color: "var(--text-faint)", fontFamily: "var(--font-mono, monospace)", whiteSpace: "nowrap", textAlign: "center" }}>
                      {Math.round((ideasConcluded / (ideasConcluded + ideasActive + ideasAbandoned)) * 100)}% done
                      {(() => {
                        const avgDepth = activeIdeas > 0 ? (finished / activeIdeas).toFixed(1) : null;
                        return avgDepth ? <> · ~{avgDepth}/idea</> : null;
                      })()}
                    </span>
                  )}
                </div>
                <div style={{ flex: 1 }}>
                  <div class="idea-health-bar">
                    {ideasActive > 0    && <span class="ihb-seg ihb-active"    style={{ flex: ideasActive }}    title={`${ideasActive} active`} />}
                    {ideasConcluded > 0 && <span class="ihb-seg ihb-concluded" style={{ flex: ideasConcluded }} title={`${ideasConcluded} concluded`} />}
                    {ideasAbandoned > 0 && <span class="ihb-seg ihb-abandoned" style={{ flex: ideasAbandoned }} title={`${ideasAbandoned} abandoned`} />}
                  </div>
                </div>
              </div>
              <IdeaMiniLeaderboard
                experiments={experiments}
                ideas={ideas}
                metric={metric}
                lower={lower}
              />
            </div>
          }
        >
          <div class="review-panel review-map-panel">
            <DagView />
          </div>
        </ReviewDisclosure>

        <ReviewDisclosure
          id="review-compare"
          title="Scatter"
          action={(() => {
            const xk = scatterXMetric.value || metric;
            const yk = scatterYMetric.value || "elapsed_s";
            const pairs = done.filter(e => typeof e.metrics?.[xk] === "number" && typeof e.metrics?.[yk] === "number");
            if (pairs.length < 5) return `${fmtMetricName(xk)} × ${fmtMetricName(yk)}`;
            const xs = pairs.map(e => e.metrics![xk] as number);
            const ys = pairs.map(e => e.metrics![yk] as number);
            const mx = xs.reduce((a,b) => a+b, 0) / xs.length;
            const my = ys.reduce((a,b) => a+b, 0) / ys.length;
            const num = xs.reduce((s, x, i) => s + (x - mx) * (ys[i] - my), 0);
            const den = Math.sqrt(xs.reduce((s,x) => s + (x-mx)**2, 0) * ys.reduce((s,y) => s + (y-my)**2, 0));
            const r = den === 0 ? 0 : num / den;
            return `${fmtMetricName(xk)} × ${fmtMetricName(yk)} · ${pairs.length} pts`;
          })()}
          preview={(() => {
            const xk = scatterXMetric.value || metric;
            const yk = scatterYMetric.value || "elapsed_s";
            const pairs = done.filter(e => typeof e.metrics?.[xk] === "number" && typeof e.metrics?.[yk] === "number");
            if (pairs.length < 5) return undefined;
            const xs = pairs.map(e => e.metrics![xk] as number);
            const ys = pairs.map(e => e.metrics![yk] as number);
            const mx = xs.reduce((a,b) => a+b, 0) / xs.length;
            const my = ys.reduce((a,b) => a+b, 0) / ys.length;
            const num = xs.reduce((s, x, i) => s + (x - mx) * (ys[i] - my), 0);
            const den = Math.sqrt(xs.reduce((s,x) => s + (x-mx)**2, 0) * ys.reduce((s,y) => s + (y-my)**2, 0));
            const r = den === 0 ? 0 : num / den;
            const absR = Math.abs(r);
            const color = absR > 0.7 ? "var(--green)" : absR > 0.3 ? "var(--yellow)" : "var(--text-faint)";
            const label = absR < 0.15 ? "r≈0" : `r=${r.toFixed(2)}`;
            const dir = r > 0 ? "↑" : r < 0 ? "↓" : "";
            return (
              <span style={{ display: "inline-flex", alignItems: "center", gap: 3, background: "var(--bg-hi)", borderRadius: 4, padding: "1px 6px", fontSize: "9px", fontFamily: "var(--font-mono)", color, fontWeight: 600 }}>
                {label}{dir && <span style={{ color, opacity: 0.8 }}>{dir}</span>}
              </span>
            );
          })()}
        >
          <div class="review-panel review-scatter-panel">
            <ScatterChart />
          </div>
        </ReviewDisclosure>

        <div class="review-section-sep">explore</div>

        <ReviewDisclosure
          id="review-detail"
          title="Idea detail"
          autoOpen={!!selected}
          action={selected
            ? `idea #${selected}${selectedIdeaBest != null ? ` · best: ${selectedIdeaBest.toFixed(3)}` : ""}`
            : "none selected"}
          preview={!selected ? (
            <span style={{ fontSize: "8px", color: "var(--text-faint)", fontStyle: "italic", fontFamily: "var(--font-mono)" }}>
              click any idea above ↑
            </span>
          ) : undefined}
        >
          <div class="review-panel review-detail-panel">
            <DetailPanel />
          </div>
        </ReviewDisclosure>

        <div class="review-section-sep">operations</div>

        <ReviewDisclosure
          id="review-ops"
          title="Queue"
          action={queued + liveCount > 0
            ? `${queued} queued · ${liveCount} running`
            : `ready · ${liveCount} running`}
          preview={
            queued + liveCount > 0 ? (
              <div class="idea-health-bar">
                {liveCount > 0 && <span class="ihb-seg ihb-active" style={{ flex: liveCount }} title={`${liveCount} running`} />}
                {queued > 0    && <span class="ihb-seg ihb-queued" style={{ flex: queued }}    title={`${queued} queued`} />}
              </div>
            ) : undefined
          }
        >
          <div class="review-panel review-ops-panel">
            <QueueView />
          </div>
        </ReviewDisclosure>

        <ReviewDisclosure
          id="review-log"
          title="Log"
          action={logs.length > 0
            ? `${logs.length} events · last: ${logs[0].title?.slice(0, 40) || logs[0].type}`
            : "no events yet"}
          preview={logs.length > 0 ? (
            <div style={{ display: "flex", gap: 3, alignItems: "center" }}>
              {logs.slice(0, 6).map((e, i) => {
                const c = e.type === "experiment_completed" ? "var(--green)"
                  : e.type === "experiment_failed" ? "var(--red)"
                  : e.type === "idea_created" ? "var(--accent)"
                  : "var(--text-faint)";
                return <span key={i} style={{ width: 6, height: 6, borderRadius: "50%", background: c, flexShrink: 0, opacity: 1 - i * 0.12 }} title={e.title || e.type} />;
              })}
            </div>
          ) : undefined}
        >
          <div class="review-panel review-log-panel">
            <LogView />
          </div>
        </ReviewDisclosure>
      </div>
    </div>
  );
}


function ReviewDisclosure({
  id,
  title,
  action,
  preview,
  children,
  autoOpen,
}: {
  id: string;
  title: string;
  action: string;
  preview?: preact.ComponentChildren;
  children: preact.ComponentChildren;
  autoOpen?: boolean;
}) {
  // Use a ref so we can imperatively open/close without re-rendering
  const ref = (el: HTMLDetailsElement | null) => {
    if (!el) return;
    if (autoOpen !== undefined) el.open = autoOpen;
    // Dispatch resize when details opens so inner canvas-based components re-render
    el.addEventListener("toggle", () => {
      if (el.open) window.dispatchEvent(new Event("resize"));
    }, { once: false });
  };
  return (
    <details ref={ref} class="review-disclosure review-section" id={id}>
      <summary>
        <div>
          <h2>{title}</h2>
        </div>
        <div class="review-disclosure-summary">
          <p>{action}</p>
          {preview}
        </div>
      </summary>
      {children}
    </details>
  );
}

function ReviewSummaryPills({ items }: { items: Array<[string, string | number]> }) {
  return (
    <div class="review-summary-pills">
      {items.map(([label, value]) => (
        <span class="review-summary-pill" key={label}>
          <span>{label}</span>
          <b>{value}</b>
        </span>
      ))}
    </div>
  );
}

function ReviewBranchPreview() {
  const graph = graphData.value;
  const layout = currentLayout.value;
  const highlighted = highlightedIdea.value;
  if (!graph || !layout || graph.nodes.length === 0) {
    return <div class="review-branch-empty">no ideas</div>;
  }

  const width = 360;
  const height = 64;
  const pad = 9;
  const maxDepth = Math.max(1, layout.maxDepth);
  const maxLane = Math.max(1, layout.numLanes - 1);

  const pos = (id: number) => ({
    x: pad + ((layout.depth[id] ?? 0) / maxDepth) * (width - pad * 2),
    y: pad + ((layout.laneRow[layout.ideaLane[id]] ?? 0) / maxLane) * (height - pad * 2),
  });

  return (
    <svg class="review-branch-preview" viewBox={`0 0 ${width} ${height}`} role="img" aria-label="Branch map preview">
      {graph.edges.map((e) => {
        if (!layout.nodeMap[e.from] || !layout.nodeMap[e.to]) return null;
        const a = pos(e.from);
        const b = pos(e.to);
        const active = highlighted === e.from || highlighted === e.to;
        return (
          <path
            key={`${e.from}-${e.to}`}
            d={`M ${a.x} ${a.y} C ${(a.x + b.x) / 2} ${a.y}, ${(a.x + b.x) / 2} ${b.y}, ${b.x} ${b.y}`}
            class={active ? "active" : ""}
          />
        );
      })}
      {graph.nodes.map((n) => {
        const p = pos(n.id);
        const status = n.has_running ? "running" : n.has_queued ? "queued" : n.status;
        const active = highlighted === n.id;
        return (
          <circle
            key={n.id}
            cx={p.x}
            cy={p.y}
            r={active ? 5 : 3.6}
            fill={getStatusColor(status)}
            class={active ? "active" : ""}
            onMouseEnter={() => { highlightedIdea.value = n.id; }}
            onMouseLeave={() => { if (highlightedIdea.value === n.id) highlightedIdea.value = null; }}
            onClick={(e) => { e.preventDefault(); navigateToIdea(n.id); }}
          >
            <title>{`idea/${n.id} · ${n.description}`}</title>
          </circle>
        );
      })}
    </svg>
  );
}
