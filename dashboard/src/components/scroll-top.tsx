/**
 * ScrollTop — a back-to-top affordance for the scrolling content areas. Renders
 * a fixed button that appears once the nearest `.app-scroll` is scrolled down,
 * and smooth-scrolls it back to the top on click. Safe to render unconditionally;
 * it hides itself when there's no scroll container or scroll position is near top.
 */
import { useEffect, useRef, useState } from "preact/hooks";

export function ScrollTop() {
  const [show, setShow] = useState(false);
  const ref = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    const scroller = document.querySelector(".app-content .app-scroll") as HTMLElement | null;
    if (!scroller) { setShow(false); return; }
    const onScroll = () => setShow(scroller.scrollTop > 280);
    scroller.addEventListener("scroll", onScroll, { passive: true });
    onScroll();
    return () => scroller.removeEventListener("scroll", onScroll);
  });

  const toTop = () => {
    const scroller = document.querySelector(".app-content .app-scroll") as HTMLElement | null;
    scroller?.scrollTo({ top: 0, behavior: "smooth" });
  };

  return (
    <button
      ref={ref}
      class={`scroll-top${show ? " is-visible" : ""}`}
      title="Back to top"
      aria-label="Back to top"
      onClick={toTop}
    >
      ↑
    </button>
  );
}
