import { usePlotControls, matchesPlotSearch } from "../../lib/hooks/use-plot-controls";
import { CtlDivider, CtlSearch, CtlSeg, CtlToggle, MetricPicker } from "./chart-controls";
import { inSelection, selectionLabels } from "../../state/filters";
import { useEffect, useMemo, useState } from "preact/hooks";
import { allExperiments, allIdeas, currentLayout, cloneChartPanel, updatePanelTitle } from "../../state/signals";
import {
  selectedMetric, filterText, activeTagFilters, tagFilterMode,
  showAbandoned, showConcluded, showRunning, colorblindMode, chartPointLabels } from "../../state/settings";
import { collectChartKeys } from "../../lib/chart-data";
import { isLowerBetter } from "../../lib/colors";
import { fmtMetricName, fmtScoreShort, scoreFormatter } from "../../lib/format";
import { MiniMetricsChart } from "./mini-metrics-chart";
import { Toggle, IconButton } from "../ui";

export function MetricsChart({ instanceId, initialMetric, hideClone, onMetricChange }: { instanceId?: string; initialMetric?: string; hideClone?: boolean; onMetricChange?: (metric: string) => void } = {}) {
  // Cloned instances use local state; the original uses the global signal
  const isClone = !!instanceId;
  const [localMetric, setLocalMetric] = useState(initialMetric || "");
  const { chartView, chartXAxis, chartTimeCurve, improvementsOnly, ideaMean, clipOutliers, reverseTime, colorMode, plotFilter, logScale: logScaleSignal } = usePlotControls(instanceId);
  const logScale = logScaleSignal.value;
  const setLogScale = (value: boolean) => { logScaleSignal.value = value; };

  const allRows = allExperiments.value;
  const selection = selectionLabels.value;
  const localQuery = plotFilter.value;
  const globalQuery = filterText.value;
  const ideaMap = allIdeas.value;
  const experiments = useMemo(() => allRows.filter(e => inSelection(e) && matchesPlotSearch(e, ideaMap[e.idea_id]?.description ?? "", localQuery)), [allRows, selection, localQuery, globalQuery, ideaMap]);
  const ideas = allIdeas.value;
  const layout = currentLayout.value;
  const metric = isClone ? localMetric : selectedMetric.value;
  const mode = colorMode.value;
  const impOnly = improvementsOnly.value;
  const tags = activeTagFilters.value;
  const tagMode = tagFilterMode.value;
  const reversed = reverseTime.value;
  const clip = clipOutliers.value;
  const mean = ideaMean.value;
  const view = chartView.value;
  const timeCurve = chartTimeCurve.value;
  const xAxis = chartXAxis.value;
  const _cb = colorblindMode.value; // subscribe — colors recompute on change

  // Idea-status filters (abandoned/concluded). The "running" toggle is
  // experiment-level — see hideRunning below — so completed experiments
  // under still-active ideas remain visible when running is toggled off.
  const hiddenStatuses = new Set<string>();
  if (!showAbandoned.value) hiddenStatuses.add("abandoned");
  if (!showConcluded.value) hiddenStatuses.add("concluded");
  const hideRunning = !showRunning.value;

  // Collect chartable keys grouped by source
  const grouped = useMemo(() => collectChartKeys(allRows), [allRows]);
  const allKeys = [...grouped.metrics, ...grouped.nested, ...grouped.meta, ...grouped.timing];

  useEffect(() => { if (metric) onMetricChange?.(metric); }, [metric]);

  const setMetric = isClone
    ? (v: string) => { setLocalMetric(v); if (instanceId && updatePanelTitle) updatePanelTitle(instanceId, `Metrics: ${v}`); }
    : (v: string) => { selectedMetric.value = v; };

  // Auto-select best metric via useEffect. Re-runs when allKeys changes (more experiments load).
  // Also upgrades to preferred metrics if a non-preferred metric was selected initially.
  const allKeysStr = allKeys.join(',');
  useEffect(() => {
    if (isClone) return;
    if (allKeys.length === 0) return;
    const preferred = [
      "accuracy_per_mtoken", "agent_accuracy", "accuracy",
      "score", "total_score", "progress_score", "final_score",
      "f1", "bleu", "rouge", "pass_at_1", "pass_at_10",
    ];
    const current = selectedMetric.peek();
    if (current && allKeys.includes(current)) return;
    const alreadyPreferred = current && preferred.includes(current);
    if (alreadyPreferred) return; // already on a good metric
    // Pick best preferred metric: prefer non-zero values AND meaningful variance (not constants)
    const nonZeroKeys = allKeys.filter(k => {
      const lower = isLowerBetter(k);
      const vals = experiments
        .filter(e => !e._running && typeof e.metrics?.[k] === "number")
        .map(e => e.metrics![k] as number);
      if (vals.length === 0) return false;
      const hasNonZero = lower ? vals.some(v => v < Infinity) : vals.some(v => v > 0);
      if (!hasNonZero) return false;
      // Exclude constant metrics (all same value) — they're config params, not performance
      const hasVariance = vals.some(v => v !== vals[0]);
      return hasVariance;
    });
    const keyPool = nonZeroKeys.length > 0 ? nonZeroKeys : allKeys;
    const best = preferred.find(k => keyPool.includes(k)) || preferred.find(k => allKeys.includes(k));
    if (best && best !== current) setMetric(best);
    else if (!current) setMetric(keyPool[0] || allKeys[0]);
  }, [allKeysStr, isClone]); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <div class="chart-toolbar ctl-toolbar">
        {isClone && <CtlSearch value={plotFilter.value} onInput={v => { plotFilter.value = v; }} placeholder="Filter this graph…" />}
        <MetricPicker icon="y" title="Metric (Y axis)" value={metric} onChange={setMetric} format={fmtMetricName} wide groups={[
          { label: "Metrics", keys: grouped.metrics }, { label: "Nested", keys: grouped.nested },
          { label: "Timing", keys: grouped.timing }, { label: "Meta", keys: grouped.meta },
        ]} />
        {(() => {
          // Count milestones for the Step button label
          const lower = isLowerBetter(metric);
          let best: number | null = null, mCount = 0;
          for (const e of experiments) {
            if (e._running) continue;
            const v = e.metrics?.[metric];
            if (typeof v !== "number") continue;
            if (best === null || (lower ? v < best : v > best)) { best = v; mCount++; }
          }
          return (
            <CtlToggle active={impOnly} icon={impOnly ? "step" : "all"} onClick={() => { improvementsOnly.value = !impOnly; }}
              title={impOnly ? `Step: record-setting runs + runs since the last record (${mCount} records). Click for all experiments.` : "All experiments — click for the step view (records only)"}>
              {impOnly ? `Step${mCount > 0 ? ` ${mCount}` : ""}` : "All"}
            </CtlToggle>
          );
        })()}
        <CtlToggle active={mean} icon="mean" onClick={() => { ideaMean.value = !mean; }} title="Idea mean: one point per idea (mean of its runs)">mean</CtlToggle>
        <CtlToggle active={clip} icon="outliers" onClick={() => { clipOutliers.value = !clip; }} title="Hide outliers (values far outside the rest)" />
        <CtlToggle active={logScale} icon="log" onClick={() => { setLogScale(!logScale); }} title="Logarithmic Y axis">log</CtlToggle>
        <CtlDivider />
        <CtlSeg icon="axis" name="X axis" value={xAxis} onChange={(a) => { chartXAxis.value = a; }} options={[
          { value: "experiments", icon: "ax-exp", label: "exp", title: "X axis: experiments in order" },
          { value: "ideas", icon: "ax-idea", label: "idea", title: "X axis: experiments grouped by idea (ideas in order of their first experiment)" },
          { value: "time", icon: "ax-time", label: "time", title: "X axis: finish time" },
          { value: "compute", icon: "ax-hours", label: "hours", title: "X axis: cumulative experiment runtime (hours) — score against compute spent" },
        ]} />
        <CtlSeg icon="view" name="View: point size and spacing" value={view} onChange={(v) => { chartView.value = v; }} options={[
          { value: "x", icon: "v-line", title: "Line only, fit to the full width" },
          { value: "s", icon: "v-s", title: "Tiniest dots, densest packing" },
          { value: "m", icon: "v-m", title: "Small, tightly packed dots" },
          { value: "l", icon: "v-l", title: "Roomy: larger dots, wide spacing" },
        ]} />
        <CtlSeg icon="line" name="Purple line" value={timeCurve} onChange={(c) => { chartTimeCurve.value = c; }} options={[
          { value: "best", icon: "l-max", label: "max", title: "Purple line: best so far" },
          { value: "avg", icon: "l-avg", label: "avg", title: "Purple line: moving average (window sized from result count and noise)" },
        ]} />
        <CtlSeg icon="labels" name="Point labels (#idea)" value={chartPointLabels.value} onChange={(m) => { chartPointLabels.value = m; }} options={[
          { value: "off", icon: "lab-off", title: "No point labels (hovering still labels an idea's points)" },
          { value: "records", icon: "lab-rec", title: "Label the record-setting points and the runs in progress" },
          { value: "auto", icon: "lab-auto", title: "Records and running first, then the best scores, then anything else that fits without overlapping" },
        ]} />
        {/* Clone is hidden in the single-chart review (hideClone); kept in the workbench */}
        {!hideClone && (
          <span class="chart-toolbar-end">
            <IconButton onClick={() => { if (cloneChartPanel) cloneChartPanel("metrics", metric); }} title="Clone this chart as a new tab">
              + Clone
            </IconButton>
          </span>
        )}
      </div>
      {/* Summary line: count, median, best — of idea means in mean mode. */}
      {(() => {
        const done = experiments.filter(e => !e._running && typeof e.metrics?.[metric] === "number");
        let vals = done.map(e => e.metrics![metric] as number);
        let unit = "exp";
        if (mean) {
          const by = new Map<number, { s: number; n: number }>();
          for (const e of done) { const g = by.get(e.idea_id) ?? { s: 0, n: 0 }; g.s += e.metrics![metric] as number; g.n += 1; by.set(e.idea_id, g); }
          vals = [...by.values()].map(g => g.s / g.n);
          unit = "idea means";
        }
        if (vals.length < 5) return null;
        const lower = isLowerBetter(metric);
        const peak = lower ? Math.min(...vals) : Math.max(...vals);
        const median = [...vals].sort((a,b) => a-b)[Math.floor(vals.length/2)];
        // Records: new bests in run order (what Step shows).
        let records = 0, best: number | null = null;
        for (const e of [...done].sort((a, b) => (a.finished_at || a.created_at || "").localeCompare(b.finished_at || b.created_at || ""))) {
          const v = e.metrics![metric] as number;
          if (best === null || (lower ? v < best : v > best)) { best = v; records++; }
        }
        // Show hint only when data is sparse at top (median < 30% of peak)
        if (peak === 0 || median / peak > 0.3) return null;
        return (
          <div class="chart-dist-hint">
            {vals.length} {unit}
            {impOnly && !mean && (
              <span style={{ color: "var(--text-faint)", fontSize: "var(--text-xs)" }}>
                {" "}({records} records)
              </span>
            )}
            <span style={{ color: "var(--text-faint)" }}> · </span>
            <span style={{ color: "var(--border-soft)", fontWeight: 600 }}>·</span>
            {" "}median {scoreFormatter([median, peak])(median)}
            <span style={{ color: "var(--text-faint)" }}> · </span>
            <span style={{ color: "var(--purple)", fontWeight: 600 }}>·</span>
            {" "}best {scoreFormatter([median, peak])(peak)}
            {impOnly && experiments.some(e => e._running) && (
              <>
                <span style={{ color: "var(--text-faint)" }}> · </span>
                <span style={{ color: "var(--yellow)", fontSize: "var(--text-xs)" }}>△ running</span>
              </>
            )}
          </div>
        );
      })()}
      {/* Empty state — shown when no metric selected or no data yet */}
      {(!metric || allKeys.length === 0) && (
        <div style={{ flex: 1, display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", gap: 10, padding: "8px 16px" }}>
          {allKeys.length === 0 ? (
            <>
              <span style={{ fontSize: "var(--text-xl)", opacity: 0.3, fontFamily: "var(--font-mono)" }}>◈</span>
              <span style={{ fontSize: "var(--text-sm)", color: "var(--text-faint)", fontFamily: "var(--font-mono)" }}>loading experiments…</span>
              <span style={{ fontSize: "var(--text-xs)", opacity: 0.4, fontFamily: "var(--font-mono)", color: "var(--text-faint)" }}>waiting for data</span>
            </>
          ) : (
            <>
              {/* Featured metric cards — quick overview of key metrics */}
              <div style={{ display: "flex", gap: 8, flexWrap: "wrap", justifyContent: "center", width: "100%" }}>
                {["score","n_levels_completed","accuracy","f1","total_score","pass_at_1"]
                  .filter(k => allKeys.includes(k))
                  .slice(0, 4)
                  .map(k => {
                    const lower = isLowerBetter(k);
                    const vals = experiments.filter(e => !e._running && typeof e.metrics?.[k] === "number").map(e => e.metrics![k] as number);
                    const best = vals.length > 0 ? (lower ? Math.min(...vals) : Math.max(...vals)) : null;
                    const fmt = best === null ? "--" : fmtScoreShort(best);
                    return (
                      <div key={k}
                        class="chart-metric-card"
                        onClick={(ev) => { ev.stopPropagation(); setMetric(k); }}
                        title={`Click to chart ${k.replace(/_/g, " ")}`}
                      >
                        <span style={{ fontSize: "var(--text-xs)", color: "var(--text-faint)", fontFamily: "var(--font-mono)", textTransform: "uppercase", letterSpacing: "0.05em" }}>{k.replace(/_/g, " ")}</span>
                        <span style={{ fontSize: "calc(var(--text-xl) * 1.4)", fontWeight: 700, fontFamily: "var(--font-mono)", color: best !== null ? "var(--purple)" : "var(--text-faint)", lineHeight: 1 }}>{fmt}</span>
                        {best !== null && <span style={{ fontSize: "var(--text-xs)", color: "var(--text-faint)" }}>★ best · {vals.length} exp</span>}
                      </div>
                    );
                  })
                }
              </div>
              <span style={{ fontSize: "var(--text-sm)", opacity: 0.45, fontFamily: "var(--font-mono)", color: "var(--text-faint)" }}>click any card or use the metric selector above ↑</span>
            </>
          )}
        </div>
      )}
      <div id="chart-wrap" style={{ flex: 1, minHeight: 0, display: (!metric || allKeys.length === 0) ? "none" : undefined }}>
        <div style={{ width: "100%", height: "100%" }}>
          <MiniMetricsChart
            metric={metric}
            experiments={experiments}
            ideas={ideas}
            layout={layout}
            hiddenStatuses={hiddenStatuses}
            hideRunning={hideRunning}
            impOnly={impOnly}
            colorMode={mode}
            tags={tags}
            tagMode={tagMode}
            reversed={reversed}
            mean={mean}
            logScale={logScale}
            clip={clip}
            pointSize={view}
            timeCurve={timeCurve}
            xAxis={xAxis}
          />
        </div>
      </div>
    </div>
  );
}
