import { render } from "preact";
import { App } from "./app";
import { colorTheme, fontFamily, fontSize, colorblindMode, uiTexture } from "./state/settings";
import { effect } from "@preact/signals";
import { ALL_PAIRINGS, DEFAULT_PAIRING } from "./lib/fonts";
import "dockview-core/dist/styles/dockview.css";
import "./styles/tailwind.css";
import "./styles/dockview-overrides.scss";
import "./styles/global.scss";
import "./styles/_ui.scss";
import "./styles/topbar.scss";
import "./styles/chart.scss";
import "./styles/subway.scss";
import "./styles/detail.scss";
import "./styles/log.scss";
import "./styles/api.scss";
import "./styles/sandbox.scss";
import "./styles/prompts.scss";
import "./styles/agents.scss";
import "./styles/queue.scss";
import "./styles/suggest.scss";
import "./styles/tags.scss";
import "./styles/task.scss";
import "./styles/stats.scss";
import "./styles/chat.scss";
import "./styles/table.scss";
import "./styles/_shell.scss";
import "./styles/activity.scss";
import "./styles/settings-panel.scss";

// Apply theme / font-family / font-size on every change
effect(() => {
  document.documentElement.setAttribute("data-theme", colorTheme.value);
});
effect(() => {
  const id = fontFamily.value;
  // Set data-font-family attribute (drives CSS variable overrides in _tokens.scss)
  if (id === "mono") document.documentElement.removeAttribute("data-font-family");
  else document.documentElement.setAttribute("data-font-family", id);
  // Lazy-load the font files for this pairing
  const pairing = ALL_PAIRINGS.find((p) => p.id === id) ?? DEFAULT_PAIRING;
  pairing.load().catch(() => {}); // silently ignore load failures
});
effect(() => {
  const s = fontSize.value;
  if (s === "m") document.documentElement.removeAttribute("data-font-size");
  else document.documentElement.setAttribute("data-font-size", s);
});
// Slight hacker-noise ambient texture (see styles/_ui.scss).
effect(() => {
  document.documentElement.setAttribute("data-texture", uiTexture.value ? "on" : "off");
});

// Colorblind mode: override status CSS variables with Okabe-Ito safe palette.
// The Okabe-Ito set (2002) is the scientific standard — validated across all
// common colorblind types by simulating deuteranopia, protanopia, tritanopia.
// Key insight: replaces the red-green axis with blue-orange, which is
// preserved across all types of red-green colorblindness.
const CB_STYLE_ID = "the-lab-colorblind-overrides";
effect(() => {
  const el = document.getElementById(CB_STYLE_ID);
  if (colorblindMode.value) {
    if (!el) {
      const style = document.createElement("style");
      style.id = CB_STYLE_ID;
      // Override status colors with Okabe-Ito palette.
      // We use CSS variable overrides so they work with every theme:
      //   active (green)    → #009E73  bluish-green (teal axis, safe vs red)
      //   abandoned (red)   → #D55E00  vermillion   (orange axis, safe vs green)
      //   running (yellow)  → #F0E442  yellow       (keep — safe in all types)
      //   accent (blue)     → #0072B2  deep blue    (keep — safe in all types)
      style.textContent = `
        :root, [data-theme] {
          --green:  #009E73;
          --red:    #D55E00;
          --yellow: #F0E442;
          --accent: #0072B2;
        }
      `;
      document.head.appendChild(style);
    }
  } else {
    el?.remove();
  }
});

render(<App />, document.getElementById("app")!);
