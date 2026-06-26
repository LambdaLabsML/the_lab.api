/**
 * Topbar — slim, unified identity/status strip. The mode switching and the
 * settings menu moved out: primary navigation is the left NavRail, and settings
 * is its own panel (components/settings-panel.tsx). This bar intentionally does
 * NOT repeat the campaign stats shown in the Activity / Review dashboards — it
 * carries only identity, websocket status, and the current branch/idea context.
 */
import { backlogData, allIdeas } from "../state/signals";
import { wsConnected, wsAuthFailed } from "../state/ws";

export function Topbar() {
  const data = backlogData.value;
  const ideas = allIdeas.value;

  const currentBranch = data?.current_branch ?? "";
  const branchIdeaId = currentBranch.startsWith("idea/") ? Number(currentBranch.slice(5)) : null;
  const branchIdea = branchIdeaId ? ideas[branchIdeaId] : null;
  const branchTitle = branchIdea?.description?.split("\n")[0].slice(0, 70) ?? null;

  const isWsConnected = wsConnected.value;
  const isWsAuthFailed = wsAuthFailed.value;

  return (
    <header class="slim-topbar">
      <span class="slim-topbar-mark">the_lab</span>
      <span
        class={`ws-dot ${isWsAuthFailed ? "ws-dot--auth" : isWsConnected ? "ws-dot--on" : "ws-dot--off"}`}
        title={isWsAuthFailed ? "WebSocket: auth failed" : isWsConnected ? "WebSocket: connected" : "WebSocket: reconnecting…"}
      />
      {currentBranch && (
        <span class="slim-topbar-branch" title={branchTitle ?? currentBranch}>
          <span class="slim-topbar-branch-name">{currentBranch}</span>
          {branchTitle && <span class="slim-topbar-branch-title">· {branchTitle}</span>}
        </span>
      )}
      <span class="slim-topbar-spacer" />
    </header>
  );
}
