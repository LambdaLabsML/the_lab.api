/**
 * Resolve a CSS custom property value from the current theme at runtime.
 * Needed by Chart.js and canvas renderers that cannot use var(--token) strings.
 */
export function getCssVar(name: string): string {
  return getComputedStyle(document.documentElement)
    .getPropertyValue(name)
    .trim();
}
