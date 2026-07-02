/**
 * AgentTree — structure A. Each live agent is a colored bullet with its current
 * action + a few nested (⎿) trail lines beneath it. Reused in the Overview
 * sidebar (compact: 1–2 nested lines) and the Agents tab (full).
 *
 * Data comes from the live agent-activity store; no props required beyond the
 * display options.
 */
import { useState } from "preact/hooks";
import { agentStates, type AgentState, type Pulse } from "../../state/agent-activity";
import { agentColor } from "../../lib/colors";
import { navigateToIdea } from "../../lib/navigate";

// Minified real-time strip: one tick per recent api/msg/exp mark, oldest→newest
// (left→right), fading with age. Makes API traffic + inter-agent messages
// visible at a glance without text.
function MiniPulse({ pulses }: { pulses: Pulse[] }) {
  if (pulses.length === 0) return null;
  const now = Date.now();
  const marks = pulses.slice(0, 16).reverse();
  return (
    <div class="aa-pulsestrip" aria-hidden="true">
      {marks.map((p, i) => (
        <span
          key={`${p.ts}-${i}`}
          class={`aa-tick aa-tick-${p.kind}`}
          style={{ opacity: Math.max(0.25, 1 - (now - p.ts) / 120_000) }}
        />
      ))}
    </div>
  );
}

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

function Line({ glyph, text, tone, age }: { glyph: string; text: string; tone?: string; age?: string }) {
  return (
    <div class={`aa-sub aa-tone-${tone ?? "neutral"}`}>
      <span class="aa-branch">⎿</span>
      <span class="aa-glyph">{glyph}</span>
      <span class="aa-sub-text">{text}</span>
      {age && <span class="aa-sub-age">{age}</span>}
    </div>
  );
}

// Hysteresis for activeOnly listing: an agent ENTERS when active, but only
// LEAVES after this much quiet — so a row hovering at the active-window
// boundary dims instead of flapping in and out of the sidebar.
const LINGER_MS = 5 * 60_000;

export function AgentTree({ compact = false, activeOnly = false, maxNested = compact ? 3 : 5 }: {
  compact?: boolean;
  activeOnly?: boolean;
  maxNested?: number;
}) {
  // Per-row disclosure: expanded rows show the agent's recent history (last
  // labapi/MCP calls + experiment/message events, with ages).
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const toggle = (id: string) =>
    setExpanded((prev) => {
      const n = new Set(prev);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });

  let states = Object.values(agentStates.value);
  if (activeOnly) {
    const now = Date.now();
    states = states.filter(
      (s) => s.active || (s.lastActiveTs > 0 && now - s.lastActiveTs < LINGER_MS),
    );
  }
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
        // Head prefers the assigned/running experiment (persists past other
        // events); otherwise the current action.
        const showExp = active && st.currentExp;
        const glyph = showExp ? "▶" : h.glyph;
        const text = showExp ? `exp/${st.currentExp}` : h.text;
        const tone = showExp ? "accent" : (active ? (st.current?.tone ?? "accent") : "neutral");
        // Compact (sidebar): a minified real-time strip. Full: the last labapi/
        // MCP endpoints (falling back to the event trail before any are seen).
        const apis = st.apiCalls.slice(0, maxNested);
        const events = apis.length === 0 ? st.recent.slice(0, maxNested) : [];
        // Compact: one line of "what it last did" — the most recent labapi/MCP
        // call, falling back to the latest event when no call has been seen.
        const lastApi = st.apiCalls[0];
        const lastLine = lastApi
          ? { glyph: "⟳", tone: "neutral", text: `${lastApi.method} ${lastApi.path}` }
          : st.recent[0]
            ? { glyph: st.recent[0].glyph, tone: st.recent[0].tone, text: st.recent[0].text }
            : null;
        const open = expanded.has(st.agentId);
        return (
          <div class={`aa-agent${active ? "" : " aa-quiet"}`} key={st.agentId}>
            {/* Row click = expand/collapse history. Clicking the action text
                still navigates to the idea. */}
            <div class="aa-head" role="button" onClick={() => toggle(st.agentId)}>
              {/* Status-language state dot: yellow = active/working, faint =
                  quiet; filled = listening on the-lab messages, hollow = not.
                  (Identity color lives on the agent id only.) */}
              <span
                class={`aa-dot aa-dot--state${active ? " is-active" : ""}${st.listening ? " is-listening" : ""}${st.listening && active ? " aa-pulse" : ""}`}
                title={`${st.listening ? "listening" : "not listening"} · ${active ? "working" : "quiet"}`}
              />
              <span class="aa-id" style={{ color }}>{st.agentId}</span>
              <span class="aa-role">{st.role}</span>
              <span class={`aa-glyph aa-tone-${tone}`}>{glyph}</span>
              <span
                class={`aa-head-text${st.ideaId != null ? " is-link" : ""}`}
                onClick={st.ideaId != null
                  ? (e) => { e.stopPropagation(); navigateToIdea(st.ideaId!); }
                  : undefined}
              >
                {text}
              </span>
              {st.lastActiveTs > 0 && <span class="aa-age">{ago(st.lastActiveTs)}</span>}
              <span class="aa-chev" aria-hidden="true">{open ? "▾" : "▸"}</span>
            </div>
            {open ? (
              // Expanded: recent history — API/MCP calls, then events, with ages.
              <div class="aa-subs">
                {st.apiCalls.map((c, i) => (
                  <Line key={`api${i}`} glyph="⟳" tone="neutral"
                    text={`${c.method} ${c.path}`} age={ago(c.ts)} />
                ))}
                {st.recent.map((e) => (
                  <Line key={e.key} glyph={e.glyph} tone={e.tone} text={e.text} age={ago(e.ts)} />
                ))}
                {st.apiCalls.length === 0 && st.recent.length === 0 && (
                  <div class="aa-sub"><span class="aa-branch">⎿</span><span class="aa-sub-text">no history yet</span></div>
                )}
              </div>
            ) : compact ? (
              <>
                {lastLine && <Line glyph={lastLine.glyph} tone={lastLine.tone} text={lastLine.text} />}
                <MiniPulse pulses={st.pulses} />
              </>
            ) : (apis.length > 0 || events.length > 0) && (
              <div class="aa-subs">
                {apis.map((c, i) => (
                  <Line key={`api${i}`} glyph="⟳" tone="neutral"
                    text={`${c.method} ${c.path}`} />
                ))}
                {events.map((e) => <Line key={e.key} glyph={e.glyph} tone={e.tone} text={e.text} />)}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
