/** Shared monitor: running experiments, collapsible queue, and compact agents. */
import { Eyebrow } from "../ui";
import { ActivityShortlog } from "./activity-shortlog";
import { PersonalMessages } from "./personal-messages";
import { AgentTree } from "../agent-activity/agent-tree";

export function ActivitySidebar() {
  return (
    <div class="activity-sidebar">
      <ActivityShortlog />
      <PersonalMessages />
      <div class="activity-sidebar-agents">
        <div class="nav-secondary-head nav-secondary-head--row">
          <Eyebrow>Active agents</Eyebrow>
        </div>
        <div class="activity-sidebar-scroll">
          <AgentTree compact activeOnly historyLimit={0} />
        </div>
      </div>
    </div>
  );
}
