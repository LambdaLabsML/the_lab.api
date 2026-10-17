import { useEffect, useRef, useState } from "preact/hooks";
import { effect } from "@preact/signals";
import { DetailPanel } from "./components/detail-panel";
import { NavRail, SecondaryPanel, type NavSection } from "./components/nav-rail";
import { ActivitySidebar } from "./components/activity/activity-sidebar";
import { MessagePreviewOverlay } from "./components/agent-activity/message-preview";
import { Toasts, startCollabNotifications } from "./components/toasts";
import { SettingsPanel } from "./components/settings-panel";
import { SearchFilter, type FilterItem } from "./components/search-filter/search-filter";
import { MessagesView } from "./views/messages-view";
import { QueueView } from "./views/queue-view";
import { DatabaseView } from "./views/database-view";
import { ArtifactsSidebar, ArtifactsView, artifactPath } from "./views/artifacts-view";
import { AgentsView } from "./views/agents-view";
import { SandboxView } from "./views/sandbox-view";
import { PromptsView } from "./views/prompts-view";
import { StatsView } from "./views/stats-view";
import { ApiView } from "./views/api-view";
import { RecentIdeas } from "./components/recent-ideas";
import { GraphWorkspace } from "./components/chart-panel/graph-workspace";
import {
  selectedIdea, selectedMetric, colorMode, improvementsOnly, activeTagFilters,
  tagFilterMode, reverseTime, showAbandoned, showConcluded, showRunning,
  clipOutliers, ideaMean, scatterXMetric, scatterYMetric, applyServerDefaults,
  filterText, useSetting,
} from "./state/settings";
import { allExperiments, allIdeas, selectedExperiment, setActivatePanel, totalAgentCost } from "./state/signals";
import { reviewSearchPills, activeHighlights, isIdeaHighlighted, toggleIdeaHighlight, type ReviewPill } from "./state/review-filter";
import { plotExperiments, selectionLabels, activeSelection, activeFilter } from "./state/filters";
import { FilterPicker } from "./components/filter-picker";
import { AgentBar } from "./components/agent-bar";
import { startPolling, stopPolling } from "./state/polling";
import { startWs, stopWs } from "./state/ws";
import { startAgentActivity, stopAgentActivity } from "./state/agent-activity";
import { inboxUnreadCount, inboxView } from "./state/inbox";
import { startMessageNotifications, messagesUnreadCount } from "./state/messages";
import { navigateToIdea } from "./lib/navigate";
import { fmtMetricName, fmtScoreShort } from "./lib/format";
import { resolveNumericValue } from "./lib/chart-data";
import { ExperimentActions, ResearchIcon } from "./components/experiment-actions";
import { isLowerBetter } from "./lib/colors";

