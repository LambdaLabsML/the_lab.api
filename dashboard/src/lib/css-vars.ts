// Cache getComputedStyle() for the duration of one synchronous callstack.
// Cleared after the current microtask so theme changes always get fresh values.
let _cachedStyle: CSSStyleDeclaration | null = null;

function _getStyle(): CSSStyleDeclaration {
  if (!_cachedStyle) {
    _cachedStyle = getComputedStyle(document.documentElement);
    Promise.resolve().then(() => { _cachedStyle = null; });
  }
  return _cachedStyle;
}

/**
 * Resolve a CSS custom property value from the current theme at runtime.
 * Needed by Chart.js and canvas renderers that cannot use var(--token) strings.
 */
export function getCssVar(name: string): string {
  return _getStyle().getPropertyValue(name).trim();
}

/**
 * Resolve a CSS custom property that holds a px value to a plain number.
 * Use this for Chart.js font.size which requires a number, not a string.
 * e.g. getCssVarPx("--text-xs") → 10
 */
export function getCssVarPx(name: string): number {
  return parseInt(getCssVar(name), 10) || 10;
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
