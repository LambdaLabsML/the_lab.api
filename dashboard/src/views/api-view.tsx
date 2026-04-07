import { useState, useEffect, useMemo } from "preact/hooks";
import { apiSpec } from "../state/signals";
import { getOpenApiSpec, getApiStats } from "../state/api";
import type { ApiStatsResponse } from "../state/api";
import { syntaxHighlight } from "../lib/syntax-highlight";
import { escapeHtml } from "../lib/format";

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

interface Endpoint {
  method: string;
  path: string;
  summary: string;
  op: any;
}

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
  const [params, setParams] = useState<Record<string, string>>({});
  const [body, setBody] = useState("");
  const [response, setResponse] = useState<{ status: string; body: string; ok: boolean } | null>(null);
  const [sending, setSending] = useState(false);
  const [stats, setStats] = useState<ApiStatsResponse | null>(null);
  const [showStats, setShowStats] = useState(false);

  useEffect(() => {
    if (!spec) getOpenApiSpec().then((s) => (apiSpec.value = s));
    getApiStats().then(setStats).catch(() => {});
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

  async function send() {
    if (!selected) return;
    setSending(true);
    setResponse(null);
    let resolvedPath = selected.path;
    const query: string[] = [];
    for (const param of selected.op.parameters || []) {
      const val = params[param.name]?.trim();
      if (!val) continue;
      if (param.in === "path") resolvedPath = resolvedPath.replace(`{${param.name}}`, encodeURIComponent(val));
      else if (param.in === "query") query.push(`${encodeURIComponent(param.name)}=${encodeURIComponent(val)}`);
    }
    const url = resolvedPath + (query.length ? "?" + query.join("&") : "");
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

  // Build endpoint → call count lookup from stats
  const callCounts = useMemo(() => {
    const map: Record<string, number> = {};
    if (!stats) return map;
    for (const { endpoint, count } of stats.calls) {
      // endpoint is "GET /api/v1/ideas/{id}" — extract method + path
      map[endpoint] = count;
    }
    return map;
  }, [stats]);

  function getCount(method: string, path: string): number {
    // Try exact match, then with {id} normalization
    const key = `${method} ${path}`;
    if (callCounts[key]) return callCounts[key];
    const normalized = path.replace(/\/\{[^}]+\}/g, "/{id}");
    const normKey = `${method} ${normalized}`;
    return callCounts[normKey] || 0;
  }

  return (
    <>
      <div id="api-container">
        <div id="api-list">
          <div
            class="api-stats-toggle"
            onClick={() => setShowStats(!showStats)}
            style={{ padding: "8px 12px", cursor: "pointer", color: "#d29922", fontSize: "11px", borderBottom: "1px solid #21262d" }}
          >
            <span class={`arrow${showStats ? " open" : ""}`}>&#9654;</span> Usage Stats
            {stats && <span style={{ color: "#484f58", marginLeft: "8px" }}>{stats.total_calls} total calls</span>}
          </div>
          {showStats && stats && (
            <div style={{ padding: "8px 12px", fontSize: "11px", borderBottom: "1px solid #21262d", maxHeight: "300px", overflowY: "auto" }}>
              <div style={{ color: "#8b949e", marginBottom: "6px", fontWeight: 700 }}>Top Endpoints</div>
              {stats.calls.slice(0, 15).map((c) => (
                <div key={c.endpoint} style={{ display: "flex", justifyContent: "space-between", padding: "1px 0", color: "#c9d1d9" }}>
                  <span>{c.endpoint}</span>
                  <span style={{ color: "#58a6ff", marginLeft: "8px" }}>{c.count}</span>
                </div>
              ))}
              <div style={{ color: "#8b949e", marginTop: "10px", marginBottom: "6px", fontWeight: 700 }}>Common Patterns</div>
              {stats.patterns.slice(0, 15).map((p) => (
                <div key={p.sequence} style={{ display: "flex", justifyContent: "space-between", padding: "1px 0", color: "#c9d1d9" }}>
                  <span style={{ fontSize: "10px" }}>{p.sequence}</span>
                  <span style={{ color: "#3fb950", marginLeft: "8px", flexShrink: 0 }}>{p.count}</span>
                </div>
              ))}
            </div>
          )}
          {!spec && <div style={{ padding: "20px", color: "#484f58" }}>Loading API spec...</div>}
          {TAG_ORDER.map((tag) => {
            const items = groups[tag];
            if (!items?.length) return null;
            return (
              <div class="api-group" key={tag}>
                <div class="api-group-title">{TAG_LABELS[tag] || tag}</div>
                {items.map((ep) => (
                  <div
                    key={ep.method + ep.path}
                    class={`api-row${selected?.path === ep.path && selected?.method === ep.method ? " active" : ""}`}
                    onClick={() => selectEndpoint(ep)}
                  >
                    <span class={`api-method ${ep.method.toLowerCase()}`}>{ep.method}</span>
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
            <span class={`api-method ${selected.method.toLowerCase()}`}>{selected.method}</span>
            <span id="api-detail-url">{selected.path}</span>
            <span class="close-btn" onClick={() => setSelected(null)}>&times;</span>
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
                  {param.required && <span style={{ color: "#f85149" }}> *</span>}
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
            <div style={{ marginTop: "6px" }}>
              <button id="api-send-btn" onClick={send} disabled={sending}>
                {sending ? "Sending..." : "Send"}
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
