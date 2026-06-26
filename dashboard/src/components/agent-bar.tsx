/**
 * AgentBar — a slim top strip showing each live agent and the idea it currently
 * has checked out, as a clickable `idea/{id}` badge with a hover card. Lets you
 * see who is working on what at a glance from anywhere. Hidden when no agents.
 */
import { useEffect, useState } from "preact/hooks";
import { listAgents } from "../state/api";
import { navigateToIdea } from "../lib/navigate";
import { ideaTitle } from "../lib/format";
import { allIdeas } from "../state/signals";
import { Tooltip } from "./ui";
import type { AgentEntry } from "../lib/types";

export function AgentBar() {
  const [agents, setAgents] = useState<AgentEntry[]>([]);
  const ideas = allIdeas.value;

  useEffect(() => {
    let dead = false;
    const load = () => listAgents().then((l) => { if (!dead) setAgents(l); }).catch(() => {});
    load();
    const t = window.setInterval(load, 5000);
    return () => { dead = true; window.clearInterval(t); };
  }, []);

  const live = agents.filter((a) => a.pid != null);
  if (live.length === 0) return null;

  return (
    <div class="agent-bar" aria-label="Active agents">
      <span class="agent-bar-label">agents</span>
      <div class="agent-bar-list">
        {live.map((a) => {
          const idea = a.current_idea;
          const title = idea ? ideaTitle(idea.description ?? ideas[idea.id]?.description ?? "") : "";
          return (
            <Tooltip
              key={a.agent_id}
              placement="bottom"
              content={
                <>
                  <span class="ui-tip-title">{a.role || a.agent_id}</span>
                  <span class="ui-tip-row"><span>agent</span><b>{a.agent_id}</b></span>
                  {idea ? (
                    <>
                      <span class="ui-tip-row"><span>idea</span><b>#{idea.id} · {idea.status}</b></span>
                      {title && <span class="ui-tip-dim">{title}</span>}
                    </>
                  ) : (
                    <span class="ui-tip-dim">no idea checked out</span>
                  )}
                </>
              }
            >
              <button
                type="button"
                class="agent-bar-badge"
                onClick={() => { if (idea) navigateToIdea(idea.id); }}
                disabled={!idea}
              >
                <span class="agent-bar-dot" />
                <span class="agent-bar-role">{a.role || a.agent_id}</span>
                <span class="agent-bar-idea">{idea ? `idea/${idea.id}` : "idle"}</span>
              </button>
            </Tooltip>
          );
        })}
      </div>
    </div>
  );
}
