import { currentView } from "../state/settings";

const TABS = [
  { key: "dag", label: "Graph", path: "/graph" },
  { key: "timeline", label: "Timeline", path: "/timeline" },
  { key: "log", label: "Log", path: "/log" },
  { key: "api", label: "API", path: "/api" },
  { key: "stats", label: "Stats", path: "/stats" },
  { key: "sandbox", label: "Sandbox", path: "/sandbox" },
] as const;

export function ViewTabs() {
  const view = currentView.value;

  function switchTo(key: string) {
    currentView.value = key;
    // URL is auto-updated by the effect in app.tsx
  }

  return (
    <div id="view-tabs">
      {TABS.map((tab) => (
        <div
          key={tab.key}
          class={`view-tab${view === tab.key ? " active" : ""}`}
          onClick={() => switchTo(tab.key)}
        >
          {tab.label}
        </div>
      ))}
    </div>
  );
}
