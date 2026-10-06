// Code-split wrapper: ScatterChart is the only Chart.js consumer, and Chart.js
// (chart.js/auto, ~200 KB min / ~70 KB gz) was half of the initial bundle's
// vendor weight. Load it on first mount instead of at startup.
import { useEffect, useState } from "preact/hooks";

type Mod = typeof import("./scatter-chart");
type Props = Parameters<Mod["ScatterChart"]>[0];

let loaded: Mod | null = null;
let loading: Promise<Mod> | null = null;

export function LazyScatterChart(props: Props) {
  const [mod, setMod] = useState<Mod | null>(loaded);
  useEffect(() => {
    if (mod) return;
    let alive = true;
    (loading ??= import("./scatter-chart")).then((m) => {
      loaded = m;
      if (alive) setMod(m);
    });
    return () => { alive = false; };
  }, []);
  if (!mod) return <div style={{ width: "100%", height: "100%" }} />;
  const C = mod.ScatterChart;
  return <C {...props} />;
}
