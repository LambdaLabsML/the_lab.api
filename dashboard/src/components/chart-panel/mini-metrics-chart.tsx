/**
 * MiniMetricsChart — landing-page-style SVG chart for the minified mode.
 *
 * Dots = experiments, stepped purple line = running best, gold dots = new
 * global best, dashed vertical drops to x-axis. Colors and ordering follow
 * the normal chart mode exactly (uses buildChartData). Min 8px per point
 * with horizontal scroll when needed. Click a dot to navigate to its idea.
 */

import { useEffect, useRef, useState } from "preact/hooks";
import { buildChartData } from "../../lib/chart-data";
import { isLowerBetter } from "../../lib/colors";
import { navigateToIdea } from "../../lib/navigate";
import { highlightedIdea } from "../../state/signals";
import type { Experiment, IdeaNode, SubwayLayout } from "../../lib/types";

const H = 200;
const PAD = { l: 40, r: 14, t: 14, b: 26 };
const MIN_PX_PER_POINT = 8;

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

// ── component ─────────────────────────────────────────────────────────────────

export function MiniMetricsChart({
  metric,
  experiments,
  ideas,
  layout,
  hiddenStatuses,
  hideRunning,
  impOnly,
  colorMode,
  tags,
  tagMode,
  reversed,
  mean,
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
  const globalHighlight = highlightedIdea.value; // graph → mini: expand matching dots
  const wrapRef = useRef<HTMLDivElement>(null);
  const [containerW, setContainerW] = useState(560);

  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setContainerW(el.clientWidth));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // Use the same buildChartData pipeline as the normal chart for consistent
  // colors, filtering, direction, and mean-mode support.
  const chartData = metric ? buildChartData(
    metric, experiments, tags, tagMode, impOnly, colorMode,
    ideas, layout, reversed, hiddenStatuses, mean, hideRunning,
  ) : null;

  if (!chartData || chartData.values.length === 0) {
    return (
      <div style={{ width: "100%", height: "100%", display: "flex", alignItems: "center", justifyContent: "center" }}>
        <span style={{ color: "var(--text-faint)", fontSize: "var(--text-sm)" }}>
          {metric ? `no data for ${metric}` : "select a metric"}
        </span>
      </div>
    );
  }

  const lower = isLowerBetter(metric);
  const { values, pointColors, expData, labels } = chartData;
  const n = values.length;

  // Dynamic SVG width — expand to guarantee MIN_PX_PER_POINT per dot
  const minSvgW = PAD.l + n * MIN_PX_PER_POINT + PAD.r;
  const svgW = Math.max(containerW, minSvgW);
  const plotW = svgW - PAD.l - PAD.r;
  const plotH = H - PAD.t - PAD.b;
  const rightEdge = svgW - PAD.r;

  const toX = (i: number) => PAD.l + (n <= 1 ? plotW / 2 : (i / (n - 1)) * plotW);
  const toY = (v: number) => PAD.t + (1 - (v - yMin) / (yMax - yMin)) * plotH;

  // Y range
  const finite = values.filter(isFinite);
  const dataMin = Math.min(...finite);
  const dataMax = Math.max(...finite);
  const yPad = (dataMax - dataMin) * 0.12 || Math.abs(dataMax) * 0.05 || 0.01;
  const yMin = dataMin - yPad;
  const yMax = dataMax + yPad;

  // Running best + milestone detection — always computed in chronological
  // order (oldest→newest). When reversed=true the display is newest→oldest,
  // so we iterate right-to-left and the running best is a suffix max/min.
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

  // Y ticks
  const nTicks = 4;
  const tickStep = (yMax - yMin) / (nTicks - 1);
  const yTicks = Array.from({ length: nTicks }, (_, i) => yMin + i * tickStep);

  const bestVal = lower ? Math.min(...finite) : Math.max(...finite);
  const needsScroll = svgW > containerW;

  return (
    <div
      ref={wrapRef}
      style={{ width: "100%", height: "100%", overflowX: needsScroll ? "auto" : "hidden", overflowY: "hidden" }}
    >
      <svg
        viewBox={`0 0 ${svgW} ${H}`}
        preserveAspectRatio="xMidYMid meet"
        style={`${needsScroll ? `min-width:${svgW}px;` : "width:100%;"}height:100%;display:block;`}
      >
        <defs>
          <linearGradient id="miniBestFill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stop-color="var(--purple, #bc8cff)" stop-opacity="0.22" />
            <stop offset="100%" stop-color="var(--purple, #bc8cff)" stop-opacity="0.03" />
          </linearGradient>
        </defs>

        {/* Y grid + labels */}
        {yTicks.map((y, i) => (
          <g key={i}>
            <line x1={PAD.l} x2={rightEdge} y1={toY(y)} y2={toY(y)}
              stroke="var(--border-soft, #21262d)" stroke-width="1" />
            <text x={PAD.l - 5} y={toY(y) + 3}
              fill="var(--text-faint, #484f58)" font-size="9" text-anchor="end"
              font-family="var(--font-mono, JetBrains Mono, monospace)"
            >{fmtVal(y)}</text>
          </g>
        ))}

        {/* X baseline */}
        <line x1={PAD.l} x2={rightEdge} y1={H - PAD.b} y2={H - PAD.b}
          stroke="var(--border, #30363d)" />

        {/* Best-line fill + stroke */}
        <path d={`${stepPath} V${H - PAD.b} H${PAD.l} Z`}
          fill="url(#miniBestFill)" opacity={impOnly ? 1 : 0.7} />
        <path d={stepPath} fill="none"
          stroke="var(--purple, #bc8cff)"
          stroke-width={impOnly ? 2.4 : 1.8}
          stroke-linejoin="round" stroke-linecap="round" />

        {/* Dots */}
        {values.map((v, i) => {
          const x = toX(i);
          const y = toY(v);
          const isMilestone = milestones.has(i);
          const isHovered = hoveredIdx === i;
          const color = pointColors[i] ?? "var(--text-muted)";
          const exp = expData?.[i];
          const ideaHighlighted = exp != null && globalHighlight === exp.idea_id;
          const anyHighlight = globalHighlight !== null;
          const faded = anyHighlight && !ideaHighlighted;
          const isRunning = exp?._running ?? false;
          const r = ideaHighlighted
            ? (isMilestone ? 6.5 : 5.5)
            : isHovered ? (isMilestone ? 6 : 5)
            : (isMilestone ? 4.5 : 3);
          const dotColor = isMilestone ? "var(--yellow, #d29922)" : color;
          const strokeColor = ideaHighlighted ? "var(--text, #c9d1d9)" : isMilestone ? "var(--bg, #0d1117)" : "none";
          const strokeW = ideaHighlighted ? 1.5 : isMilestone ? 1.2 : 0;

          return (
            <g
              key={i}
              style="cursor:pointer;"
              onMouseEnter={() => { setHoveredIdx(i); if (exp) highlightedIdea.value = exp.idea_id; }}
              onMouseLeave={() => { setHoveredIdx(null); highlightedIdea.value = null; }}
              onClick={() => exp && navigateToIdea(exp.idea_id, exp.label ?? String(exp.id))}
            >
              <line x1={x} x2={x} y1={y} y2={H - PAD.b}
                stroke={isMilestone ? "var(--yellow, #d29922)" : "var(--border, #30363d)"}
                stroke-width="1" stroke-dasharray="2 3"
                opacity={faded ? 0.12 : isMilestone ? 0.55 : 0.35} />
              {isRunning ? (
                <path d={trianglePath(x, y, r)}
                  fill="transparent"
                  stroke={dotColor}
                  stroke-width={Math.max(strokeW, 1.5)}
                  opacity={faded ? 0.2 : 1}
                />
              ) : (
                <circle cx={x} cy={y} r={r}
                  fill={dotColor}
                  stroke={strokeColor}
                  stroke-width={strokeW}
                  opacity={faded ? 0.2 : 1}
                />
              )}
              {isMilestone && !faded && (
                <text x={x + 6} y={y - 6}
                  fill="var(--yellow, #d29922)" font-size="9"
                  font-family="var(--font-mono, JetBrains Mono, monospace)"
                >new best</text>
              )}
              <circle cx={x} cy={y} r="9" fill="transparent" />
            </g>
          );
        })}

        {/* Hover tooltip */}
        {hoveredIdx !== null && (() => {
          const i = hoveredIdx;
          const x = toX(i);
          const y = toY(values[i]);
          const exp = expData?.[i];
          const label = labels?.[i] ?? String(i);
          const idea = exp ? ideas[exp.idea_id] : null;
          const ideaRows = wrapText(idea?.description ?? "", 100, 2);
          const tooltipW = 210;
          const tooltipH = 46 + ideaRows.length * 12;
          const tx = Math.max(PAD.l + 2, Math.min(svgW - tooltipW - PAD.r, x + 10));
          const ty = Math.max(PAD.t + 2, y - tooltipH - 10);
          return (
            <g pointer-events="none">
              <line x1={x} y1={y - 5} x2={tx + 12} y2={ty + tooltipH}
                stroke="var(--border, #30363d)" stroke-width="1" />
              <rect x={tx} y={ty} width={tooltipW} height={tooltipH}
                fill="var(--bg, #0d1117)" stroke="var(--purple, #bc8cff)" stroke-width="1" />
              <text x={tx + 8} y={ty + 14}
                fill="var(--text, #c9d1d9)" font-size="10" font-weight="700"
                font-family="var(--font-mono, JetBrains Mono, monospace)">
                {label} · {fmtVal(values[i])}
              </text>
              <text x={tx + 8} y={ty + 28}
                fill="var(--purple, #bc8cff)" font-size="9"
                font-family="var(--font-mono, JetBrains Mono, monospace)">
                idea #{exp?.idea_id}
              </text>
              {ideaRows.length > 0 && (
                <text x={tx + 8} y={ty + 40}
                  fill="var(--text-muted, #8b949e)" font-size="9"
                  font-family="var(--font-mono, JetBrains Mono, monospace)">
                  {ideaRows.map((row, idx) => (
                    <tspan key={idx} x={tx + 8} dy={idx === 0 ? 0 : 12}>{row}</tspan>
                  ))}
                </text>
              )}
            </g>
          );
        })()}

        {/* Bottom labels */}
        <g font-family="var(--font-mono, JetBrains Mono, monospace)" font-size="9">
          <text x={PAD.l} y={H - 7} fill="var(--text-faint, #484f58)">
            experiments left → right
          </text>
          <text x={rightEdge} y={H - 7} text-anchor="end" fill="var(--purple, #bc8cff)">
            best {fmtVal(bestVal)}
          </text>
        </g>
      </svg>
    </div>
  );
}
