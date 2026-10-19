/** Shared progress treatment for queue cards and the live monitor. */
export function ProgressBar({ value, label = "Progress", class: cls = "" }: { value?: number; label?: string; class?: string }) {
  const known = typeof value === "number" && Number.isFinite(value);
  const percent = known ? Math.min(100, Math.max(0, value!)) : undefined;
  return <span class={`ui-progress ${cls}`} role="progressbar" aria-label={label} aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent} aria-valuetext={known ? `${Math.round(percent!)}%` : "Progress not reported"}>
    <span class={`ui-progress-fill${known ? "" : " is-indeterminate"}`} style={known ? { width: `${percent}%` } : undefined} />
  </span>;
}
