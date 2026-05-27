import { useState, useEffect, useRef } from "preact/hooks";
import { backlogData, totalAgentCost, totalAgentInputTokens, totalAgentOutputTokens } from "../state/signals";
import { reverseTime, colorMode, colorTheme, fontFamily, fontSize } from "../state/settings";
import { wsConnected, wsAuthFailed } from "../state/ws";
// Font display data lives here — NOT imported from fonts.ts.
// Importing fonts.ts created a topbar → fonts → lazy-chunk TDZ in Vite's
// bundle that crashed dockview's fromJSON and killed the WebSocket setup.
// fonts.ts is still used by main.tsx for the actual lazy CSS loading.
interface FontPickerDef { id: string; label: string; uiFont: string; }
const FONT_PICKER: FontPickerDef[] = [
  { id: "mono",   label: "JetBrains Mono",  uiFont: "'JetBrains Mono','SF Mono',monospace" },
  { id: "geist",  label: "Geist",           uiFont: "'Geist',system-ui,sans-serif" },
  { id: "ibm",    label: "IBM Plex",        uiFont: "'IBM Plex Sans',Helvetica,sans-serif" },
  { id: "mona",   label: "Mona Sans",       uiFont: "'Mona Sans',system-ui,sans-serif" },
  { id: "sora",   label: "Sora",            uiFont: "'Sora',system-ui,sans-serif" },
  { id: "space",  label: "Space Grotesk",   uiFont: "'Space Grotesk',system-ui,sans-serif" },
];

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


const FONT_SIZES = ["xs", "s", "m", "l", "xl", "xxl"] as const;

const THEMES: ThemeDef[] = [
  // Sorted brightest → darkest by perceived luminance of the background swatch
  { id: "light",          name: "Light",      swatches: ["#ffffff", "#0969da", "#1a7f37"] },   // 1.000
  { id: "linen",          name: "Linen",      swatches: ["#FFFFF8", "#5B8DB8", "#5A8A5A"] },   // 0.997
  { id: "solarized-light",name: "Solarized",  swatches: ["#FDF6E3", "#268BD2", "#859900"] },   // 0.964
  { id: "gruvbox-light",  name: "Gruvbox",    swatches: ["#FBF1C7", "#458588", "#98971A"] },   // 0.938
  { id: "nord",      name: "Nord",      swatches: ["#2e3440", "#88c0d0", "#a3be8c"] },         // 0.202
  { id: "dracula",   name: "Dracula",   swatches: ["#282a36", "#bd93f9", "#50fa7b"] },         // 0.168
  { id: "ocean",     name: "Ocean",     swatches: ["#0d1b2a", "#2ea8ff", "#3fb950"] },         // 0.096
  { id: "charcoal",  name: "Charcoal",  swatches: ["#12110F", "#A3B18A", "#D4A373"] },         // 0.067
  { id: "graphite",  name: "Graphite",  swatches: ["#111015", "#8B8CF6", "#D8A03D"] },         // 0.066
  { id: "default",   name: "Default",   swatches: ["#0d1117", "#58a6ff", "#3fb950"] },         // 0.065
  { id: "petroleum", name: "Petroleum", swatches: ["#071013", "#2DD4BF", "#F59E0B"] },         // 0.054
  { id: "orchid",    name: "Orchid",    swatches: ["#070B18", "#6EA8FE", "#C084FC"] },         // 0.044
  { id: "ink",       name: "Ink + Ice", swatches: ["#080B0F", "#7DD3FC", "#B7E35F"] },         // 0.041
  { id: "crimson",   name: "Crimson",   swatches: ["#120608", "#FF6B81", "#7EC8A4"] },         // 0.038
  { id: "oled",      name: "OLED",      swatches: ["#000000", "#58a6ff", "#3fb950"] },         // 0.000
];

