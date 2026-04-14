import { useEffect, useRef, useCallback } from "preact/hooks";
import { effect, signal } from "@preact/signals";
import { render } from "preact";

// Tracks which panels are available to add — closed panels + overlay candidates when maximized
const availablePanels = signal<string[]>([]);
const isMaximized = signal(false);
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
import { DetailPanel } from "./components/detail-panel";
import { MetricsChart } from "./components/chart-panel/metrics-chart";
import { ScatterChart } from "./components/chart-panel/scatter-chart";
import { FilterBar } from "./components/filter-bar";
import {
  currentView, selectedIdea, selectedMetric, colorMode,
  improvementsOnly, activeTagFilters, tagFilterMode, reverseTime,
  showAbandoned, showConcluded, showRunning,
  applyServerDefaults,
  dashboardLayout,
} from "./state/settings";
import { startPolling, stopPolling } from "./state/polling";
import { setActivatePanel, setCloneChartPanel, setUpdatePanelTitle } from "./state/signals";
import { initTouchMoveMenu } from "./lib/touch-move-menu";

// ---------------------------------------------------------------------------
// Panel component map — maps panel ID to Preact component
// ---------------------------------------------------------------------------

const PANEL_NAMES: Record<string, string> = {
  graph: "Graph", timeline: "Timeline", log: "Log",
  api: "API", stats: "Stats", sandbox: "Sandbox",
  metrics: "Metrics", scatter: "Scatter", detail: "Detail",
  filters: "Filters", suggest: "Suggest", task: "Task",
};

const ALL_PANEL_IDS = Object.keys(PANEL_NAMES);

