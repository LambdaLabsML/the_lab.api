import { useEffect, useMemo, useState } from "preact/hooks";
import { allExperiments, allIdeas, currentLayout, cloneChartPanel, updatePanelTitle } from "../../state/signals";
import {
  selectedMetric,
  colorMode,
  improvementsOnly,
  activeTagFilters,
  tagFilterMode,
  reverseTime,
  showAbandoned,
  showConcluded,
  showRunning,
  clipOutliers,
  ideaMean,
  chartPointSize,
  colorblindMode,
} from "../../state/settings";
import { collectChartKeys } from "../../lib/chart-data";
import { isLowerBetter } from "../../lib/colors";
import { fmtMetricName } from "../../lib/format";
import { MiniMetricsChart } from "./mini-metrics-chart";
import { Toggle, IconButton } from "../ui";

export function MetricsChart({ instanceId, initialMetric, hideClone }: { instanceId?: string; initialMetric?: string; hideClone?: boolean } = {}) {
  // Cloned instances use local state; the original uses the global signal
  const isClone = !!instanceId;
  const [localMetric, setLocalMetric] = useState(initialMetric || "");
  const [logScale, setLogScale] = useState(false);

  const experiments = allExperiments.value;
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
  const pointSize = chartPointSize.value;
  const _cb = colorblindMode.value; // subscribe — colors recompute on change

  // Idea-status filters (abandoned/concluded). The "running" toggle is
  // experiment-level — see hideRunning below — so completed experiments
  // under still-active ideas remain visible when running is toggled off.
  const hiddenStatuses = new Set<string>();
  if (!showAbandoned.value) hiddenStatuses.add("abandoned");
  if (!showConcluded.value) hiddenStatuses.add("concluded");
  const hideRunning = !showRunning.value;

  // Collect chartable keys grouped by source
  const grouped = useMemo(() => collectChartKeys(experiments), [experiments]);
  const allKeys = [...grouped.metrics, ...grouped.nested, ...grouped.meta, ...grouped.timing];

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
    const current = selectedMetric.value;
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
      <div class="chart-toolbar">
        <span>
          Metric:{" "}
          <select value={metric} onChange={(e) => { setMetric((e.target as HTMLSelectElement).value); }}>
            {grouped.metrics.length > 0 && <optgroup label="Metrics">{grouped.metrics.map((k) => <option key={k} value={k}>{fmtMetricName(k)}</option>)}</optgroup>}
            {grouped.nested.length > 0 && <optgroup label="Nested">{grouped.nested.map((k) => <option key={k} value={k}>{fmtMetricName(k)}</option>)}</optgroup>}
            {grouped.timing.length > 0 && <optgroup label="Timing">{grouped.timing.map((k) => <option key={k} value={k}>{fmtMetricName(k)}</option>)}</optgroup>}
            {grouped.meta.length > 0 && <optgroup label="Meta">{grouped.meta.map((k) => <option key={k} value={k}>{fmtMetricName(k)}</option>)}</optgroup>}
          </select>
        </span>
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
            <Toggle active={impOnly} onClick={() => { improvementsOnly.value = !impOnly; }} title={impOnly ? `Step-function: milestones + experiments since last record (${mCount} new bests). Click for all experiments.` : "Show step-function — milestones + experiments since the last record"}>
              ▲ {impOnly ? `Step${mCount > 0 ? ` (${mCount})` : ""}` : "All"}
            </Toggle>
          );
        })()}
        <Toggle active={mean} onClick={() => { ideaMean.value = !mean; }} title="One point per idea (mean)">
          μ Idea Mean
        </Toggle>
        <Toggle active={clip} onClick={() => { clipOutliers.value = !clip; }} title="Hide outliers">
          ⤢ Outliers
        </Toggle>
        <Toggle active={logScale} onClick={() => { setLogScale(!logScale); }} title="Logarithmic Y axis">
          Log Y
        </Toggle>
        {/* Point-size control — drives chartPointSize (persisted). Scales the mini dot radii. */}
        <span class="chart-ptsize" title="Dot size">
          {(["s", "m", "l"] as const).map((sz) => (
            <button
              key={sz}
              type="button"
              class={`chart-ptsize-btn${pointSize === sz ? " is-active" : ""}`}
              onClick={() => { chartPointSize.value = sz; }}
              title={`Dot size: ${sz === "s" ? "small" : sz === "m" ? "medium" : "large"}`}
            >
              {sz}
            </button>
          ))}
        </span>
        {/* Clone is hidden in the single-chart review (hideClone); kept in the workbench */}
        {!hideClone && (
          <IconButton onClick={() => { if (cloneChartPanel) cloneChartPanel("metrics", metric); }} title="Clone this chart as a new tab">
            + Clone
          </IconButton>
        )}
      </div>
      {/* Data distribution hint — shown when most values cluster near bottom */}
      {(() => {
        const vals = allExperiments.value.filter(e => !e._running && typeof e.metrics?.[metric] === "number").map(e => e.metrics![metric] as number);
        if (vals.length < 5) return null;
        const lower = isLowerBetter(metric);
        const peak = lower ? Math.min(...vals) : Math.max(...vals);
        const median = [...vals].sort((a,b) => a-b)[Math.floor(vals.length/2)];
        const pctAtPeak = vals.filter(v => lower ? v === peak : v === peak).length;
        // Show hint only when data is sparse at top (median < 25% of peak)
        if (peak === 0 || median / peak > 0.3) return null;
        return (
          <div class="chart-dist-hint">
            {vals.length} exp
            {impOnly && (
              <span style={{ color: "var(--text-faint)", fontSize: "var(--text-xs)" }}>
                {" "}({pctAtPeak} milestones)
              </span>
            )}
            <span style={{ color: "var(--text-faint)" }}> · </span>
            <span style={{ color: "var(--border-soft)", fontWeight: 600 }}>·</span>
            {" "}median {Math.abs(median) >= 1 ? median.toFixed(1) : median.toFixed(2)}
            <span style={{ color: "var(--text-faint)" }}> · </span>
            <span style={{ color: "var(--purple)", fontWeight: 600 }}>·</span>
            {" "}best {Math.abs(peak) >= 100 ? peak.toFixed(0) : Math.abs(peak) >= 1 ? peak.toFixed(2) : peak.toFixed(3)}
            {impOnly && allExperiments.value.some(e => e._running) && (
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
                    const fmt = best === null ? "--" : Math.abs(best) >= 100 ? best.toFixed(0) : Math.abs(best) >= 1 ? best.toFixed(2) : best.toFixed(3);
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
            pointSize={pointSize}
          />
        </div>
      </div>
    </div>
  );
}
