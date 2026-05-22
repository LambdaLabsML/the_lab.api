import { render } from "preact";
import { App } from "./app";
import { colorTheme } from "./state/settings";
import { effect } from "@preact/signals";
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

// Apply theme on every change
effect(() => {
  document.documentElement.setAttribute("data-theme", colorTheme.value);
});

render(<App />, document.getElementById("app")!);
