/**
 * Lab-call display helpers — shared by the activity tree and the hover card.
 */
import type { ApiCall } from "../../state/agent-activity";

// ── lab calls as compact function calls ──────────────────────────────────────
// "POST /experiments/130.2/cancel" reads as cancel_experiment(130.2) — mirrors
// the MCP bridge's tool naming so the UI and the agent's tools speak the same
// vocabulary. Falls back to verb_segment(args) for unmapped routes.
const FN_ROUTES: Array<[RegExp, string, (m: RegExpMatchArray, get: boolean) => string]> = [
  [/^\/experiments\/log$/, "", () => "get_failed_logs"],
  [/^\/experiments\/([^/]+)\/(progress|log|output|script|timeseries|cancel|rerun|start|tags)$/, "$1", (m) => ({
    progress: "get_progress", log: "get_log", output: "get_output", script: "get_script",
    timeseries: "get_timeseries", cancel: "cancel_experiment", rerun: "rerun_experiment",
    start: "start_experiment", tags: "update_tags",
  }[m[2]] as string)],
  [/^\/experiments\/([^/]+)$/, "$1", () => "get_experiment"],
  [/^\/experiments$/, "", () => "list_experiments"],
  [/^\/ideas\/new$/, "", () => "create_idea"],
  [/^\/ideas\/search$/, "", () => "search_ideas"],
  [/^\/ideas\/(\d+)\/experiments\/batch$/, "$1", () => "create_experiments"],
  [/^\/ideas\/(\d+)\/experiments$/, "$1", (_m, get) => (get ? "list_experiments" : "create_experiment")],
  [/^\/ideas\/(\d+)\/(checkout|conclude|abandon|adopt|reopen|note|notes|diff|tree|parent)$/, "$1", (m) => ({
    checkout: "checkout_idea", conclude: "conclude_idea", abandon: "abandon_idea",
    adopt: "adopt_idea", reopen: "reopen_idea", note: "add_note", notes: "list_notes",
    diff: "get_diff", tree: "get_tree", parent: "get_parent",
  }[m[2]] as string)],
  [/^\/ideas\/(\d+)$/, "$1", () => "get_idea"],
  [/^\/ideas$/, "", () => "list_ideas"],
  [/^\/leaderboard\/search$/, "", () => "leaderboard_search"],
  [/^\/leaderboard$/, "", () => "leaderboard"],
  [/^\/wait$/, "", () => "wait_for_experiment"],
  [/^\/orient$/, "", () => "orient"],
  [/^\/digest$/, "", () => "digest"],
  [/^\/instructions$/, "", () => "get_instructions"],
];

export function fnCall(c: ApiCall): string {
  const path = c.path.split("?")[0].replace(/\/+$/, "") || "/";
  const get = c.method.toUpperCase() === "GET";
  for (const [re, argTpl, name] of FN_ROUTES) {
    const m = path.match(re);
    if (m) {
      const arg = argTpl ? argTpl.replace(/\$(\d)/g, (_, i) => m[Number(i)] ?? "") : "";
      return `${name(m, get)}(${arg})`;
    }
  }
  // Fallback: verb from the last static segment, args from the dynamic ones.
  const segs = path.split("/").filter(Boolean);
  const args = segs.filter((s) => /\d/.test(s));
  const last = [...segs].reverse().find((s) => !/\d/.test(s)) ?? segs[segs.length - 1] ?? "call";
  return `${get ? "get" : "do"}_${last.replace(/-/g, "_")}(${args.join(", ")})`;
}

/** Best-effort focus target for a lab call: idea id from /ideas/N, or an
 *  "idea.seq" experiment ref whose prefix is the idea id. */
export function apiFocus(c: ApiCall): { ideaId: number; expLabel?: string } | null {
  const path = c.path.split("?")[0];
  const idea = path.match(/^\/ideas\/(\d+)/);
  if (idea) return { ideaId: Number(idea[1]) };
  const exp = path.match(/^\/experiments\/((\d+)\.[\w.-]+)/);
  if (exp) return { ideaId: Number(exp[2]), expLabel: exp[1] };
  return null;
}
