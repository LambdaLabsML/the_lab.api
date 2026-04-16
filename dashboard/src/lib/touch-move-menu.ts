/**
 * Touch-friendly "Move panel" menu.
 *
 * Long-press (500ms) on a tab shows a floating menu with options:
 *   - Move to [group name] — moves panel to another existing group
 *   - Float — detaches as floating window
 *   - New group (right/below) — splits into a new group
 *
 * Works on both touch and mouse (right-click also triggers).
 */
import type { DockviewComponent } from "dockview-core";
import { sendToTray } from "../state/signals";

const LONG_PRESS_MS = 500;

let _dv: DockviewComponent | null = null;
let _pressTimer: ReturnType<typeof setTimeout> | null = null;
let _menuEl: HTMLElement | null = null;

export function initTouchMoveMenu(dv: DockviewComponent, container: HTMLElement) {
  _dv = dv;

  container.addEventListener("pointerdown", onPointerDown, { passive: true });
  container.addEventListener("pointerup", cancelPress, { passive: true });
  container.addEventListener("pointermove", cancelPress, { passive: true });
  container.addEventListener("pointercancel", cancelPress, { passive: true });
  container.addEventListener("contextmenu", onContextMenu);
}

function cancelPress() {
  if (_pressTimer) {
    clearTimeout(_pressTimer);
    _pressTimer = null;
  }
}

function dismissMenu() {
  if (_menuEl) {
    _menuEl.remove();
    _menuEl = null;
  }
  document.removeEventListener("pointerdown", onDismiss);
}

function onDismiss(e: PointerEvent) {
  if (_menuEl && !_menuEl.contains(e.target as Node)) {
    dismissMenu();
  }
}

function findTabElement(target: EventTarget | null): HTMLElement | null {
  let el = target as HTMLElement | null;
  while (el) {
    if (el.classList?.contains("dv-tab")) return el;
    el = el.parentElement;
  }
  return null;
}

function findPanelForTab(tabEl: HTMLElement): { panelId: string; groupId: string } | null {
  if (!_dv) return null;
  // The tab's data attribute or text content can identify it
  // Walk dockview panels to find which one matches this DOM element
  for (const panel of _dv.panels) {
    const group = panel.group;
    if (!group) continue;
    // Check if the tab element is within this group's DOM
    if (group.element.contains(tabEl)) {
      // Find which panel in this group matches
      for (const p of group.panels) {
        const tabTitle = tabEl.textContent?.trim();
        if (tabTitle && p.title === tabTitle) {
          return { panelId: p.id, groupId: group.id };
        }
      }
      // Fallback: use active panel
      if (group.activePanel) {
        return { panelId: group.activePanel.id, groupId: group.id };
      }
    }
  }
  return null;
}

function onPointerDown(e: PointerEvent) {
  const tabEl = findTabElement(e.target);
  if (!tabEl) return;

  cancelPress();
  _pressTimer = setTimeout(() => {
    _pressTimer = null;
    showMenu(tabEl, e.clientX, e.clientY);
  }, LONG_PRESS_MS);
}

function onContextMenu(e: MouseEvent) {
  const tabEl = findTabElement(e.target);
  if (!tabEl) return;
  e.preventDefault();
  showMenu(tabEl, e.clientX, e.clientY);
}

function showMenu(tabEl: HTMLElement, x: number, y: number) {
  dismissMenu();
  if (!_dv) return;

  const info = findPanelForTab(tabEl);
  if (!info) return;

  const menu = document.createElement("div");
  menu.className = "dv-move-menu";
  menu.style.left = Math.min(x, window.innerWidth - 200) + "px";
  menu.style.top = Math.min(y, window.innerHeight - 200) + "px";

  const title = document.createElement("div");
  title.className = "dv-move-menu-title";
  title.textContent = `Move: ${info.panelId}`;
  menu.appendChild(title);

  // List other groups
  for (const group of _dv.groups) {
    if (group.id === info.groupId) continue;
    const panels = group.panels.map((p) => p.title || p.id).join(", ");
    const label = panels ? `→ ${panels.slice(0, 30)}` : "→ (empty group)";
    const btn = document.createElement("div");
    btn.className = "dv-move-menu-item";
    btn.textContent = label;
    btn.addEventListener("click", () => {
      _dv!.moveGroupOrPanel({
        from: { groupId: info.groupId, panelId: info.panelId },
        to: { group, position: "center" },
      });
      dismissMenu();
    });
    menu.appendChild(btn);
  }

  // Float option
  const floatBtn = document.createElement("div");
  floatBtn.className = "dv-move-menu-item";
  floatBtn.textContent = "⬚ Float";
  floatBtn.addEventListener("click", () => {
    const panel = _dv!.panels.find((p) => p.id === info.panelId);
    if (panel) {
      const group = panel.group;
      if (group && group.api.location.type !== "floating") {
        _dv!.addFloatingGroup(group, {
          x: 60, y: 60,
          width: Math.min(500, window.innerWidth * 0.4),
          height: Math.min(400, window.innerHeight * 0.4),
        });
      }
    }
    dismissMenu();
  });
  menu.appendChild(floatBtn);

  // New group right
  const rightBtn = document.createElement("div");
  rightBtn.className = "dv-move-menu-item";
  rightBtn.textContent = "↔ Split right";
  rightBtn.addEventListener("click", () => {
    const panel = _dv!.panels.find((p) => p.id === info.panelId);
    if (panel) {
      const currentGroup = panel.group;
      _dv!.addPanel({
        id: info.panelId + "_tmp",
        component: "default",
        title: panel.title || info.panelId,
        position: { referenceGroup: currentGroup, direction: "right" },
      });
      // Move the panel to the new group, remove temp
      const newPanel = _dv!.panels.find((p) => p.id === info.panelId + "_tmp");
      if (newPanel) {
        _dv!.moveGroupOrPanel({
          from: { groupId: info.groupId, panelId: info.panelId },
          to: { group: newPanel.group, position: "center" },
        });
        _dv!.removePanel(newPanel);
      }
    }
    dismissMenu();
  });
  menu.appendChild(rightBtn);

  // New group below
  const belowBtn = document.createElement("div");
  belowBtn.className = "dv-move-menu-item";
  belowBtn.textContent = "↕ Split below";
  belowBtn.addEventListener("click", () => {
    const panel = _dv!.panels.find((p) => p.id === info.panelId);
    if (panel) {
      const currentGroup = panel.group;
      _dv!.addPanel({
        id: info.panelId + "_tmp",
        component: "default",
        title: panel.title || info.panelId,
        position: { referenceGroup: currentGroup, direction: "below" },
      });
      const newPanel = _dv!.panels.find((p) => p.id === info.panelId + "_tmp");
      if (newPanel) {
        _dv!.moveGroupOrPanel({
          from: { groupId: info.groupId, panelId: info.panelId },
          to: { group: newPanel.group, position: "center" },
        });
        _dv!.removePanel(newPanel);
      }
    }
    dismissMenu();
  });
  menu.appendChild(belowBtn);

  // Send to tray
  if (sendToTray) {
    const trayBtn = document.createElement("div");
    trayBtn.className = "dv-move-menu-item";
    trayBtn.textContent = "\u2913 Send to tray";
    trayBtn.addEventListener("click", () => {
      sendToTray!(info.panelId);
      dismissMenu();
    });
    menu.appendChild(trayBtn);
  }

  document.body.appendChild(menu);
  _menuEl = menu;

  // Dismiss on click outside (next tick to avoid immediate dismiss)
  setTimeout(() => document.addEventListener("pointerdown", onDismiss), 10);
}