export function Topbar(props: LayoutActions) {
  const data = backlogData.value;
  const reversed = reverseTime.value;
  const isWsConnected = wsConnected.value;
  const isWsAuthFailed = wsAuthFailed.value;
  const [showLayoutMenu, setShowLayoutMenu] = useState(false);
  const [saveName, setSaveName] = useState("");
  const [layoutVersion, setLayoutVersion] = useState(0);
  const [compact, setCompact] = useState(false);
  const [statIdx, setStatIdx] = useState(0);
  const barRef = useRef<HTMLDivElement>(null);

  const savedLayouts = props.getSavedLayouts?.() || [];
  void layoutVersion; // trigger re-render when layouts change
  void colorMode; // used elsewhere

  const activeTheme  = colorTheme.value;
  const activeFont   = fontFamily.value;
  const activeFontSz = fontSize.value;

  const cost = totalAgentCost.value;
  const inTok = totalAgentInputTokens.value;
  const outTok = totalAgentOutputTokens.value;

  function fmtTok(n: number): string {
    if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
    if (n >= 1_000) return `${Math.round(n / 1_000)}K`;
    return String(n);
  }

  const tokLabel = (inTok != null && outTok != null)
    ? `↓${fmtTok(inTok)} ↑${fmtTok(outTok)}`
    : "--";
  const tokTitle = (inTok != null && outTok != null)
    ? `Input (consumed): ${inTok.toLocaleString()} · Output (generated): ${outTok.toLocaleString()}`
    : undefined;

  // Build the stats array once so it's shared between compact and full renders
  const stats = [
    { key: "ideas",   label: "Ideas",   value: data ? String(data.active_ideas.length) : "--" },
    { key: "running", label: "Running", value: data ? String(data.total_running) : "--" },
    { key: "pending", label: "Pending", value: data ? String(data.total_pending) : "--" },
    { key: "branch",  label: "Branch",  value: data ? data.current_branch : "--" },
    { key: "cost",    label: "Cost",    value: cost != null ? `$${cost.toFixed(2)}` : "--" },
    { key: "tokens",  label: "Tokens",  value: tokLabel, title: tokTitle },
  ];

  // Detect overflow: switch to cycling mode when topbar is narrow
  useEffect(() => {
    const el = barRef.current;
    if (!el) return;
    const check = () => setCompact(el.clientWidth < 580);
    check();
    const ro = new ResizeObserver(check);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const clampedIdx = statIdx % stats.length;
  const prevStat = () => setStatIdx((i) => (i - 1 + stats.length) % stats.length);
  const nextStat = () => setStatIdx((i) => (i + 1) % stats.length);

  return (
    <div id="topbar" ref={barRef}>
      <span class="title">The Lab</span>
      <span
        class={`ws-dot ${isWsAuthFailed ? "ws-dot--auth" : isWsConnected ? "ws-dot--on" : "ws-dot--off"}`}
        title={isWsAuthFailed ? "WebSocket: auth failed" : isWsConnected ? "WebSocket: connected" : "WebSocket: reconnecting..."}
      />

      {compact ? (
        /* ── Compact cycling mode ── */
        <div class="topbar-stat-cycle">
          <button class="topbar-cycle-btn" onClick={prevStat} title="Previous stat">‹</button>
          <span class="topbar-cycle-stat" title={stats[clampedIdx].title}>
            <span class="topbar-cycle-label">{stats[clampedIdx].label}</span>
            <b>{stats[clampedIdx].value}</b>
          </span>
          <button class="topbar-cycle-btn" onClick={nextStat} title="Next stat">›</button>
          <div class="topbar-cycle-dots">
            {stats.map((_, i) => (
              <span
                key={i}
                class={`topbar-cycle-dot${i === clampedIdx ? " topbar-cycle-dot--active" : ""}`}
                onClick={() => setStatIdx(i)}
              />
            ))}
          </div>
        </div>
      ) : (
        /* ── Full stats row ── */
        <div class="topbar-stats">
          {stats.map((s) => (
            <span key={s.key} class="stat" title={s.title}>
              {s.label}: <b>{s.value}</b>
            </span>
          ))}
        </div>
      )}

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
                {FONT_PICKER.map((p) => (
                  <button
                    key={p.id}
                    class={`font-swatch${activeFont === p.id ? " font-swatch--active" : ""}`}
                    style={`font-family:${p.uiFont}`}
                    onClick={() => { fontFamily.value = p.id; }}
                    title={p.label}
                  >
                    {p.label}
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
