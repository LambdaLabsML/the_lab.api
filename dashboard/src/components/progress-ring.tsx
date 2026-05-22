/**
 * ProgressRing — tiny SVG donut that shows pct_complete.
 * Used in running experiment badges across table, detail, and graph.
 */

export function ProgressRing({ pct, size = 16 }: { pct: number; size?: number }) {
  const strokeWidth = 2;
  const r = (size - strokeWidth) / 2;
  const circumference = 2 * Math.PI * r;
  const offset = circumference * (1 - Math.min(pct, 100) / 100);
  const center = size / 2;

  return (
    <svg
      width={size}
      height={size}
      style="vertical-align: middle; display: inline-block; margin-right: 3px;"
    >
      <circle
        cx={center} cy={center} r={r}
        fill="none" stroke="var(--border)" stroke-width={strokeWidth}
      />
      <circle
        cx={center} cy={center} r={r}
        fill="none" stroke="var(--yellow)" stroke-width={strokeWidth}
        stroke-dasharray={circumference} stroke-dashoffset={offset}
        stroke-linecap="round"
        transform={`rotate(-90 ${center} ${center})`}
      />
      {size >= 20 && (
        <text
          x={center} y={center}
          text-anchor="middle" dominant-baseline="central"
          fill="var(--yellow)" font-size={size * 0.35}
          font-family="SF Mono, Fira Code, Consolas, monospace"
        >
          {Math.round(pct)}
        </text>
      )}
    </svg>
  );
}

/**
 * RunningBadge — "running" badge with optional progress ring.
 * Drop-in replacement for the static badge in experiment views.
 */
export function RunningBadge({ pct }: { pct?: number }) {
  return (
    <span class="badge badge-running">
      {typeof pct === "number" ? <ProgressRing pct={pct} size={14} /> : null}
      running{typeof pct === "number" ? ` ${Math.round(pct)}%` : ""}
    </span>
  );
}
