/**
 * JsonView — collapsible, syntax-highlighted JSON display.
 *
 * Level 0 (top keys) are always visible. Deeper levels are collapsed
 * by default. Strings are truncated with ellipsis (click to expand).
 */
import { useState } from "preact/hooks";

const MAX_STRING_LEN = 60;

export function JsonView({ data, label, labelColor }: { data: any; label?: string; labelColor?: string }) {
  if (data == null || (typeof data === "object" && Object.keys(data).length === 0)) {
    return null;
  }

  return (
    <div class="json-view">
      {label && <span class="json-label" style={labelColor ? { color: labelColor } : undefined}>{label}</span>}
      <div class="json-tree">
        <JsonNode value={data} depth={0} />
      </div>
    </div>
  );
}

function JsonNode({ value, depth }: { value: any; depth: number }) {
  if (value === null || value === undefined) {
    return <span class="json-null">null</span>;
  }

  if (typeof value === "boolean") {
    return <span class="json-bool">{String(value)}</span>;
  }

  if (typeof value === "number") {
    return <span class="json-num">{formatNumber(value)}</span>;
  }

  if (typeof value === "string") {
    return <JsonString value={value} />;
  }

  if (Array.isArray(value)) {
    return <JsonArray items={value} depth={depth} />;
  }

  if (typeof value === "object") {
    return <JsonObject obj={value} depth={depth} />;
  }

  return <span class="json-str">{String(value)}</span>;
}

function JsonString({ value }: { value: string }) {
  const [expanded, setExpanded] = useState(false);

  if (value.length <= MAX_STRING_LEN || expanded) {
    return (
      <span class="json-str" onClick={() => expanded && setExpanded(false)} style={expanded ? { cursor: "pointer" } : undefined}>
        "{value}"
      </span>
    );
  }

  return (
    <span class="json-str json-truncated" onClick={() => setExpanded(true)} title={value}>
      "{value.slice(0, MAX_STRING_LEN)}…"
    </span>
  );
}

function JsonObject({ obj, depth }: { obj: Record<string, any>; depth: number }) {
  const keys = Object.keys(obj);
  const [open, setOpen] = useState(depth < 1);

  if (keys.length === 0) return <span class="json-brace">{"{}"}</span>;

  if (!open) {
    return (
      <span class="json-collapsed" onClick={() => setOpen(true)}>
        <span class="json-brace">{"{"}</span>
        <span class="json-ellipsis">{keys.length} keys…</span>
        <span class="json-brace">{"}"}</span>
      </span>
    );
  }

  return (
    <span class="json-block">
      <span class="json-brace json-toggle" onClick={() => setOpen(false)}>{"{"}</span>
      <div class="json-indent">
        {keys.map((k, i) => (
          <div key={k} class="json-entry">
            <span class="json-key">{k}</span>
            <span class="json-colon">: </span>
            <JsonNode value={obj[k]} depth={depth + 1} />
            {i < keys.length - 1 && <span class="json-comma">,</span>}
          </div>
        ))}
      </div>
      <span class="json-brace">{"}"}</span>
    </span>
  );
}

function JsonArray({ items, depth }: { items: any[]; depth: number }) {
  const [open, setOpen] = useState(depth < 1);

  if (items.length === 0) return <span class="json-brace">[]</span>;

  // Short arrays of primitives: inline
  if (items.length <= 5 && items.every((v) => typeof v !== "object" || v === null)) {
    return (
      <span>
        <span class="json-brace">[</span>
        {items.map((v, i) => (
          <span key={i}>
            <JsonNode value={v} depth={depth + 1} />
            {i < items.length - 1 && <span class="json-comma">, </span>}
          </span>
        ))}
        <span class="json-brace">]</span>
      </span>
    );
  }

  if (!open) {
    return (
      <span class="json-collapsed" onClick={() => setOpen(true)}>
        <span class="json-brace">[</span>
        <span class="json-ellipsis">{items.length} items…</span>
        <span class="json-brace">]</span>
      </span>
    );
  }

  return (
    <span class="json-block">
      <span class="json-brace json-toggle" onClick={() => setOpen(false)}>[</span>
      <div class="json-indent">
        {items.map((v, i) => (
          <div key={i} class="json-entry">
            <JsonNode value={v} depth={depth + 1} />
            {i < items.length - 1 && <span class="json-comma">,</span>}
          </div>
        ))}
      </div>
      <span class="json-brace">]</span>
    </span>
  );
}

function formatNumber(n: number): string {
  if (Number.isInteger(n)) return String(n);
  if (Math.abs(n) < 0.0001 && n !== 0) return n.toExponential(2);
  return n.toFixed(4).replace(/0+$/, "").replace(/\.$/, "");
}
