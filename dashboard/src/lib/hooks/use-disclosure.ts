/**
 * useDisclosure — open/closed toggle state. Replaces the many
 * `const [open, setOpen] = useState(false)` + `onClick={() => setOpen(!open)}`
 * pairs scattered across menus, lightboxes, JSON nodes, and column pickers.
 */
import { useState, useCallback } from "preact/hooks";

export function useDisclosure(initial = false) {
  const [open, setOpen] = useState(initial);
  const toggle = useCallback(() => setOpen((o) => !o), []);
  const onOpen = useCallback(() => setOpen(true), []);
  const onClose = useCallback(() => setOpen(false), []);
  return { open, setOpen, toggle, onOpen, onClose };
}
