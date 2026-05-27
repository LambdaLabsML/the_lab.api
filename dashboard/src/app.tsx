import { useEffect, useRef, useCallback } from "preact/hooks";
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
import { setActivatePanel, setCloneChartPanel, setUpdatePanelTitle, setSendToTray } from "./state/signals";
import { initTouchMoveMenu } from "./lib/touch-move-menu";

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
    maxBtn.title = "Maximize panel";
    maxBtn.textContent = "⤢";

    const updateMaxBtn = () => {
      const dv = (group.api as any).accessor as DockviewComponent;
      const isMax = dv.isMaximizedGroup(group);
      maxBtn.textContent = isMax ? "⤡" : "⤢";
      maxBtn.title = isMax ? "Restore panel" : "Maximize panel";
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

// Mobile/narrow layout: all panels stacked vertically in two groups
function buildMobileLayout(dv: DockviewComponent) {
  const top = dv.addPanel({ id: "graph", component: "default", title: "Graph" });
  dv.addPanel({ id: "metrics", component: "default", title: "Metrics", position: { referencePanel: top } });
  dv.addPanel({ id: "scatter", component: "default", title: "Scatter", position: { referencePanel: top } });
  dv.addPanel({ id: "timeline", component: "default", title: "Timeline", position: { referencePanel: top } });
  dv.addPanel({ id: "log", component: "default", title: "Log", position: { referencePanel: top } });
  dv.addPanel({ id: "table", component: "default", title: "Table", position: { referencePanel: top } });

  const bottom = dv.addPanel({
    id: "detail", component: "default", title: "Detail",
    position: { referencePanel: top, direction: "below" },
  });
  dv.addPanel({ id: "api", component: "default", title: "API", position: { referencePanel: bottom } });
  dv.addPanel({ id: "filters", component: "default", title: "Filters", position: { referencePanel: bottom } });
  dv.addPanel({ id: "stats", component: "default", title: "Stats", position: { referencePanel: bottom } });
  dv.addPanel({ id: "suggest", component: "default", title: "Suggest", position: { referencePanel: bottom } });
  dv.addPanel({ id: "task", component: "default", title: "Task", position: { referencePanel: bottom } });
  dv.addPanel({ id: "sandbox", component: "default", title: "Sandbox", position: { referencePanel: bottom } });
  dv.addPanel({ id: "prompts", component: "default", title: "Prompts", position: { referencePanel: bottom } });
  dv.addPanel({ id: "agents", component: "default", title: "Agents", position: { referencePanel: bottom } });
  dv.addPanel({ id: "queue", component: "default", title: "Queue", position: { referencePanel: bottom } });
}

const NARROW_BREAKPOINT = 800;

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

  // Initialize dockview
  useEffect(() => {
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
  }, []);

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
    if (!dv) return;
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
    dv.clear();
    buildDefaultLayout(dv);
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

  return (
    <>
      <Topbar
        onResetLayout={handleResetLayout}
        onSaveLayout={handleSaveLayout}
        onLoadLayout={handleLoadLayout}
        onDeleteLayout={handleDeleteLayout}
        getSavedLayouts={getSavedLayouts}
      />
      <div
        id="dockview-container"
        ref={containerRef}
      />
      <div class="panel-tray">
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
      <ChatPanel />
    </>
  );
}
