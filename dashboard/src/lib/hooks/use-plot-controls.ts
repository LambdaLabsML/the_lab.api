import { useEffect, useMemo } from "preact/hooks";
import { effect, signal, type Signal } from "@preact/signals";
import {
  chartView, chartXAxis, chartTimeCurve, improvementsOnly, ideaMean,
  clipOutliers, reverseTime, colorMode, filterText, useSetting,
} from "../../state/settings";

const plotFilter = signal("");
const logScale = useSetting("mainGraphLogScale", false);
export const mainPlotControls = { chartView, chartXAxis, chartTimeCurve, improvementsOnly, ideaMean, clipOutliers, reverseTime, colorMode, logScale, plotFilter };
const shared = mainPlotControls;
type Controls = typeof shared;
export type PlotValues = { [K in keyof Controls]: Controls[K] extends Signal<infer V> ? V : never };

export const controlsRevision = signal(0);
const registry = new Map<string, Controls>();
export function readPlotControls(id?: string): PlotValues {
  const controls = id ? registry.get(id) : shared;
  if (controls) return Object.fromEntries(Object.entries(controls).map(([key, value]) => [key, value.value])) as PlotValues;
  try { return { ...readPlotControls(), ...JSON.parse(localStorage.getItem(`the-lab:plotControls:${id}`) || "{}") }; } catch { return readPlotControls(); }
}
export function restorePlotControls(values: PlotValues, id?: string) {
  const controls = id ? registry.get(id) : shared;
  if (controls) for (const [key, value] of Object.entries(values)) (controls[key as keyof Controls] as Signal<unknown>).value = value;
  if (id) localStorage.setItem(`the-lab:plotControls:${id}`, JSON.stringify(values));
  controlsRevision.value++;
}

/** A new plot snapshots the main controls once, then persists its own choices. */
export function usePlotControls(id?: string): Controls {
  const controls = useMemo(() => {
    if (!id) return shared;
    if (registry.has(id)) return registry.get(id)!;
    let saved: Partial<PlotValues> = {};
    try { saved = JSON.parse(localStorage.getItem(`the-lab:plotControls:${id}`) || "{}"); } catch { /* defaults */ }
    const entries = Object.entries(shared).map(([key, source]) => {
      const initial = source.peek();
      const stored = saved?.[key as keyof PlotValues];
      return [key, signal(typeof stored === typeof initial ? stored : initial)];
    });
    const result = Object.fromEntries(entries) as Controls;
    registry.set(id, result);
    return result;
  }, [id]);
  useEffect(() => {
    if (!id) return;
    controlsRevision.value++;
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
