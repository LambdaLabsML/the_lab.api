/**
 * NavRail — the thin primary navigation rail (level 1 of the two-level left nav).
 * The ⬡ mark doubles as the connection-status indicator (color + hover tooltip).
 * Settings is a toggle (not a content section): it reveals the SettingsPanel in
 * the secondary panel without changing the center content.
 * SecondaryPanel — the resizable / collapsible level-2 panel next to the rail.
 */
import type { ComponentChildren } from "preact";
import { useState, useEffect } from "preact/hooks";
import { sidebarWidth, sidebarCollapsed } from "../state/settings";
import { wsConnected, wsAuthFailed, wsLastMessageAt } from "../state/ws";
import { Tooltip } from "./ui";

function relAgo(ms: number | null): string {
  if (!ms) return "—";
  const s = Math.max(0, Math.floor((Date.now() - ms) / 1000));
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  return `${Math.floor(m / 60)}h ago`;
}

export type NavSection = "review" | "activity" | "queue" | "workbench" | "tools";

interface RailItem { id: NavSection; label: string; icon: string }

const PRIMARY: RailItem[] = [
  { id: "review",    label: "Overview",  icon: "▦" },
  { id: "activity",  label: "Activity",  icon: "◉" },
  { id: "queue",     label: "Queue",     icon: "≡" },
  { id: "workbench", label: "Workbench", icon: "⊞" },
  { id: "tools",     label: "Tools",     icon: "⚒" },
];

export function NavRail({
  section,
  onSelect,
  settingsOpen,
  onToggleSettings,
}: {
  section: NavSection;
  onSelect: (s: NavSection) => void;
  settingsOpen: boolean;
  onToggleSettings: () => void;
}) {
  // tick every 6s so the "last update Xs ago" stat stays live while hovering
  const [, setTick] = useState(0);
  useEffect(() => {
    const t = window.setInterval(() => setTick((n) => n + 1), 6000);
    return () => window.clearInterval(t);
  }, []);

  const wsState = wsAuthFailed.value ? "auth" : wsConnected.value ? "on" : "off";
  const wsLabel = wsAuthFailed.value ? "auth failed" : wsConnected.value ? "connected" : "reconnecting…";
  const lastStr = relAgo(wsLastMessageAt.value);

  const item = (it: RailItem) => {
    const active = section === it.id && !settingsOpen;
    return (
      <button
        key={it.id}
        type="button"
        class={`nav-rail-btn${active ? " is-active" : ""}`}
        onClick={() => onSelect(it.id)}
        title={it.label}
        aria-current={active ? "page" : undefined}
      >
        <span class="nav-rail-icon" aria-hidden="true">{it.icon}</span>
        <span class="nav-rail-label">{it.label}</span>
      </button>
    );
  };

  return (
    <nav class="nav-rail" aria-label="Primary navigation">
      <Tooltip
        content={
          <>
            <span class="ui-tip-title">the_lab</span>
            <span class="ui-tip-row"><span>websocket</span><b class={`ws-word ws-word--${wsState}`}>{wsLabel}</b></span>
            <span class="ui-tip-row"><span>last update</span><b>{lastStr}</b></span>
          </>
        }
        placement="bottom"
      >
        <span
          class={`nav-rail-mark nav-rail-mark--${wsState}`}
          aria-label={`Connection: ${wsLabel}`}
        >⬡</span>
      </Tooltip>

      <div class="nav-rail-group">{PRIMARY.map(item)}</div>
      <div class="nav-rail-spacer" />
      <div class="nav-rail-group">
        <button
          type="button"
          class={`nav-rail-btn${settingsOpen ? " is-active" : ""}`}
          onClick={onToggleSettings}
          title="Settings"
          aria-pressed={settingsOpen}
        >
          <span class="nav-rail-icon" aria-hidden="true">⚙</span>
          <span class="nav-rail-label">Settings</span>
        </button>
      </div>
    </nav>
  );
}

/**
 * SecondaryPanel — wraps the contextual level-2 content. Drag the right edge to
 * resize (persisted), or collapse it to a thin re-open tab.
 */
export function SecondaryPanel({
  label,
  children,
}: {
  label: string;
  children: ComponentChildren;
}) {
  const collapsed = sidebarCollapsed.value;
  const width = sidebarWidth.value;

  if (collapsed) {
    return (
      <button
        class="nav-secondary-reopen"
        title="Show panel"
        aria-label="Show panel"
        onClick={() => { sidebarCollapsed.value = false; }}
      >
        ›
      </button>
    );
  }

  const startDrag = (e: PointerEvent) => {
    e.preventDefault();
    const startX = e.clientX;
    const startW = width;
    const move = (ev: PointerEvent) => {
      sidebarWidth.value = Math.max(190, Math.min(560, startW + (ev.clientX - startX)));
    };
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      document.body.style.cursor = "";
    };
    document.body.style.cursor = "col-resize";
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  };

  return (
    <aside class="nav-secondary" style={{ width: `${width}px` }} aria-label={label}>
      <div class="nav-secondary-inner">{children}</div>
      <button
        class="nav-secondary-collapse"
        title="Hide panel"
        aria-label="Hide panel"
        onClick={() => { sidebarCollapsed.value = true; }}
      >
        ‹
      </button>
      <div class="nav-secondary-resize" onPointerDown={startDrag} title="Drag to resize" />
    </aside>
  );
}
