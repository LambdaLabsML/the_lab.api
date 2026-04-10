import { useState } from "preact/hooks";
import { backlogData } from "../state/signals";
import { reverseTime, colorMode } from "../state/settings";

interface LayoutActions {
  onResetLayout?: () => void;
  onSaveLayout?: (name: string) => void;
  onLoadLayout?: (name: string) => void;
  onDeleteLayout?: (name: string) => void;
  getSavedLayouts?: () => string[];
  onAddPanel?: (id: string) => void;
  getClosedPanels?: () => string[];
}

export function Topbar(props: LayoutActions) {
  const data = backlogData.value;
  const reversed = reverseTime.value;
  const [showLayoutMenu, setShowLayoutMenu] = useState(false);
  const [saveName, setSaveName] = useState("");
  const [layoutVersion, setLayoutVersion] = useState(0);

  const savedLayouts = props.getSavedLayouts?.() || [];
  void layoutVersion; // trigger re-render when layouts change

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
        {reversed ? "\u2190 newest" : "oldest \u2192"}
      </button>

      {/* Add closed panels back — always render so signal subscription stays active */}
      {(() => {
        const closed = props.getClosedPanels?.() || [];
        if (closed.length === 0) return null;
        return (
          <div class="layout-menu-container">
            <button
              class="time-direction-btn"
              onClick={(e) => {
                const btn = e.currentTarget as HTMLElement;
                const menu = btn.nextElementSibling as HTMLElement;
                if (menu) menu.style.display = menu.style.display === "block" ? "none" : "block";
              }}
              title={closed.length > 0 ? "Re-open closed panels or float over maximized" : "Float panels over maximized view"}
            >
              + Panel
            </button>
            <div class="layout-menu" style={{ display: "none" }}>
              {closed.map((id) => (
                <button
                  key={id}
                  class="layout-menu-btn"
                  onClick={(e) => {
                    props.onAddPanel?.(id);
                    const menu = (e.currentTarget as HTMLElement).parentElement!;
                    menu.style.display = "none";
                  }}
                >
                  {id.charAt(0).toUpperCase() + id.slice(1)}
                </button>
              ))}
            </div>
          </div>
        );
      })()}

      {/* Layout management */}
      <div class="layout-menu-container">
        <button
          class="time-direction-btn"
          onClick={() => setShowLayoutMenu(!showLayoutMenu)}
          title="Manage layouts"
        >
          Layouts
        </button>
        {showLayoutMenu && (
          <div class="layout-menu">
            <div class="layout-menu-section">
              <button class="layout-menu-btn" onClick={() => { props.onResetLayout?.(); setShowLayoutMenu(false); }}>
                Reset to Default
              </button>
            </div>
            <div class="layout-menu-section">
              <div class="layout-menu-label">Save current:</div>
              <div class="layout-save-row">
                <input
                  class="layout-save-input"
                  placeholder="layout name..."
                  value={saveName}
                  onInput={(e) => setSaveName((e.target as HTMLInputElement).value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && saveName.trim()) {
                      props.onSaveLayout?.(saveName.trim());
                      setSaveName("");
                    }
                  }}
                />
                <button
                  class="layout-menu-btn"
                  onClick={() => { if (saveName.trim()) { props.onSaveLayout?.(saveName.trim()); setSaveName(""); setLayoutVersion((v) => v + 1); } }}
                >
                  Save
                </button>
              </div>
            </div>
            {savedLayouts.length > 0 && (
              <div class="layout-menu-section">
                <div class="layout-menu-label">Saved layouts:</div>
                {savedLayouts.map((name) => (
                  <div key={name} class="layout-saved-row">
                    <span
                      class="layout-saved-name"
                      onClick={() => { props.onLoadLayout?.(name); setShowLayoutMenu(false); }}
                      title={`Load "${name}"`}
                    >
                      {name}
                    </span>
                    <span
                      class="layout-saved-delete"
                      onClick={() => { props.onDeleteLayout?.(name); setLayoutVersion((v) => v + 1); }}
                      title="Delete"
                    >
                      x
                    </span>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
