// ------------------------------------------------------------
// Utility formatting functions extracted from dashboard.html.
// Pure functions — no DOM or global state dependencies.
// ------------------------------------------------------------

/**
 * Escape a string for safe insertion into HTML.
 * Uses a regex-based approach (no DOM required) so this works in
 * both browser and SSR/test contexts.
 */
export function escapeHtml(s: string): string {
  if (!s) return '';
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

/** Format an ISO timestamp for display. Returns '--' when missing. */
export function formatTime(iso: string): string {
  if (!iso) return '--';
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

/** Truncate a string to `max` characters, appending '...' if needed. */
export function truncate(s: string, max: number): string {
  if (!s) return '';
  return s.length > max ? s.substring(0, max) + '...' : s;
}

/**
 * Extract a short title from an idea description:
 * text before the first ':', or before the first non-word/space
 * character if there is no colon.
 */
export function ideaTitle(s: string): string {
  if (!s) return '';
  const colonIdx = s.indexOf(':');
  if (colonIdx > 0) return s.substring(0, colonIdx).trim();
  const m = s.match(/^[\w\s]+/);
  return m ? m[0].trim() : s;
}

/** Return a badge `<span>` HTML string for a given status.
 *  When status is "running" and pct is provided, includes a small SVG progress ring. */
export function badgeHtml(status: string, pct?: number): string {
  if (status === "running" && typeof pct === "number") {
    const size = 12;
    const sw = 2;
    const r = (size - sw) / 2;
    const c = 2 * Math.PI * r;
    const offset = c * (1 - Math.min(pct, 100) / 100);
    const center = size / 2;
    const ring =
      '<svg width="' + size + '" height="' + size + '" style="vertical-align:middle;margin-right:3px">' +
      '<circle cx="' + center + '" cy="' + center + '" r="' + r + '" fill="none" stroke="#30363d" stroke-width="' + sw + '"/>' +
      '<circle cx="' + center + '" cy="' + center + '" r="' + r + '" fill="none" stroke="#d29922" stroke-width="' + sw + '"' +
      ' stroke-dasharray="' + c + '" stroke-dashoffset="' + offset + '" stroke-linecap="round"' +
      ' transform="rotate(-90 ' + center + ' ' + center + ')"/></svg>';
    return '<span class="badge badge-running">' + ring + 'running ' + Math.round(pct) + '%</span>';
  }
  return '<span class="badge badge-' + status + '">' + status + '</span>';
}
