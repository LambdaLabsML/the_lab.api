import { useEffect } from "preact/hooks";
import type { ComponentChildren } from "preact";

interface LightboxProps {
  title: string;
  onClose: () => void;
  children: ComponentChildren;
  /** Optional toolbar rendered to the right of the title */
  toolbar?: ComponentChildren;
}

export function Lightbox({ title, onClose, children, toolbar }: LightboxProps) {
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  function handleBackdrop(e: MouseEvent) {
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
        <div class="lightbox-body">
          {children}
        </div>
      </div>
    </div>
  );
}
