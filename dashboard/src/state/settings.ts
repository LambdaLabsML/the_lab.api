// ------------------------------------------------------------
// LocalStorage-backed Preact signals for user preferences.
//
// Each setting is a signal whose initial value is read from
// localStorage (falling back to a default) and whose writes
// are automatically persisted back.
//
// Server-provided defaults from GET /api/v1/config override
// the hardcoded defaults for first-time users (before localStorage
// has any values).
// ------------------------------------------------------------

import { signal, type Signal, effect } from "@preact/signals";

const PREFIX = "the-lab:";

/**
 * Create a signal that is initialised from localStorage and stays
 * in sync on every write.  The stored value is JSON-serialised so
 * any JSON-safe type works (strings, numbers, booleans, arrays, objects).
 */
export function useSetting<T>(key: string, defaultValue: T): Signal<T> {
  const storageKey = PREFIX + key;

  let initial = defaultValue;
  try {
    const raw = localStorage.getItem(storageKey);
    if (raw !== null) {
      initial = JSON.parse(raw) as T;
    }
  } catch {
    // Corrupted or missing — fall back to default.
  }

  const s = signal<T>(initial);

  // Persist every change back to localStorage.
  effect(() => {
    try {
      localStorage.setItem(storageKey, JSON.stringify(s.value));
    } catch {
      // Storage full or unavailable — silently ignore.
    }
  });

  return s;
}

// ---------------------------------------------------------------------------
// Exported user-preference signals
// ---------------------------------------------------------------------------

/** Whether the metrics chart panel is expanded. */
export const chartOpen = useSetting("chartOpen", true);

/** The currently active main view (dag | timeline | log | api). */
export const currentView = useSetting<string>("view", "dag");

/** The metric key selected in the chart dropdown. */
export const selectedMetric = useSetting("selectedMetric", "");

/** Color mode for nodes and chart points. */
export const colorMode = useSetting("colorMode", "status+improve");

/** When true, the chart shows only experiments that improved on the
 *  previous global best. Defaults to true for cleaner step-function view. */
export const improvementsOnly = useSetting("improvementsOnly", true);

/** Active tag filter chips (empty = no filtering). */
export const activeTagFilters = useSetting<string[]>("tagFilters", []);

/** Tag filter mode: "or" = match any, "and" = match all. */
export const tagFilterMode = useSetting<"or" | "and">("tagFilterMode", "and");

/** Whether the "Suggest idea" panel is expanded. */
export const suggestOpen = useSetting("suggestOpen", false);

/** Currently selected idea ID (shown in the detail panel). */
export const selectedIdea = useSetting<number | null>("selectedIdea", null);

/** When true, newest items appear on the left/top instead of right/bottom. */
export const reverseTime = useSetting("reverseTime", false);

/** Status visibility filters for graph and chart. */
export const showAbandoned = useSetting("showAbandoned", true);
export const showConcluded = useSetting("showConcluded", true);
export const showRunning = useSetting("showRunning", true);
export const clipOutliers = useSetting("clipOutliers", false);
export const ideaMean = useSetting("ideaMean", false);

/** Scatter chart axis metric selections. */
export const scatterXMetric = useSetting("scatterXMetric", "");
export const scatterYMetric = useSetting("scatterYMetric", "");
/** Whether the scatter chart column is visible. */
export const scatterOpen = useSetting("scatterOpen", true);
/** Free-text filter for experiment/idea names. */
export const filterText = useSetting("filterText", "");
/** Whether the filter bar (tags + status) is expanded. */
export const filterBarOpen = useSetting("filterBarOpen", true);

/** Persisted dockview layout JSON (null = use default). */
export const dashboardLayout = useSetting<object | null>("dashboardLayout", null);

/** Whether the detail panel shows notes+experiments interleaved in a timeline. */
export const detailTimeline = useSetting("detailTimeline", true);
/** When true, newest items appear first in the detail panel. */
export const detailSortNewest = useSetting("detailSortNewest", true);
/** Idea panel: show the notes boxes between the experiments. */
export const detailShowNotes = useSetting("detailShowNotes", true);
/** Show cancelled experiments (idea panel, Recent list); off = hidden. */
export const showCancelled = useSetting("showCancelled", false);

