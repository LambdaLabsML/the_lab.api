import { useState, useEffect, useMemo } from "preact/hooks";
import { apiSpec } from "../state/signals";
import { getOpenApiSpec, getApiStats } from "../state/api";
import { syntaxHighlight } from "../lib/syntax-highlight";
import { escapeHtml } from "../lib/format";
import { Badge, IconButton, EmptyState } from "../components/ui";
import type { BadgeTone } from "../components/ui";
import { useSelection, useCopyToClipboard, useEscape } from "../lib/hooks";

const TAG_ORDER = [
  "orientation", "ideas", "experiments", "monitoring", "comparison", "lifecycle", "other",
];
const TAG_LABELS: Record<string, string> = {
  orientation: "Orientation",
  ideas: "Ideas",
  experiments: "Experiments",
  monitoring: "Monitoring",
  comparison: "Comparison",
  lifecycle: "Lifecycle",
  other: "Other",
};

/** Map an HTTP method to a design-language badge tone. */
function methodTone(method: string): BadgeTone {
  switch (method.toUpperCase()) {
    case "GET": return "good";
    case "POST": return "warn";
    case "PUT":
    case "PATCH": return "concluded";
    case "DELETE": return "bad";
    default: return "neutral";
  }
}

interface Endpoint {
  method: string;
  path: string;
  summary: string;
  op: any;
}

const epKey = (ep: { method: string; path: string }) => `${ep.method} ${ep.path}`;

