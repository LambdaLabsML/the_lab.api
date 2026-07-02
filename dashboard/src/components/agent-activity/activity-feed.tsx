/**
 * ActivityFeed — structure B. The global newest-first event stream, with
 * consecutive events from the same agent collapsed under one colored header.
 * New lines fade in. Used in the Agents tab.
 */
import { activityFeed, agentStates, type ActivityEvent } from "../../state/agent-activity";
import { agentColor } from "../../lib/colors";
import { navigateToIdea } from "../../lib/navigate";

function clock(ts: number): string {
  const d = new Date(ts);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

interface Group { agentId: string | null; ts: number; events: ActivityEvent[]; }

function group(events: ActivityEvent[]): Group[] {
  const out: Group[] = [];
  for (const e of events) {
    const last = out[out.length - 1];
    if (last && last.agentId === e.agentId) last.events.push(e);
    else out.push({ agentId: e.agentId, ts: e.ts, events: [e] });
  }
  return out;
}

export function ActivityFeed({ limit = 30 }: { limit?: number }) {
  // Only show events from currently-active agents — a "live" feed shouldn't be
  // padded with lines from agents that have long since gone quiet.
  const states = agentStates.value;
  const feed = activityFeed.value
    .filter((e) => e.agentId != null && states[e.agentId]?.active)
    .slice(0, limit);
  if (feed.length === 0) {
    return <div class="aa-empty">no active agents</div>;
  }
  const groups = group(feed);
  return (
    <div class="aa-feed">
      {groups.map((g) => {
        const color = agentColor(g.agentId);
        const label = g.agentId ?? "system";
        const ideaId = g.events.find((e) => e.ideaId != null)?.ideaId;
        return (
          <div class="aa-feed-group" key={g.events[0].key}>
            <div
              class="aa-feed-head"
              role={ideaId != null ? "button" : undefined}
              onClick={ideaId != null ? () => navigateToIdea(ideaId) : undefined}
            >
              <span class="aa-dot" style={{ background: color }} />
              <span class="aa-id" style={{ color }}>{label}</span>
              <span class="aa-age">{clock(g.ts)}</span>
            </div>
            <div class="aa-subs">
              {g.events.map((e) => (
                <div class={`aa-sub aa-fade aa-tone-${e.tone ?? "neutral"}`} key={e.key}>
                  <span class="aa-branch">└</span>
                  <span class="aa-glyph">{e.glyph}</span>
                  <span class="aa-sub-text">{e.text}</span>
                </div>
              ))}
            </div>
          </div>
        );
      })}
    </div>
  );
}
