/**
 * Chart toolbar building blocks shared by the main graph and the 2D plot:
 * icon toggles, segmented groups with an icon label, and a styled select.
 * One height (28px), one font size, the dashboard's border / accent style.
 */
import type { ComponentChildren } from "preact";

type IconName =
  | "step" | "all" | "mean" | "outliers" | "log"
  | "ax-exp" | "ax-idea" | "ax-time" | "ax-hours" | "axis"
  | "view" | "v-line" | "v-s" | "v-m" | "v-l"
  | "line" | "l-max" | "l-avg"
  | "labels" | "lab-off" | "lab-rec" | "lab-auto"
  | "x" | "y" | "search";

const P = { fill: "none", stroke: "currentColor", "stroke-width": 1.6, "stroke-linecap": "round", "stroke-linejoin": "round" } as const;

const ICONS: Record<IconName, ComponentChildren> = {
  // Staircase: only record-setting runs (step function).
  step: <path {...P} d="M2 13h3V9h3V6h3V3h3" />,
  all: <><circle cx="3" cy="11" r="1.2" fill="currentColor" /><circle cx="6.5" cy="7" r="1.2" fill="currentColor" /><circle cx="10" cy="9.5" r="1.2" fill="currentColor" /><circle cx="13" cy="4.5" r="1.2" fill="currentColor" /></>,
  mean: <text x="8" y="12.2" text-anchor="middle" font-size="12" font-family="serif" fill="currentColor">μ</text>,
  // A dot outside the box: outliers.
  outliers: <><rect {...P} x="2" y="5" width="8" height="8" rx="1" /><circle cx="13" cy="3" r="1.5" fill="currentColor" /></>,
  log: <path {...P} d="M2 14C3 7 6 4 14 3M2 14h12" />,
  "ax-exp": <><circle cx="3" cy="8" r="1.3" fill="currentColor" /><circle cx="8" cy="8" r="1.3" fill="currentColor" /><circle cx="13" cy="8" r="1.3" fill="currentColor" /></>,
  "ax-idea": <><path {...P} d="M2 12V6M6 12V4M10 12V8M14 12V5" /></>,
  "ax-time": <><circle {...P} cx="8" cy="8" r="6" /><path {...P} d="M8 4.5V8l2.5 1.5" /></>,
  "ax-hours": <path {...P} d="M4 2h8M4 14h8M5 2c0 4 6 4 6 6s-6 2-6 6M11 2c0 4-6 4-6 6" />,
  // X axis: an axis line with an arrow and a small "x".
  axis: <><path {...P} d="M1.5 12.5h12M11 10l2.5 2.5L11 15" /><text x="5" y="8.5" text-anchor="middle" font-size="7" font-family="monospace" fill="currentColor">x</text></>,
  view: <><path {...P} d="M1.5 8S4 3.5 8 3.5 14.5 8 14.5 8 12 12.5 8 12.5 1.5 8 1.5 8Z" /><circle cx="8" cy="8" r="2" fill="currentColor" /></>,
  "v-line": <path {...P} d="M2 12l4-4 3 2 5-6" />,
  "v-s": <><circle cx="4" cy="8" r="1" fill="currentColor" /><circle cx="8" cy="8" r="1" fill="currentColor" /><circle cx="12" cy="8" r="1" fill="currentColor" /></>,
  "v-m": <><circle cx="4" cy="8" r="1.7" fill="currentColor" /><circle cx="12" cy="8" r="1.7" fill="currentColor" /></>,
  "v-l": <circle cx="8" cy="8" r="3" fill="currentColor" />,
  // The chart's purple line: a purple stroke.
  line: <path fill="none" stroke="var(--purple)" stroke-width="2.2" stroke-linecap="round" d="M2 12 6 8l3 2 5-6" />,
  "l-max": <path {...P} d="M2 13h3V9h3V6h3V3h3" />,
  "l-avg": <path {...P} d="M2 11c2-4 3-4 5-1s3 3 5-1 2-3 2-3" />,
  labels: <><path {...P} d="M2 3h6l6 6-5 5-6-6Z" /><circle cx="5" cy="6" r="1" fill="currentColor" /></>,
  "lab-off": <><path {...P} d="M2 3h6l6 6-5 5-6-6Z" /><path {...P} d="M1 15 15 1" /></>,
  "lab-rec": <path {...P} d="M8 2l1.8 3.8 4.2.5-3.1 2.9.8 4.1L8 11.3 4.3 13.3l.8-4.1L2 6.3l4.2-.5Z" />,
  "lab-auto": <><path {...P} d="M2 3h6l6 6-5 5-6-6Z" /><path {...P} d="M11 1.5v3M9.5 3h3" /></>,
  x: <path {...P} d="M2 12h12M11 9l3 3-3 3" />,
  y: <path {...P} d="M4 14V2M1 5l3-3 3 3" />,
  search: <><circle {...P} cx="7" cy="7" r="4.5" /><path {...P} d="M10.5 10.5 14 14" /></>,
};

