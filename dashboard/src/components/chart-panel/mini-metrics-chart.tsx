/**
 * MiniMetricsChart — landing-page-style SVG chart for the minified mode.
 *
 * Dots = experiments, stepped purple line = running best, gold dots = new
 * global best at that point, dashed vertical drops to the x-axis.
 * Mirrors the look of the_lab.api.landingpage's MetricsChart component but
 * driven by real experiment data.
 */

import { useState } from "preact/hooks";
import { IDEA_PALETTE } from "../../lib/colors";
import { isLowerBetter } from "../../lib/colors";
import type { Experiment, IdeaNode } from "../../lib/types";

const W = 560;
const H = 200;
const PAD = { l: 40, r: 12, t: 14, b: 26 };

// ── data prep ────────────────────────────────────────────────────────────────

type RunPoint = {
  expId: number;
  label: string;
  ideaId: number;
  ideaDesc: string;
  value: number;
  color: string;
  isMilestone: boolean;
};

function buildPoints(
  metric: string,
  experiments: Experiment[],
  ideas: Record<number, IdeaNode>,
  hiddenStatuses: Set<string>,
  hideRunning: boolean,
  impOnly: boolean,
): RunPoint[] {
  const lower = isLowerBetter(metric);

  // Assign palette colors by first-seen idea order
  const ideaColorMap: Record<number, string> = {};
  let colorIdx = 0;

  const eligible = experiments
    .filter((e) => {
      if (e._running && hideRunning) return false;
      if (e._running) return false; // running exps have no final value
      const idea = ideas[e.idea_id];
      const status = idea?.status ?? "active";
      if (hiddenStatuses.has(status)) return false;
      const v = e.metrics?.[metric];
      return typeof v === "number" && isFinite(v);
    })
    .slice()
    .sort((a, b) => {
      const ta = a.finished_at || a.started_at || "";
      const tb = b.finished_at || b.started_at || "";
      return ta.localeCompare(tb);
    });

  // Assign colors
  for (const e of eligible) {
    if (!(e.idea_id in ideaColorMap)) {
      ideaColorMap[e.idea_id] = IDEA_PALETTE[colorIdx % IDEA_PALETTE.length];
      colorIdx++;
    }
  }

  // Compute milestones (running global best)
  let best: number | null = null;
  const points: RunPoint[] = [];
  for (const e of eligible) {
    const value = e.metrics![metric] as number;
    const isMilestone = best === null || (lower ? value < best : value > best);
    if (isMilestone) best = value;

    const idea = ideas[e.idea_id];
    points.push({
      expId: e.id,
      label: e.label ?? String(e.id),
      ideaId: e.idea_id,
      ideaDesc: idea?.description ?? "",
      value,
      color: ideaColorMap[e.idea_id],
      isMilestone,
    });
  }

  if (impOnly) {
    // Keep only milestone points
    return points.filter((p) => p.isMilestone);
  }
  return points;
}

function bestThrough(points: RunPoint[], i: number, lower: boolean): number {
  let b = points[0].value;
  for (let j = 1; j <= i; j++) {
    if (lower ? points[j].value < b : points[j].value > b) b = points[j].value;
  }
  return b;
}

function niceTickCount(range: number): number {
  if (range === 0) return 4;
  return 4;
}

