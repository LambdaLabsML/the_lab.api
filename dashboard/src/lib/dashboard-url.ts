/** Pure URL codec. Paths describe the screen; queries describe its visible setup. */
import type { PlotValues } from "./hooks/use-plot-controls";
import type { Plot } from "../state/plots";
export type Section = "review" | "messages" | "artifacts" | "database" | "queue";
export type DetailView = "log" | "output" | "script" | "diff";
export interface DashboardRoute {
  section: Section; settings?: boolean; tool?: string; artifact?: string; edit?: boolean;
  thread?: number; message?: number; dock?: boolean; database?: "experiments" | "ideas" | "graph" | "log";
  scope?: { ideaId: number; label: string }; idea?: number; experiment?: string; view?: DetailView;
}
export interface OverviewSetup {
  metric: string; main: PlotValues; height: number; labels: "off" | "records" | "auto";
  plots: (Plot & { controls: PlotValues })[];
  filter: string | null; where: string; tags: string[]; tagMode: "and" | "or"; query: string;
  hidden: string[]; highlights: { category: "idea" | "experiment"; value: string; label: string }[];
}
export interface DashboardLocation {
  route: DashboardRoute; overview?: OverviewSetup;
  inbox?: { view: string; audience: "all" | "me" | "users"; query: string; status: string; sender: string };
}
export const MAX_INLINE_URL_LENGTH = 240;
export const DEFAULT_CONTROLS: PlotValues = { chartView: "l", chartXAxis: "experiments", chartTimeCurve: "best", improvementsOnly: true, ideaMean: false, clipOutliers: false, reverseTime: false, colorMode: "status+improve", logScale: false, plotFilter: "" };
const controlsKeys: Record<keyof PlotValues, string> = { chartView: "size", chartXAxis: "axis", chartTimeCurve: "curve", improvementsOnly: "step", ideaMean: "mean", clipOutliers: "clip", reverseTime: "reverse", colorMode: "color", logScale: "log", plotFilter: "search" };
const atom = (value: string) => encodeURIComponent(value).replace(/%2F/gi, "/");
const unatom = (value: string) => { try { return decodeURIComponent(value); } catch { return value; } };
const id = (value?: string) => value && /^\d+$/.test(value) && Number(value) > 0 ? Number(value) : undefined;
const height = (value: string | null) => Math.min(760, Math.max(220, Number(value) || 360));
export function defaultOverview(): OverviewSetup {
  return { metric: "", main: { ...DEFAULT_CONTROLS }, height: 360, labels: "auto", plots: [{ id: "url-0", type: "scatter", controls: { ...DEFAULT_CONTROLS } }], filter: null, where: "", tags: [], tagMode: "and", query: "", hidden: [], highlights: [] };
}
function writeControls(values: PlotValues, set: (key: string, value: string) => void) {
  for (const [key, name] of Object.entries(controlsKeys)) {
    const k = key as keyof PlotValues, value = values[k];
    if (value === DEFAULT_CONTROLS[k]) continue;
    if (k === "improvementsOnly" && value === false) set("all", "");
    else set(name, typeof value === "boolean" ? "" : String(value));
  }
}
function readControls(params: URLSearchParams): PlotValues {
  const result = { ...DEFAULT_CONTROLS };
  for (const [key, name] of Object.entries(controlsKeys)) {
    const k = key as keyof PlotValues;
    if (!params.has(name)) continue;
    if (typeof DEFAULT_CONTROLS[k] === "boolean") (result as any)[k] = params.get(name) !== "0";
    else (result as any)[k] = params.get(name)!;
  }
  if (params.has("all")) result.improvementsOnly = false;
  if (!["x", "s", "m", "l"].includes(result.chartView)) result.chartView = "l";
  if (!["experiments", "ideas", "time", "compute"].includes(result.chartXAxis)) result.chartXAxis = "experiments";
  if (!["best", "avg"].includes(result.chartTimeCurve)) result.chartTimeCurve = "best";
  return result;
}
export function routePath(route: DashboardRoute): string {
  if (route.settings) return `/settings${route.tool ? "/" + atom(route.tool) : ""}`;
  let path = "/" + (route.section === "review" ? "overview" : route.section);
  if (route.section === "artifacts") return path + (route.artifact ? "/" + route.artifact.split("/").map(atom).join("/") : "");
  if (route.section === "database") path += "/" + (route.database ?? "experiments");
  if (route.section === "messages" && route.thread) path += `/threads/${route.thread}${route.message ? `/messages/${route.message}` : ""}`;
  if (route.idea) path += `/ideas/${route.idea}${route.experiment ? `/experiments/${atom(route.experiment)}` : ""}${route.view ? "/" + route.view : ""}`;
  return path;
}
export function formatDashboardUrl(location: DashboardLocation): string {
  const { route, overview: view, inbox } = location;
  const params = new URLSearchParams();
  if (view && route.section === "review" && !route.settings) {
    if (view.metric) params.set("metric", view.metric);
    writeControls(view.main, (key, value) => params.set(key, value));
    if (view.height !== 360) params.set("height", String(view.height));
    if (view.labels !== "auto") params.set("labels", view.labels);
    if (view.filter) params.set("filter", view.filter);
    if (view.where) params.set("where", view.where);
    if (view.query) params.set("q", view.query);
    for (const tag of view.tags) params.append("tag", tag);
    if (view.tagMode !== "and") params.set("tags", view.tagMode);
    if (view.hidden.length) params.set("hide", view.hidden.join(","));
    for (const item of view.highlights) params.append(item.category === "idea" ? "highlight" : "experiment", item.category === "idea" ? item.value : item.label.replace(/^exp\//, ""));
    // The default is one correlation graph; an explicit empty value means no extras.
    if (!view.plots.length) params.append("graph", "none");
    for (const plot of view.plots) {
      const parts = [plot.type === "metrics" ? `line:${atom(plot.metric || view.metric)}` : `scatter:${atom(plot.x || "")},${atom(plot.y || "")}`];
      if (plot.width === "half") parts.push("half");
      if (plot.height && plot.height !== 360) parts.push(`height=${plot.height}`);
      writeControls(plot.controls, (key, value) => parts.push(key + (value ? "=" + atom(value) : "")));
      if (view.plots.length === 1 && parts.join(";") === "scatter:,") continue;
      params.append("graph", parts.join(";"));
    }
  }
  if (route.dock) { params.set("dock", ""); if (route.thread) params.set("thread", String(route.thread)); if (route.message) params.set("message", String(route.message)); }
  if (inbox) {
    if (inbox.view !== "for_you") params.set("inbox", inbox.view);
    if (inbox.audience !== "all") params.set("audience", inbox.audience);
    if (inbox.query) params.set(route.section === "messages" ? "q" : "inbox-search", inbox.query);
    if (inbox.status !== "all") params.set("status", inbox.status);
    if (inbox.sender) params.set("sender", inbox.sender);
  }
  if (route.scope && route.section === "database") { params.set("scope", route.scope.label || String(route.scope.ideaId)); }
  if (route.edit) params.set("edit", "");
  const query = params.toString().replace(/=(&|$)/g, "$1").replace(/%2C/gi, ",").replace(/%3A/gi, ":").replace(/%3B/gi, ";").replace(/%2F/gi, "/");
  return routePath(route) + (query ? "?" + query : "");
}
export function parseDashboardUrl(input: string): DashboardLocation & { viewRef?: string; home: boolean; legacy: boolean } {
  const url = new URL(input, "http://dashboard.local");
  let parts = url.pathname.split("/").filter(Boolean).map(unatom), legacy = false;
  const params = url.searchParams, hash = url.hash.slice(1).replace(/^\//, "");
  const oldView = new URLSearchParams(hash.includes("=") ? hash : "");
  let oldIdea = id(parts[0] === "ideas" ? parts[1] : undefined) ?? id(params.get("idea") ?? undefined) ?? id(oldView.get("idea") ?? undefined);
  if (hash && !hash.includes("=")) { parts = hash.split("/").map(unatom); legacy = true; }
  if (parts[0] === "ideas") { parts = ["overview"]; legacy = true; }
  const first = parts.shift();
  const route: DashboardRoute = { section: ({ overview: "review", review: "review", messages: "messages", database: "database", queue: "queue", artifacts: "artifacts" } as Record<string, Section>)[first || ""] || "review" };
  if (first === "settings" || first === "tools") { route.settings = true; route.tool = parts.shift(); }
  let viewRef: string | undefined;
  if (parts[0] === "views") { parts.shift(); viewRef = parts.shift(); }
  if (route.section === "artifacts" && !viewRef) route.artifact = parts.join("/");
  else {
    if (route.section === "database" && ["experiments", "ideas", "graph", "log"].includes(parts[0])) route.database = parts.shift() as DashboardRoute["database"];
    if (route.section === "messages" && (parts[0] === "threads" || id(parts[0]))) {
      if (parts[0] === "threads") parts.shift();
      route.thread = id(parts.shift());
      if (parts[0] === "messages") parts.shift();
      route.message = id(parts[0]); if (route.message) parts.shift();
    }
    while (parts.length) {
      const key = parts.shift();
      if (key === "ideas") route.idea = id(parts.shift());
      else if (key === "experiments") route.experiment = parts.shift();
      else if (["log", "output", "script", "diff"].includes(key!)) route.view = key as DetailView;
    }
  }
  route.idea ??= oldIdea;
  route.experiment ??= params.get("e") || oldView.get("exp") || undefined;
  const oldPanel = params.get("view")?.split("/");
  const panel = oldPanel?.[0] || oldView.get("view");
  if (panel && ["log", "output", "script", "diff"].includes(panel)) { route.view = panel as DetailView; route.experiment ||= oldPanel?.slice(1).join("/") || undefined; legacy = true; }
  route.dock = params.has("dock") || undefined;
  if (route.dock) { route.thread = id(params.get("thread") ?? undefined); route.message = id(params.get("message") ?? undefined); }
  if (route.section === "messages" && params.has("message")) route.message = id(params.get("message") ?? undefined);
  route.edit = params.has("edit") || undefined;
  if (params.has("scope")) { const label = params.get("scope")!; const ideaId = id(label.split(".")[0]); if (ideaId) route.scope = { ideaId, label: label.includes(".") ? label : "" }; }
  let overview: OverviewSetup | undefined;
  if (route.section === "review" && !route.settings) {
    overview = defaultOverview(); overview.metric = params.get("metric") || params.get("m") || "";
    overview.main = readControls(params); overview.height = height(params.get("height"));
    if (["off", "records"].includes(params.get("labels")!)) overview.labels = params.get("labels") as "off" | "records";
    overview.filter = params.get("filter"); overview.where = params.get("where") || "";
    overview.query = params.get("q") || ""; overview.tags = params.getAll("tag"); overview.tagMode = params.get("tags") === "or" || params.get("tm") === "or" ? "or" : "and";
    if (!overview.tags.length && (params.has("t") || (params.get("tags") !== "and" && params.get("tags") !== "or"))) overview.tags = (params.get("t") || params.get("tags") || "").split(",").filter(Boolean);
    overview.hidden = (params.get("hide") || "").split(",").filter(Boolean);
    if (/^[acr]+$/.test(params.get("hide") || "")) overview.hidden = [...params.get("hide")!].map(code => ({ a: "abandoned", c: "concluded", r: "running" })[code]!);
    if (params.has("cm")) overview.main.colorMode = params.get("cm")!;
    if (params.has("imp")) overview.main.improvementsOnly = true;
    if (params.has("noclip")) overview.main.clipOutliers = false;
    if (params.has("norev")) overview.main.reverseTime = false;
    overview.highlights = [...params.getAll("highlight").filter(value => id(value)).map(value => ({ category: "idea" as const, value, label: `#${value}` })), ...params.getAll("experiment").map(value => ({ category: "experiment" as const, value: value.split(".")[0], label: `exp/${value}` }))];
    if (params.has("graph")) overview.plots = params.getAll("graph").filter(value => value !== "none").map((value, index) => {
      const [heading, ...options] = value.split(";"), [kind, ...metrics] = heading.split(":");
      const axes = metrics.join(":").split(",").map(unatom);
      const opts = new URLSearchParams(options.map(option => { const [key, ...rest] = option.split("="); return [key, unatom(rest.join("="))]; }));
      return { id: `url-${index}`, type: kind === "line" ? "metrics" as const : "scatter" as const, ...(kind === "line" ? { metric: axes[0] } : { x: axes[0], y: axes[1] || "" }), width: opts.has("half") ? "half" as const : "full" as const, height: height(opts.get("height")), controls: readControls(opts) };
    });
  }
  const inbox = route.section === "messages" || route.dock ? { view: params.get("inbox") || "for_you", audience: ["me", "users"].includes(params.get("audience")!) ? params.get("audience") as "me" | "users" : "all" as const, query: params.get(route.section === "messages" ? "q" : "inbox-search") || "", status: params.get("status") || "all", sender: params.get("sender") || "" } : undefined;
  return { route, overview, inbox, viewRef, home: url.pathname === "/" && !url.search && !hash, legacy: legacy || !!hash || !!oldIdea };
}
