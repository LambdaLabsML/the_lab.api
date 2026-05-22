/**
 * Resolve a CSS custom property value from the current theme at runtime.
 * Needed by Chart.js and canvas renderers that cannot use var(--token) strings.
 */
export function getCssVar(name: string): string {
  return getComputedStyle(document.documentElement)
    .getPropertyValue(name)
    .trim();
}

/**
 * Resolve any color string to a concrete value.
 * - 'var(--green)' → resolved hex from current theme (for Canvas / Chart.js)
 * - '#3fb950'      → returned as-is
 * Use this wherever Canvas 2D or Chart.js need a concrete color string.
 */
export function resolveColor(color: string): string {
  if (!color.startsWith('var(')) return color;
  const name = color.slice(4, -1); // strip 'var(' and ')'
  return getCssVar(name);
}
