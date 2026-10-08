// UI contrast (-2 … +2) on top of any colour theme: text and border tokens
// are pushed away from the background (more contrast) or toward it (less),
// as inline overrides on <html>. Accent / status / badge colours stay put.

const TOKENS = ["--bg", "--bg-elev", "--bg-hi", "--border", "--border-soft", "--text", "--text-muted", "--text-faint"] as const;
type Rgb = [number, number, number];

function parse(value: string): Rgb | null {
  const v = value.trim();
  let m = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(v);
  if (m) {
    const h = m[1].length === 3 ? m[1].replace(/./g, (c) => c + c) : m[1];
    return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16)) as Rgb;
  }
  m = /^rgba?\(\s*([\d.]+)[\s,]+([\d.]+)[\s,]+([\d.]+)/i.exec(v);
  return m ? [Number(m[1]), Number(m[2]), Number(m[3])] : null;
}

const mix = (a: Rgb, b: Rgb, t: number): Rgb => a.map((x, i) => x + (b[i] - x) * t) as Rgb;
const css = (c: Rgb) => `rgb(${c.map((x) => Math.round(Math.min(255, Math.max(0, x)))).join(" ")})`;
const luminance = ([r, g, b]: Rgb) => (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255;

/** Apply contrast ``level`` to the current theme (call after data-theme changes). */
export function applyContrast(level: number): void {
  const root = document.documentElement;
  for (const t of TOKENS) root.style.removeProperty(t);
  if (!level) return;
  const cs = getComputedStyle(root);
  const c: Partial<Record<(typeof TOKENS)[number], Rgb>> = {};
  for (const t of TOKENS) {
    const rgb = parse(cs.getPropertyValue(t));
    if (!rgb) return; // unexpected token format: leave the theme untouched
    c[t] = rgb;
  }
  const bg = c["--bg"]!;
  const dark = luminance(bg) < 0.5;
  const ink: Rgb = dark ? [255, 255, 255] : [0, 0, 0];
  const a = Math.min(2, Math.abs(level)) / 2; // 0.5 or 1
  const out: Partial<Record<(typeof TOKENS)[number], Rgb>> = {};
  if (level > 0) {
    // Each tier moves toward the next stronger one; panels separate a bit more.
    out["--text"] = mix(c["--text"]!, ink, 0.55 * a);
    out["--text-muted"] = mix(c["--text-muted"]!, out["--text"], 0.4 * a);
    out["--text-faint"] = mix(c["--text-faint"]!, c["--text-muted"]!, 0.55 * a);
    out["--border"] = mix(c["--border"]!, c["--text-faint"]!, 0.45 * a);
    out["--border-soft"] = mix(c["--border-soft"]!, c["--border"]!, 0.6 * a);
    out["--bg-elev"] = mix(c["--bg-elev"]!, ink, 0.03 * a);
    out["--bg-hi"] = mix(c["--bg-hi"]!, ink, 0.06 * a);
  } else {
    // Softer: everything drifts toward the background.
    out["--text"] = mix(c["--text"]!, bg, 0.2 * a);
    out["--text-muted"] = mix(c["--text-muted"]!, bg, 0.25 * a);
    out["--text-faint"] = mix(c["--text-faint"]!, bg, 0.3 * a);
    out["--border"] = mix(c["--border"]!, bg, 0.35 * a);
    out["--border-soft"] = mix(c["--border-soft"]!, bg, 0.4 * a);
  }
  for (const [t, v] of Object.entries(out)) root.style.setProperty(t, css(v as Rgb));
}
