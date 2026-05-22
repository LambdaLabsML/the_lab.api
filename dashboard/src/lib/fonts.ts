/**
 * Lazy-load a font pairing on demand.
 * Each pairing is a separate dynamic import chunk — fonts only download
 * when the user selects them.
 */

export type FontPairing = {
  id: string;
  label: string;       // display name in picker
  uiFont: string;      // CSS font-family stack for --font
  monoFont: string;    // CSS font-family stack for --font-mono
  load: () => Promise<void>;
};

// Default pairing (JetBrains Mono for everything — loads immediately since
// it was previously always-on; kept in the list so the picker shows it)
export const DEFAULT_PAIRING: FontPairing = {
  id: "mono",
  label: "JetBrains Mono",
  uiFont: "'JetBrains Mono', 'SF Mono', 'Fira Code', monospace",
  monoFont: "'JetBrains Mono', monospace",
  load: async () => {
    await import("@fontsource/jetbrains-mono/400.css");
    await import("@fontsource/jetbrains-mono/700.css");
  },
};

export const FONT_PAIRINGS: FontPairing[] = [
  {
    id: "geist",
    label: "Geist",
    uiFont: "'Geist', system-ui, sans-serif",
    monoFont: "'Geist Mono', 'JetBrains Mono', monospace",
    load: async () => {
      // Geist package is Next.js-only for JS; we use a local CSS shim with
      // @font-face rules pointing to the woff2 files in node_modules/geist.
      await import("../styles/geist.css");
    },
  },
  {
    id: "ibm",
    label: "IBM Plex",
    uiFont: "'IBM Plex Sans', Helvetica, sans-serif",
    monoFont: "'IBM Plex Mono', 'Fira Code', monospace",
    load: async () => {
      await import("@fontsource/ibm-plex-sans/400.css");
      await import("@fontsource/ibm-plex-sans/600.css");
      await import("@fontsource/ibm-plex-mono/400.css");
      await import("@fontsource/ibm-plex-mono/600.css");
    },
  },
  {
    id: "mona",
    label: "Mona Sans",
    uiFont: "'Mona Sans', system-ui, sans-serif",
    monoFont: "'JetBrains Mono', monospace",
    load: async () => {
      await import("@fontsource/mona-sans/400.css");
      await import("@fontsource/mona-sans/600.css");
      await import("@fontsource/jetbrains-mono/400.css");
    },
  },
  {
    id: "sora",
    label: "Sora",
    uiFont: "'Sora', system-ui, sans-serif",
    monoFont: "'JetBrains Mono', monospace",
    load: async () => {
      await import("@fontsource/sora/400.css");
      await import("@fontsource/sora/600.css");
      await import("@fontsource/jetbrains-mono/400.css");
    },
  },
  {
    id: "space",
    label: "Space Grotesk",
    uiFont: "'Space Grotesk', system-ui, sans-serif",
    monoFont: "'IBM Plex Mono', 'Fira Code', monospace",
    load: async () => {
      await import("@fontsource/space-grotesk/400.css");
      await import("@fontsource/space-grotesk/600.css");
      await import("@fontsource/ibm-plex-mono/400.css");
    },
  },
];

export const ALL_PAIRINGS: FontPairing[] = [DEFAULT_PAIRING, ...FONT_PAIRINGS];