function niceTicks(min: number, max: number): number[] {
  const range = max - min;
  const n = niceTickCount(range);
  const step = range / (n - 1);
  return Array.from({ length: n }, (_, i) => min + i * step);
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
  hiddenStatuses,
  hideRunning,
  impOnly,
}: {
  metric: string;
  experiments: Experiment[];
  ideas: Record<number, IdeaNode>;
  hiddenStatuses: Set<string>;
  hideRunning: boolean;
  impOnly: boolean;
}) {
  const [hoveredIdx, setHoveredIdx] = useState<number | null>(null);

  const lower = isLowerBetter(metric);
  const points = buildPoints(metric, experiments, ideas, hiddenStatuses, hideRunning, impOnly);

  if (points.length === 0) {
    return (
      <div style={{ width: "100%", height: "100%", display: "flex", alignItems: "center", justifyContent: "center" }}>
        <span style={{ color: "var(--text-faint)", fontSize: "var(--text-sm)" }}>no data for {metric}</span>
      </div>
    );
  }

  const vals = points.map((p) => p.value);
  const dataMin = Math.min(...vals);
  const dataMax = Math.max(...vals);
  const pad = (dataMax - dataMin) * 0.12 || Math.abs(dataMax) * 0.05 || 0.01;
  const yMin = dataMin - pad;
  const yMax = dataMax + pad;

  const plotW = W - PAD.l - PAD.r;
  const plotH = H - PAD.t - PAD.b;
  const rightEdge = W - PAD.r;
  const n = points.length;

  const toX = (i: number) => PAD.l + (n <= 1 ? plotW / 2 : (i / (n - 1)) * plotW);
  const toY = (v: number) => PAD.t + (1 - (v - yMin) / (yMax - yMin)) * plotH;

  // Stepped best-score path
  const stepPoints = points.map((_, i) => [toX(i), toY(bestThrough(points, i, lower))] as const);
  let stepPath = `M${stepPoints[0][0].toFixed(1)},${stepPoints[0][1].toFixed(1)}`;
  for (let i = 1; i < stepPoints.length; i++) {
    stepPath += ` H${stepPoints[i][0].toFixed(1)} V${stepPoints[i][1].toFixed(1)}`;
  }
  stepPath += ` H${rightEdge.toFixed(1)}`;

  const ticks = niceTicks(yMin, yMax);
  const bestVal = bestThrough(points, points.length - 1, lower);

  return (
    <svg
      viewBox={`0 0 ${W} ${H}`}
      preserveAspectRatio="xMidYMid meet"
      style="width:100%;height:100%;display:block;"
    >
      <defs>
        <linearGradient id="miniBestFill" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="var(--purple, #bc8cff)" stop-opacity="0.22" />
          <stop offset="100%" stop-color="var(--purple, #bc8cff)" stop-opacity="0.03" />
        </linearGradient>
      </defs>

      {/* Y-axis grid lines + labels */}
      {ticks.map((y, i) => (
        <g key={i}>
          <line
            x1={PAD.l} x2={rightEdge}
            y1={toY(y)} y2={toY(y)}
            stroke="var(--border-soft, #21262d)" stroke-width="1"
          />
          <text
            x={PAD.l - 5} y={toY(y) + 3}
            fill="var(--text-faint, #484f58)"
            font-size="9"
            text-anchor="end"
            font-family="var(--font-mono, JetBrains Mono, monospace)"
          >{fmtVal(y)}</text>
        </g>
      ))}

      {/* X-axis baseline */}
      <line x1={PAD.l} x2={rightEdge} y1={H - PAD.b} y2={H - PAD.b} stroke="var(--border, #30363d)" />

      {/* Best-line fill + stroke */}
      <path
        d={`${stepPath} V${H - PAD.b} H${PAD.l} Z`}
        fill="url(#miniBestFill)"
        opacity={impOnly ? 1 : 0.7}
      />
      <path
        d={stepPath}
        fill="none"
        stroke="var(--purple, #bc8cff)"
        stroke-width={impOnly ? 2.4 : 1.8}
        stroke-linejoin="round"
        stroke-linecap="round"
      />

      {/* Dots */}
      {points.map((p, i) => {
        const x = toX(i);
        const y = toY(p.value);
        const isHovered = hoveredIdx === i;
        return (
          <g
            key={p.expId}
            style="cursor:default;"
            onMouseEnter={() => setHoveredIdx(i)}
            onMouseLeave={() => setHoveredIdx(null)}
          >
            {/* Drop line */}
            <line
              x1={x} x2={x}
              y1={y} y2={H - PAD.b}
              stroke={p.isMilestone ? "var(--yellow, #d29922)" : "var(--border, #30363d)"}
              stroke-width="1"
              stroke-dasharray="2 3"
              opacity={p.isMilestone ? 0.55 : 0.35}
            />
            {/* Dot */}
            <circle
              cx={x} cy={y}
              r={isHovered ? (p.isMilestone ? 6 : 5) : (p.isMilestone ? 4.5 : 3)}
              fill={p.isMilestone ? "var(--yellow, #d29922)" : p.color}
              stroke={p.isMilestone ? "var(--bg, #0d1117)" : "none"}
              stroke-width={p.isMilestone ? 1.2 : 0}
              style="transition: r 0.12s ease;"
            />
            {/* Milestone label */}
            {p.isMilestone && (
              <text
                x={x + 6} y={y - 6}
                fill="var(--yellow, #d29922)"
                font-size="9"
                font-family="var(--font-mono, JetBrains Mono, monospace)"
              >new best</text>
            )}
            {/* Hit area */}
            <circle cx={x} cy={y} r="9" fill="transparent" />
          </g>
        );
      })}

      {/* Hover tooltip */}
      {hoveredIdx !== null && (() => {
        const p = points[hoveredIdx];
        const x = toX(hoveredIdx);
        const y = toY(p.value);
        const tooltipW = 200;
        const ideaRows = wrapText(p.ideaDesc, 100, 2);
        const tooltipH = 46 + ideaRows.length * 12;
        const tx = Math.max(PAD.l + 2, Math.min(W - tooltipW - PAD.r, x + 10));
        const ty = Math.max(PAD.t + 2, y - tooltipH - 10);
        return (
          <g pointer-events="none">
            <line x1={x} y1={y - 5} x2={tx + 12} y2={ty + tooltipH} stroke="var(--border, #30363d)" stroke-width="1" />
            <rect x={tx} y={ty} width={tooltipW} height={tooltipH} rx="0"
              fill="var(--bg, #0d1117)" stroke="var(--purple, #bc8cff)" stroke-width="1" />
            <text x={tx + 8} y={ty + 14}
              fill="var(--text, #c9d1d9)" font-size="10" font-weight="700"
              font-family="var(--font-mono, JetBrains Mono, monospace)">
              {p.label} · {fmtVal(p.value)}
            </text>
            <text x={tx + 8} y={ty + 28}
              fill="var(--purple, #bc8cff)" font-size="9"
              font-family="var(--font-mono, JetBrains Mono, monospace)">
              idea #{p.ideaId}
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
  );
}
