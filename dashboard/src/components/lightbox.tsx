import type { ComponentChildren } from "preact";
import { useState } from "preact/hooks";
import { useEscape } from "../lib/hooks";
import { IconButton } from "./ui";

interface LightboxProps {
  title: string;
  onClose: () => void;
  children: ComponentChildren;
  toolbar?: ComponentChildren;
  bodyRef?: { current: HTMLDivElement | null };
  onBodyScroll?: (e: UIEvent) => void;
  /** Extra class on .lightbox-body (e.g. "is-doc" for edge-to-edge iframes). */
  bodyClass?: string;
}

/** ⛶-style corners icon, matching the 10×10 chevron icons elsewhere. */
function FullscreenIcon({ exit = false }: { exit?: boolean }) {
  // Corners point outward to enter fullscreen, inward to exit.
  const d = exit
    ? "M4.5 1v3.5H1M8.5 1v3.5H12M4.5 11V7.5H1M8.5 11V7.5H12"
    : "M1 4V1h3M12 4V1H9M1 8v3h3M12 8v3H9";
  return (
    <svg width="10" height="10" viewBox="0 0 13 12" aria-hidden="true">
      <path d={d} fill="none" stroke="currentColor" stroke-width="1.4" />
    </svg>
  );
}

function CloseIcon() {
  return (
    <svg width="10" height="10" viewBox="0 0 12 12" aria-hidden="true">
      <path d="M2 2l8 8M10 2l-8 8" fill="none" stroke="currentColor" stroke-width="1.4" />
    </svg>
  );
}

export function Lightbox({ title, onClose, children, toolbar, bodyRef, onBodyScroll, bodyClass }: LightboxProps) {
  const [full, setFull] = useState(false);
  // Escape-to-close via the shared window-level handler (auto cleanup).
  useEscape(onClose);

  function handleBackdrop(e: MouseEvent) {
    // backdrop-click guard: only close when the click is on the backdrop itself
    if ((e.target as HTMLElement).classList.contains("lightbox-backdrop")) {
      onClose();
    }
  }

  return (
    <div class={`lightbox-backdrop${full ? " is-full" : ""}`} onClick={handleBackdrop}>
      <div class={`lightbox${full ? " is-full" : ""}`}>
        <div class="lightbox-header">
          <span class="lightbox-title">{title}</span>
          {toolbar && <span class="lightbox-toolbar">{toolbar}</span>}
          <span class="lightbox-actions">
            <IconButton
              active={full}
              title={full ? "Exit fullscreen" : "Fullscreen"}
              onClick={() => setFull((f) => !f)}
            >
              <FullscreenIcon exit={full} />
            </IconButton>
            <IconButton title="Close (Esc)" onClick={onClose}>
              <CloseIcon />
            </IconButton>
          </span>
        </div>
        <div
          class={`lightbox-body${bodyClass ? ` ${bodyClass}` : ""}`}
          ref={bodyRef}
          onScroll={onBodyScroll as any}
        >
          {children}
        </div>
      </div>
    </div>
  );
}
