/**
 * MiniMetricsChart — landing-page-style SVG chart for the minified mode.
 *
 * Dots = experiments, stepped purple line = current best, gold dots = metric
 * improvements, dashed vertical drops to x-axis. Colors and ordering follow
 * the normal chart mode exactly (uses buildChartData). Click a dot to navigate.
 *
 * Design notes (see dashboard/DESIGN.md):
 *  - Renders 1:1 in real pixels (viewBox == measured size) so dots and text are
 *    crisp and small, not blown up by a scaled 200-unit viewBox.
 *  - Font sizes are read from the --text-* tokens, so the chart honours the
 *    user's font-size setting instead of hardcoding font-size="9".
 *  - Flat purple tint under the best-line — no gradient.
 *  - Click/hover routed through the shared useEntityNav hook.
 */

import { useEffect, useRef, useState } from "preact/hooks";
import { buildChartData } from "../../lib/chart-data";
import { isLowerBetter } from "../../lib/colors";
import { useEntityNav } from "../../lib/hooks";
import { highlightedIdea } from "../../state/signals";
import { fmtMetricName } from "../../lib/format";
import type { Experiment, IdeaNode, SubwayLayout } from "../../lib/types";

const MIN_PX_PER_POINT = 8;
const PAD = { l: 34, r: 12, t: 10, b: 18 };

// ── helpers ──────────────────────────────────────────────────────────────────

/** Upward-pointing triangle path centred on (cx, cy) with half-size r. */
function trianglePath(cx: number, cy: number, r: number): string {
  const h = r * 1.8;
  const x0 = cx, y0 = cy - h;           // apex
  const x1 = cx - r, y1 = cy + h * 0.4; // bottom-left
  const x2 = cx + r, y2 = cy + h * 0.4; // bottom-right
  return `M${x0.toFixed(1)},${y0.toFixed(1)} L${x1.toFixed(1)},${y1.toFixed(1)} L${x2.toFixed(1)},${y2.toFixed(1)} Z`;
}

function fmtVal(v: number): string {
  if (Math.abs(v) >= 1000) return v.toFixed(0);
  if (Math.abs(v) >= 10)   return v.toFixed(1);
  if (Math.abs(v) >= 1)    return v.toFixed(2);
  return v.toFixed(3);
}

function wrapText(text: string, maxChars: number, maxRows: number): string[] {
  const clean = (text ?? "").replace(/\s+/g, " ").trim().slice(0, maxChars);
  const words = clean.split(" ");
  const rowChars = Math.ceil(maxChars / maxRows);
  const rows: string[] = [];
  for (const word of words) {
    const cur = rows[rows.length - 1] ?? "";
    if (!cur) { rows.push(word); }
    else if (`${cur} ${word}`.length <= rowChars) { rows[rows.length - 1] = `${cur} ${word}`; }
    else if (rows.length < maxRows) { rows.push(word); }
    else { rows[rows.length - 1] = `${cur}...`; break; }
  }
  return rows.slice(0, maxRows);
}

// ── single dot (its own component so it can use the nav hook) ──────────────────

function MiniDot({
  exp, x, y, baselineY, color, isMilestone, fontXs, onHoverChange,
}: {
  exp: (Experiment & { _running?: boolean }) | undefined;
  x: number; y: number; baselineY: number;
  color: string; isMilestone: boolean; fontXs: number;
  onHoverChange: (hovered: boolean) => void;
}) {
  const ideaId = exp?.idea_id ?? -1;
  const label = exp?.label ?? (exp?.id != null ? String(exp.id) : undefined);
  const nav = useEntityNav(ideaId, label);

  const anyHighlight = highlightedIdea.value !== null;
  const highlighted = nav.highlighted;
  const faded = anyHighlight && !highlighted;
  const isRunning = exp?._running ?? false;

  // small, crisp radii (1:1 px) — milestones a touch larger, highlight larger still
  const r = highlighted
    ? (isMilestone ? 4 : 3.2)
    : (isMilestone ? 3.2 : 2.4);
  const dotColor = isMilestone ? "var(--yellow)" : color;
  const strokeColor = highlighted ? "var(--text)" : isMilestone ? "var(--bg)" : "none";
  const strokeW = highlighted ? 1.3 : isMilestone ? 1 : 0;

  return (
    <g
      style={exp ? "cursor:pointer;" : undefined}
      onMouseEnter={() => { onHoverChange(true); if (exp) nav.bind.onMouseEnter(); }}
      onMouseLeave={() => { onHoverChange(false); if (exp) nav.bind.onMouseLeave(); }}
      onClick={exp ? (nav.bind.onClick as any) : undefined}
    >
      <line x1={x} x2={x} y1={y} y2={baselineY}
        stroke={isMilestone ? "var(--yellow)" : "var(--border)"}
        stroke-width="1" stroke-dasharray="2 3"
        opacity={faded ? 0.1 : isMilestone ? 0.5 : 0.3} />
      {isRunning ? (
        <path d={trianglePath(x, y, r)} fill="transparent" stroke={dotColor}
          stroke-width={Math.max(strokeW, 1.3)} opacity={faded ? 0.2 : 1} />
      ) : (
        <circle cx={x} cy={y} r={r} fill={dotColor}
          stroke={strokeColor} stroke-width={strokeW} opacity={faded ? 0.2 : 1} />
      )}
      {isMilestone && !faded && (
        <text x={x + 5} y={y - 5} fill="var(--yellow)" font-size={fontXs}
          font-family="var(--font-mono)">metric improved</text>
      )}
      <circle cx={x} cy={y} r="9" fill="transparent" />
    </g>
  );
}