const PANEL_MAP: Record<string, (params?: any) => preact.JSX.Element> = {
  graph: () => <DagView />,
  timeline: () => <TimelineView />,
  log: () => <LogView />,
  api: () => <ApiView />,
  stats: () => <StatsView />,
  sandbox: () => <SandboxView />,
  metrics: (p?: any) => <MetricsChart instanceId={p?.instanceId} initialMetric={p?.metric} />,
  scatter: (p?: any) => <ScatterChart instanceId={p?.instanceId} initialXMetric={p?.xMetric} initialYMetric={p?.yMetric} />,
  detail: () => <DetailPanel />,
  filters: () => <FilterBar />,
  suggest: () => <SuggestPanel />,
  task: () => <TaskBanner />,
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

// Default graph fullscreen workspace for new users
const DEFAULT_GRAPH_FULLSCREEN = {"grid":{"root":{"type":"branch","data":[{"type":"leaf","data":{"views":["graph","timeline","log","sandbox"],"activeView":"graph","id":"4"},"size":825},{"type":"leaf","data":{"views":["api","stats","suggest"],"activeView":"suggest","id":"3"},"size":826}],"size":994},"width":1651,"height":994,"orientation":"HORIZONTAL","maximizedNode":{"location":[0]}},"panels":{"graph":{"id":"graph","contentComponent":"default","title":"Graph"},"timeline":{"id":"timeline","contentComponent":"default","title":"Timeline"},"log":{"id":"log","contentComponent":"default","title":"Log"},"sandbox":{"id":"sandbox","contentComponent":"default","title":"Sandbox"},"api":{"id":"api","contentComponent":"default","title":"API"},"stats":{"id":"stats","contentComponent":"default","title":"Stats"},"suggest":{"id":"suggest","contentComponent":"default","title":"Suggest"},"detail":{"id":"detail","contentComponent":"default","title":"Detail"},"metrics":{"id":"metrics","contentComponent":"default","title":"Metrics"},"scatter":{"id":"scatter","contentComponent":"default","title":"Scatter"},"filters":{"id":"filters","contentComponent":"default","title":"Filters"}},"activeGroup":"4","floatingGroups":[{"data":{"views":["detail"],"activeView":"detail","id":"2"},"position":{"bottom":278.672,"right":40,"width":500,"height":368.390625}},{"data":{"views":["metrics","scatter"],"activeView":"metrics","id":"7"},"position":{"bottom":14.2031,"left":40.2031,"width":1059.75,"height":312.703125}},{"data":{"views":["filters"],"activeView":"filters","id":"1"},"position":{"bottom":11.9531,"right":38.6562,"width":500.0625,"height":255.671875}}]};

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
          const savedLayout = layouts[panelId] || (panelId === "graph" ? DEFAULT_GRAPH_FULLSCREEN : null);
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

// Default layout JSON — captured from the user's preferred arrangement:
// Top row: Task | Suggest | Filters
// Middle row: Metrics + Scatter (tabbed)
// Bottom row: Graph/Timeline/Log | Detail/API/Stats/Sandbox
const DEFAULT_LAYOUT: SerializedDockview = {"grid":{"root":{"type":"branch","data":[{"type":"branch","data":[{"type":"leaf","data":{"views":["task"],"activeView":"task","id":"11"},"size":559},{"type":"leaf","data":{"views":["suggest"],"activeView":"suggest","id":"13"},"size":541},{"type":"leaf","data":{"views":["filters"],"activeView":"filters","id":"12"},"size":551}],"size":153},{"type":"leaf","data":{"views":["metrics","scatter"],"activeView":"metrics","id":"9"},"size":249},{"type":"branch","data":[{"type":"leaf","data":{"views":["graph","timeline","log"],"activeView":"graph","id":"5"},"size":928},{"type":"leaf","data":{"views":["detail","api","stats","sandbox"],"activeView":"detail","id":"7"},"size":723}],"size":592}],"size":1651},"width":1651,"height":994,"orientation":"VERTICAL"},"panels":{"task":{"id":"task","contentComponent":"default","title":"Task"},"suggest":{"id":"suggest","contentComponent":"default","title":"Suggest"},"filters":{"id":"filters","contentComponent":"default","title":"Filters"},"metrics":{"id":"metrics","contentComponent":"default","title":"Metrics"},"scatter":{"id":"scatter","contentComponent":"default","title":"Scatter"},"graph":{"id":"graph","contentComponent":"default","title":"Graph"},"timeline":{"id":"timeline","contentComponent":"default","title":"Timeline"},"log":{"id":"log","contentComponent":"default","title":"Log"},"detail":{"id":"detail","contentComponent":"default","title":"Detail"},"api":{"id":"api","contentComponent":"default","title":"API"},"stats":{"id":"stats","contentComponent":"default","title":"Stats"},"sandbox":{"id":"sandbox","contentComponent":"default","title":"Sandbox"}},"activeGroup":"5"} as any;

// Mobile/narrow layout: all panels stacked vertically in two groups
function buildMobileLayout(dv: DockviewComponent) {
  const top = dv.addPanel({ id: "graph", component: "default", title: "Graph" });
  dv.addPanel({ id: "metrics", component: "default", title: "Metrics", position: { referencePanel: top } });
  dv.addPanel({ id: "scatter", component: "default", title: "Scatter", position: { referencePanel: top } });
  dv.addPanel({ id: "timeline", component: "default", title: "Timeline", position: { referencePanel: top } });
  dv.addPanel({ id: "log", component: "default", title: "Log", position: { referencePanel: top } });

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

function readFiltersFromUrl() {
  const path = window.location.pathname;
  const params = new URLSearchParams(window.location.search);

  const m = path.match(/^\/ideas\/(\d+)/);
  if (m) {
    currentView.value = "dag";
    selectedIdea.value = parseInt(m[1]);
  } else if (path.match(/^\/experiments\/(\d+)/)) {
    currentView.value = "dag";
  } else {
    const viewMap: Record<string, string> = {
      "/": "dag", "/dag": "dag", "/graph": "dag",
      "/timeline": "timeline", "/log": "log", "/api": "api", "/stats": "stats", "/sandbox": "sandbox",
    };
    if (viewMap[path]) currentView.value = viewMap[path];
  }

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

function syncUrlFromSignals() {
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
    dag: "/graph", timeline: "/timeline", log: "/log", api: "/api", stats: "/stats", sandbox: "/sandbox",
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

    // Save layout on changes
    const layoutDisposable = dv.onDidLayoutChange(() => {
      try {
        dashboardLayout.value = dv.toJSON();
      } catch {
        // Ignore serialization errors
      }
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

    const addDisposable = dv.onDidAddPanel(() => {
      try { dashboardLayout.value = dv.toJSON(); } catch { /* ignore */ }
      updateAvailablePanels();
    });

    const removeDisposable = dv.onDidRemovePanel(() => {
      try { dashboardLayout.value = dv.toJSON(); } catch { /* ignore */ }
      updateAvailablePanels();
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

    // ── Feature: click empty tab-bar space to collapse/expand group ──
    // Keyed by group ID (stable). After every collapse/expand, we re-enforce
    // TAB_BAR_HEIGHT on all still-collapsed rows so dockview doesn't
    // redistribute space into them.
    const collapsedInfo = new Map<string, { siblingIds: string[]; origHeight: number }>();
    const TAB_BAR_HEIGHT = 35;

    function getRowSiblings(group: any): any[] {
      const top = Math.round(group.element.getBoundingClientRect().top);
      return dv.groups.filter((g) => {
        const gTop = Math.round(g.element.getBoundingClientRect().top);
        return Math.abs(gTop - top) < 5;
      });
    }

    /** Re-enforce collapsed height on all still-collapsed rows. */
    function reEnforceCollapsed() {
      // Collect unique rows that are still collapsed (dedupe by siblingIds set)
      const seen = new Set<string>();
      for (const [gid, info] of collapsedInfo) {
        const key = info.siblingIds.sort().join(",");
        if (seen.has(key)) continue;
        seen.add(key);
        const g = dv.groups.find((gg) => gg.id === gid);
        if (g) {
          g.api.setConstraints({ minimumHeight: TAB_BAR_HEIGHT, maximumHeight: TAB_BAR_HEIGHT });
          g.api.setSize({ height: TAB_BAR_HEIGHT });
        }
      }
    }

    /** Clear the max-height lock on collapsed groups (call before expanding). */
    function unlockCollapsedMaxHeight() {
      for (const [gid] of collapsedInfo) {
        const g = dv.groups.find((gg) => gg.id === gid);
        if (g) g.api.setConstraints({ maximumHeight: undefined as any });
      }
    }

    function onVoidClick(e: MouseEvent) {
      const voidEl = (e.target as HTMLElement).closest(".dv-void-container");
      if (!voidEl) return;
      const groupEl = voidEl.closest(".dv-groupview") as HTMLElement | null;
      if (!groupEl) return;
      const group = dv.groups.find((g) => g.element === groupEl || g.element.contains(groupEl));
      if (!group) return;

      if (collapsedInfo.has(group.id)) {
        // Expand: unlock max-height on all collapsed, restore this row
        unlockCollapsedMaxHeight();
        const info = collapsedInfo.get(group.id)!;
        for (const id of info.siblingIds) {
          collapsedInfo.delete(id);
          const g = dv.groups.find((gg) => gg.id === id);
          if (g) g.api.setConstraints({ minimumHeight: undefined as any, maximumHeight: undefined as any });
        }
        group.api.setSize({ height: info.origHeight });
        // Re-lock remaining collapsed rows after layout settles
        requestAnimationFrame(reEnforceCollapsed);
      } else {
        // Collapse: find siblings, store info, shrink, then lock all collapsed
        const siblings = getRowSiblings(group);
        const currentHeight = group.height;
        if (currentHeight > TAB_BAR_HEIGHT + 10) {
          const siblingIds = siblings.map((g) => g.id);
          for (const g of siblings) {
            collapsedInfo.set(g.id, { siblingIds, origHeight: currentHeight });
          }
          // Set both min and max to lock collapsed rows at TAB_BAR_HEIGHT
          for (const g of siblings) {
            g.api.setConstraints({ minimumHeight: TAB_BAR_HEIGHT, maximumHeight: TAB_BAR_HEIGHT });
          }
          group.api.setSize({ height: TAB_BAR_HEIGHT });
          // Re-enforce on next frame in case dockview redistributed
          requestAnimationFrame(reEnforceCollapsed);
        }
      }
    }
    container.addEventListener("click", onVoidClick);

    return () => {
      container.removeEventListener("click", onVoidClick);
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
    const dispose = effect(syncUrlFromSignals);
    return () => {
      window.removeEventListener("popstate", readFiltersFromUrl);
      stopPolling();
      dispose();
    };
  }, []);

  // --- Layout management ---

  const SAVED_LAYOUTS_KEY = "the-lab:savedLayouts";

  const handleResetLayout = useCallback(() => {
    const dv = dockviewRef.current;
    if (!dv) return;
    dv.clear();
    buildDefaultLayout(dv);
    dashboardLayout.value = null;
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

  const getClosedPanels = useCallback((): string[] => {
    return availablePanels.value;
  }, []);

  const handleAddPanel = useCallback((id: string) => {
    const dv = dockviewRef.current;
    if (!dv) return;
    const title = PANEL_NAMES[id] || id;
    const maxGroup = dv.groups.find((g) => dv.isMaximizedGroup(g));

    if (maxGroup) {
      // Maximized mode: add as floating on top.
      // Dockview exits maximize whenever a non-maximized group activates,
      // so we re-maximize after the operation.
      const w = Math.min(500, window.innerWidth * 0.4);
      const h = Math.min(400, window.innerHeight * 0.4);
      const floatCount = dv.groups.filter((g) => g.api.location.type === "floating").length;
      const offset = floatCount * 30;
      const pos = { x: 60 + offset, y: 60 + offset, width: w, height: h };

      const existingPanel = dv.panels.find((p) => p.id === id);
      if (existingPanel && existingPanel.group.api.location.type === "floating") {
        existingPanel.api.setActive();
        return;
      }

      const floatGroup = dv.addGroup();
      dv.addFloatingGroup(floatGroup, pos);

      if (existingPanel) {
        dv.moveGroupOrPanel({
          from: { groupId: existingPanel.group.id, panelId: existingPanel.id },
          to: { group: floatGroup, position: "center" },
        });
      } else {
        dv.addPanel({
          id, component: "default", title,
          position: { referenceGroup: floatGroup },
        });
      }

      // Re-maximize — dockview exited it during addGroup/addPanel
      requestAnimationFrame(() => {
        if (!dv.hasMaximizedGroup()) {
          dv.maximizeGroup(maxGroup);
        }
      });
      return;
    }

    // Normal mode: only add if closed
    if (dv.panels.some((p) => p.id === id)) return;
    const activeGroup = dv.activeGroup;
    if (activeGroup) {
      dv.addPanel({ id, component: "default", title, position: { referenceGroup: activeGroup } });
    } else {
      dv.addPanel({ id, component: "default", title });
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
        onAddPanel={handleAddPanel}
        getClosedPanels={getClosedPanels}
      />
      <div
        id="dockview-container"
        ref={containerRef}
      />
      <ChatPanel />
    </>
  );
}
