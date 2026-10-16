import { signal } from "@preact/signals";
import { useSetting } from "./settings";

export const databaseTab = useSetting<"experiments" | "ideas" | "graph" | "log">("databaseTab", "experiments");
export const databaseScope = signal<{ ideaId: number; label: string } | null>(null);
