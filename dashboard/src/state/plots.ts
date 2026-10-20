import { useSetting } from "./settings";
export type Plot = { id: string; height?: number; width?: "full" | "half"; type: "metrics" | "scatter"; metric?: string; x?: string; y?: string };
export const overviewPlots = useSetting<Plot[]>("overviewPlots", [{ id: "correlation", type: "scatter" }]);
