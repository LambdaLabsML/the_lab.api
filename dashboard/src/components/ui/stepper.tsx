/**
 * Stepper — the compact ghost up/down chevron pair used to grow/shrink a
 * section (rows, panel height, history length). Extracted from the Overview
 * sections' height steppers; `size` scales it (m = the Overview look).
 * See dashboard/DESIGN.md § Components.
 */

function Chevron({ dir }: { dir: "up" | "down" }) {
  return (
    <svg class="review-chev ui-stepper-chev" width="10" height="10" viewBox="0 0 10 10" aria-hidden="true"
      fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
      {dir === "up"
        ? <polyline points="2,6.5 5,3.5 8,6.5" />
        : <polyline points="2,3.5 5,6.5 8,3.5" />}
    </svg>
  );
}

export function Stepper({ value, min, max, step, onChange, what, size = "m" }: {
  value: number; min: number; max: number; step: number;
  onChange: (next: number) => void;
  /** Used in tooltips/aria: "milestone rows", "chart height", "history". */
  what: string;
  size?: "s" | "m" | "l";
}) {
  return (
    <span class={`review-steppers ui-stepper ui-stepper--${size}`} role="group" aria-label={`${what} size`}>
      <button type="button" class="ui-btn review-step"
        title={`Fewer / shorter (${what})`} aria-label={`Fewer ${what}`} disabled={value <= min}
        onClick={(e) => { e.preventDefault(); e.stopPropagation(); onChange(Math.max(min, value - step)); }}>
        <Chevron dir="up" />
      </button>
      <button type="button" class="ui-btn review-step"
        title={`More / taller (${what})`} aria-label={`More ${what}`} disabled={value >= max}
        onClick={(e) => { e.preventDefault(); e.stopPropagation(); onChange(Math.min(max, value + step)); }}>
        <Chevron dir="down" />
      </button>
    </span>
  );
}