/** Active colour theme name. Applied as data-theme on <html>. */
export const colorTheme = useSetting<string>("colorTheme", "default");
/** Contrast on top of the theme: -2 (soft) … 0 (as designed) … +2 (high). */
export const uiContrast = useSetting<number>("uiContrast", 0);

// ---------------------------------------------------------------------------
// Server-provided defaults from GET /api/v1/config
// ---------------------------------------------------------------------------

const CONFIG_KEYS: Record<string, Signal<any>> = {
  tagFilters: activeTagFilters,
  tagFilterMode: tagFilterMode,
  selectedMetric: selectedMetric,
  colorMode: colorMode,
  improvementsOnly: improvementsOnly,
  reverseTime: reverseTime,
  showAbandoned: showAbandoned,
  showConcluded: showConcluded,
  showRunning: showRunning,
};

/**
 * Fetch server config and apply defaults for keys that have no
 * localStorage value yet (first-time users). Called once on startup.
 */
/** True when the server runs in read-only demo mode (`the-lab serve --demo`).
 *  Mutating UI affordances hide themselves; the rail shows a demo badge. */
export const serverDemoMode = signal(false);

export async function applyServerDefaults(): Promise<void> {
  try {
    const resp = await fetch("/api/v1/config");
    if (!resp.ok) return;
    const config = await resp.json();
    if (config.demo === true) serverDemoMode.value = true;
    for (const [key, sig] of Object.entries(CONFIG_KEYS)) {
      if (config[key] === undefined) continue;
      // Only apply if localStorage has no value for this key
      const storageKey = PREFIX + key;
      if (localStorage.getItem(storageKey) === null) {
        sig.value = config[key];
      }
    }
  } catch {
    // Config endpoint not available — use hardcoded defaults.
  }
}

/** Active font family. Applied as data-font-family on <html>. "mono" = default (no override). */
export const fontFamily = useSetting<string>("fontFamily", "mono");

/** Active font size step. Applied as data-font-size on <html>. "m" = default (no override). */
export const fontSize = useSetting<string>("fontSize", "m");

/** Show a step-function "current best" line on the metrics chart. */
export const showBestLine = useSetting("showBestLine", true);

/** Minified chart: tiny dots, no axis labels — global overview mode. */
export const chartMinified = useSetting("chartMinified", false);

/** Chart point/dot size: s | m | l, or x = no dots, time-scaled x axis fit to width. */
export const chartPointSize = useSetting<"s" | "m" | "l" | "none" | "x">("chartPointSize", "m");
const chartTimeAxisLegacy = useSetting<boolean>("chartTimeAxis", false);

/** Metrics chart view: x = purple line only, fit to the width; s / m / l =
 *  dots with the purple line, from tightly packed tiny dots (s) to roomy (l). Replaces chartPointSize (+ chartTimeAxis),
 *  whose s/m/l meant something else: a stored time-axis / no-dots choice
 *  becomes x, anything else l (the old default look). */
export const chartView = useSetting<"x" | "s" | "m" | "l">(
  "chartView",
  chartPointSize.value === "x" || chartPointSize.value === "none" || chartTimeAxisLegacy.value ? "x" : "l",
);

/** Metrics chart x axis: experiment order, experiments grouped by idea,
 *  finish time, or cumulative experiment runtime. Until now the x view was the
 *  time axis and s/m/l experiment order, so the default follows the view. */
export const chartXAxis = useSetting<"experiments" | "ideas" | "time" | "compute">(
  "chartXAxis",
  chartView.value === "x" ? "time" : "experiments",
);

/** The purple line: best-so-far step line, or moving average. */
export const chartTimeCurve = useSetting<"best" | "avg">("chartTimeCurve", "best");
/** Main chart point labels (#idea): off, records only, or as many as fit. */
export const chartPointLabels = useSetting<"off" | "records" | "auto">("chartPointLabels", "auto");

/** Review metrics-graph height in px (user-adjustable via chevron steppers). */
export const reviewChartHeight = useSetting("reviewChartHeight", 360);

