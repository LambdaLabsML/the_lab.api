import { useEffect, useMemo } from "preact/hooks";
import { effect, signal, type Signal } from "@preact/signals";
import {
  chartView, chartXAxis, chartTimeCurve, improvementsOnly, ideaMean,
  clipOutliers, reverseTime, showHiddenExperiments, colorMode, filterText, useSetting,
} from "../../state/settings";

const plotFilter = signal("");
const logScale = useSetting("mainGraphLogScale", false);
const shared = { chartView, chartXAxis, chartTimeCurve, improvementsOnly, ideaMean, clipOutliers, reverseTime, showHiddenExperiments, colorMode, logScale, plotFilter };
type Controls = typeof shared;
type Values = { [K in keyof Controls]: Controls[K] extends Signal<infer V> ? V : never };

/** A new plot snapshots the main controls once, then persists its own choices. */
export function usePlotControls(id?: string): Controls {
  const controls = useMemo(() => {
    if (!id) return shared;
    let saved: Partial<Values> = {};
    try { saved = JSON.parse(localStorage.getItem(`the-lab:plotControls:${id}`) || "{}"); } catch { /* defaults */ }
    const entries = Object.entries(shared).map(([key, source]) => {
      const initial = source.peek();
      const stored = saved?.[key as keyof Values];
      return [key, signal(typeof stored === typeof initial ? stored : initial)];
    });
    return Object.fromEntries(entries) as Controls;
  }, [id]);
  useEffect(() => {
    if (!id) return;
    return effect(() => {
      const values = Object.fromEntries(Object.entries(controls).map(([key, control]) => [key, control.value]));
      try { localStorage.setItem(`the-lab:plotControls:${id}`, JSON.stringify(values)); } catch { /* storage unavailable */ }
    });
  }, [controls, id]);
  return controls;
}

/** Workspace search applies to all plots; a plot can additionally narrow its data. */
export function matchesPlotSearch(exp: import("../types").Experiment, ideaDescription: string, localQuery: string): boolean {
  const haystack = `${exp.label ?? exp.id} ${exp.description} ${ideaDescription} ${(exp.tags ?? []).join(" ")}`.toLowerCase();
  return [filterText.value, localQuery].every(query => query.toLowerCase().trim().split(/\s+/).filter(Boolean).every(word => haystack.includes(word)));
}
