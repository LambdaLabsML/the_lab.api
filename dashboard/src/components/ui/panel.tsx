/**
 * Panel primitives — the one panel shell every view/panel uses.
 *
 *   <Panel>
 *     <PanelHeader title="EXPERIMENTS" count={42} actions={<IconButton …/>} />
 *     <PanelBody>…</PanelBody>
 *   </Panel>
 *
 * The header is the micro-eyebrow row, separated from the body by a single
 * hairline — never a wrapping box. See dashboard/DESIGN.md.
 */
import type { ComponentChildren, JSX } from "preact";

export function Panel({
  children,
  class: cls = "",
  scroll = false,
  ...rest
}: {
  children: ComponentChildren;
  class?: string;
  /** make the panel body scroll within a fixed-height parent */
  scroll?: boolean;
} & Omit<JSX.HTMLAttributes<HTMLDivElement>, "class">) {
  return (
    <div class={`ui-panel${scroll ? " ui-panel--scroll" : ""} ${cls}`} {...rest}>
      {children}
    </div>
  );
}

export function PanelHeader({
  title,
  count,
  actions,
  class: cls = "",
}: {
  title: ComponentChildren;
  count?: number | string;
  actions?: ComponentChildren;
  class?: string;
}) {
  return (
    <div class={`ui-panel-head ${cls}`}>
      <span class="ui-eyebrow ui-panel-head-title">{title}</span>
      {count != null && <span class="ui-panel-head-count">{count}</span>}
      {actions && <div class="ui-panel-head-actions">{actions}</div>}
    </div>
  );
}

export function PanelBody({
  children,
  class: cls = "",
  pad = true,
  ...rest
}: {
  children: ComponentChildren;
  class?: string;
  pad?: boolean;
} & Omit<JSX.HTMLAttributes<HTMLDivElement>, "class">) {
  return (
    <div class={`ui-panel-body${pad ? "" : " ui-panel-body--flush"} ${cls}`} {...rest}>
      {children}
    </div>
  );
}
