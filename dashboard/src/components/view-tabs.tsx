import { currentView } from "../state/settings";
import { IconButton } from "./ui";

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
      {TABS.map((tab) => {
        const active = view === tab.key;
        return (
          <IconButton
            key={tab.key}
            active={active}
            class="view-tab-btn"
            onClick={() => switchTo(tab.key)}
            // active tab = subtle accent underline, not a heavy box
            style={{
              borderRadius: 0,
              padding: "7px 14px 6px",
              fontSize: "var(--text-base)",
              boxShadow: active ? "inset 0 -2px var(--accent)" : "inset 0 -2px transparent",
            }}
          >
            {tab.label}
          </IconButton>
        );
      })}
    </div>
  );
}
