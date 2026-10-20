import { signal } from "@preact/signals";
export const artifactPath = signal("");
export const editingPath = signal<string | null>(null);
export const selectedIsDir = signal(true);
