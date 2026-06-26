/**
 * AgentBar — a slim top line (no background) pinning the work in flight: the main
 * git worktree as a "you" pill, followed by each live agent and the idea it has
 * checked out, as clickable `idea/{id}` badges with hover cards.
 */
import { useEffect, useState } from "preact/hooks";
import { listAgents } from "../state/api";
import { navigateToIdea } from "../lib/navigate";
import { ideaTitle } from "../lib/format";
import { allIdeas, backlogData } from "../state/signals";
import { Tooltip } from "./ui";
import type { AgentEntry } from "../lib/types";

function branchIdeaId(branch: string): number | null {
  if (!branch.startsWith("idea/")) return null;
  const n = Number(branch.slice(5));
  return Number.isFinite(n) ? n : null;
}

export function AgentBar() {
  const [agents, setAgents] = useState<AgentEntry[]>([]);
  const ideas = allIdeas.value;
  const branch = backlogData.value?.current_branch ?? "";

  useEffect(() => {
    let dead = false;
    const load = () => listAgents().then((l) => { if (!dead) setAgents(l); }).catch(() => {});
    load();
    const t = window.setInterval(load, 5000);
    return () => { dead = true; window.clearInterval(t); };
  }, []);

  const live = agents.filter((a) => a.pid != null);
  if (live.length === 0 && !branch) return null;

  const bIdea = branch ? branchIdeaId(branch) : null;
  const bTitle = bIdea != null ? ideaTitle(ideas[bIdea]?.description ?? "") : "";

  return (
    <div class="agent-bar" aria-label="Active work">
      <div class="agent-bar-list">
        {branch && (
          <Tooltip
            placement="bottom"
            content={
              <>
                <span class="ui-tip-title">you · main worktree</span>
                <span class="ui-tip-row"><span>branch</span><b>{branch}</b></span>
                {bTitle && <span class="ui-tip-dim">{bTitle}</span>}
              </>
            }
          >
            <button
              type="button"
              class="agent-bar-badge agent-bar-badge--user"
              onClick={() => { if (bIdea != null) navigateToIdea(bIdea); }}
              disabled={bIdea == null}
            >
              <span class="agent-bar-dot agent-bar-dot--user" />
              <span class="agent-bar-role">you</span>
              <span class="agent-bar-idea">{branch}</span>
            </button>
          </Tooltip>
        )}

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
