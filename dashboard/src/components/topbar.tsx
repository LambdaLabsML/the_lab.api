import { useState } from "preact/hooks";
import { backlogData } from "../state/signals";
import { reverseTime, colorMode, colorTheme, fontFamily, fontSize } from "../state/settings";
import { wsConnected, wsAuthFailed } from "../state/ws";

interface LayoutActions {
  onResetLayout?: () => void;
  onSaveLayout?: (name: string) => void;
  onLoadLayout?: (name: string) => void;
  onDeleteLayout?: (name: string) => void;
  getSavedLayouts?: () => string[];
}

interface ThemeDef {
  id: string;
  name: string;
  swatches: string[]; // 3 colours: bg, accent, green
}

interface FontDef { id: string; name: string; stack: string; }

const FONTS: FontDef[] = [
  { id: "mono",   name: "JetBrains", stack: "'JetBrains Mono', monospace" },
  { id: "fira",   name: "Fira Code", stack: "'Fira Code', monospace" },
  { id: "space",  name: "Grotesk",   stack: "'Space Grotesk', sans-serif" },
  { id: "outfit", name: "Outfit",    stack: "'Outfit', sans-serif" },
];

const FONT_SIZES = ["xs", "s", "m", "l", "xl", "xxl"] as const;

const THEMES: ThemeDef[] = [
  { id: "default", name: "Default",  swatches: ["#0d1117", "#58a6ff", "#3fb950"] },
  { id: "lambda",  name: "Lambda",   swatches: ["#0b0b0b", "#6236f4", "#00e600"] },
  { id: "light",   name: "Light",    swatches: ["#ffffff", "#0969da", "#1a7f37"] },
  { id: "dracula", name: "Dracula",  swatches: ["#282a36", "#bd93f9", "#50fa7b"] },
  { id: "ocean",   name: "Ocean",    swatches: ["#0d1b2a", "#2ea8ff", "#3fb950"] },
  { id: "nord",    name: "Nord",     swatches: ["#2e3440", "#88c0d0", "#a3be8c"] },
];

export function Topbar(props: LayoutActions) {
  const data = backlogData.value;
  const reversed = reverseTime.value;
  const isWsConnected = wsConnected.value;
  const isWsAuthFailed = wsAuthFailed.value;
  const [showLayoutMenu, setShowLayoutMenu] = useState(false);
  const [saveName, setSaveName] = useState("");
  const [layoutVersion, setLayoutVersion] = useState(0);

  const savedLayouts = props.getSavedLayouts?.() || [];
  void layoutVersion; // trigger re-render when layouts change
  void colorMode; // used elsewhere

  const activeTheme  = colorTheme.value;
  const activeFont   = fontFamily.value;
  const activeFontSz = fontSize.value;

  return (
    <div id="topbar">
      <span class="title">The Lab</span>
      <span
        class={`ws-dot ${isWsAuthFailed ? "ws-dot--auth" : isWsConnected ? "ws-dot--on" : "ws-dot--off"}`}
        title={isWsAuthFailed ? "WebSocket: auth failed" : isWsConnected ? "WebSocket: connected" : "WebSocket: reconnecting..."}
      />
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
              <div class="layout-menu-label">Color theme:</div>
              <div class="theme-picker-grid">
                {THEMES.map((t) => (
                  <button
                    key={t.id}
                    class={`theme-swatch${activeTheme === t.id ? " theme-swatch--active" : ""}`}
                    onClick={() => { colorTheme.value = t.id; }}
                    title={t.name}
                  >
                    <span class="theme-swatch-colors">
                      {t.swatches.map((c, i) => (
                        <span key={i} class="theme-swatch-dot" style={`background:${c}`} />
                      ))}
                    </span>
                    <span class="theme-swatch-name">{t.name}</span>
                  </button>
                ))}
              </div>
            </div>
            {/* Font family */}
            <div class="layout-menu-section">
              <div class="layout-menu-label">Font:</div>
              <div class="font-picker-grid">
                {FONTS.map((f) => (
                  <button
                    key={f.id}
                    class={`font-swatch${activeFont === f.id ? " font-swatch--active" : ""}`}
                    style={`font-family:${f.stack}`}
                    onClick={() => { fontFamily.value = f.id; }}
                    title={f.name}
                  >
                    {f.name}
                  </button>
                ))}
              </div>
            </div>

            {/* Font size */}
            <div class="layout-menu-section">
              <div class="layout-menu-label">Size:</div>
              <div class="font-size-row">
                {FONT_SIZES.map((sz) => (
                  <button
                    key={sz}
                    class={`font-size-btn${activeFontSz === sz ? " font-size-btn--active" : ""}`}
                    onClick={() => { fontSize.value = sz; }}
                  >
                    {sz}
                  </button>
                ))}
              </div>
            </div>

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