function classifyEndpoint(method: string, path: string, summary: string): string {
  const p = path.toLowerCase();
  if (p === "/api/v1/digest" || p === "/api/v1/leaderboard" || p === "/api/v1/backlog" || p === "/api/v1/graph" || p === "/api/v1/task" || p === "/api/v1/stats") return "orientation";
  if (p.match(/\/experiments\/.*\/(progress|log|timeseries)/)) return "monitoring";
  if (p.match(/\/experiments\/compare/) || p.match(/compare-curves/)) return "comparison";
  if (p.match(/\/experiments\/.*\/(start|restart|cancel)/) || (method === "post" && p.match(/\/experiments\//))) return "lifecycle";
  if (p.match(/\/experiments/)) return "experiments";
  if (p.match(/\/ideas/)) return "ideas";
  if (p === "/api/v1/wait") return "lifecycle";
  return "other";
}

function resolveSchema(schema: any, spec: any): any {
  if (!schema) return null;
  if (schema["$ref"]) {
    const parts = schema["$ref"].split("/");
    let resolved = spec;
    for (let i = 1; i < parts.length; i++) resolved = resolved[parts[i]];
    return resolved;
  }
  return schema;
}

function buildExample(schema: any, spec: any, depth = 0): any {
  if (!schema || depth > 4) return "...";
  schema = resolveSchema(schema, spec);
  if (!schema) return "";
  if (schema.type === "object" || schema.properties) {
    const obj: any = {};
    for (const k in schema.properties || {}) {
      obj[k] = buildExample(schema.properties[k], spec, depth + 1);
    }
    return obj;
  }
  if (schema.type === "array") return [buildExample(schema.items, spec, depth + 1)];
  if (schema.enum) return schema.enum[0];
  if (schema.default !== undefined) return schema.default;
  if (schema.type === "string") return "";
  if (schema.type === "integer") return 0;
  if (schema.type === "number") return 0.0;
  if (schema.type === "boolean") return false;
  return null;
}

export function ApiView() {
  const spec = apiSpec.value;
  const [selected, setSelected] = useState<Endpoint | null>(null);
  const { selected: selectedKey, setSelected: setSelectedKey, clear: clearSelection } =
    useSelection<string>();
  const [params, setParams] = useState<Record<string, string>>({});
  const [body, setBody] = useState("");
  const [response, setResponse] = useState<{ status: string; body: string; ok: boolean } | null>(null);
  const [sending, setSending] = useState(false);
  const [callCounts, setCallCounts] = useState<Record<string, number>>({});
  const { copied, copy } = useCopyToClipboard();

  useEscape(() => closeDetail(), selected !== null);

  useEffect(() => {
    if (!spec) getOpenApiSpec().then((s) => (apiSpec.value = s));
    getApiStats().then((s) => {
      const map: Record<string, number> = {};
      for (const { endpoint, count } of s.calls) map[endpoint] = count;
      setCallCounts(map);
    }).catch(() => {});
  }, []);

  const groups = useMemo(() => {
    if (!spec) return {};
    const g: Record<string, Endpoint[]> = {};
    for (const path in spec.paths || {}) {
      if (path === "/" || path === "/openapi.json") continue;
      for (const method in spec.paths[path]) {
        if (method === "parameters") continue;
        const op = spec.paths[path][method];
        const tag = classifyEndpoint(method, path, op.summary || op.operationId || "");
        if (!g[tag]) g[tag] = [];
        g[tag].push({ method: method.toUpperCase(), path, summary: op.summary || op.operationId || "", op });
      }
    }
    return g;
  }, [spec]);

  function selectEndpoint(ep: Endpoint) {
    setSelected(ep);
    setSelectedKey(epKey(ep));
    setResponse(null);
    const p: Record<string, string> = {};
    for (const param of ep.op.parameters || []) {
      const def = param.schema?.default;
      p[param.name] = def !== undefined ? String(def) : "";
    }
    setParams(p);
    if (ep.op.requestBody) {
      const content = ep.op.requestBody.content;
      const jsonSchema = content?.["application/json"]?.schema;
      setBody(jsonSchema ? JSON.stringify(buildExample(jsonSchema, spec, 0), null, 2) : "{}");
    } else {
      setBody("");
    }
  }

  function closeDetail() {
    setSelected(null);
    clearSelection();
  }

  /** Build the resolved URL (with substituted path + query params) for the request. */
  function resolvedUrl(): string {
    if (!selected) return "";
    let resolvedPath = selected.path;
    const query: string[] = [];
    for (const param of selected.op.parameters || []) {
      const val = params[param.name]?.trim();
      if (!val) continue;
      if (param.in === "path") resolvedPath = resolvedPath.replace(`{${param.name}}`, encodeURIComponent(val));
      else if (param.in === "query") query.push(`${encodeURIComponent(param.name)}=${encodeURIComponent(val)}`);
    }
    return resolvedPath + (query.length ? "?" + query.join("&") : "");
  }

  /** Assemble the equivalent curl command for the current request. */
  function curlCommand(): string {
    if (!selected) return "";
    const url = resolvedUrl();
    const parts = [`curl -X ${selected.method}`, `'${url}'`];
    if (body && (selected.method === "POST" || selected.method === "PUT" || selected.method === "PATCH")) {
      parts.push(`-H 'Content-Type: application/json'`);
      parts.push(`-d '${body.replace(/'/g, "'\\''")}'`);
    }
    return parts.join(" ");
  }

  async function send() {
    if (!selected) return;
    setSending(true);
    setResponse(null);
    const url = resolvedUrl();
    const opts: RequestInit = { method: selected.method, headers: {} as Record<string, string> };
    if (body && (selected.method === "POST" || selected.method === "PUT" || selected.method === "PATCH")) {
      (opts.headers as Record<string, string>)["Content-Type"] = "application/json";
      opts.body = body;
    }
    const t0 = performance.now();
    try {
      const r = await fetch(url, opts);
      const elapsed = Math.round(performance.now() - t0);
      const text = await r.text();
      let formatted: string;
      try {
        formatted = syntaxHighlight(JSON.stringify(JSON.parse(text), null, 2));
      } catch {
        formatted = escapeHtml(text);
      }
      setResponse({ status: `${r.status} ${r.statusText}  (${elapsed}ms)`, body: formatted, ok: r.ok });
    } catch (e) {
      setResponse({ status: "Error", body: String(e), ok: false });
    } finally {
      setSending(false);
    }
  }

  function getCount(method: string, path: string): number {
    const key = `${method} ${path}`;
    if (callCounts[key]) return callCounts[key];
    const normalized = path.replace(/\/\{[^}]+\}/g, "/{id}");
    return callCounts[`${method} ${normalized}`] || 0;
  }

  const totalEndpoints = TAG_ORDER.reduce((n, t) => n + (groups[t]?.length || 0), 0);

  return (
    <>
      <div id="api-container">
        <div class="pane-bar">
          <h2 class="pane-bar-title">API Reference</h2>
          <span class="pane-bar-count">{totalEndpoints} endpoints</span>
        </div>
        <div id="api-list">
          {!spec && (
            <EmptyState icon="⋯" title="Loading API spec…" />
          )}
          {TAG_ORDER.map((tag) => {
            const items = groups[tag];
            if (!items?.length) return null;
            return (
              <div class="api-group" key={tag}>
                <div class="api-group-title ui-eyebrow">{TAG_LABELS[tag] || tag}</div>
                {items.map((ep) => (
                  <div
                    key={epKey(ep)}
                    class={`api-row${selectedKey === epKey(ep) ? " active" : ""}`}
                    onClick={() => selectEndpoint(ep)}
                  >
                    <Badge tone={methodTone(ep.method)} class="api-method">{ep.method}</Badge>
                    <span class="api-path">{ep.path}</span>
                    {(() => { const n = getCount(ep.method, ep.path); return n > 0 ? <span class="api-call-count">{n}</span> : null; })()}
                    {ep.summary && <span class="api-desc">{ep.summary}</span>}
                  </div>
                ))}
              </div>
            );
          })}
        </div>
      </div>
      {selected && (
        <div id="api-detail" class="open">
          <div id="api-detail-header">
            <Badge tone={methodTone(selected.method)} class="api-method">{selected.method}</Badge>
            <span id="api-detail-url">{selected.path}</span>
            <div class="api-detail-actions">
              <IconButton
                class={copied ? "is-copied" : ""}
                onClick={() => copy(curlCommand())}
                title="Copy as curl"
              >
                {copied ? "Copied!" : "Copy curl"}
              </IconButton>
              <IconButton onClick={closeDetail} title="Close">&times;</IconButton>
            </div>
          </div>
          {selected.op.description && (
            <div class="api-description">
              {selected.op.description.split("\n").map((line: string, i: number) => {
                const trimmed = line.trim();
                if (!trimmed) return <br key={i} />;
                if (trimmed.startsWith("Example:") || trimmed.startsWith("→")) {
                  return <div key={i} class="api-example-line">{trimmed}</div>;
                }
                return <div key={i}>{trimmed}</div>;
              })}
            </div>
          )}
          <div id="api-params" class="has-params">
            {(selected.op.parameters || []).map((param: any) => (
              <div key={param.name}>
                <label>
                  {param.name}
                  {param.required && <span class="api-param-req"> *</span>}
                  {" "}({param.in})
                </label>
                <input
                  type="text"
                  value={params[param.name] || ""}
                  onInput={(e) => setParams({ ...params, [param.name]: (e.target as HTMLInputElement).value })}
                  placeholder={param.schema?.type || ""}
                />
              </div>
            ))}
            {selected.op.requestBody && (
              <div>
                <label>Body (JSON)</label>
                <textarea value={body} onInput={(e) => setBody((e.target as HTMLTextAreaElement).value)} />
              </div>
            )}
            <div class="api-send-row">
              <button id="api-send-btn" class="ui-btn ui-btn--outlined is-active" onClick={send} disabled={sending}>
                {sending ? "Sending…" : "Send"}
              </button>
            </div>
          </div>
          <div id="api-response">
            {response && (
              <>
                <div id="api-response-status" class={response.ok ? "ok" : "err"}>{response.status}</div>
                <pre id="api-response-body" dangerouslySetInnerHTML={{ __html: response.body }} />
              </>
            )}
          </div>
        </div>
      )}
    </>
  );
}