/** Per-section, per-mode Overview content heights, keyed "<sectionId>:<summary|detail>".
 *  Summary mode → visible row count; detail mode → panel height (px). Persisted. */
export const reviewSectionHeights = useSetting<Record<string, number>>("reviewSectionHeights", {});

/** Colorblind-friendly status color overrides. */
export const colorblindMode = useSetting("colorblindMode", false);

/** Graph node display mode. true = full labeled nodes ("Text"); false = compact
 *  node-only pill map ("Mini"). The graph toolbar offers Text vs Mini as a single
 *  exclusive choice. Defaults to Mini for a clean overview of the idea branch. */
export const showNodeText = useSetting("showNodeText", false);

/** Width (px) of the left secondary nav panel, and whether it's collapsed. */
export const sidebarWidth = useSetting("sidebarWidth", 248);
export const sidebarCollapsed = useSetting("sidebarCollapsed", false);

/** Per-section open/closed (summary/detail) state of the Overview disclosures. */
export const reviewOpenSections = useSetting<Record<string, boolean>>("reviewOpenSections", {});

/** Slight hacker-noise ambient texture behind the UI. Applied as data-texture. */
export const uiTexture = useSetting("uiTexture", true);

/** Message ids the UI user has personally marked read. Client-side only (per
 *  browser) — kept separate from each message's agent `read_by` list. */
export const messagesReadByMe = useSetting<Record<string, boolean>>("messagesReadByMe", {});
/** This browser's name ("David"): the primary human identity, sent as
 *  `as_user` on every message/thread this browser composes (stored as
 *  "user:<slug>"). Per browser, until real accounts exist. */
export const myName = useSetting<string>("myName", "");
/** Extra names/aliases this browser's user also goes by ("Dave, DH"):
 *  mentioning one inside a thread post still counts as addressed to them,
 *  on top of their primary name above. */
export const messagesMyNames = useSetting<string>("messagesMyNames", "");
/** Messages thread list: which threads to show by status. */
export const threadStatusFilter = useSetting<"open" | "closed" | "all">("threadStatusFilter", "open");
/** Messages thread list: only threads opened by you. */
export const threadOpenedByMe = useSetting<boolean>("threadOpenedByMe", false);

// Migration: seed the primary name from the first alias already set (the
// pre-identity "Your names" field), once — if myName has never been set and
// an alias exists, adopt it as the primary name. Persists itself via
// useSetting's effect, so it only ever runs on a browser's first load after
// this feature shipped.
if (!myName.value) {
  const first = messagesMyNames.value.split(/[,;]/).map((s) => s.trim()).filter(Boolean)[0];
  if (first) myName.value = first;
}

/** Detail rows shown per agent in the activity tree (stepper-adjustable). */
export const agentHistoryLimit = useSetting("agentHistoryLimit", 3);

// ---------------------------------------------------------------------------
// One-time default migrations
//
// localStorage always wins over a hardcoded default, so changing a default
// never reaches users who already have a stored value. When we change an
// *intended* default and want it to land on existing sessions too, bump
// SETTINGS_DEFAULTS_VERSION and add a guarded re-apply below: it overrides the
// stored value exactly once, then records the new version so the user's later
// toggles persist normally.
// ---------------------------------------------------------------------------
const DEFAULTS_VERSION_KEY = PREFIX + "defaultsVersion";
const SETTINGS_DEFAULTS_VERSION = 1;

(function applyDefaultMigrations() {
  let v = 0;
  try {
    v = Number(localStorage.getItem(DEFAULTS_VERSION_KEY)) || 0;
  } catch {
    // Storage unavailable — treat as fresh.
  }
  if (v >= SETTINGS_DEFAULTS_VERSION) return;

  // v < 1 — "Step" (improvements-only) is the default chart flag. Re-apply once
  // so sessions that toggled to "All" before this default land on the
  // step-function view too.
  if (v < 1) improvementsOnly.value = true;

  try {
    localStorage.setItem(DEFAULTS_VERSION_KEY, String(SETTINGS_DEFAULTS_VERSION));
  } catch {
    // Storage unavailable — skip; migration will retry next load.
  }
})();
