/**
 * SettingsPanel — appearance / display / workspace settings as a real panel
 * (lives in the left secondary nav, not a modal). Ported from the old topbar
 * `settingsPanel()` dropdown onto the new design language: eyebrow-headed
 * sections, hairline separators, ghost buttons, accent for the active thing.
 *
 * Keep the `SettingsPanel` export and `SettingsPanelProps` interface stable —
 * the app shell passes the layout callbacks.
 */
import { useState } from "preact/hooks";
import { Panel, PanelHeader, PanelBody, Eyebrow, Toggle, IconButton, Separator } from "./ui";
import {
  colorTheme,
  fontFamily,
  fontSize,
  colorblindMode,
  uiTexture,
  reverseTime,
} from "../state/settings";

export interface SettingsPanelProps {
  onResetLayout?: () => void;
  onSaveLayout?: (name: string) => void;
  onLoadLayout?: (name: string) => void;
  onDeleteLayout?: (name: string) => void;
  getSavedLayouts?: () => string[];
}

// ── Local display data (ported from topbar.tsx — kept here so this panel owns
//    its own copy and has no cross-component import coupling). ──────────────

interface ThemeDef {
  id: string;
  name: string;
  swatches: string[]; // 3 colours: bg, accent, green
}

const THEMES: ThemeDef[] = [
  // Sorted brightest → darkest by perceived luminance of the background swatch
  { id: "light",          name: "Light",      swatches: ["#ffffff", "#0969da", "#1a7f37"] },
  { id: "linen",          name: "Linen",      swatches: ["#FFFFF8", "#5B8DB8", "#5A8A5A"] },
  { id: "solarized-light",name: "Solarized",  swatches: ["#FDF6E3", "#268BD2", "#859900"] },
  { id: "gruvbox-light",  name: "Gruvbox",    swatches: ["#FBF1C7", "#458588", "#98971A"] },
  { id: "nord",      name: "Nord",      swatches: ["#2e3440", "#88c0d0", "#a3be8c"] },
  { id: "dracula",   name: "Dracula",   swatches: ["#282a36", "#bd93f9", "#50fa7b"] },
  { id: "ocean",     name: "Ocean",     swatches: ["#0d1b2a", "#2ea8ff", "#3fb950"] },
  { id: "charcoal",  name: "Charcoal",  swatches: ["#12110F", "#A3B18A", "#D4A373"] },
  { id: "graphite",  name: "Graphite",  swatches: ["#111015", "#8B8CF6", "#D8A03D"] },
  { id: "default",   name: "Default",   swatches: ["#0d1117", "#58a6ff", "#3fb950"] },
  { id: "petroleum", name: "Petroleum", swatches: ["#071013", "#2DD4BF", "#F59E0B"] },
  { id: "orchid",    name: "Orchid",    swatches: ["#070B18", "#6EA8FE", "#C084FC"] },
  { id: "ink",       name: "Ink + Ice", swatches: ["#080B0F", "#7DD3FC", "#B7E35F"] },
  { id: "crimson",   name: "Crimson",   swatches: ["#120608", "#FF6B81", "#7EC8A4"] },
  { id: "oled",      name: "OLED",      swatches: ["#000000", "#58a6ff", "#3fb950"] },
];

interface FontPickerDef { id: string; label: string; uiFont: string; }
const FONT_PICKER: FontPickerDef[] = [
  { id: "mono",   label: "JetBrains Mono",  uiFont: "'JetBrains Mono','SF Mono',monospace" },
  { id: "geist",  label: "Geist",           uiFont: "'Geist',system-ui,sans-serif" },
  { id: "ibm",    label: "IBM Plex",        uiFont: "'IBM Plex Sans',Helvetica,sans-serif" },
  { id: "mona",   label: "Mona Sans",       uiFont: "'Mona Sans',system-ui,sans-serif" },
  { id: "sora",   label: "Sora",            uiFont: "'Sora',system-ui,sans-serif" },
  { id: "space",  label: "Space Grotesk",   uiFont: "'Space Grotesk',system-ui,sans-serif" },
];

const FONT_SIZES = ["xs", "s", "m", "l", "xl", "xxl"] as const;

