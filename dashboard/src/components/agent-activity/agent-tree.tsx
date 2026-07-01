/**
 * AgentTree — structure A. Each live agent is a colored bullet with its current
 * action + a few nested (⎿) trail lines beneath it. Reused in the Overview
 * sidebar (compact: 1–2 nested lines) and the Agents tab (full).
 *
 * Data comes from the live agent-activity store; no props required beyond the
 * display options.
 */
import { agentStates, type AgentState, type ActivityEvent } from "../../state/agent-activity";
import { agentColor } from "../../lib/colors";
import { navigateToIdea } from "../../lib/navigate";

function ago(ts: number): string {
  if (!ts) return "";
  const s = Math.max(0, Math.floor((Date.now() - ts) / 1000));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  return `${Math.floor(s / 3600)}h`;
}

// `st.active` (recency-based) comes from the store. An active agent keeps
// showing its current/last action (or its idea) rather than flipping to idle.
function head(st: AgentState): { glyph: string; kind: string; text: string } {
  if (st.current && st.active) {
    return { glyph: st.current.glyph, kind: st.current.kind, text: st.current.text };
  }
  if (st.active) {
    return { glyph: "●", kind: "active", text: st.ideaId != null ? `on idea/${st.ideaId}` : "working" };
  }
  return { glyph: "○", kind: "idle", text: "idle" };
}

function Line({ e }: { e: ActivityEvent }) {
  return (
    <div class={`aa-sub aa-tone-${e.tone ?? "neutral"}`}>
      <span class="aa-branch">⎿</span>
      <span class="aa-glyph">{e.glyph}</span>
      <span class="aa-sub-text">{e.text}</span>
    </div>
  );
}

export function AgentTree({ compact = false, activeOnly = false, maxNested = compact ? 2 : 5 }: {
  compact?: boolean;
  activeOnly?: boolean;
  maxNested?: number;
}) {
  let states = Object.values(agentStates.value);
  if (activeOnly) states = states.filter((s) => s.active);
  if (states.length === 0) {
    return <div class="aa-empty">{activeOnly ? "no active agents" : "no agents registered"}</div>;
  }
  // Active + most-recently-active first.
  states.sort((a, b) =>
    (Number(b.active) - Number(a.active)) || (b.lastActiveTs - a.lastActiveTs));

  return (
    <div class={`aa-tree${compact ? " aa-compact" : ""}`}>
      {states.map((st) => {
        const color = agentColor(st.agentId);
        const h = head(st);
        const active = h.kind !== "idle";
        const nested = st.recent.slice(0, maxNested);
        return (
          <div class="aa-agent" key={st.agentId}>
            <div
              class="aa-head"
              role={st.ideaId != null ? "button" : undefined}
              onClick={st.ideaId != null ? () => navigateToIdea(st.ideaId!) : undefined}
            >
              <span
                class={`aa-dot${active ? " aa-pulse" : ""}`}
                style={{ background: color }}
                title={st.live ? "live" : "registered"}
              />
              <span class="aa-id" style={{ color }}>{st.agentId}</span>
              <span class="aa-role">{st.role}</span>
              {st.listening && <span class="aa-listen" title="listening (the-lab messages)" />}
              <span class={`aa-glyph aa-tone-${active ? (st.current?.tone ?? "accent") : "neutral"}`}>{h.glyph}</span>
              <span class="aa-head-text">{h.text}</span>
              {st.lastActiveTs > 0 && <span class="aa-age">{ago(st.lastActiveTs)}</span>}
            </div>
            {nested.length > 0 && (
              <div class="aa-subs">
                {nested.map((e) => <Line key={e.key} e={e} />)}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
