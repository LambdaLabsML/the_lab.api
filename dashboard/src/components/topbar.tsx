import { backlogData } from "../state/signals";

export function Topbar() {
  const data = backlogData.value;
  return (
    <div id="topbar">
      <span class="title">The Lab</span>
      <span class="stat">
        Active ideas: <b>{data ? data.active_ideas.length : "--"}</b>
      </span>
      <span class="stat">
        Running: <b>{data ? data.total_running : "--"}</b>
      </span>
      <span class="stat">
        Pending: <b>{data ? data.total_pending : "--"}</b>
      </span>
      <span class="stat">
        Branch: <b>{data ? data.current_branch : "--"}</b>
      </span>
    </div>
  );
}
