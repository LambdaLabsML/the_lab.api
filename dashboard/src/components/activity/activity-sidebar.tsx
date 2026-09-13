/**
 * ActivitySidebar — THE shared secondary panel for Overview / Activity / Queue.
 * Top: running + recent experiments (shortlog). Middle: what the agents are
 * doing (fills all remaining space, scrolls). Bottom: the message composer,
 * pinned above the scroll area.
 */
import { Eyebrow, Stepper } from "../ui";
import { ActivityShortlog } from "./activity-shortlog";
import { AgentTree } from "../agent-activity/agent-tree";
import { AgentQuickChat } from "../agent-activity/agent-chat";
import { agentHistoryLimit } from "../../state/settings";

export function ActivitySidebar() {
  return (
    <div class="activity-sidebar">
      <ActivityShortlog />
      <div class="activity-sidebar-agents">
        <div class="nav-secondary-head nav-secondary-head--row">
          <Eyebrow>Active agents</Eyebrow>
          <Stepper size="s" value={agentHistoryLimit.value} min={1} max={8} step={1}
            what="detail rows" onChange={(v) => { agentHistoryLimit.value = v; }} />
        </div>
        <div class="activity-sidebar-scroll">
          <AgentTree compact activeOnly historyLimit={agentHistoryLimit.value} />
        </div>
      </div>
      <AgentQuickChat />
    </div>
  );
}
