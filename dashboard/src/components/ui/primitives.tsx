/**
 * UI primitives — the small shared building blocks of the design language.
 * See dashboard/DESIGN.md. Prefer these over bespoke per-view markup.
 */
import type { ComponentChildren, JSX } from "preact";

/** Uppercase, letter-spaced micro-label. Section heads, stat labels, captions. */
export function Eyebrow({
  children,
  class: cls = "",
}: {
  children: ComponentChildren;
  class?: string;
}) {
  return <span class={`ui-eyebrow ${cls}`}>{children}</span>;
}

/** A confident metric: large mono value + eyebrow label + faint sub. */
export function Stat({
  value,
  label,
  sub,
  tone,
  size = "md",
  class: cls = "",
}: {
  value: ComponentChildren;
  label?: ComponentChildren;
  sub?: ComponentChildren;
  /** colors the value (e.g. "best" → purple, "good" → green) */
  tone?: "default" | "best" | "good" | "warn" | "bad" | "accent";
  size?: "sm" | "md" | "lg";
  class?: string;
}) {
  return (
    <div class={`ui-stat ui-stat--${size} ${cls}`}>
      {label && <span class="ui-stat-label">{label}</span>}
      <span class={`ui-stat-value${tone && tone !== "default" ? ` ui-stat-value--${tone}` : ""}`}>
        {value}
      </span>
      {sub && <span class="ui-stat-sub">{sub}</span>}
    </div>
  );
}

export type BadgeTone =
  | "active"
  | "running"
  | "concluded"
  | "abandoned"
  | "neutral"
  | "best"
  | "good"
  | "warn"
  | "bad";

/** Status pill. Maps to --badge-* token pairs. */
export function Badge({
  tone = "neutral",
  dot = false,
  children,
  class: cls = "",
}: {
  tone?: BadgeTone;
  /** show a pulsing status dot before the label */
  dot?: boolean;
  children: ComponentChildren;
  class?: string;
}) {
  return (
    <span class={`ui-badge ui-badge--${tone} ${cls}`}>
      {dot && <span class="ui-badge-dot" />}
      {children}
    </span>
  );
}

/** Ghost chrome button. Transparent → --bg-hi on hover → --accent when active. */
export function IconButton({
  active = false,
  outlined = false,
  title,
  onClick,
  children,
  class: cls = "",
  ...rest
}: {
  active?: boolean;
  outlined?: boolean;
  title?: string;
  onClick?: (e: MouseEvent) => void;
  children: ComponentChildren;
  class?: string;
} & Omit<JSX.HTMLAttributes<HTMLButtonElement>, "class" | "onClick">) {
  return (
    <button
      type="button"
      title={title}
      onClick={onClick as any}
      class={`ui-btn${active ? " is-active" : ""}${outlined ? " ui-btn--outlined" : ""} ${cls}`}
      {...rest}
    >
      {children}
    </button>
  );
}

/** Labeled toggle pill (filter chips, view options). */
export function Toggle({
  active,
  onClick,
  title,
  children,
  class: cls = "",
}: {
  active: boolean;
  onClick: (e: MouseEvent) => void;
  title?: string;
  children: ComponentChildren;
  class?: string;
}) {
  return (
    <button
      type="button"
      title={title}
      onClick={onClick as any}
      class={`ui-toggle${active ? " is-active" : ""} ${cls}`}
    >
      {children}
    </button>
  );
}

/** Hairline separator. Horizontal by default; `vertical` for inline dividers. */
export function Separator({ vertical = false }: { vertical?: boolean }) {
  return <span class={`ui-sep${vertical ? " ui-sep--v" : ""}`} aria-hidden="true" />;
}

/** The single empty/zero-data treatment. */
export function EmptyState({
  icon,
  title,
  body,
}: {
  icon?: ComponentChildren;
  title: ComponentChildren;
  body?: ComponentChildren;
}) {
  return (
    <div class="ui-empty">
      {icon && <div class="ui-empty-icon">{icon}</div>}
      <div class="ui-empty-title">{title}</div>
      {body && <div class="ui-empty-body">{body}</div>}
    </div>
  );
}
