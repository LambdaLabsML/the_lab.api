import { render } from "preact";
import { App } from "./app";
import "./styles/global.css";
import "./styles/topbar.css";
import "./styles/chart.css";
import "./styles/subway.css";
import "./styles/detail.css";
import "./styles/log.css";
import "./styles/api.css";
import "./styles/sandbox.css";
import "./styles/suggest.css";
import "./styles/tags.css";
import "./styles/task.css";

render(<App />, document.getElementById("app")!);