// ── component ─────────────────────────────────────────────────────────────────

export function MiniMetricsChart({
  metric, experiments, ideas, layout, hiddenStatuses, hideRunning,
  impOnly, colorMode, tags, tagMode, reversed, mean,
}: {
  metric: string;
  experiments: Experiment[];
  ideas: Record<number, IdeaNode>;
  layout: SubwayLayout | null;
  hiddenStatuses: Set<string>;
  hideRunning: boolean;
  impOnly: boolean;
  colorMode: string;
  tags: string[];
  tagMode: string;
  reversed: boolean;
  mean: boolean;
}) {
  const [hoveredIdx, setHoveredIdx] = useState<number | null>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ w: 560, h: 180 });
  // font sizes read from the --text-* tokens so the chart honours font-size setting
  const [font, setFont] = useState({ xs: 8, sm: 9 });

  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const read = () => {
      setSize({ w: el.clientWidth || 560, h: el.clientHeight || 180 });
      const cs = getComputedStyle(el);
      const px = (name: string, fallback: number) =>
        parseFloat(cs.getPropertyValue(name)) || fallback;
      setFont({ xs: px("--text-xs", 8), sm: px("--text-sm", 9) });
    };
    read();
    const ro = new ResizeObserver(read);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const chartData = metric ? buildChartData(
    metric, experiments, tags, tagMode, impOnly, colorMode,
    ideas, layout, reversed, hiddenStatuses, mean, hideRunning,
  ) : null;

  if (!chartData || chartData.values.length === 0) {
    return (
      <div ref={wrapRef} style={{ width: "100%", height: "100%", display: "flex", alignItems: "center", justifyContent: "center" }}>
        <span style={{ color: "var(--text-faint)", fontSize: "var(--text-sm)" }}>
          {metric ? `no data for ${metric}` : "select a metric"}
        </span>
      </div>
    );
  }

  const lower = isLowerBetter(metric);
  const { values, pointColors, expData, labels } = chartData;
  const n = values.length;

  // 1:1 pixel coordinate space — viewBox matches measured size, no scaling.
  const containerW = size.w;
  const H = Math.max(120, size.h);
  const minSvgW = PAD.l + n * MIN_PX_PER_POINT + PAD.r;
  const svgW = Math.max(containerW, minSvgW);
  const needsScroll = svgW > containerW + 0.5;
  const plotW = svgW - PAD.l - PAD.r;
  const plotH = H - PAD.t - PAD.b;
  const rightEdge = svgW - PAD.r;
  const baselineY = H - PAD.b;

  // Y range
  const finite = values.filter(isFinite);
  const dataMin = Math.min(...finite);
  const dataMax = Math.max(...finite);
  const yPad = (dataMax - dataMin) * 0.12 || Math.abs(dataMax) * 0.05 || 0.01;
  const yMin = dataMin - yPad;
  const yMax = dataMax + yPad;

  const toX = (i: number) => PAD.l + (n <= 1 ? plotW / 2 : (i / (n - 1)) * plotW);
  const toY = (v: number) => PAD.t + (1 - (v - yMin) / (yMax - yMin)) * plotH;

  // Running best + milestone detection (chronological; suffix-max when reversed)
  const milestones = new Set<number>();
  const runningBest: number[] = new Array(n);
  if (reversed) {
    let best: number | null = null;
    for (let i = n - 1; i >= 0; i--) {
      const isBetter = best === null || (lower ? values[i] < best : values[i] > best);
      if (isBetter) { best = values[i]; milestones.add(i); }
      runningBest[i] = best!;
    }
  } else {
    let best: number | null = null;
    for (let i = 0; i < n; i++) {
      const isBetter = best === null || (lower ? values[i] < best : values[i] > best);
      if (isBetter) { best = values[i]; milestones.add(i); }
      runningBest[i] = best!;
    }
  }

  // Stepped best-score path
  const stepPoints = values.map((_, i) => [toX(i), toY(runningBest[i])] as const);
  let stepPath = `M${stepPoints[0][0].toFixed(1)},${stepPoints[0][1].toFixed(1)}`;
  for (let i = 1; i < stepPoints.length; i++) {
    stepPath += ` H${stepPoints[i][0].toFixed(1)} V${stepPoints[i][1].toFixed(1)}`;
  }
  stepPath += ` H${rightEdge.toFixed(1)}`;

  const nTicks = 4;
  const tickStep = (yMax - yMin) / (nTicks - 1);
  const yTicks = Array.from({ length: nTicks }, (_, i) => yMin + i * tickStep);
  const bestVal = lower ? Math.min(...finite) : Math.max(...finite);

  return (
    <div
      ref={wrapRef}
      style={{ width: "100%", height: "100%", overflowX: needsScroll ? "auto" : "hidden", overflowY: "hidden" }}
    >
      <svg
        width={needsScroll ? svgW : "100%"}
        height={H}
        viewBox={`0 0 ${svgW} ${H}`}
        preserveAspectRatio="xMidYMid meet"
        style={`display:block;${needsScroll ? `min-width:${svgW}px;` : ""}`}
      >
        {/* Y grid + labels */}
        {yTicks.map((y, i) => (
          <g key={i}>
            <line x1={PAD.l} x2={rightEdge} y1={toY(y)} y2={toY(y)}
              stroke="var(--border-soft)" stroke-width="1" />
            <text x={PAD.l - 5} y={toY(y) + font.xs * 0.34}
              fill="var(--text-faint)" font-size={font.xs} text-anchor="end"
              font-family="var(--font-mono)">{fmtVal(y)}</text>
          </g>
        ))}

        {/* X baseline */}
        <line x1={PAD.l} x2={rightEdge} y1={baselineY} y2={baselineY} stroke="var(--border)" />

        {/* Best-line flat tint + stroke (no gradient) */}
        <path d={`${stepPath} V${baselineY} H${PAD.l} Z`}
          fill="var(--purple)" fill-opacity={impOnly ? 0.07 : 0.05} />
        <path d={stepPath} fill="none" stroke="var(--purple)"
          stroke-width={impOnly ? 1.8 : 1.4}
          stroke-linejoin="round" stroke-linecap="round" />

        {/* Dots */}
        {values.map((v, i) => (
          <MiniDot
            key={i}
            exp={expData?.[i] as any}
            x={toX(i)} y={toY(v)} baselineY={baselineY}
            color={pointColors[i] ?? "var(--text-muted)"}
            isMilestone={milestones.has(i)}
            fontXs={font.xs}
            onHoverChange={(h) => setHoveredIdx(h ? i : (cur) => (cur === i ? null : cur))}
          />
        ))}

        {/* Hover tooltip */}
        {hoveredIdx !== null && (() => {
          const i = hoveredIdx;
          const x = toX(i);
          const y = toY(values[i]);
          const exp = expData?.[i];
          const label = labels?.[i] ?? String(i);
          const idea = exp ? ideas[exp.idea_id] : null;
          const status = idea?.status ?? "";
          const ideaRows = wrapText(idea?.description ?? "", 92, 2);
          const isMs = milestones.has(i);
          const lh = font.xs + 3;
          const headRows = 4; // metric · value · idea/status · best
          const tooltipW = 214;
          const tooltipH = lh * (headRows + ideaRows.length) + 12;
          const tx = Math.max(PAD.l + 2, Math.min(svgW - tooltipW - PAD.r, x + 10));
          const ty = Math.max(PAD.t + 2, y - tooltipH - 8);
          const ny = (n: number) => ty + lh * n;
          return (
            <g pointer-events="none" font-family="var(--font-mono)">
              <line x1={x} y1={y - 4} x2={tx + 10} y2={ty + tooltipH}
                stroke="var(--border)" stroke-width="1" />
              <rect x={tx} y={ty} width={tooltipW} height={tooltipH} rx="3"
                fill="var(--bg)" stroke="var(--purple)" stroke-width="1"
                style="filter:drop-shadow(0 6px 14px rgba(0,0,0,0.45));" />
              <text x={tx + 8} y={ny(1)} fill="var(--text-faint)" font-size={font.xs}
                style="text-transform:uppercase;letter-spacing:0.08em;">
                {metric ? fmtMetricName(metric) : "metric"}
              </text>
              <text x={tx + 8} y={ny(2)} fill="var(--text)" font-size={font.sm} font-weight="700">
                {label} · {fmtVal(values[i])}{isMs ? "  ★" : ""}
              </text>
              <text x={tx + 8} y={ny(3)} fill="var(--purple)" font-size={font.xs}>
                idea #{exp?.idea_id}{status ? ` · ${status}` : ""}
              </text>
              <text x={tx + 8} y={ny(4)} fill={isMs ? "var(--yellow)" : "var(--text-muted)"} font-size={font.xs}>
                best so far {fmtVal(runningBest[i])}{isMs ? " · new record" : ""}
              </text>
              {ideaRows.length > 0 && (
                <text x={tx + 8} y={ny(5)} fill="var(--text-muted)" font-size={font.xs}>
                  {ideaRows.map((row, idx) => (
                    <tspan key={idx} x={tx + 8} dy={idx === 0 ? 0 : lh}>{row}</tspan>
                  ))}
                </text>
              )}
            </g>
          );
        })()}

        {/* Bottom labels */}
        <g font-family="var(--font-mono)" font-size={font.xs}>
          <text x={PAD.l} y={H - 5} fill="var(--text-faint)">experiments left → right</text>
          <text x={rightEdge} y={H - 5} text-anchor="end" fill="var(--purple)">
            current best {fmtVal(bestVal)}
          </text>
        </g>
      </svg>
    </div>
  );
}