const inspectorWidth = useSetting("inspectorWidth", 420);
const NAV_SECTIONS = ["review", "messages", "artifacts", "database", "queue"];
const utilityViews = { agents: AgentsView, sandbox: SandboxView, prompts: PromptsView, stats: StatsView, api: ApiView };
type Utility = keyof typeof utilityViews;
function parseNavHash(): { section: NavSection; utility?: Utility; artifact: string } {
  const h = location.hash.replace(/^#\/?/, "");
  const [sec, tool] = h.split("/");
  let artifact = "";
  try { if (sec === "artifacts") artifact = decodeURIComponent(h.slice(10)); } catch { /* malformed link */ }
  return {
    section: (NAV_SECTIONS.includes(sec) ? sec : "review") as NavSection,
    utility: sec === "tools" && tool in utilityViews ? tool as Utility : undefined,
    artifact,
  };
}

function readFiltersFromUrl() {
  const path = window.location.pathname;
  const p = new URLSearchParams(window.location.search);
  const lightbox = new URLSearchParams(window.location.hash.slice(1));
  if (lightbox.has("idea")) {
    const id = Number(lightbox.get("idea"));
    if (Number.isFinite(id) && id > 0) selectedIdea.value = id;
  }

  // Path: /ideas/13
  const ideaMatch = path.match(/^\/ideas\/(\d+)/);
  if (ideaMatch) {
    selectedIdea.value = parseInt(ideaMatch[1]);
  } else if (p.has("idea")) {
    // Legacy: ?idea=13
    selectedIdea.value = parseInt(p.get("idea")!) || null;
  }

  selectedExperiment.value = p.get("e") || null;

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

  // Experiment selection: saved filter (by name or id) and/or expression
  if (p.has("filter") || p.has("where")) {
    activeSelection.value = { filter: p.get("filter") || null, where: p.get("where") || "" };
  }
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
  const _sel = activeSelection.value;
  const _selName = activeFilter.value?.name ?? _sel.filter;

  // Path: /ideas/13 or /
  const path = _idea !== null ? `/ideas/${_idea}` : "/";

  // Only encode non-default values
  const p = new URLSearchParams();
  if (_metric) p.set("m", _metric);
  if (selectedExperiment.value) p.set("e", selectedExperiment.value);
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
  if (_selName) p.set("filter", _selName);
  if (_sel.where.trim()) p.set("where", _sel.where);
  // An open lightbox (?view=output/1177.2) is owned by the idea panel: keep it.
  const _view = new URLSearchParams(window.location.search).get("view");
  if (_view) p.set("view", _view);

  const qs = p.toString()
    .replace(/=(&|$)/g, "$1")  // strip trailing = for valueless params (imp, mean, norev, noclip)
    .replace(/%2F/gi, "/")     // readable view=output/1177.2
    .replace(/&$/, "");
  const url = path + (qs ? "?" + qs : "") + window.location.hash;

  if (url !== window.location.pathname + window.location.search + window.location.hash) {
    history.replaceState(null, "", url);
  }
}


export function App() {
  const initial = useRef(parseNavHash()).current;
  const [section, setSection] = useState<NavSection>(initial.section);
  const [visited, setVisited] = useState<Set<NavSection>>(() => new Set([initial.section]));
  const [settingsOpen, setSettingsOpen] = useState(!!initial.utility);
  const [utility, setUtility] = useState<Utility | null>(initial.utility ?? null);
  const [detailsOpen, setDetailsOpen] = useState(selectedIdea.value != null);
  const [messagesDocked, setMessagesDocked] = useState(false);
  const [searchOpen, setSearchOpen] = useState(false);
  const searchRef = useRef<HTMLDivElement>(null);
  const [mobile, setMobile] = useState(() => matchMedia("(max-width: 760px)").matches);
  useEffect(() => {
    const mq = matchMedia("(max-width: 760px)");
    const change = () => setMobile(mq.matches);
    mq.addEventListener("change", change);
    return () => mq.removeEventListener("change", change);
  }, []);
  useEffect(() => {
    if (searchOpen) searchRef.current?.querySelector<HTMLInputElement>("input")?.focus();
  }, [searchOpen]);
  const [narrow, setNarrow] = useState(() => matchMedia("(max-width: 1100px)").matches);
  useEffect(() => {
    const mq = matchMedia("(max-width: 1100px)");
    const change = () => setNarrow(mq.matches);
    mq.addEventListener("change", change);
    return () => mq.removeEventListener("change", change);
  }, []);
  const inspectorRef = useRef<HTMLElement>(null);
  const messageRef = useRef<HTMLElement>(null);
  const detailsTrigger = useRef<HTMLElement | null>(null);
  const artifact = artifactPath.value;
  const selected = selectedIdea.value;
  const unread = inboxUnreadCount.value ?? messagesUnreadCount.value;
  const rememberView = (next: NavSection) => {
    setSection(next);
    setVisited(prev => new Set([...prev, next]));
    setSettingsOpen(false);
  };
  useEffect(() => {
    if (initial.artifact) artifactPath.value = initial.artifact;
    const onHash = () => {
      const next = parseNavHash();
      rememberView(next.section);
      if (next.utility) { setUtility(next.utility); setSettingsOpen(true); }
      if (next.section === "artifacts") artifactPath.value = next.artifact;
    };
    const openDatabase = () => { rememberView("database"); setDetailsOpen(false); };
    window.addEventListener("the-lab:open-database", openDatabase);
    window.addEventListener("hashchange", onHash);
    return () => { window.removeEventListener("hashchange", onHash); window.removeEventListener("the-lab:open-database", openDatabase); };
  }, []);
  useEffect(() => {
    const hash = settingsOpen && utility ? `#tools/${utility}` : section === "artifacts"
      ? `#artifacts${artifact ? "/" + artifact.split("/").map(encodeURIComponent).join("/") : ""}` : `#${section}`;
    const lightbox = new URLSearchParams(location.hash.slice(1)).has("view");   // legacy #..&view= links
    const keepsSubPath = !settingsOpen && location.hash.startsWith(`${hash}/`);   // e.g. #messages/6674/11944
    if (location.hash !== hash && !keepsSubPath && !(lightbox && section === "review" && !settingsOpen)) {
      history.replaceState(null, "", location.pathname + location.search + hash);
    }
    requestAnimationFrame(() => window.dispatchEvent(new Event("resize")));
  }, [section, artifact, settingsOpen, utility, messagesDocked, detailsOpen]);
  useEffect(() => {
    let disposed = false;
    let disposeUrl = () => {};
    // Read links before creating the URL-sync effect so startup never overwrites them.
    readFiltersFromUrl();
    applyServerDefaults().then(() => { if (!disposed) { readFiltersFromUrl(); disposeUrl = effect(syncUrlFromSignals); } });
    window.addEventListener("popstate", readFiltersFromUrl);
    startPolling(); startWs(); startAgentActivity();
    const stopNotifications = startMessageNotifications();
    const stopCollab = startCollabNotifications();
    setActivatePanel(id => {
      if (id === "detail") {
        detailsTrigger.current = document.activeElement as HTMLElement;
        setDetailsOpen(true);
      }
      if (id === "messages") rememberView("messages");
    });
    return () => {
      disposed = true; disposeUrl(); stopPolling(); stopWs(); stopAgentActivity(); stopNotifications(); stopCollab();
      setActivatePanel(() => {});
      window.removeEventListener("popstate", readFiltersFromUrl);
    };
  }, []);
  useEffect(() => { if (selected != null) setDetailsOpen(true); }, [selected]);
  const inspectorVisible = detailsOpen && selected != null && !settingsOpen && section !== "artifacts";
  const showMessages = section === "messages" || (messagesDocked && section === "review" && !settingsOpen);
  // Focus a newly opened mobile sheet; Escape returns to the selected graph/queue item.
  useEffect(() => {
    if (!inspectorVisible || !matchMedia("(max-width: 1100px)").matches) return;
    inspectorRef.current?.focus();
  }, [inspectorVisible, narrow]);
  function closeDetails() {
    setDetailsOpen(false);
    detailsTrigger.current?.focus();
  }
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") { closeDetails(); setMessagesDocked(false); }
      if (e.key !== "Tab") return;
      const root = inspectorVisible && matchMedia("(max-width: 1100px)").matches ? inspectorRef.current
        : showMessages && section !== "messages" && matchMedia("(max-width: 760px)").matches ? messageRef.current : null;
      if (!root) return;
      const nodes = [...root.querySelectorAll<HTMLElement>('button, a[href], input, select, textarea, [tabindex="0"]')].filter(el => el.getClientRects().length && !(el as HTMLButtonElement).disabled);
      if (!nodes.length) return;
      if (e.shiftKey && (document.activeElement === nodes[0] || document.activeElement === root)) { e.preventDefault(); nodes[nodes.length - 1].focus(); }
      else if (!e.shiftKey && document.activeElement === nodes[nodes.length - 1]) { e.preventDefault(); nodes[0].focus(); }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [inspectorVisible, showMessages, section]);
  const UtilityView = utility ? utilityViews[utility] : null;
  const settingsContent = <><SettingsPanel /><div class="research-utilities"><span class="ui-eyebrow">Administration</span>
    {(Object.keys(utilityViews) as Utility[]).map(key => <button class={`nav-secondary-btn${utility === key ? " is-active" : ""}`} onClick={() => setUtility(key)}>{key === "api" ? "API" : key[0].toUpperCase() + key.slice(1)}</button>)}
  </div></>;
  return (
    <div class="app-shell research-shell">
      <NavRail section={section} onSelect={rememberView} settingsOpen={settingsOpen}
        onToggleSettings={() => { setSettingsOpen(v => !v); setUtility(null); }} />
      {!(mobile && settingsOpen) && <SecondaryPanel label={settingsOpen ? "Settings" : section === "artifacts" ? "Artifacts" : "Live monitor"}>
        {settingsOpen ? settingsContent : section === "artifacts" ? <ArtifactsSidebar /> : <ActivitySidebar />}
      </SecondaryPanel>}
      <div class="app-main">
        {/* Phones: the ⬡ mark (agents popover) is hidden, keep the strip there. */}
        <div class="agent-bar-mobile"><AgentBar /></div>
        <MessagePreviewOverlay /><Toasts /><ExperimentActions />
        <div class="research-toolbar">
          <span class="research-view-name"><span class="ui-eyebrow">{settingsOpen ? utility ?? "Settings" : section === "review" ? "Overview" : section}</span><button hidden={mobile && settingsOpen} class="ui-btn research-monitor-button" onClick={() => window.dispatchEvent(new Event("the-lab:open-monitor"))}>{settingsOpen ? "Options" : section === "artifacts" ? "Files" : "Monitor"}</button></span>
          {section === "review" && !settingsOpen && <>
            {/* One place for selecting experiments: text / tag / idea pills and
                highlights (search field) plus the saved filter / expression. */}
            <div ref={searchRef} class="research-header-search" onKeyDown={e => { if (e.key === "Escape") { e.stopPropagation(); (document.activeElement as HTMLElement | null)?.blur(); } }}><ReviewSearchFilter experiments={allExperiments.value} ideas={allIdeas.value} /></div>
            <FilterPicker />
            <SelectionCount />
          </>}
          <div class="research-toolbar-actions">
            {selected != null && <button class="ui-btn" aria-expanded={inspectorVisible} onClick={() => setDetailsOpen(v => !v)}>Idea #{selected} · Details</button>}
            {section === "review" && !settingsOpen && <button class={`ui-btn${messagesDocked ? " is-active" : ""}`} aria-expanded={messagesDocked} onClick={() => { if (!messagesDocked && unread > 0) { inboxView.value = "for_you"; window.dispatchEvent(new Event("the-lab:show-inbox")); } setMessagesDocked(v => !v); setVisited(prev => new Set([...prev, "messages"])); }}>Messages{unread ? ` · ${unread} for you` : ""}</button>}
          </div>
        </div>
        <div class={`research-body${messagesDocked && section === "review" ? " has-messages" : ""}`}>
          <div class={`research-center${messagesDocked && section === "review" && !settingsOpen ? " has-drawer" : ""}`}>
            {settingsOpen && <div class="research-page app-scroll">{UtilityView ? <UtilityView /> : mobile ? <div class="research-mobile-settings">{settingsContent}</div> : <div class="research-welcome"><h2>Settings</h2><p>Adjust appearance and preferences in the settings panel. Administration tools are below.</p></div>}</div>}
            {(["review", "database", "queue", "artifacts"] as NavSection[]).filter(v => visited.has(v)).map(view => (
              <div key={view} class="research-page app-scroll" hidden={settingsOpen || section !== view}>
                {view === "review" ? <Overview /> : view === "database" ? <DatabaseView /> : view === "queue" ? <QueueView /> : <ArtifactsView />}
              </div>
            ))}
            {visited.has("messages") && <section ref={messageRef} tabIndex={-1} class={`research-messages${section === "messages" ? " is-page" : " is-drawer"}`} hidden={!showMessages || settingsOpen} aria-label="Messages">
              {section !== "messages" && <div class="research-panel-head"><strong>Messages</strong><div><button class="ui-btn" onClick={() => rememberView("messages")}>Full view ↗</button><button class="ui-btn" aria-label="Close messages" onClick={() => setMessagesDocked(false)}>×</button></div></div>}
              <MessagesView />
            </section>}
          </div>
          {inspectorVisible && <button class="research-detail-scrim" aria-label="Close details" onClick={closeDetails} />}
          <aside ref={inspectorRef} tabIndex={-1} role={narrow ? "dialog" : "complementary"} aria-modal={narrow ? true : undefined} style={{ "--inspector-width": `${inspectorWidth.value}px` }} class="research-inspector" hidden={!inspectorVisible} aria-label="Idea and experiment details">
            <button class="inspector-resize" aria-label="Resize details panel" title="Drag to resize details" onKeyDown={e => { if (e.key === "ArrowLeft" || e.key === "ArrowRight") { e.preventDefault(); inspectorWidth.value = Math.max(320, Math.min(680, inspectorWidth.value + (e.key === "ArrowLeft" ? 20 : -20))); } }} onPointerDown={e => {
              e.preventDefault();
              const start = e.clientX, width = inspectorWidth.value;
              const move = (event: PointerEvent) => { inspectorWidth.value = Math.max(320, Math.min(680, width + start - event.clientX)); };
              const end = () => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", end); window.removeEventListener("pointercancel", end); };
              window.addEventListener("pointermove", move); window.addEventListener("pointerup", end); window.addEventListener("pointercancel", end);
            }} />
            <div class="research-panel-head"><strong>{selectedExperiment.value ? `Experiment ${selectedExperiment.value}` : `Idea #${selected}`}</strong><div class="research-inspector-actions">{selected != null && <button class={`ui-btn${isIdeaHighlighted(selected) ? " is-active" : ""}`} aria-pressed={isIdeaHighlighted(selected)} aria-label={`Highlight idea #${selected}`} title="Toggle this idea in the graph highlights; manage highlights in Overview search" onClick={() => { toggleIdeaHighlight(selected, allIdeas.value[selected]?.description); setSearchOpen(true); searchRef.current?.querySelector<HTMLInputElement>("input")?.focus(); }}>{isIdeaHighlighted(selected) ? "Highlighted" : "Highlight"}</button>}<button class="ui-btn" onClick={closeDetails} aria-label="Close details">×</button></div></div>
            <div class="research-inspector-content"><DetailPanel focused /></div>
          </aside>
        </div>
      </div>
    </div>
  );
}

/** "2,656 of 4,321" while a filter / text / tag selection narrows the experiments. */
function SelectionCount() {
  const total = allExperiments.value.length;
  const shown = plotExperiments.value.length;
  const narrowed = selectionLabels.value !== null;
  if (!narrowed || !total) return null;
  return <span class="research-selection-count" title="Experiments in the current filter selection">{shown.toLocaleString()} of {total.toLocaleString()}</span>;
}

function Overview() {
  const experiments = plotExperiments.value;
  const ideas = allIdeas.value;
  const metric = selectedMetric.value;
  const done = experiments.filter(e => !e._running && e.status === "completed");
  const chronological = [...done].sort((a,b) => Date.parse(a.finished_at || a.created_at || "") - Date.parse(b.finished_at || b.created_at || "") || a.id - b.id);
  const lower = isLowerBetter(metric);
  let best: number | null = null, records = 0, sinceBest = 0;
  for (const exp of chronological) {
    const score = resolveNumericValue(exp, metric);
    if (typeof score !== "number" || !Number.isFinite(score)) continue;
    if (best === null || (lower ? score < best : score > best)) { best = score; records++; sinceBest = 0; }
    else sinceBest++;
  }
  const stats = [
    { label: `Best ${fmtMetricName(metric || "score")}`, value: best == null ? "—" : fmtScoreShort(best), sub: lower ? "↓ lower is better" : "↑ higher is better", best: true },
    { label: "Experiments", value: String(done.length), sub: `${experiments.filter(e => e._running || e.status === "running").length} running` },
    { label: "Since best", value: best == null ? "—" : String(sinceBest), sub: `${records} records` },
    { label: "Ideas", value: String(Object.values(ideas).filter(i => !["abandoned", "concluded"].includes(i.status)).length), sub: "active" },
    { label: "Agent cost", value: totalAgentCost.value == null ? "—" : `$${totalAgentCost.value.toFixed(0)}`, sub: "all sessions" },
  ];
  return <main class="research-overview">
    {/* KPI cards share the graphs' header line (one row instead of three). */}
    <GraphWorkspace head={<div class="research-kpis is-compact">{stats.map(stat => <div class="research-kpi"><span class="ui-eyebrow">{stat.label}</span><strong class={stat.best ? "is-best" : ""}>{stat.value}</strong><span>{stat.sub}</span></div>)}</div>} />
    <RecentIdeas />
  </main>;
}

const REVIEW_FILTER_COLORS: Record<string, string> = {
  idea: "var(--accent)",
  tag: "var(--purple)",
  // both spellings map to the experiment color (green): "exp/" prefix chips
  // use "exp", applied pills + "90/" numeric results use "experiment".
  exp: "var(--green)",
  experiment: "var(--green)",
  text: "var(--text-muted)",
};

function ReviewSearchFilter({ experiments, ideas }: {
  experiments: import("./lib/types").Experiment[];
  ideas: Record<number, import("./lib/types").IdeaNode>;
}) {
  type Pill = ReviewPill;
  const applied = reviewSearchPills.value;
  const setApplied = (update: (previous: Pill[]) => Pill[]) => {
    const next = update(reviewSearchPills.value);
    activeTagFilters.value = next.filter(pill => pill.active !== false && pill.category === "tag").map(pill => pill.value);
    filterText.value = next.filter(pill => pill.active !== false && pill.category === "text").map(pill => pill.value).join(" ");
    reviewSearchPills.value = next;
  };

  // Other chart controls can change the same text/tag filters. Keep their
  // pills current without discarding the inspector's persistent highlights.
  useEffect(() => {
    let next = reviewSearchPills.value;
    const tags = activeTagFilters.value;
    const oldTags = next.filter(pill => pill.active !== false && pill.category === "tag").map(pill => pill.value);
    if (JSON.stringify(tags) !== JSON.stringify(oldTags)) {
      next = next.filter(pill => pill.category !== "tag" || (pill.active === false && !tags.includes(pill.value)));
      next = [...next, ...tags.map(tag => ({ id: `tag:${tag}`, category: "tag", label: tag, value: tag, active: true }))];
    }
    const text = filterText.value;
    const oldText = next.filter(pill => pill.active !== false && pill.category === "text").map(pill => pill.value).join(" ");
    if (text !== oldText) {
      next = next.filter(pill => pill.category !== "text" || pill.active === false);
      if (text.trim()) next = [...next.filter(pill => pill.id !== `text:${text}`), { id: `text:${text}`, category: "text", label: text, value: text, active: true }];
    }
    if (next !== reviewSearchPills.value) reviewSearchPills.value = next;
  }, [activeTagFilters.value, filterText.value]);

  // (#76) When `category` is set (via a category prefix in the field), return
  // EVERY matching item in that category — including all of them when `query`
  // is empty. When unset, behave as before: a mixed, capped set. The per-
  // category cap is lifted in single-category mode so the user can browse all.
  function suggest(query: string, category?: string): FilterItem[] {
    const q = query.toLowerCase();
    const out: FilterItem[] = [];
    const ideaArr = Object.values(ideas);

    // (#83) `24/` or `#24/` initiates experiment selection WITHIN idea 24 —
    // return that idea's experiments, label-matching the part after `/` (all
    // when empty). value carries the idea_id for navigation.
    const initiator = query.match(/^#?(\d+)\/(.*)$/);
    if (initiator) {
      const ideaId = Number(initiator[1]);
      const sub = initiator[2].toLowerCase();
      for (const e of experiments) {
        if (e.idea_id !== ideaId) continue;
        const lab = e.label ?? String(e.id);
        if (!sub || lab.toLowerCase().includes(sub)) {
          out.push({ id: `experiment:${e.id}`, category: "experiment", label: `exp/${lab}`, value: String(e.idea_id) });
        }
      }
      return out;
    }

    // Accept both "exp" (slash prefix) and "experiment" spellings.
    const wantIdeas = !category || category === "idea";
    const wantTags = !category || category === "tag";
    const wantExps = !category || category === "exp" || category === "experiment";
    // category-scoped: return ALL matches (everything when query empty), with a
    // sane upper bound; mixed (no category): the small 8-per-category preview.
    const cap = category ? 50 : 8;

    // ideas — by #id and description
    if (wantIdeas) {
      let n = 0;
      for (const idea of ideaArr) {
        const desc = (idea.description ?? "").split("\n")[0];
        const hay = `#${idea.id} ${desc}`.toLowerCase();
        if (!q || hay.includes(q)) {
          out.push({ id: `idea:${idea.id}`, category: "idea", label: `#${idea.id} ${desc.slice(0, 48)}`.trim(), value: String(idea.id) });
          if (++n >= cap) break;
        }
      }
    }
    // tags — campaign tag set (same source as the existing tag filter)
    if (wantTags) {
      const tagSet = new Set<string>();
      for (const e of experiments) if (e.tags) for (const t of e.tags) tagSet.add(t);
      for (const idea of ideaArr) { const it = (idea as any).tags as string[] | undefined; if (it) for (const t of it) tagSet.add(t); }
      let n = 0;
      for (const t of [...tagSet].sort()) {
        if (!q || t.toLowerCase().includes(q)) {
          out.push({ id: `tag:${t}`, category: "tag", label: t, value: t });
          if (++n >= cap) break;
        }
      }
    }
    // experiments — by label; value carries the idea_id for navigation
    if (wantExps) {
      let n = 0;
      for (const e of experiments) {
        const lab = e.label ?? String(e.id);
        if (!q || lab.toLowerCase().includes(q)) {
          out.push({ id: `experiment:${e.id}`, category: "experiment", label: `exp/${lab}`, value: String(e.idea_id) });
          if (++n >= cap) break;
        }
      }
    }
    return out;
  }

  function addPill(item: FilterItem) {
    setApplied(prev => prev.some(p => p.id === item.id) ? prev : [...prev, { ...item, active: true }]);
    // idea/experiment also navigate immediately (open the idea detail)
    if (item.category === "idea") navigateToIdea(Number(item.value));
    else if (item.category === "experiment") navigateToIdea(Number(item.value), item.label.replace(/^exp\//, ""));
  }

  function addText(text: string) {
    const id = `text:${text}`;
    setApplied(prev => prev.some(p => p.id === id) ? prev : [...prev, { id, category: "text", label: text, value: text, active: true }]);
  }

  function toggle(id: string) {
    setApplied(prev => prev.map(p => p.id === id ? { ...p, active: p.active === false } : p));
  }

  function remove(id: string) {
    setApplied(prev => prev.filter(p => p.id !== id));
  }

  return (
    <div class="review-search">
      <SearchFilter
        class="review-search-field"
        placeholder="Search & filter experiments — text, tag, #idea, exp…"
        applied={applied}
        suggest={suggest}
        onApply={addPill}
        onApplyText={addText}
        onToggle={toggle}
        onRemove={remove}
        categoryColor={(c) => REVIEW_FILTER_COLORS[c] ?? "var(--text-muted)"}
      />
    </div>
  );
}
