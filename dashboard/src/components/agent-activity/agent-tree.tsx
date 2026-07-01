/**
 * AgentTree — structure A. Each live agent is a colored bullet with its current
 * action + a few nested (⎿) trail lines beneath it. Reused in the Overview
 * sidebar (compact: 1–2 nested lines) and the Agents tab (full).
 *
 * Data comes from the live agent-activity store; no props required beyond the
 * display options.
 */
import { agentStates, type AgentState, type ActivityEvent } from "../../state/agent-activity";
import { agentColor, agentInitials } from "../../lib/colors";
import { navigateToIdea } from "../../lib/navigate";

function ago(ts: number): string {
  if (!ts) return "";
  const s = Math.max(0, Math.floor((Date.now() - ts) / 1000));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  return `${Math.floor(s / 3600)}h`;
}

// "Active" = the agent's process is live (pid running). A live agent is working
// even between events, so it must NOT flip to "idle" just because no event
// arrived recently — we keep showing its current/last action. Only a
// registered-but-not-live agent (process gone) reads as idle.
function isActive(st: AgentState): boolean {
  return st.live || (!!st.lastActiveTs && Date.now() - st.lastActiveTs < 45_000);
}

function head(st: AgentState): { glyph: string; kind: string; text: string } {
  const active = isActive(st);
  if (st.current && active) {
    return { glyph: st.current.glyph, kind: st.current.kind, text: st.current.text };
  }
  if (active) {
    // Live but no recent event — still working; show its idea, not "idle".
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
  if (activeOnly) states = states.filter(isActive);
  if (states.length === 0) {
    return <div class="aa-empty">{activeOnly ? "no active agents" : "no agents registered"}</div>;
  }
  // Live + most-recently-active first.
  states.sort((a, b) =>
    (Number(b.live) - Number(a.live)) || (b.lastActiveTs - a.lastActiveTs));

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
              >
                <span class="aa-initials">{agentInitials(st.agentId)}</span>
              </span>
              <span class="aa-id" style={{ color }}>{st.agentId}</span>
              {!compact && <span class="aa-role">{st.role}</span>}
              {st.listening && <span class="aa-listen" title="listening on the-lab messages">🎧</span>}
              <span class={`aa-glyph aa-head-glyph aa-tone-${active ? (st.current?.tone ?? "accent") : "neutral"}`}>{h.glyph}</span>
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