export function CtlIcon({ name }: { name: IconName }) {
  return <svg class="ctl-icon" width="14" height="14" viewBox="0 0 16 16" aria-hidden="true">{ICONS[name]}</svg>;
}

/** An on/off toggle with an icon and a short label. */
export function CtlToggle({ active, onClick, title, icon, children }: {
  active: boolean; onClick: () => void; title: string; icon: IconName; children?: ComponentChildren;
}) {
  return (
    <button type="button" class={`ctl-btn${active ? " is-active" : ""}`} aria-pressed={active} title={title} onClick={onClick}>
      <CtlIcon name={icon} />{children != null && <span class="ctl-text">{children}</span>}
    </button>
  );
}

/** A segmented choice; the group is named by an icon (title = its name). */
export function CtlSeg<T extends string>({ icon, name, value, options, onChange }: {
  icon: IconName;
  name: string;
  value: T;
  options: { value: T; label?: ComponentChildren; icon?: IconName; title: string }[];
  onChange: (v: T) => void;
}) {
  return (
    <span class="ctl-seg" role="group" aria-label={name}>
      <span class="ctl-seg-name" title={name}><CtlIcon name={icon} /></span>
      {options.map(o => (
        <button key={o.value} type="button" class={`ctl-seg-btn${value === o.value ? " is-active" : ""}`}
          aria-pressed={value === o.value} title={o.title} onClick={() => onChange(o.value)}>
          {o.icon && <CtlIcon name={o.icon} />}{o.label != null && <span class="ctl-text">{o.label}</span>}
        </button>
      ))}
    </span>
  );
}

/** A select styled like the other controls, with a leading icon. */
export function CtlSelect({ icon, title, value, onChange, children, wide = false }: {
  icon: IconName; title: string; value: string; onChange: (v: string) => void; children: ComponentChildren; wide?: boolean;
}) {
  return (
    <label class={`ctl-select${wide ? " is-wide" : ""}`} title={title}>
      <CtlIcon name={icon} />
      <select aria-label={title} value={value} onChange={(e) => onChange((e.target as HTMLSelectElement).value)}>{children}</select>
      <svg class="ctl-chevron" width="10" height="10" viewBox="0 0 10 10" aria-hidden="true"><path d="M2 3.5 5 6.5 8 3.5" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" /></svg>
    </label>
  );
}

export function CtlSearch({ value, onInput, placeholder }: { value: string; onInput: (v: string) => void; placeholder: string }) {
  return (
    <label class="ctl-select ctl-search" title="Narrow this graph's data (on top of the page filter)">
      <CtlIcon name="search" />
      <input aria-label={placeholder} placeholder={placeholder} value={value} onInput={(e) => onInput((e.target as HTMLInputElement).value)} />
    </label>
  );
}

export function CtlDivider() {
  return <span class="ctl-divider" aria-hidden="true" />;
}
