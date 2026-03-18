import { currentView } from "../state/settings";

const TABS = [
  { key: "dag", label: "DAG", path: "/dag" },
  { key: "timeline", label: "Timeline", path: "/timeline" },
  { key: "log", label: "Log", path: "/log" },
  { key: "api", label: "API", path: "/api" },
] as const;

export function ViewTabs() {
  const view = currentView.value;

  function switchTo(key: string, path: string) {
    currentView.value = key;
    history.pushState(null, "", path);
  }

  return (
    <div id="view-tabs">
      {TABS.map((tab) => (
        <div
          key={tab.key}
          class={`view-tab${view === tab.key ? " active" : ""}`}
          onClick={() => switchTo(tab.key, tab.path)}
        >
          {tab.label}
        </div>
      ))}
    </div>
  );
}
