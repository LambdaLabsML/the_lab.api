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

/** Scientific notation without the "+" in positive exponents: 2.50e7, 3.83e-4. */
function toSci(v: number, digits: number): string {
  return v.toExponential(digits).replace("e+", "e");
}

/** Short score for dense tables/rows: 123 · 1.23 · 0.123 · 3.83e-4 (the
 *  Overview experiment tables' format — reuse this everywhere a score is
 *  inlined). Tiny (< 1e-2) and huge (≥ 1e6) values go scientific. */
export function fmtScoreShort(v: number): string {
  if (!Number.isFinite(v)) return String(v);
  if (v === 0) return "0";
  const a = Math.abs(v);
  if (a < 1e-2 || a >= 1e6) return toSci(v, 2);
  return a >= 100 ? v.toFixed(0) : a >= 1 ? v.toFixed(2) : v.toFixed(3);
}

/** Whether a set of values shown together reads better in scientific notation. */
export function prefersSci(values: number[]): boolean {
  let minAbs = Infinity, maxAbs = 0;
  for (const v of values) {
    const a = Math.abs(v);
    if (!Number.isFinite(a) || a === 0) continue;
    if (a < minAbs) minAbs = a;
    if (a > maxAbs) maxAbs = a;
  }
  if (maxAbs === 0) return false;
  return maxAbs < 1e-2 || maxAbs >= 1e6 || (minAbs < 1e-2 && maxAbs / minAbs >= 1e3);
}

/**
 * Formatter for a set of values shown together (axis ticks, a best/median
 * pair). Picks ONE notation for the whole set: scientific when the values are
 * tiny (< 1e-2), huge (≥ 1e6), or span ≥ 3 decades down into the small range
 * (typical for a log axis); fixed otherwise. Precision grows until values that
 * differ no longer render identically; exact powers of ten (log-axis decades)
 * drop the mantissa decimals: 1e-5, 1e-3, 1e-1.
 */
export function scoreFormatter(values: number[]): (v: number) => string {
  const vals = values.filter((v) => Number.isFinite(v));
  const mags = vals.map(Math.abs).filter((a) => a > 0);
  if (mags.length === 0) return fmtScoreShort;
  const maxAbs = Math.max(...mags);
  const sci = prefersSci(vals);

  const make = (p: number) => (v: number) =>
    !Number.isFinite(v) ? String(v) : v === 0 ? "0" : sci ? toSci(v, p) : v.toFixed(p);
  const distinct = new Set(vals).size;
  let p = sci ? 1 : maxAbs >= 100 ? 0 : maxAbs >= 1 ? 2 : 3;
  const maxP = sci ? 4 : 6;
  while (p < maxP && new Set(vals.map(make(p))).size < distinct) p++;
  if (sci && vals.every((v) => Number(make(0)(v)) === Number(make(p)(v)))) p = 0;
  return make(p);
}

/**
 * Human-readable metric key:
 *   duration_s       → "duration (s)"
 *   accuracy_per_mtoken → "accuracy/Mtok"
 *   score            → "score"
 */
export function fmtMetricName(key: string): string {
  return key
    .replace(/_per_mtoken$/, '/Mtok')
    .replace(/_per_token$/, '/tok')
    .replace(/_ms$/, ' (ms)')
    .replace(/_s$/, ' (s)')
    .replace(/_pct$/, ' (%)')
    .replace(/_score$/, ' score')
    .replace(/_/g, ' ');
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

/** "12s ago" / "5m ago" / "3h ago" / "2d ago" for an ISO timestamp. */
export function relativeTime(iso: string) {
  const s = Math.max(0, Math.floor((Date.now() - Date.parse(iso)) / 1000));
  if (!Number.isFinite(s)) return iso || "--";
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 172800) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}
