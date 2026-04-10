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
 *  previous global best. */
export const improvementsOnly = useSetting("improvementsOnly", false);

/** Active tag filter chips (empty = no filtering). */
export const activeTagFilters = useSetting<string[]>("tagFilters", []);

/** Tag filter mode: "or" = match any, "and" = match all. */
export const tagFilterMode = useSetting<"or" | "and">("tagFilterMode", "and");

/** Whether the "Suggest idea" panel is expanded. */
export const suggestOpen = useSetting("suggestOpen", false);

/** Currently selected idea ID (shown in the detail panel). */
export const selectedIdea = useSetting<number | null>("selectedIdea", null);

/** When true, newest items appear on the left/top instead of right/bottom. */
export const reverseTime = useSetting("reverseTime", true);

/** Status visibility filters for graph and chart. */
export const showAbandoned = useSetting("showAbandoned", true);
export const showConcluded = useSetting("showConcluded", true);
export const showRunning = useSetting("showRunning", true);
export const clipOutliers = useSetting("clipOutliers", true);
export const ideaMean = useSetting("ideaMean", false);

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
