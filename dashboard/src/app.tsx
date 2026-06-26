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

import { ScrollTop } from "./components/scroll-top";
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
import { Eyebrow, Stat, Tooltip } from "./components/ui";
import { NavRail, SecondaryPanel, type NavSection } from "./components/nav-rail";
import { ActivityPane } from "./components/activity/activity-pane";
import { ActivityShortlog } from "./components/activity/activity-shortlog";
import { SettingsPanel } from "./components/settings-panel";
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

// Review dashboard sections, surfaced as the left secondary-nav list.
const REVIEW_SECTIONS: { id: string; label: string; icon: string }[] = [
  { id: "review-runs",    label: "Experiments", icon: "◉" },
  { id: "review-ideas",   label: "Ideas",       icon: "◈" },
  { id: "review-compare", label: "Correlation", icon: "⊞" },
  { id: "review-detail",  label: "Idea Detail", icon: "▸" },
];

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
  const [navSection, setNavSection] = useState<NavSection>("activity");
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [layoutName, setLayoutName] = useState("");
  const [layoutVersion, setLayoutVersion] = useState(0);

  // Initialize dockview
  useEffect(() => {
    if (navSection !== "workbench") return;
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
  }, [navSection]);

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
  const openReviewSection = useCallback((id: string) => {
    setNavSection("review");
    requestAnimationFrame(() => {
      const el = document.getElementById(id);
      if (el instanceof HTMLDetailsElement) el.open = true;
      el?.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  }, []);

  const showSecondary = settingsOpen || navSection === "review" || navSection === "queue" || navSection === "workbench";
  void layoutVersion; // re-read saved layouts after a save/delete
  const savedLayouts = getSavedLayouts();
  return (
    <div class="app-shell">
      <NavRail
        section={navSection}
        onSelect={(s) => { setNavSection(s); setSettingsOpen(false); }}
        settingsOpen={settingsOpen}
        onToggleSettings={() => setSettingsOpen((v) => !v)}
      />

      {showSecondary && (
        <SecondaryPanel
          label={settingsOpen ? "Settings" : navSection === "workbench" ? "Workbench" : navSection === "queue" ? "Queue" : "Review sections"}
        >
          {settingsOpen ? (
            <SettingsPanel />
          ) : (
            <>
              {navSection === "review" && (
                <>
                  <nav class="nav-secondary-list">
                    <div class="nav-secondary-head"><Eyebrow>Sections</Eyebrow></div>
                    {REVIEW_SECTIONS.map((s) => (
                      <button key={s.id} class="nav-secondary-btn" onClick={() => openReviewSection(s.id)}>
                        <span class="nav-secondary-icon" aria-hidden="true">{s.icon}</span>
                        {s.label}
                      </button>
                    ))}
                  </nav>
                  <ActivityShortlog />
                </>
              )}
              {navSection === "queue" && <ActivityShortlog />}
              {navSection === "workbench" && (
                <nav class="nav-secondary-list">
                  <div class="nav-secondary-head"><Eyebrow>Add panel</Eyebrow></div>
                  {ALL_PANEL_IDS.filter((id) => !_open.has(id)).map((id) => (
                    <button key={id} class="nav-secondary-btn" onClick={() => handleAddPanel(id)} title={`Open ${PANEL_NAMES[id]}`}>
                      <span class="nav-secondary-dot" />
                      {PANEL_NAMES[id]}
                    </button>
                  ))}
                  {ALL_PANEL_IDS.filter((id) => !_open.has(id)).length === 0 && (
                    <div class="nav-secondary-sub"><Eyebrow>all panels open</Eyebrow></div>
                  )}
                  {trayItems.length > 0 && (
                    <>
                      <div class="nav-secondary-sub"><Eyebrow>Floating</Eyebrow></div>
                      {trayItems.map((id) => (
                        <button
                          key={id}
                          class={`nav-secondary-btn${_floating.has(id) ? " is-open" : ""}`}
                          onClick={() => handleToggleTrayPanel(id)}
                          title={PANEL_NAMES[id] || id}
                        >
                          <span class="nav-secondary-dot" data-open={_floating.has(id) ? "true" : undefined} />
                          {PANEL_NAMES[id] || id}
                        </button>
                      ))}
                    </>
                  )}

                  <div class="nav-secondary-sub"><Eyebrow>Layouts</Eyebrow></div>
                  <div class="nav-layout-save">
                    <input
                      class="nav-layout-input"
                      placeholder="save as…"
                      value={layoutName}
                      onInput={(e) => setLayoutName((e.target as HTMLInputElement).value)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" && layoutName.trim()) {
                          handleSaveLayout(layoutName.trim()); setLayoutName(""); setLayoutVersion((v) => v + 1);
                        }
                      }}
                    />
                    <button
                      class="ui-btn ui-btn--outlined"
                      onClick={() => { if (layoutName.trim()) { handleSaveLayout(layoutName.trim()); setLayoutName(""); setLayoutVersion((v) => v + 1); } }}
                    >
                      Save
                    </button>
                  </div>
                  {savedLayouts.map((name) => (
                    <div class="nav-layout-row" key={name}>
                      <button class="nav-secondary-btn nav-layout-load" onClick={() => handleLoadLayout(name)} title={`Load "${name}"`}>
                        <span class="nav-secondary-icon" aria-hidden="true">▤</span>
                        {name}
                      </button>
                      <button
                        class="nav-layout-del"
                        onClick={() => { handleDeleteLayout(name); setLayoutVersion((v) => v + 1); }}
                        title="Delete layout"
                        aria-label={`Delete layout ${name}`}
                      >
                        ×
                      </button>
                    </div>
                  ))}
                  <button class="nav-secondary-btn" onClick={handleResetLayout} title="Reset to the default layout">
                    <span class="nav-secondary-icon" aria-hidden="true">↺</span>
                    Reset layout
                  </button>
                </nav>
              )}
            </>
          )}
        </SecondaryPanel>
      )}

      <div class="app-main">
        <div class="app-content">
          {navSection === "activity" && <ActivityPane />}

          {navSection === "review" && (
            <div class="app-scroll">
              <main class="dashboard-page">
                <ReviewDashboard onOpenWorkbench={() => setNavSection("workbench")} />
              </main>
            </div>
          )}

          {navSection === "queue" && (
            <div class="app-scroll">
              <main class="dashboard-page">
                <QueueView />
              </main>
            </div>
          )}

          {navSection === "workbench" && (
            <section class="workspace-shell" aria-label="Dashboard workspace">
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
              <div id="dockview-container" ref={containerRef} />
            </section>
          )}

          <ScrollTop />
        </div>
      </div>
    </div>
  );
}

