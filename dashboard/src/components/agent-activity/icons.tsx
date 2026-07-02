/**
 * Small 10×10 stroke icons for the activity rows — same visual language as the
 * Stepper chevrons (currentColor stroke 1.6, round caps, no fill), replacing
 * the tiny ⟳ / ↔ text glyphs.
 */

/** Lab/API call — a circular arrow (⟳). */
export function ApiIcon() {
  return (
    <svg class="aa-icon" width="10" height="10" viewBox="0 0 10 10" aria-hidden="true"
      fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round">
      <path d="M 8.5 5 A 3.5 3.5 0 1 1 5 1.5" />
      <polyline points="3.2,0.6 5,1.5 4.2,3.3" />
    </svg>
  );
}

/** Inter-agent message — a double-headed arrow (↔). */
export function MsgIcon() {
  return (
    <svg class="aa-icon" width="10" height="10" viewBox="0 0 10 10" aria-hidden="true"
      fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round">
      <line x1="1.6" y1="5" x2="8.4" y2="5" />
      <polyline points="3.4,2.8 1.2,5 3.4,7.2" />
      <polyline points="6.6,2.8 8.8,5 6.6,7.2" />
    </svg>
  );
}
