# The Lab — Design Language

> Single source of truth for the dashboard UI refactor. Every panel, view, and
> component should conform to this. When in doubt, match `the_lab.api.landingpage`
> (`../the_lab.api.landingpage/src/styles/labapi.css`) — its design language is the
> north star: terminal-clean, breathable, confident.

## Philosophy

**Terminal-grade clarity.** Monospace everywhere. Data-dense but never cramped.

**Guide the eye with whitespace and alignment — not with boxes and lines.**
Borders are a last resort. Prefer:
1. **Hairline separators** — a 1px line in `--border-soft`, or a `gap: 1px` grid on a
   `--border` background (landingpage style), instead of wrapping everything in a box.
2. **Elevation steps** — `--bg` (canvas) → `--bg-elev` (panel) → `--bg-hi` (hover/active).
   Let surfaces sit on each other; don't outline them.

**Color means something.** Reserve color for status and the single accent. Never
decorative. Numbers and labels are `--text` / `--text-muted` / `--text-faint`; the
accent (`--accent`) marks the *one* active/selected thing in view.

**No gradients.** Flat fills only. Where a subtle tint is wanted, use
`color-mix(in srgb, var(--accent) 8%, transparent)` — never a `linear-gradient`.

**Small by default.** Type is small unless the user bumps it in settings. Use the
`--text-*` tokens — never hardcode `font-size` in px (especially in SVG `font-size=`
attributes, which is the #1 reason mini panels look wrong at non-default sizes).

**Slight hacker-noise, and only slight.** A near-invisible grain + faint scanline
ambient layer lives behind everything (`[data-texture="on"]`, ~2–3% opacity). It is
flavor, not decoration. Never raise its opacity to where it competes with content.

## Tokens (see `styles/_tokens.scss`)

| Group | Tokens | Notes |
|---|---|---|
| Surface | `--bg`, `--bg-elev`, `--bg-hi` | canvas → panel → hover; the only 3 surface steps |
| Hairlines | `--border`, `--border-soft` | `--border` = visible 1px; `--border-soft` = whisper |
| Text | `--text`, `--text-muted`, `--text-faint` | value → secondary → label/eyebrow |
| Accent | `--accent`, `--accent-dim` | the one active/selected color |
| Status | `--green`, `--yellow`, `--red`, `--purple` | done / running / failed / best-line |
| Type | `--text-xs … --text-xl` | 8/9/10/12/15 at default. ALWAYS use these. |
| Radius | `--radius-sm/md/lg` | 3 / 4 / 6 — sharp, hacker-clean. No big pills except true pills. |
| Space | `--space-1 … --space-8` | 4/8/12/16/24/32 |
| Font | `--font`, `--font-mono` | both mono by default |

## Components (use these — `components/ui/`)

- **`<Panel>` / `<PanelHeader title actions>` / `<PanelBody>`** — every panel. The header
  is the micro-eyebrow row (uppercase, letter-spaced, `--text-xs`, `--text-faint`) + an
  actions slot on the right. Separated from the body by a single hairline, **not** a box.
- **`<Eyebrow>`** — the uppercase micro-label. Section heads, stat labels, table captions.
- **`<Stat value label sub>`** — a confident metric: large mono `value`, eyebrow `label`,
  faint `sub`. This is how numbers are shown (cf. landingpage `.live-stat-value`).
- **`<Badge tone>`** — status pill. tones: `active|running|concluded|abandoned|neutral|best`.
- **`<IconButton>` / `<Toggle>`** — chrome buttons. Ghost by default (transparent, `--text-faint`),
  `--bg-hi` on hover, `--accent` when active. No borders unless `outlined`.
- **`<Separator>`** — a hairline (horizontal or vertical).
- **`<EmptyState icon title body>`** — the one empty/zero-data treatment.

Equivalent CSS classes exist in `styles/_ui.scss` (`.ui-panel`, `.ui-eyebrow`,
`.ui-stat`, `.ui-badge`, `.ui-btn`, `.ui-toggle`, `.ui-sep`, `.ui-empty`,
`.ui-hairline-grid`) for places that style via SCSS rather than JSX.

## Interactivity (use these — `lib/hooks/`)

Clickable element-actions are centralized. Do not re-implement these inline:

- **`useEntityNav(ideaId, label?)`** → `{ highlighted, bind }`. Spread `bind` onto any
  clickable node/row/dot that represents an idea: click navigates (via `navigateToIdea`),
  hover sets/clears `highlightedIdea` with the correct guard. `highlighted` is reactive.
- **`chartNavClick(elements)`** — drop-in `onClick` body for Chart.js datasets.
- **`useDisclosure(initial?)`** → `{ open, toggle, setOpen }`.
- **`useCopyToClipboard(resetMs?)`** → `{ copied, error, copy }`.
- **`useSelection<T>(initial?)`** → `{ selected, select, clear, isSelected }` (single, toggle-off).
- **`useEscape(handler)` / `useKey(key, handler)`** — window-level key handlers, auto-cleanup.

## Patterns

- **Mini ↔ full parity.** A panel's collapsed/mini rendering and its expanded rendering
  must read as the *same component, smaller* — same fonts (token-scaled), same colors, same
  dot sizes proportionally. Mini is not a different visual language. Dots/markers in mini
  charts: ~2–3px radius (milestones +1), never the chunky 4.5–6.5px of the old mini chart.
- **Panel headers** are all the eyebrow row. No panel invents its own title styling.
- **Tables**: sticky `--bg-elev` header, eyebrow column labels, hairline row separators in
  `--border-soft`, hover row → `--bg-hi`. No vertical gridlines.
- **Empty states** always use `<EmptyState>`.
- **Scrollbars**: thin, `--border` thumb on transparent track.

## What we are removing

- `linear-gradient` fills (chart area fills, `.dash-cell.hl`, topbar bg) → flat tints.
- `border: 1px solid` wrapping every panel → hairline separators / elevation.
- Hardcoded SVG `font-size="9"` / `"10"` → `var(--text-xs)` etc.
- Oversized mini-chart dots and axis text.
- One-off bespoke classes that duplicate a primitive — replace with the primitive.
