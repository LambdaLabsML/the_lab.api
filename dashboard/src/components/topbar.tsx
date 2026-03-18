import { backlogData } from "../state/signals";
import { reverseTime } from "../state/settings";

export function Topbar() {
  const data = backlogData.value;
  const reversed = reverseTime.value;

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
      <button
        class="time-direction-btn"
        onClick={() => { reverseTime.value = !reversed; }}
        title={reversed ? "Newest left/top (click to reverse)" : "Oldest left/top (click to reverse)"}
      >
        {reversed ? "← newest" : "oldest →"}
      </button>
    </div>
  );
}
