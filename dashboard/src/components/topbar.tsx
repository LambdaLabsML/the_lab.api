/**
 * Topbar — slim, unified identity/status strip. The mode switching and the
 * settings menu moved out: primary navigation is the left NavRail, and settings
 * is its own panel (components/settings-panel.tsx). This bar intentionally does
 * NOT repeat the campaign stats shown in the Activity / Review dashboards — it
 * carries only identity, websocket status, and the current branch/idea context.
 */
import { backlogData, allIdeas } from "../state/signals";
import { wsCatchingUp, wsConnected, wsAuthFailed } from "../state/ws";
import { chartHistory, chartHistoryLimit } from "../state/chart-query";
import { apiFailures, lastApiSuccess } from "../state/request-health";
import { retryDashboard } from "../state/polling";
import { useEffect, useState } from "preact/hooks";

export function Topbar() {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 5000);
    return () => clearInterval(timer);
  }, []);
  const failures = Object.entries(apiFailures.value);
  const updated = lastApiSuccess.value;
  const data = backlogData.value;
  const ideas = allIdeas.value;

  const currentBranch = data?.current_branch ?? "";
  const branchIdeaId = currentBranch.startsWith("idea/") ? Number(currentBranch.slice(5)) : null;
  const branchIdea = branchIdeaId ? ideas[branchIdeaId] : null;
  const branchTitle = branchIdea?.description?.split("\n")[0].slice(0, 70) ?? null;

  const isWsConnected = wsConnected.value;
  const isWsAuthFailed = wsAuthFailed.value;
  const isCatchingUp = wsCatchingUp.value;

  return (
    <header class="slim-topbar">
      <span class="slim-topbar-mark">the_lab</span>
      <span
        class={`ws-dot ${isWsAuthFailed ? "ws-dot--auth" : isCatchingUp ? "ws-dot--busy" : isWsConnected ? "ws-dot--on" : "ws-dot--off"}`}
        title={isWsAuthFailed ? "WebSocket: auth failed" : isCatchingUp ? "Catching up on missed events…" : isWsConnected ? "WebSocket: connected" : "WebSocket: reconnecting…"}
      />
      {isCatchingUp && <span class="ws-catchup">catching up…</span>}
      {currentBranch && (
        <span class="slim-topbar-branch" title={branchTitle ?? currentBranch}>
          <span class="slim-topbar-branch-name">{currentBranch}</span>
          {branchTitle && <span class="slim-topbar-branch-title">· {branchTitle}</span>}
        </span>
      )}
      <span class="slim-topbar-spacer" />
      <span role="status" title={failures.map(([path, error]) => `${path}: ${error}`).join("\n")}>
        {failures.length ? "Some data is stale · " : ""}
        {updated ? `Last API update ${Math.max(0, Math.floor((now - updated) / 1000))}s ago` : "Loading data…"}
        {failures.length > 0 && <button onClick={() => { void retryDashboard(); }}>Retry refresh</button>}
      </span>
      {chartHistory.value.has_more && <span>
        Charts/table: newest {chartHistory.value.loaded} of {chartHistory.value.total} completed runs ·
        <button onClick={() => { chartHistoryLimit.value += 250; }}>Load 250 older</button>
        <button onClick={() => { chartHistoryLimit.value = 0; }}>Load all</button>
      </span>}
    </header>
  );
}
