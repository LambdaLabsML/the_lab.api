// ------------------------------------------------------------
// LocalStorage-backed Preact signals for user preferences.
//
// Each setting is a signal whose initial value is read from
// localStorage (falling back to a default) and whose writes
// are automatically persisted back.
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

/** Whether the "Suggest idea" panel is expanded. */
export const suggestOpen = useSetting("suggestOpen", false);

/** Currently selected idea ID (shown in the detail panel). */
export const selectedIdea = useSetting<number | null>("selectedIdea", null);
