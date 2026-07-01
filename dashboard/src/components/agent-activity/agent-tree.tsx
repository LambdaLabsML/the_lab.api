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

// Idle if no activity for a while (agent registered but quiet).
const IDLE_MS = 45_000;

function headGlyph(st: AgentState): { glyph: string; kind: string } {
  const fresh = st.lastActiveTs && Date.now() - st.lastActiveTs < IDLE_MS;
  if (fresh && st.current) return { glyph: st.current.glyph, kind: st.current.kind };
  return { glyph: "○", kind: "idle" };
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

export function AgentTree({ compact = false, maxNested = compact ? 2 : 5 }: {
  compact?: boolean;
  maxNested?: number;
}) {
  const states = Object.values(agentStates.value);
  if (states.length === 0) {
    return <div class="aa-empty">no agents registered</div>;
  }
  // Live + most-recently-active first.
  states.sort((a, b) =>
    (Number(b.live) - Number(a.live)) || (b.lastActiveTs - a.lastActiveTs));

  return (
    <div class={`aa-tree${compact ? " aa-compact" : ""}`}>
      {states.map((st) => {
        const color = agentColor(st.agentId);
        const { glyph, kind } = headGlyph(st);
        const active = kind !== "idle";
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
              <span class={`aa-glyph aa-head-glyph aa-tone-${st.current?.tone ?? "neutral"}`}>{glyph}</span>
              <span class="aa-head-text">
                {active && st.current ? st.current.text : "idle"}
              </span>
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
