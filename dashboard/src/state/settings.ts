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

/** Active colour theme name. Applied as data-theme on <html>. */
export const colorTheme = useSetting<string>("colorTheme", "default");

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
export async function applyServerDefaults(): Promise<void> {
  try {
    const resp = await fetch("/api/v1/config");
    if (!resp.ok) return;
    const config = await resp.json();
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

/** Chart point/dot size: s | m | l. */
export const chartPointSize = useSetting<"s" | "m" | "l">("chartPointSize", "m");

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
