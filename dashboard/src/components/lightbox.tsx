import type { ComponentChildren } from "preact";
import { useEscape } from "../lib/hooks";

interface LightboxProps {
  title: string;
  onClose: () => void;
  children: ComponentChildren;
  toolbar?: ComponentChildren;
  bodyRef?: { current: HTMLDivElement | null };
  onBodyScroll?: (e: UIEvent) => void;
}

export function Lightbox({ title, onClose, children, toolbar, bodyRef, onBodyScroll }: LightboxProps) {
  // Escape-to-close via the shared window-level handler (auto cleanup).
  useEscape(onClose);

  function handleBackdrop(e: MouseEvent) {
    // backdrop-click guard: only close when the click is on the backdrop itself
    if ((e.target as HTMLElement).classList.contains("lightbox-backdrop")) {
      onClose();
    }
  }

  return (
    <div class="lightbox-backdrop" onClick={handleBackdrop}>
      <div class="lightbox">
        <div class="lightbox-header">
          <span class="lightbox-title">{title}</span>
          {toolbar && <span class="lightbox-toolbar">{toolbar}</span>}
          <span class="lightbox-close" onClick={onClose}>&times;</span>
        </div>
        <div class="lightbox-body" ref={bodyRef} onScroll={onBodyScroll as any}>
          {children}
        </div>
      </div>
    </div>
  );
}