// ── Idea mini leaderboard ─────────────────────────────────────────────────────

function IdeaMiniLeaderboard({ experiments, ideas, metric, lower }: {
  experiments: import("./lib/types").Experiment[];
  ideas: Record<number, import("./lib/types").IdeaNode>;
  metric: string;
  lower: boolean;
}) {
  // When no metric data yet: show most recently active ideas (last experiment ran)
  const hasMetricData = experiments.some(e => !e._running && typeof e.metrics?.[metric] === "number");
  if (!hasMetricData) {
    const ideaLastRun: Record<number, { count: number; lastId: number }> = {};
    for (const e of experiments) {
      if (e._running) continue;
      const cur = ideaLastRun[e.idea_id];
      if (!cur) ideaLastRun[e.idea_id] = { count: 1, lastId: e.id };
      else { cur.count++; if (e.id > cur.lastId) cur.lastId = e.id; }
    }
    // Find untested ideas (never explored) — show FIRST as highest opportunity
    const untestedIdeas = experiments.length > 20
      ? Object.values(ideas).filter(i => i.status !== "concluded" && i.status !== "abandoned" && !ideaLastRun[i.id])
      : [];
    const top5 = Object.entries(ideaLastRun)
      .sort((a, b) => b[1].lastId - a[1].lastId).slice(0, Math.max(0, 5 - untestedIdeas.slice(0,2).length));
    const allRows = [...untestedIdeas.slice(0, 2).map(i => ({ id: String(i.id), data: null, untested: true })),
                     ...top5.map(([id, data]) => ({ id, data, untested: false }))];
    if (allRows.length === 0) return null;
    return (
      <div class="emr-section">
        <span class="emr-label">top ideas{untestedIdeas.length > 0 ? <span style={{ color: "var(--yellow)", marginLeft: 4 }}>◇ {untestedIdeas.length} untried</span> : null}</span>
        <div class="emr-rows">
          {allRows.map(({ id, data, untested }, i) => {
            const idea = ideas[Number(id)];
            const rawDesc = idea?.description?.split("\n")[0] ?? "";
            // For child ideas with generic auto-generated "idea/N (child of...)" descriptions,
            // find the first ancestor with a real description
            const isGenericChildDesc = /^idea\/\d+ \(child of/.test(rawDesc);
            const getAncestorTitle = (ideaObj: typeof idea, depth = 0): string => {
              if (!ideaObj || depth > 3) return rawDesc.slice(0, 40) || `idea #${id}`;
              const desc = ideaObj.description?.split("\n")[0] ?? "";
              if (!/^idea\/\d+ \(child of/.test(desc) && desc.length > 0) return desc.slice(0, 38);
              const parentId = ideaObj.parent_ids?.[0];
              return parentId && ideas[parentId] ? getAncestorTitle(ideas[parentId], depth + 1) : desc.slice(0, 40);
            };
            const title = isGenericChildDesc
              ? `↳ ${getAncestorTitle(idea?.parent_ids?.[0] ? ideas[idea.parent_ids[0]] : undefined)}`
              : rawDesc.slice(0, 40) || `idea #${id}`;
            const isActive = idea?.status !== "concluded" && idea?.status !== "abandoned";
            const hasRunning = !untested && experiments.some(e => (e._running || e.status === "running") && e.idea_id === Number(id));
            return (
              <div key={id} class={`emr-row${i === 0 ? " emr-milestone" : ""}`} style={{ cursor: "pointer", background: untested ? "color-mix(in srgb, var(--yellow) 4%, transparent)" : undefined }}
                onClick={() => navigateToIdea(Number(id))} title={`${title}${hasRunning ? " · running now" : untested ? " · never tried!" : ""}`}>
                <span class="emr-rank">{i + 1}</span>
                <span class="emr-idea">#{id}</span>
                {untested
                  ? <span style={{ fontSize: "var(--text-xs)", color: "var(--yellow)", flexShrink: 0 }}>◇</span>
                  : hasRunning
                    ? <span class="sq-running" style={{ width: 6, height: 6, borderRadius: "50%", flexShrink: 0, display: "inline-block" }} title="running now" />
                    : <span style={{ fontSize: "var(--text-xs)", opacity: 0.7, flexShrink: 0, color: isActive ? "var(--green)" : "var(--accent)" }}>●</span>
                }
                <span class="emr-exp" style={{ color: untested ? "var(--yellow)" : hasRunning ? "var(--text)" : "var(--text-muted)", maxWidth: 128, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", fontWeight: (untested || hasRunning) ? 600 : 400 }}>{title}</span>
                {untested
                  ? <span style={{ fontSize: "var(--text-xs)", color: "var(--yellow)", flexShrink: 0 }}>new!</span>
                  : <span class="emr-count" style={{ color: "var(--text-faint)", fontSize: "var(--text-xs)" }}>{(data as any)?.count}×</span>
                }
              </div>
            );
          })}
        </div>
      </div>
    );
  }

  // Compute best score, count, and last-run time per idea
  const ideaBest: Record<number, { best: number; expLabel: string; expId: number; count: number; lastFinished: string | null }> = {};
  for (const e of experiments) {
    if (e._running) continue;
    if (typeof e.metrics?.[metric] !== "number") continue;
    const v = e.metrics![metric] as number;
    const cur = ideaBest[e.idea_id];
    if (!cur) {
      ideaBest[e.idea_id] = { best: v, expLabel: e.label ?? String(e.id), expId: e.id, count: 1, lastFinished: e.finished_at ?? null };
    } else {
      cur.count++;
      if (e.finished_at && (!cur.lastFinished || e.finished_at > cur.lastFinished)) cur.lastFinished = e.finished_at;
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
    .sort((a, b) => {
      const scoreDiff = lower ? a.best - b.best : b.best - a.best;
      if (Math.abs(scoreDiff) > 1e-9) return scoreDiff; // different scores
      return a.count - b.count; // tied: fewer experiments = more efficient = ranked higher
    })
    .slice(0, 5);

  if (ranked.length === 0) return null;

  function fmtV(v: number) {
    return Math.abs(v) >= 100 ? v.toFixed(0) : Math.abs(v) >= 1 ? v.toFixed(2) : v.toFixed(3);
  }

  // Tiny sparkline for an idea's score history
  function IdeaSparkline({ vals, lower: lo }: { vals: number[]; lower: boolean }) {
    if (vals.length === 0) return null;
    const W = 46, H = 16;
    if (vals.length === 1) {
      return (
        <svg width={W} height={H} style={{ flexShrink: 0, opacity: 0.5 }}>
          <circle cx={W/2} cy={H/2} r={2.4} fill="var(--text-faint)" />
        </svg>
      );
    }
    const lo_ = Math.min(...vals), hi_ = Math.max(...vals);
    const range = hi_ - lo_;
    if (range < 1e-9) {
      // flat line
      return (
        <svg width={W} height={H} style={{ flexShrink: 0, opacity: 0.5 }}>
          <line x1={3} y1={H/2} x2={W-3} y2={H/2} stroke="var(--text-faint)" strokeWidth="1.25" strokeDasharray="2.5 2" />
        </svg>
      );
    }
    const px = (i: number) => 3 + (i / (vals.length - 1)) * (W - 6);
    const py = (v: number) => H - 3 - ((v - lo_) / range) * (H - 6);
    const d = vals.map((v, i) => `${i === 0 ? "M" : "L"}${px(i).toFixed(1)},${py(v).toFixed(1)}`).join(" ");
    const trend = lo ? vals[0] - vals[vals.length - 1] : vals[vals.length - 1] - vals[0];
    const color = trend > range * 0.1 ? "var(--green)" : trend < -range * 0.1 ? "var(--red)" : "var(--text-faint)";
    return (
      <svg width={W} height={H} style={{ flexShrink: 0, opacity: 0.8 }}>
        <path d={d} fill="none" stroke={color} strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    );
  }

  // Compute score range for relative bars
  const bestOverall = ranked[0]?.best ?? 0;
  const worstRanked = ranked[ranked.length - 1]?.best ?? 0;
  const scoreRange = Math.abs(bestOverall - worstRanked);

  return (
    <div class="emr-section emr-section--lb">
      <span class="emr-label">top ideas · best {fmtMetricName(metric)}</span>
      {/* column headers — make the leaderboard read standalone */}
      <div class="emr-row emr-colhead" role="row">
        <span class="emr-rank">#</span>
        <span class="emr-idea">idea</span>
        <span class="emr-exp">title</span>
        <span class="emr-count emr-colhead-runs">runs</span>
        <span class="emr-colhead-trend">trend</span>
        <span class="emr-val">best</span>
      </div>
      <div class="emr-rows">
        {ranked.map((r, i) => {
          const idea = ideas[r.ideaId];
          const rawDesc2 = idea?.description?.split("\n")[0] ?? "";
          const isGeneric2 = /^idea\/\d+ \(child of/.test(rawDesc2);
          const getAncestor2 = (ideaObj: typeof idea, d = 0): string => {
            if (!ideaObj || d > 3) return rawDesc2.slice(0, 40) || `idea #${r.ideaId}`;
            const desc = ideaObj.description?.split("\n")[0] ?? "";
            if (!/^idea\/\d+ \(child of/.test(desc) && desc.length > 0) return desc.slice(0, 38);
            const pid = ideaObj.parent_ids?.[0];
            return pid && ideas[pid] ? getAncestor2(ideas[pid], d + 1) : desc.slice(0, 40);
          };
          const title = isGeneric2
            ? `↳ ${getAncestor2(idea?.parent_ids?.[0] ? ideas[idea.parent_ids[0]] : undefined)}`
            : rawDesc2.slice(0, 40) || `idea #${r.ideaId}`;
          const history = ideaHistory[r.ideaId] ?? [];
          // Relative score bar: 100% = best, proportional for others
          const scorePct = ranked.length > 1 && scoreRange > 1e-9
            ? lower
              ? Math.round(((worstRanked - r.best) / scoreRange) * 80 + 20)
              : Math.round(((r.best - worstRanked) / scoreRange) * 80 + 20)
            : 100;
          return (
            <div key={r.ideaId} class={`emr-row${i === 0 ? " emr-milestone" : ""}`}
              style={{ cursor: "pointer", position: "relative" }}
              onClick={() => navigateToIdea(r.ideaId)}
              title={`idea #${r.ideaId}: ${idea?.description?.split("\n")[0] ?? ""}`}
            >
              {/* Relative score bar at bottom of row */}
              {ranked.length > 1 && (
                <span style={{ position: "absolute", bottom: 0, left: 0, height: 2, width: `${scorePct}%`, background: i === 0 ? "var(--purple)" : "var(--border-soft)", borderRadius: 1, opacity: i === 0 ? 0.5 : 0.35 }} />
              )}
              <span class="emr-rank">{i + 1}</span>
              <span class="emr-idea">#{r.ideaId}</span>
              <span style={{ fontSize: "var(--text-xs)", opacity: 0.7, flexShrink: 0, color: idea?.status === "active" ? "var(--green)" : idea?.status === "concluded" ? "var(--accent)" : "var(--red)" }}>●</span>
              <span class="emr-exp" style={{ color: "var(--text-muted)", maxWidth: 132, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{title}</span>
              <span class="emr-count" style={{ color: r.count < 5 ? "var(--yellow)" : r.count > 30 ? "var(--text-faint)" : "var(--text-muted)" }}>{r.count}</span>
              <IdeaSparkline vals={history} lower={lower} />
              <span class="emr-val">{fmtV(r.best)}</span>
              {r.lastFinished && (() => {
                const hoursAgo = Math.floor((Date.now() - Date.parse(r.lastFinished)) / 3600000);
                const daysAgo = Math.floor(hoursAgo / 24);
                const isHot = hoursAgo < 24;  // ran in last 24h
                const isStale = daysAgo > 7;
                if (isHot) {
                  return (
                    <span class="sq-running" style={{ width: 5, height: 5, borderRadius: "50%", flexShrink: 0, display: "inline-block" }}
                      title={`Last experiment: ${hoursAgo}h ago (active!)`} />
                  );
                }
                return (
                  <span style={{ fontSize: "var(--text-xs)", color: isStale ? "var(--yellow)" : "var(--text-faint)", flexShrink: 0, fontFamily: "var(--font-mono)" }}
                    title={`Last experiment: ${daysAgo}d ago`}>
                    {daysAgo}d
                  </span>
                );
              })()}
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

function ExperimentGrid({ experiments, successRate, milestoneIds, ideas, metric }: {
  experiments: import("./lib/types").Experiment[];
  successRate: number | null;
  milestoneIds?: Set<number>;
  ideas: Record<number, import("./lib/types").IdeaNode>;
  metric: string;
}) {
  const highlighted = highlightedIdea.value;
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
  const hasMilestones = milestoneIds && milestoneIds.size > 0;
  // Find the last 8 completed experiment IDs (by id) for recency highlighting
  const recentIds = new Set(
    chrono.filter(e => !e._running && e.status !== "running").slice(-8).map(e => e.id)
  );

  const STATUS_WORD: Record<string, string> = {
    running: "running", completed: "done", active: "done", failed: "failed",
    abandoned: "abandoned", concluded: "concluded", queued: "queued",
    pending: "queued", cancelled: "cancelled",
  };
  function fmtScore(v: number) {
    return Math.abs(v) >= 100 ? v.toFixed(0) : Math.abs(v) >= 1 ? v.toFixed(2) : v.toFixed(3);
  }

  return (
    <div class="exp-grid-wrap">
      {/* Caption — say what this strip is before the reader has to guess. */}
      <div class="exp-grid-caption">
        <span class="emr-label">experiment timeline</span>
        <span class="exp-grid-caption-sub">each ▪ = one run · grouped &amp; colored by idea · left→right = oldest→newest</span>
      </div>
      <div class="exp-grid">
        {ideaOrder.map((ideaId, gi) => {
          const exps = byIdea.get(ideaId)!;
          const ideaCol = ideaColor[ideaId];
          const idea = ideas[ideaId];
          const ideaText = idea?.description?.split("\n")[0] ?? "";
          const dim = highlighted != null && highlighted !== ideaId;
          const on = highlighted === ideaId;
          return (
            <span
              key={ideaId}
              class={`exp-idea-group${on ? " is-highlighted" : ""}${dim ? " is-dimmed" : ""}`}
              style={{ "--idea-color": ideaCol, marginLeft: gi > 0 ? 5 : 0, cursor: "pointer" } as any}
              onMouseEnter={() => { highlightedIdea.value = ideaId; }}
              onMouseLeave={() => { if (highlightedIdea.value === ideaId) highlightedIdea.value = null; }}
              onClick={() => navigateToIdea(ideaId)}
            >
              {exps.map((e) => {
                const status = e._running ? "running" : (e.status ?? "unknown");
                const isMilestone = milestoneIds?.has(e.id);
                const isRecent = recentIds.has(e.id);
                const cls = `exp-grid-sq ${STATUS_SQ_CLASS[status] ?? "sq-cancelled"}${isMilestone ? " sq-milestone" : ""}${isRecent ? " sq-recent" : ""}`;
                const score = e.metrics?.[metric];
                return (
                  <Tooltip
                    key={e.id}
                    content={
                      <>
                        <span class="ui-tip-title">exp/{e.label ?? e.id}{isMilestone ? " ★" : ""}</span>
                        <span class="ui-tip-row">idea <b>#{e.idea_id}</b>{ideaText ? <span class="ui-tip-dim"> {ideaText.slice(0, 48)}</span> : null}</span>
                        <span class="ui-tip-row">status <b>{STATUS_WORD[status] ?? status}</b></span>
                        {typeof score === "number" && isFinite(score) && (
                          <span class="ui-tip-row">{fmtMetricName(metric)} <b>{fmtScore(score)}</b></span>
                        )}
                        {isMilestone && <span class="ui-tip-row" style={{ color: "var(--purple)" }}>★ new record</span>}
                      </>
                    }
                  >
                    <span class={cls} />
                  </Tooltip>
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
        {hasMilestones && <span class="sq-legend sq-milestone">★ record</span>}
        <span class="sq-legend sq-recent">recent</span>
        {successRate !== null && (() => {
          const r = 7, sw = 2.5, size = 18;
          const c = 2 * Math.PI * r;
          const fill = (successRate / 100) * c;
          const arcColor = successRate < 10 ? "var(--red)" : successRate < 20 ? "var(--yellow)" : "var(--green)";
          return (
            <span class="exp-grid-success" title={`${successRate}% of experiments scored above zero`}>
              <svg width={size} height={size} style={{ display: "block", opacity: 0.8 }}>
                <circle cx={size/2} cy={size/2} r={r} fill="none" stroke="var(--border-soft)" strokeWidth={sw} />
                <circle cx={size/2} cy={size/2} r={r} fill="none" stroke={arcColor} strokeWidth={sw}
                  strokeDasharray={`${fill} ${c - fill}`}
                  strokeDashoffset={c / 4}
                  strokeLinecap="round"
                  transform={`rotate(-90 ${size/2} ${size/2})`}
                />
              </svg>
              <span style={{ fontSize: "var(--text-xs)", color: arcColor, fontFamily: "var(--font-mono)" }}>{successRate}% scored</span>
            </span>
          );
        })()}
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
  const W = 72, H = 26;
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
      <svg width={W} height={H} style={{ display: "block", flexShrink: 0, opacity: 0.5 }} title="Plateau — best score is flat">
        <line x1={4} y1={H/2} x2={W-4} y2={H/2} stroke="var(--text-faint)" strokeWidth="1.75" strokeDasharray="3 2" strokeLinecap="round" />
      </svg>
    );
  }

  const px = (i: number) => 2 + (i / (recent.length - 1)) * (W - 4);
  const py = (v: number) => H - 3 - ((v - lo) / range) * (H - 6);
  const d = recent.map((v, i) => `${i === 0 ? "M" : "L"}${px(i).toFixed(1)},${py(v).toFixed(1)}`).join(" ");
  return (
    <svg width={W} height={H} style={{ display: "block", flexShrink: 0, opacity: 0.9 }} title="Running best over recent experiments">
      <path d={d} fill="none" stroke="var(--purple)" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" />
      <circle cx={px(recent.length - 1)} cy={py(recent[recent.length - 1])} r="2.6" fill="var(--purple)" />
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
    <div class="review-mini score-dist">
      {/* caption header — tells you what this is + the y-axis units (count) */}
      <div class="score-dist-head">
        <span class="emr-label">{fmtMetricName(metric)} distribution</span>
        <span class="score-dist-head-n" title={`${values.length} experiments · tallest bar = ${maxC}`}>n={values.length}</span>
      </div>
      <div class="score-dist-bar" title={`How ${fmtMetricName(metric)} is distributed across ${values.length} experiments. Bar height = # of experiments in that range. Range ${fmtN(lo)}–${fmtN(hi)}.`}>
        {counts.map((c, i) => {
          const frac = lower ? 1 - i / (BUCKETS - 1) : i / (BUCKETS - 1);
          const h = c > 0 ? Math.max(4, Math.round((c / maxC) * 36)) : 1;
          const clr = `hsl(${Math.round(120 * frac)},70%,52%)`;
          return (
            <span key={i} class="score-dist-tick"
              style={{ height: `${h}px`, background: c > 0 ? clr : "var(--border-soft)", opacity: c > 0 ? 0.85 : 0.25 }}
              title={`${c} exp @ ${(lo + i * size).toFixed(1)}–${(lo + (i+1) * size).toFixed(1)}`}
            />
          );
        })}
      </div>
      {/* x-axis: low → high score endpoints with units */}
      <div class="score-dist-range">
        <span>{fmtN(lo)}</span>
        <span class="score-dist-range-cap">{lower ? "← better" : "worse → better"}</span>
        <span>{fmtN(hi)}</span>
      </div>
    </div>
  );
}

// ── Milestones table — the Experiments summary (#11) ─────────────────────────
// A clear list of the record-setting experiments (new global bests): which
// experiment / idea, the metric value, when it landed, and the Δ improvement
// over the prior record. Column headers, hairline rows, tabular-nums, ★.

function MilestonesTable({ experiments, metric, lower, ideas }: {
  experiments: import("./lib/types").Experiment[];
  metric: string;
  lower: boolean;
  ideas: Record<number, import("./lib/types").IdeaNode>;
}) {
  // Walk completed-with-metric chronologically; emit a row each time the
  // running best improves. Each row carries its Δ over the previous record.
  const done = experiments
    .filter((e) => !e._running && typeof e.metrics?.[metric] === "number" && isFinite(e.metrics![metric] as number))
    .slice()
    .sort((a, b) => a.id - b.id);

  type Row = { exp: import("./lib/types").Experiment; val: number; delta: number | null; prev: number | null };
  const rows: Row[] = [];
  let best: number | null = null;
  for (const e of done) {
    const v = e.metrics![metric] as number;
    const better = best === null || (lower ? v < best : v > best);
    if (!better) continue;
    rows.push({ exp: e, val: v, prev: best, delta: best === null ? null : Math.abs(v - best) });
    best = v;
  }
  if (rows.length === 0) return null;

  function fmtV(v: number) {
    return Math.abs(v) >= 100 ? v.toFixed(0) : Math.abs(v) >= 1 ? v.toFixed(2) : v.toFixed(3);
  }
  function whenAgo(iso: string | null | undefined): string {
    if (!iso) return "—";
    const sec = Math.max(0, Math.floor((Date.now() - Date.parse(iso)) / 1000));
    if (sec < 60) return `${sec}s`;
    const min = Math.floor(sec / 60);
    if (min < 60) return `${min}m`;
    const hr = Math.floor(min / 60);
    if (hr < 48) return `${hr}h`;
    return `${Math.floor(hr / 24)}d`;
  }

  // newest record first — most relevant. Cap the summary at the latest 6 so
  // it stays breathable; the full record is in the detail TablePanel.
  const MAX = 6;
  const allOrdered = rows.slice().reverse();
  const ordered = allOrdered.slice(0, MAX);
  const hidden = allOrdered.length - ordered.length;
  const overallImprovement = rows.length > 1 && rows[0].val !== 0
    ? Math.abs((best! - rows[0].val) / rows[0].val) * 100
    : null;

  return (
    <div class="review-milestones" role="table" aria-label="Milestones — record-setting experiments">
      <div class="review-mst-caption">
        <span class="emr-label">milestones · new {fmtMetricName(metric)} records</span>
        <span class="review-mst-caption-sub">
          {rows.length} record{rows.length === 1 ? "" : "s"}
          {overallImprovement != null && <> · <span style={{ color: "var(--purple)" }}>+{overallImprovement.toFixed(0)}% total</span></>}
        </span>
      </div>
      <div class="review-mst-table">
        <div class="review-mst-row review-mst-head" role="row">
          <span class="review-mst-c-rank" role="columnheader">#</span>
          <span class="review-mst-c-exp" role="columnheader">experiment</span>
          <span class="review-mst-c-exptext" role="columnheader">what it tried</span>
          <span class="review-mst-c-idea" role="columnheader">idea</span>
          <span class="review-mst-c-ideatext" role="columnheader">idea text</span>
          <span class="review-mst-c-val" role="columnheader">{fmtMetricName(metric)}</span>
          <span class="review-mst-c-delta" role="columnheader">Δ</span>
          <span class="review-mst-c-when" role="columnheader">when</span>
        </div>
        {ordered.map((r, i) => {
          const recNo = rows.length - i; // 1 = first record, N = latest
          const isLatest = i === 0;
          const idea = ideas[r.exp.idea_id];
          const ideaText = idea?.description?.split("\n")[0] ?? "";
          const expText = (r.exp.description ?? "").split("\n")[0];
          return (
            <div
              key={r.exp.id}
              class={`review-mst-row${isLatest ? " review-mst-row--latest" : ""}`}
              role="row"
              style={{ cursor: "pointer" }}
              onMouseEnter={() => { highlightedIdea.value = r.exp.idea_id; }}
              onMouseLeave={() => { if (highlightedIdea.value === r.exp.idea_id) highlightedIdea.value = null; }}
              onClick={() => navigateToIdea(r.exp.idea_id, r.exp.label ?? String(r.exp.id))}
              title={`record #${recNo} · exp/${r.exp.label ?? r.exp.id}${expText ? ` — ${expText}` : ""} · idea #${r.exp.idea_id} ${ideaText}`}
            >
              <span class="review-mst-c-rank" role="cell">
                <span class="review-mst-star">★</span>{recNo}
              </span>
              <code class="review-mst-c-exp" role="cell">exp/{r.exp.label ?? r.exp.id}</code>
              <span class="review-mst-c-exptext" role="cell">{expText || "—"}</span>
              <span class="review-mst-c-idea" role="cell">#{r.exp.idea_id}</span>
              <span class="review-mst-c-ideatext" role="cell">{ideaText || "—"}</span>
              <span class="review-mst-c-val" role="cell">{fmtV(r.val)}</span>
              <span class="review-mst-c-delta" role="cell">
                {r.delta == null
                  ? <span class="review-mst-first">first</span>
                  : <>{lower ? "−" : "+"}{fmtV(r.delta)}</>}
              </span>
              <span class="review-mst-c-when" role="cell">{whenAgo(r.exp.finished_at || r.exp.created_at)} ago</span>
            </div>
          );
        })}
        {hidden > 0 && (
          <div class="review-mst-more">+{hidden} earlier record{hidden === 1 ? "" : "s"} · open detail for all experiments</div>
        )}
      </div>
    </div>
  );
}

// ── Idea portfolio — the redesigned "idea boxes" (#7, #D) ────────────────────
// A labeled segmented bar + an explicit legend with counts. A glance tells you
// the idea-portfolio breakdown (active / concluded / abandoned / untested)
// without guessing — no more cryptic ring.

function IdeaPortfolio({ active, concluded, abandoned, untested }: {
  active: number; concluded: number; abandoned: number; untested: number;
}) {
  const total = active + concluded + abandoned;
  if (total === 0) return null;
  // untested is a subset of active (active ideas with zero experiments) — shown
  // as a hatched slice carved out of the active segment so counts still sum.
  const tested = Math.max(0, active - untested);
  const pct = (n: number) => total > 0 ? Math.round((n / total) * 100) : 0;
  const segs: { key: string; n: number; cls: string; label: string }[] = [
    { key: "tested",    n: tested,     cls: "ipf-active",    label: "active" },
    { key: "untested",  n: untested,   cls: "ipf-untested",  label: "untested" },
    { key: "concluded", n: concluded,  cls: "ipf-concluded", label: "concluded" },
    { key: "abandoned", n: abandoned,  cls: "ipf-abandoned", label: "abandoned" },
  ];
  return (
    <div class="idea-portfolio">
      <div class="ipf-head">
        <span class="emr-label">idea portfolio · {total} total</span>
        <span class="ipf-head-sub">{pct(concluded)}% concluded</span>
      </div>
      <div class="ipf-bar" role="img"
        aria-label={`${active} active, ${concluded} concluded, ${abandoned} abandoned${untested > 0 ? `, of which ${untested} untested` : ""}`}>
        {segs.filter(s => s.n > 0).map(s => (
          <span key={s.key} class={`ipf-seg ${s.cls}`} style={{ flex: s.n }}
            title={`${s.n} ${s.label}`} />
        ))}
      </div>
      <div class="ipf-legend">
        <span class="ipf-leg"><span class="ipf-swatch ipf-active" /> active <b>{active}</b></span>
        {untested > 0 && <span class="ipf-leg"><span class="ipf-swatch ipf-untested" /> untested <b>{untested}</b></span>}
        <span class="ipf-leg"><span class="ipf-swatch ipf-concluded" /> concluded <b>{concluded}</b></span>
        {abandoned > 0 && <span class="ipf-leg"><span class="ipf-swatch ipf-abandoned" /> abandoned <b>{abandoned}</b></span>}
      </div>
    </div>
  );
}

function ReviewDashboard({ onOpenWorkbench }: { onOpenWorkbench: () => void }) {
  const data = backlogData.value;
  const experiments = allExperiments.value;
  const ideas = allIdeas.value;
  const cost = totalAgentCost.value;
  const progress = runningProgress.value;
  // Use allIdeas for active count — loads faster than backlogData; fallback to backlogData if allIdeas not loaded
  const ideaListForCount = Object.values(ideas);
  const activeIdeas = ideaListForCount.length > 0
    ? ideaListForCount.filter(i => i.status !== "concluded" && i.status !== "abandoned").length
    : (data?.active_ideas.length ?? 0);
  const totalRunning = data?.total_running ?? 0;
  const branch = data?.current_branch ?? "…";
  const done = experiments.filter((e) => !e._running && e.status !== "running");
  const finished = done.length;
  const running = experiments.filter((e) => e._running || e.status === "running").length;
  const queued = data?.total_pending ?? 0;
  const failed = experiments.filter((e) => e.status === "failed").length;
  const activeRuns = experiments.filter((e) => e._running || e.status === "running");
  const selected = selectedIdea.value;
  const metric = selectedMetric.value || "metric";
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

  const liveCount = totalRunning || running;
  const isLive = liveCount > 0;

  // Last-activity: most recent finished experiment
  const lastFinishedExp = done.reduce<typeof done[0] | null>((latest, e) => {
    if (!e.finished_at) return latest;
    return !latest || e.finished_at > latest.finished_at! ? e : latest;
  }, null);
  const lastFinishedAt = lastFinishedExp?.finished_at ?? null;
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
      {/* ══ Header — one calm instrument cluster ════════════════════════
          A single status line + a hairline KPI grid + the experiment grid.
          Replaces the old status-strip / jump-nav / best-bar / snapshot /
          milestone-timeline pile-up. Color is reserved for status + the
          single purple "best" accent. */}

      {/* ── Status line — one dot, the live/idle sentence, branch, action ── */}
      <div class="review-statusline">
        <span class={`review-status-dot ${isLive ? "live" : "idle"}${expsSinceBest != null && expsSinceBest > 50 ? " stagnant" : ""}`}
          title={expsSinceBest != null && expsSinceBest > 50 ? `Stagnant: ${expsSinceBest} experiments since last record` : undefined}
        />
        <span class="review-statusline-text">
          {isLive ? (
            <>
              <strong>{liveCount}</strong> running{avgProgress > 0 ? ` · ${avgProgress}%` : ""}
              {queued > 0 && <span class="review-statusline-dim"> · {queued} queued</span>}
              {activeRuns[0]?.started_at && (() => {
                const mins = Math.floor((Date.now() - Date.parse(activeRuns[0].started_at!)) / 60000);
                return mins > 0 ? <span class="review-statusline-dim"> · {mins}m elapsed</span> : null;
              })()}
            </>
          ) : (
            <>
              idle
              {lastFinishedAt && <span class="review-statusline-dim"> · last record {timeAgo(lastFinishedAt)}</span>}
              {queued > 0 && <span class="review-statusline-dim"> · {queued} queued</span>}
              {expsSinceBest != null && expsSinceBest > 50 && (
                <span class="review-statusline-warn" title={`${expsSinceBest} experiments since the last new best — consider a new approach`}>
                  {" · "}stuck {expsSinceBest}
                </span>
              )}
            </>
          )}
        </span>
        <code class="review-statusline-branch" style={branch === "…" ? { opacity: 0.4 } : undefined}>{branch}</code>
        <button class="review-statusline-action" onClick={onOpenWorkbench}>Workbench →</button>
      </div>

      {/* ── KPI cluster — confident mono Stats on a hairline grid ──────── */}
      <div class="review-kpis ui-hairline-grid">
        <div class="review-kpi">
          <span class="ui-stat-label">best {fmtMetricName(metric)}</span>
          <span class="review-kpi-row">
            <span class="ui-stat-value ui-stat-value--best">
              {bestVal != null && typeof bestVal === "number"
                ? bestVal.toFixed(bestVal >= 100 ? 0 : bestVal >= 1 ? 2 : 3)
                : "—"}
            </span>
            <BestSparkline experiments={done} metric={metric} lower={lower} />
          </span>
          <span class="ui-stat-sub">
            {lower ? "↓ lower better" : "↑ higher better"}
            {expsSinceBest != null && expsSinceBest > 5 && (
              <span style={{ color: expsSinceBest > 50 ? "var(--red)" : expsSinceBest > 20 ? "var(--yellow)" : "var(--text-faint)", marginLeft: 6 }}
                title={`${expsSinceBest} experiments since the last new best`}>
                +{expsSinceBest} since ★
              </span>
            )}
          </span>
        </div>

        <div class="review-kpi">
          <span class="ui-stat-label">experiments</span>
          <span class="ui-stat-value">{finished}</span>
          <span class="ui-stat-sub">
            {running > 0 && <span style={{ color: "var(--yellow)" }}>{running} running</span>}
            {running > 0 && failed > 0 && " · "}
            {failed > 0 && <span style={{ color: "var(--red)" }}>{failed} failed</span>}
            {running === 0 && failed === 0 && velocityPerDay ? `${velocityPerDay}/day` : null}
          </span>
        </div>

        {successRate !== null && (
          <div class="review-kpi">
            <span class="ui-stat-label">success</span>
            <span class={`ui-stat-value${successRate < 15 ? " ui-stat-value--warn" : successRate > 50 ? " ui-stat-value--good" : ""}`}>{successRate}%</span>
            <span class="ui-stat-sub">scored above zero</span>
          </div>
        )}

        {milestonesCount > 0 && (
          <div class="review-kpi">
            <span class="ui-stat-label">records</span>
            <span class="ui-stat-value ui-stat-value--best">{milestonesCount}</span>
            <span class="ui-stat-sub">{finished > 0 ? `1 per ${Math.round(finished / milestonesCount)} exp` : "new bests"}</span>
          </div>
        )}

        {activeIdeas > 0 && (
          <div class="review-kpi">
            <span class="ui-stat-label">ideas</span>
            <span class="ui-stat-value">{activeIdeas}</span>
            <span class="ui-stat-sub">active{ideasConcluded > 0 ? ` · ${ideasConcluded} done` : ""}</span>
          </div>
        )}

        {cost != null && cost > 0 && (
          <div class="review-kpi">
            <span class="ui-stat-label">cost</span>
            <span class="ui-stat-value">${cost >= 1000 ? `${(cost / 1000).toFixed(1)}k` : cost.toFixed(0)}</span>
            <span class="ui-stat-sub">
              {milestonesCount > 0 ? `$${(cost / milestonesCount).toFixed(0)}/★` : campaignAgeDays ? `over ${campaignAgeDays}d` : "total"}
            </span>
          </div>
        )}
      </div>

      {/* ── Experiment grid — glanceable per-experiment status tiles ───── */}
      <ExperimentGrid experiments={experiments} successRate={successRate} milestoneIds={milestoneIdsSet} ideas={ideas} metric={metric} />

      {/* ── Chart — collapses to compact empty state when no metric selected ── */}
      {(() => {
        const hasChartData = selectedMetric.value !== "" &&
          done.some(e => typeof e.metrics?.[selectedMetric.value] === "number");
        return (
          <div class="review-chart-wrap" id="review-progress"
            style={!hasChartData ? { height: "136px", minHeight: "136px" } : undefined}>
            <MetricsChart />
            <a class="review-chart-skip" href="#review-runs" onClick={(e) => {
              e.preventDefault();
              document.getElementById("review-runs")?.scrollIntoView({ behavior: "smooth", block: "start" });
            }}>↓ experiments & ideas</a>
          </div>
        );
      })()}

      {/* ── Collapsible detail sections — Experiments first (results > structure) ── */}
      <div class="review-stack">
        <ReviewDisclosure
          id="review-runs"
          title="Experiments"
          icon="◉"
          accent={running > 0 ? "live" : isStagnant ? "warn" : undefined}
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
            <div class="emr-preview emr-preview--runs">
              {/* Centerpiece (#11): the milestones table — every record-setting run. */}
              <MilestonesTable experiments={experiments} metric={metric} lower={lower} ideas={ideas} />

              {/* Supporting minis: each self-explanatory with caption + axis labels. */}
              <div class="review-mini-col">
                {/* Score distribution — already labeled (x range + units caption). */}
                <ScoreDistBar experiments={experiments} metric={metric} lower={lower} />

                {/* Recent runs: last N scores as bars, with caption + value axis. */}
                {(() => {
                  const recent30 = done.slice(-30).filter(e => typeof e.metrics?.[metric] === "number");
                  if (recent30.length < 5) return null;
                  const vals = recent30.map(e => e.metrics![metric] as number);
                  const maxV = Math.max(...vals, 0.001);
                  const W = 150, H = 26;
                  return (
                    <div class="review-mini">
                      <div class="review-mini-chart" style={{ display: "flex", alignItems: "flex-end", gap: 4 }}>
                        <svg width={W} height={H} style={{ display: "block", overflow: "visible" }}
                          role="img" aria-label={`Score of the last ${recent30.length} experiments`}>
                          {vals.map((v, i) => {
                            const barW = Math.max(1.5, W / vals.length - 1.5);
                            const barH = v > 0 ? Math.max(2, (v / maxV) * (H - 2)) : 2;
                            const x = i * (W / vals.length);
                            const isRecent = i >= vals.length - 5;
                            return (
                              <rect key={i} x={x} y={H - barH} width={barW} height={barH}
                                fill={v > 0 ? (isRecent ? "var(--green)" : "var(--accent)") : "var(--border-soft)"}
                                opacity={v > 0 ? (isRecent ? 0.9 : 0.6) : 0.4}
                                rx={1}
                              />
                            );
                          })}
                        </svg>
                        <span class="review-mini-ymax">{maxV >= 100 ? maxV.toFixed(0) : maxV.toFixed(1)}</span>
                      </div>
                      <div class="review-mini-cap">
                        <span>{fmtMetricName(metric)} · last {recent30.length} runs</span>
                        <span class="review-mini-cap-end" style={{ color: "var(--green)" }}>● newest 5</span>
                      </div>
                    </div>
                  );
                })()}

                {/* Success-rate bar — explicit % label. */}
                {successRate !== null && (
                  <div class="review-mini">
                    <div class="review-mini-meter" title={`${successRate}% of experiments scored above zero`}>
                      <span style={{ width: `${successRate}%`, background: successRate > 30 ? "var(--green)" : successRate > 10 ? "var(--yellow)" : "var(--red)" }} />
                    </div>
                    <div class="review-mini-cap">
                      <span>success rate</span>
                      <span class="review-mini-cap-end">{successRate}% scored &gt; 0</span>
                    </div>
                  </div>
                )}
              </div>
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
          icon="◈"
          action={(() => {
            // Count under-explored and never-tested active ideas
            const ideaExpCountsAll: Record<number, number> = {};
            for (const e of experiments) { if (!e._running) ideaExpCountsAll[e.idea_id] = (ideaExpCountsAll[e.idea_id] || 0) + 1; }
            const ideaExpCounts: Record<number, number> = {};
            for (const e of done) { if (typeof e.metrics?.[metric] === "number") ideaExpCounts[e.idea_id] = (ideaExpCounts[e.idea_id] || 0) + 1; }
            const activeList = Object.values(ideas).filter(i => i.status !== "concluded" && i.status !== "abandoned");
            const hasLoadedExps = experiments.length > 20; // only trust counts when data has loaded
            const neverTested = hasLoadedExps ? activeList.filter(i => (ideaExpCountsAll[i.id] ?? 0) === 0).length : 0;
            const underExplored = activeList.filter(i => (ideaExpCounts[i.id] ?? 0) < 5).length;
            // Fall back to ring data when backlogData not loaded yet (shows 0 active from backlog)
            const displayActive = activeIdeas > 0 ? activeIdeas : ideasActive;
            const displayConcluded = ideasConcluded;
            // If both still 0 but we have experiment data, show loading indicator
            const uniqueIdeaCount = displayActive === 0 && displayConcluded === 0 && experiments.length > 20
              ? new Set(experiments.map(e => e.idea_id)).size : 0;
            const base = uniqueIdeaCount > 0
              ? `${uniqueIdeaCount} ideas (loading…)`
              : `${displayActive} active · ${displayConcluded} concluded${ideasAbandoned > 0 ? ` · ${ideasAbandoned} abandoned` : ""}`;
            const parts = [base];
            if (neverTested > 0) parts.push(`${neverTested} untested`);
            else if (underExplored > 0 && hasLoadedExps) parts.push(`${underExplored} under-explored`);
            return parts.join(" · ");
          })()}
          preview={
            <div class="emr-preview emr-preview--ideas">
              {/* Redesigned (#7): a labeled segmented portfolio bar + legend —
                  a glance shows active / concluded / abandoned / untested. */}
              <IdeaPortfolio
                active={ideasActive}
                concluded={ideasConcluded}
                abandoned={ideasAbandoned}
                untested={(() => {
                  if (experiments.length <= 20) return 0;
                  const counts: Record<number, number> = {};
                  for (const e of experiments) { if (!e._running) counts[e.idea_id] = (counts[e.idea_id] || 0) + 1; }
                  return Object.values(ideas).filter(i => i.status !== "concluded" && i.status !== "abandoned" && (counts[i.id] ?? 0) === 0).length;
                })()}
              />
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
          title="Correlation"
          icon="⊞"
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

            // Mini scatter — a true small version of the full ScatterChart, with axes.
            const W = 132, H = 60;
            const xlo = Math.min(...xs), xhi = Math.max(...xs);
            const ylo = Math.min(...ys), yhi = Math.max(...ys);
            const xr = xhi - xlo || 1, yr = yhi - ylo || 1;
            const px = (v: number) => 2 + ((v - xlo) / xr) * (W - 4);
            const py = (v: number) => H - 2 - ((v - ylo) / yr) * (H - 4);
            const trend = absR > 0.3 && den !== 0 ? (() => {
              const slope = num / xs.reduce((s,x) => s + (x-mx)**2, 0);
              const b = my - slope * mx;
              return { x1: px(xlo), y1: py(slope * xlo + b), x2: px(xhi), y2: py(slope * xhi + b) };
            })() : null;

            return (
              <div class="review-mini review-scatter-mini">
                <div class="review-mini-cap">
                  <span class="review-scatter-ylab" title={`y-axis: ${fmtMetricName(yk)}`}>{fmtMetricName(yk)}</span>
                  <span class="review-scatter-r" style={{ color }}>{label}{dir && <span style={{ opacity: 0.8 }}>{dir}</span>}</span>
                </div>
                <svg width={W} height={H} class="review-scatter-svg" role="img"
                  aria-label={`${fmtMetricName(xk)} versus ${fmtMetricName(yk)}, ${pairs.length} points, correlation ${label}`}>
                  {trend && <line x1={trend.x1} y1={trend.y1} x2={trend.x2} y2={trend.y2} stroke={color} strokeWidth="1" strokeDasharray="2 2" opacity={0.7} />}
                  {pairs.map((e, i) => (
                    <circle key={i} cx={px(xs[i])} cy={py(ys[i])} r={2} fill="var(--accent)" opacity={0.55} />
                  ))}
                </svg>
                <div class="review-mini-cap">
                  <span class="review-scatter-xlab" title={`x-axis: ${fmtMetricName(xk)}`}>x: {fmtMetricName(xk)}</span>
                  <span class="review-mini-cap-end">{pairs.length} pts</span>
                </div>
              </div>
            );
          })()}
        >
          <div class="review-panel review-scatter-panel">
            <ScatterChart />
          </div>
        </ReviewDisclosure>

        <div class="review-section-sep">explore ideas</div>

        {/* Idea Detail — permanent, always-open flat section (#10). Not a
            summary/detail toggle: the full DetailPanel is always visible.
            Hairline-separated heading (eyebrow + title) like a section head. */}
        <section id="review-detail" class="review-section review-permanent">
          <div class="review-permanent-head">
            <div class="review-disclosure-title">
              <span class="review-section-icon" aria-hidden="true">▸</span>
              <h2>Idea Detail</h2>
            </div>
            <span class="review-permanent-sub">
              {selected
                ? <>idea #{selected}{selectedIdeaBest != null && <> · <b style={{ color: "var(--purple)" }}>best {selectedIdeaBest.toFixed(3)}</b></>}</>
                : <span style={{ fontStyle: "italic" }}>click any idea above to explore</span>}
            </span>
          </div>
          <div class="review-panel review-detail-panel">
            <DetailPanel />
          </div>
        </section>
      </div>
    </div>
  );
}


function ReviewDisclosure({
  id,
  title,
  icon,
  action,
  preview,
  children,
  autoOpen,
  accent,
}: {
  id: string;
  title: string;
  icon?: string;
  action: string;
  preview?: preact.ComponentChildren;
  children: preact.ComponentChildren;
  autoOpen?: boolean;
  accent?: "live" | "warn";
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
    <details ref={ref} class={`review-disclosure review-section${accent ? ` review-disclosure--${accent}` : ""}`} id={id}>
      {/* The whole heading row is the summary (click anywhere to toggle).
          It reads as a flat heading + summary, with a summary⇄detail switch
          on the right — not a boxed accordion. */}
      <summary>
        <div class="review-disclosure-head">
          <div class="review-disclosure-title">
            {icon && <span class="review-section-icon" aria-hidden="true">{icon}</span>}
            <h2>{title}</h2>
          </div>
          <div class="review-disclosure-summary">
            <p>{action}</p>
            {preview}
          </div>
        </div>
        {/* summary ⇄ detail switch — .ui-toggle language, the disclosure control */}
        <span class="review-disclosure-switch" role="presentation">
          <span class="rds-opt rds-opt--summary">summary</span>
          <span class="rds-opt rds-opt--detail">detail</span>
        </span>
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
    return <span class="review-branch-empty"><Eyebrow>no ideas yet</Eyebrow></span>;
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
