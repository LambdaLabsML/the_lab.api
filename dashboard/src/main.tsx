import { render } from "preact";
import { App } from "./app";
import { colorTheme, fontFamily, fontSize } from "./state/settings";
import { effect } from "@preact/signals";
// Fonts — all OFL-1.1 open-source, loaded from npm via @fontsource
import "@fontsource/jetbrains-mono/400.css";
import "@fontsource/jetbrains-mono/700.css";
import "@fontsource/fira-code/400.css";
import "@fontsource/fira-code/700.css";
import "@fontsource/space-grotesk/400.css";
import "@fontsource/space-grotesk/600.css";
import "@fontsource/outfit/400.css";
import "@fontsource/outfit/600.css";
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
  const f = fontFamily.value;
  if (f === "mono") document.documentElement.removeAttribute("data-font-family");
  else document.documentElement.setAttribute("data-font-family", f);
});
effect(() => {
  const s = fontSize.value;
  if (s === "m") document.documentElement.removeAttribute("data-font-size");
  else document.documentElement.setAttribute("data-font-size", s);
});

render(<App />, document.getElementById("app")!);
