import type { ComponentChildren } from "preact";
import { useState } from "preact/hooks";
import { useSetting, selectedMetric, scatterXMetric, scatterYMetric, reviewChartHeight } from "../../state/settings";
import { Stepper } from "../ui";
import { MetricsChart } from "./metrics-chart";
import { LazyScatterChart } from "./lazy-scatter";

import { overviewPlots as plots, type Plot } from "../../state/plots";

/** ``head``: shown on the left of the workspace's header line (the Overview
 *  puts its KPI cards there); the "+" add-graph menu stays on the right. */
export function GraphWorkspace({ head }: { head?: ComponentChildren } = {}) {
  const [adding, setAdding] = useState(false);
  function add(type: Plot["type"]) {
    plots.value = [...plots.value, { id: globalThis.crypto?.randomUUID?.() ?? `plot-${Date.now()}-${Math.random().toString(36).slice(2)}`, type, height: reviewChartHeight.value, width: "full", metric: selectedMetric.value, x: scatterXMetric.value, y: scatterYMetric.value }];
    setAdding(false);
  }
  function update(id: string, changes: Partial<Plot>) {
    const plot = plots.value.find(p => p.id === id);
    if (!plot || Object.entries(changes).every(([key,value]) => plot[key as keyof Plot] === value)) return;
    plots.value = plots.value.map(p => p.id === id ? { ...p, ...changes } : p);
  }
  return <section class="graph-workspace" aria-label="Graphs">
    <div class="research-panel-head graph-workspace-head">
      {head ?? <strong>Graphs</strong>}
      <div class="graph-actions">
        <div class="graph-add">
          <button class="ui-btn graph-add-button" aria-label="Add graph" title="Add a graph" aria-expanded={adding} onClick={() => setAdding(v => !v)}>+</button>
          {adding && <>
            <button class="graph-menu-dismiss" tabIndex={-1} aria-label="Dismiss graph menu" onClick={() => setAdding(false)} />
            <div class="graph-add-menu" onKeyDown={e => { if (e.key === "Escape") setAdding(false); }}>
              <button class="ui-btn" onClick={() => add("metrics")}>Performance graph</button>
              <button class="ui-btn" onClick={() => add("scatter")}>Correlation plot</button>
            </div>
          </>}
        </div>
      </div>
    </div>
    <div class="graph-grid">
      <article class="graph-card graph-card-main">
        <div class="graph-card-head"><span class="ui-eyebrow">Performance</span><PlotHeight value={reviewChartHeight.value} onChange={height => { reviewChartHeight.value = height; }} /></div>
        <div class="graph-canvas" style={{ height: `${reviewChartHeight.value}px` }}><MetricsChart hideClone /></div>
      </article>
      {plots.value.map(plot => <article class={`graph-card${plot.width === "half" ? " is-half" : ""}`} key={plot.id}>
        <div class="graph-card-head"><span class="ui-eyebrow">{plot.type === "metrics" ? "Performance" : "Correlation"}</span><div class="graph-card-controls"><label class="graph-placement"><span class="ui-eyebrow">Width</span><select aria-label="Graph width" value={plot.width ?? "full"} onChange={e => update(plot.id, { width: e.currentTarget.value as Plot["width"] })}><option value="full">Full width</option><option value="half">Side by side</option></select></label><PlotHeight value={plot.height ?? 360} onChange={height => update(plot.id, { height })} /><button class="ui-btn" aria-label={`Remove ${plot.type === "metrics" ? "performance graph" : "correlation plot"}`} onClick={() => { plots.value = plots.value.filter(p => p.id !== plot.id); }}>×</button></div></div>
        <div class="graph-canvas" style={{ height: `${plot.height ?? 360}px` }}>
          {plot.type === "metrics" ? <MetricsChart instanceId={plot.id} initialMetric={plot.metric || selectedMetric.value} onMetricChange={metric => update(plot.id, { metric })} hideClone /> : <LazyScatterChart instanceId={plot.id} initialXMetric={plot.x} initialYMetric={plot.y} onAxesChange={(x,y) => update(plot.id, {x,y})} hideClone />}
        </div>
      </article>)}
    </div>
  </section>;
}

/** The original compact height stepper, shared by every plot. */
function PlotHeight({ value, onChange }: { value: number; onChange: (height: number) => void }) {
  return <span class="graph-height"><span class="ui-eyebrow">Height</span><span class="graph-height-value">{value}px</span><Stepper value={value} min={220} max={760} step={60} what="graph height" onChange={onChange} /></span>;
}
