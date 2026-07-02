/**
 * Small 10×10 stroke icons for the activity rows — same visual language as the
 * Stepper chevrons (currentColor stroke, round caps, no fill). Deliberately
 * minimal: a call is a prompt-chevron, a message is a sent-arrow.
 */

/** Lab/API call — a single prompt chevron (›). */
export function ApiIcon() {
  return (
    <svg class="aa-icon" width="10" height="10" viewBox="0 0 10 10" aria-hidden="true"
      fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
      <polyline points="3.5,2 6.5,5 3.5,8" />
    </svg>
  );
}

/** Inter-agent message — a sent arrow (→); the pill names the recipient. */
export function MsgIcon() {
  return (
    <svg class="aa-icon" width="10" height="10" viewBox="0 0 10 10" aria-hidden="true"
      fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
      <line x1="1.5" y1="5" x2="8" y2="5" />
      <polyline points="5.5,2.5 8.5,5 5.5,7.5" />
    </svg>
  );
}