export function SettingsPanel(props: SettingsPanelProps) {
  const activeTheme  = colorTheme.value;
  const activeFont   = fontFamily.value;
  const activeFontSz = fontSize.value;
  const isColorblind = colorblindMode.value;
  const textureOn    = uiTexture.value;
  const reversed     = reverseTime.value;

  const [saveName, setSaveName] = useState("");
  // Bumped after save/delete so getSavedLayouts() is re-read on re-render.
  const [layoutVersion, setLayoutVersion] = useState(0);
  void layoutVersion;

  const savedLayouts = props.getSavedLayouts?.() ?? [];
  // Workspace section only renders when the shell wires at least one layout action.
  const hasWorkspace =
    !!props.onResetLayout || !!props.onSaveLayout || !!props.getSavedLayouts;

  const commitSave = () => {
    const name = saveName.trim();
    if (!name) return;
    props.onSaveLayout?.(name);
    setSaveName("");
    setLayoutVersion((v) => v + 1);
  };

  return (
    <Panel scroll class="settings-panel">
      <PanelHeader title="Settings" />
      <PanelBody class="settings-panel-body">

        {/* ── Appearance ───────────────────────────────────────────── */}
        <section class="settings-section">
          <Eyebrow>Appearance</Eyebrow>

          <div class="settings-field">
            <span class="settings-field-label">Color theme</span>
            <div class="settings-theme-grid">
              {THEMES.map((t) => (
                <button
                  key={t.id}
                  type="button"
                  class={`settings-swatch${activeTheme === t.id ? " is-active" : ""}`}
                  onClick={() => { colorTheme.value = t.id; }}
                  title={t.name}
                  aria-pressed={activeTheme === t.id}
                >
                  <span class="settings-swatch-colors">
                    {t.swatches.map((c, i) => (
                      <span key={i} class="settings-swatch-dot" style={`background:${c}`} />
                    ))}
                  </span>
                  <span class="settings-swatch-name">{t.name}</span>
                </button>
              ))}
            </div>
          </div>

          <div class="settings-field">
            <span class="settings-field-label">Font family</span>
            <div class="settings-font-grid">
              {FONT_PICKER.map((p) => (
                <button
                  key={p.id}
                  type="button"
                  class={`settings-font-btn${activeFont === p.id ? " is-active" : ""}`}
                  style={`font-family:${p.uiFont}`}
                  onClick={() => { fontFamily.value = p.id; }}
                  title={p.label}
                  aria-pressed={activeFont === p.id}
                >
                  {p.label}
                </button>
              ))}
            </div>
          </div>

          <div class="settings-field">
            <span class="settings-field-label">Size</span>
            <div class="settings-size-row">
              {FONT_SIZES.map((sz) => (
                <button
                  key={sz}
                  type="button"
                  class={`settings-size-btn${activeFontSz === sz ? " is-active" : ""}`}
                  onClick={() => { fontSize.value = sz; }}
                  aria-pressed={activeFontSz === sz}
                >
                  {sz}
                </button>
              ))}
            </div>
          </div>
        </section>

        <Separator />

        {/* ── Display ──────────────────────────────────────────────── */}
        <section class="settings-section">
          <Eyebrow>Display</Eyebrow>

          <div class="settings-toggle-row">
            <span class="settings-toggle-text">Texture</span>
            <Toggle
              active={textureOn}
              onClick={() => { uiTexture.value = !textureOn; }}
              title="Slight hacker-noise ambient texture behind the UI"
            >
              {textureOn ? "On" : "Off"}
            </Toggle>
          </div>

          <div class="settings-toggle-row">
            <span class="settings-toggle-text">Colorblind-safe palette</span>
            <Toggle
              active={isColorblind}
              onClick={() => { colorblindMode.value = !isColorblind; }}
              title="Okabe-Ito colorblind-safe status palette"
            >
              {isColorblind ? "On" : "Off"}
            </Toggle>
          </div>

          <div class="settings-toggle-row">
            <span class="settings-toggle-text">Direction</span>
            <Toggle
              active={reversed}
              onClick={() => { reverseTime.value = !reversed; }}
              title={reversed ? "Newest first (click for oldest first)" : "Oldest first (click for newest first)"}
            >
              {reversed ? "Newest first" : "Oldest first"}
            </Toggle>
          </div>
        </section>

        {/* ── Workspace ────────────────────────────────────────────── */}
        {hasWorkspace && (
          <>
            <Separator />
            <section class="settings-section">
              <Eyebrow>Workspace</Eyebrow>

              {props.onResetLayout && (
                <div class="settings-field">
                  <IconButton
                    outlined
                    class="settings-block-btn"
                    onClick={() => { props.onResetLayout?.(); }}
                    title="Reset the workspace to its default layout"
                  >
                    Reset layout
                  </IconButton>
                </div>
              )}

              {props.onSaveLayout && (
                <div class="settings-field">
                  <span class="settings-field-label">Save current</span>
                  <div class="settings-save-row">
                    <input
                      class="settings-input"
                      placeholder="layout name"
                      value={saveName}
                      onInput={(e) => setSaveName((e.target as HTMLInputElement).value)}
                      onKeyDown={(e) => { if (e.key === "Enter") commitSave(); }}
                    />
                    <IconButton
                      outlined
                      class="settings-save-btn"
                      onClick={commitSave}
                      title="Save the current layout under this name"
                    >
                      Save
                    </IconButton>
                  </div>
                </div>
              )}

              {props.getSavedLayouts && (
                <div class="settings-field">
                  <span class="settings-field-label">Saved layouts</span>
                  {savedLayouts.length === 0 ? (
                    <p class="settings-empty">No saved layouts yet.</p>
                  ) : (
                    <div class="settings-saved-list">
                      {savedLayouts.map((name) => (
                        <div key={name} class="settings-saved-row">
                          <button
                            type="button"
                            class="settings-saved-name"
                            onClick={() => { props.onLoadLayout?.(name); }}
                            title={`Load "${name}"`}
                          >
                            {name}
                          </button>
                          <IconButton
                            class="settings-saved-delete"
                            onClick={() => {
                              props.onDeleteLayout?.(name);
                              setLayoutVersion((v) => v + 1);
                            }}
                            title={`Delete "${name}"`}
                            aria-label={`Delete ${name}`}
                          >
                            ×
                          </IconButton>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </section>
          </>
        )}

      </PanelBody>
    </Panel>
  );
}
