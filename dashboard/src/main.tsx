import { render } from "preact";
import { App } from "./app";
import { colorTheme, fontFamily, fontSize } from "./state/settings";
import { effect } from "@preact/signals";
import { ALL_PAIRINGS, DEFAULT_PAIRING } from "./lib/fonts";
import "dockview-core/dist/styles/dockview.css";
import "./styles/tailwind.css";
import "./styles/dockview-overrides.scss";
import "./styles/global.scss";
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

render(<App />, document.getElementById("app")!);
