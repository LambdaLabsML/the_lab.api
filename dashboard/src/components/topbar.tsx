import { useState, useEffect, useRef } from "preact/hooks";
import { backlogData, totalAgentCost, totalAgentInputTokens, totalAgentOutputTokens, allIdeas } from "../state/signals";
import { reverseTime, colorMode, colorTheme, fontFamily, fontSize, colorblindMode } from "../state/settings";
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
  onOpenReview?: () => void;
  onOpenReviewSection?: (id: string) => void;
  onOpenWorkbench?: () => void;
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
  const ideas = allIdeas.value;
  const reversed = reverseTime.value;

  // Current idea title from branch name (e.g. "idea/75" → idea #75 description)
  const currentBranch = data?.current_branch ?? "";
  const branchIdeaId = currentBranch.startsWith("idea/") ? Number(currentBranch.slice(5)) : null;
  const branchIdea = branchIdeaId ? ideas[branchIdeaId] : null;
  const branchTitle = branchIdea?.description?.split("\n")[0].slice(0, 40) ?? null;
  const isWsConnected = wsConnected.value;
  const isWsAuthFailed = wsAuthFailed.value;
  const [showMobileMenu, setShowMobileMenu] = useState(false);
  const [saveName, setSaveName] = useState("");
  const [layoutVersion, setLayoutVersion] = useState(0);
  const [compact, setCompact] = useState(false);
  const [statIdx, setStatIdx] = useState(0);
  const barRef = useRef<HTMLDivElement>(null);
  const statsRef = useRef<HTMLDivElement>(null);

  const savedLayouts = props.getSavedLayouts?.() || [];
  void layoutVersion; // trigger re-render when layouts change
  void colorMode; // used elsewhere

  const activeTheme  = colorTheme.value;
  const activeFont   = fontFamily.value;
  const activeFontSz = fontSize.value;
  const isColorblind = colorblindMode.value;

  const cost = totalAgentCost.value;
  const inTok = totalAgentInputTokens.value;
  const outTok = totalAgentOutputTokens.value;

  function fmtTok(n: number): string {
    if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
    if (n >= 1_000) return `${Math.round(n / 1_000)}K`;
    return String(n);
  }

  const tokLabel = (inTok != null && outTok != null)
    ? `${fmtTok(inTok)} in · ${fmtTok(outTok)} out`
    : "--";
  const tokTitle = (inTok != null && outTok != null)
    ? `Input (consumed): ${inTok.toLocaleString()} · Output (generated): ${outTok.toLocaleString()}`
    : undefined;

  const totalRunning = data?.total_running ?? 0;
  const totalPending = data?.total_pending ?? 0;

  // Build the stats array once so it's shared between compact and full renders
  // Hide Pending when 0 — it's almost always 0 and wastes topbar space
  const stats = [
    { key: "ideas",   label: "Ideas",   value: data ? String(data.active_ideas.length) : "--" },
    { key: "running", label: "Running", value: data ? String(totalRunning) : "--" },
    ...(totalPending > 0 ? [{ key: "pending", label: "Pending", value: String(totalPending) }] : []),
    { key: "branch",  label: "Branch",  value: data ? data.current_branch : "--", title: branchTitle ?? undefined },
    { key: "cost",    label: "Cost",    value: cost != null ? `$${cost.toFixed(2)}` : "--" },
    { key: "tokens",  label: "Tokens",  value: tokLabel, title: tokTitle },
  ];

  // Detect actual overflow of the stats row and switch to compact cycling mode.
  // The full stats row stays in the DOM (opacity:0 when hidden) so the browser
  // can measure its real laid-out width at all times.
  useEffect(() => {
    const check = () => {
      const el = statsRef.current;
      if (!el) return;
      setCompact(el.scrollWidth > el.clientWidth + 2); // 2px slop
    };
    check();
    const ro = new ResizeObserver(check);
    if (barRef.current) ro.observe(barRef.current);
    return () => ro.disconnect();
  }, []);

  useEffect(() => {
    if (!showMobileMenu) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") setShowMobileMenu(false);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [showMobileMenu]);

  const clampedIdx = statIdx % stats.length;
  const prevStat = () => setStatIdx((i) => (i - 1 + stats.length) % stats.length);
  const nextStat = () => setStatIdx((i) => (i + 1) % stats.length);

  const goReview = (close: () => void) => {
    props.onOpenReview?.();
    close();
  };

  const goSection = (id: string, close: () => void) => {
    props.onOpenReviewSection?.(id);
    close();
  };

  const goWorkbench = (close: () => void) => {
    props.onOpenWorkbench?.();
    close();
  };

  const settingsPanel = (close: () => void) => (
    <div class="layout-menu-content">
      <div class="layout-menu-section layout-menu-section--nav">
        <div class="layout-menu-label">Workspace</div>
        <div class="layout-menu-nav-grid">
          <button class="layout-menu-btn" onClick={() => goReview(close)}>Review dashboard</button>
          <button class="layout-menu-btn" onClick={() => goWorkbench(close)}>Pane workbench</button>
        </div>
      </div>

      <div class="layout-menu-section layout-menu-section--nav">
        <div class="layout-menu-label">Review sections</div>
        <div class="layout-menu-nav-grid layout-menu-nav-grid--compact">
          <button class="layout-menu-btn" onClick={() => goSection("review-progress", close)}>Progress</button>
          <button class="layout-menu-btn" onClick={() => goSection("review-ideas", close)}>Ideas</button>
          <button class="layout-menu-btn" onClick={() => goSection("review-runs", close)}>Runs</button>
          <button class="layout-menu-btn" onClick={() => goSection("review-ops", close)}>Queue</button>
        </div>
      </div>

      <div class="layout-menu-section">
        <div class="layout-menu-label">Direction</div>
        <button
          class="layout-menu-btn"
          onClick={() => { reverseTime.value = !reversed; }}
          title={reversed ? "Newest left/top (click to reverse)" : "Oldest left/top (click to reverse)"}
        >
          {reversed ? "Newest first" : "Oldest first"}
        </button>
      </div>

      <div class="layout-menu-section">
        <div class="layout-menu-label">Color theme</div>
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

      <div class="layout-menu-section">
        <div class="layout-menu-label">Font</div>
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

      <div class="layout-menu-section">
        <div class="layout-menu-label">Size</div>
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
        <div class="layout-menu-label">Accessibility</div>
        <button
          class={`layout-menu-btn${isColorblind ? " layout-menu-btn--active" : ""}`}
          onClick={() => { colorblindMode.value = !isColorblind; }}
          title="Okabe-Ito colorblind-safe palette"
        >
          {isColorblind ? "Colorblind mode on" : "Colorblind mode"}
        </button>
      </div>

      <div class="layout-menu-section">
        <button class="layout-menu-btn" onClick={() => { props.onResetLayout?.(); close(); }}>
          Reset workspace
        </button>
      </div>

      <div class="layout-menu-section">
        <div class="layout-menu-label">Save current</div>
        <div class="layout-save-row">
          <input
            class="layout-save-input"
            placeholder="layout name"
            value={saveName}
            onInput={(e) => setSaveName((e.target as HTMLInputElement).value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && saveName.trim()) {
                props.onSaveLayout?.(saveName.trim());
                setSaveName("");
                setLayoutVersion((v) => v + 1);
              }
            }}
          />
          <button
            class="layout-menu-btn layout-save-btn"
            onClick={() => {
              if (saveName.trim()) {
                props.onSaveLayout?.(saveName.trim());
                setSaveName("");
                setLayoutVersion((v) => v + 1);
              }
            }}
          >
            Save
          </button>
        </div>
      </div>

      {savedLayouts.length > 0 && (
        <div class="layout-menu-section">
          <div class="layout-menu-label">Saved layouts</div>
          {savedLayouts.map((name) => (
            <div key={name} class="layout-saved-row">
              <span
                class="layout-saved-name"
                onClick={() => { props.onLoadLayout?.(name); close(); }}
                title={`Load "${name}"`}
              >
                {name}
              </span>
              <button
                class="layout-saved-delete"
                onClick={() => { props.onDeleteLayout?.(name); setLayoutVersion((v) => v + 1); }}
                title="Delete"
              >
                x
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );

  return (
    <div id="topbar" ref={barRef}>
      <span class="title">the_lab</span>
      <span
        class={`ws-dot ${isWsAuthFailed ? "ws-dot--auth" : isWsConnected ? "ws-dot--on" : "ws-dot--off"}`}
        title={isWsAuthFailed ? "WebSocket: auth failed" : isWsConnected ? "WebSocket: connected" : "WebSocket: reconnecting..."}
      />

      {/* ── Stats area: single flex slot holding both full-row and cycle widget ── */}
      <div class="topbar-stats-area">
        {/* Full stats row — always in DOM so scrollWidth reflects true content width */}
        <div
          ref={statsRef}
          class="topbar-stats"
          aria-hidden={compact ? "true" : undefined}
          style={compact ? { opacity: 0, pointerEvents: "none" } : undefined}
        >
          {stats.map((s) => (
            <span
              key={s.key}
              class="stat"
              title={s.key === "branch" && branchTitle ? `${s.value}: ${branchTitle}` : s.title}
              data-key={s.key}
              data-live={s.key === "running" && s.value !== "0" && s.value !== "--" ? "true" : undefined}
            >
              {s.label}: <b>{s.value}</b>
              {s.key === "branch" && branchTitle && (
                <span class="stat-branch-title"> · {branchTitle}</span>
              )}
            </span>
          ))}
        </div>

        {/* Compact cycling widget — absolutely overlays the stats row when it overflows */}
        {compact && (
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
        )}
      </div>

      <button
        class="topbar-icon-btn topbar-menu-btn"
        onClick={() => setShowMobileMenu(true)}
        title="Open menu"
        aria-label="Open menu"
        aria-expanded={showMobileMenu ? "true" : "false"}
      >
        ☰
      </button>

      {showMobileMenu && (
        <div class="mobile-menu-shell">
          <button class="mobile-menu-scrim" onClick={() => setShowMobileMenu(false)} aria-label="Close menu" />
          <div class="mobile-menu">
            <div class="mobile-menu-head">
              <div>
                <div class="mobile-menu-title">the_lab</div>
                <div class="mobile-menu-subtitle">
                  {isWsAuthFailed ? "auth failed" : isWsConnected ? "connected" : "reconnecting"}
                </div>
              </div>
              <button class="topbar-icon-btn" onClick={() => setShowMobileMenu(false)} title="Close menu" aria-label="Close menu">×</button>
            </div>
            <div class="mobile-menu-stats">
              {stats.map((s) => (
                <span key={s.key} class="mobile-stat" title={s.title}>
                  <span>{s.label}</span>
                  <b>{s.value}</b>
                </span>
              ))}
            </div>
            {settingsPanel(() => setShowMobileMenu(false))}
          </div>
        </div>
      )}
    </div>
  );
}
