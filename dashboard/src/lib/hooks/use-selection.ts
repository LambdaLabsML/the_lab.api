/**
 * useSelection — single-select with click-again-to-deselect, and a multi-select
 * Set variant. Replaces the ad-hoc `selected === x ? null : x` toggles in the
 * stats / prompts / api views and the column-visibility Set in the table.
 */
import { useState, useCallback } from "preact/hooks";

/** Single selection. `select(x)` toggles x off if already selected. */
export function useSelection<T>(initial: T | null = null) {
  const [selected, setSelected] = useState<T | null>(initial);
  const select = useCallback(
    (value: T) => setSelected((cur) => (cur === value ? null : value)),
    [],
  );
  const clear = useCallback(() => setSelected(null), []);
  const isSelected = useCallback((value: T) => selected === value, [selected]);
  return { selected, setSelected, select, clear, isSelected };
}

/** Multi-selection backed by a Set, with immutable updates. */
export function useMultiSelection<T>(initial: Iterable<T> = []) {
  const [set, setSet] = useState<Set<T>>(() => new Set(initial));
  const toggle = useCallback((value: T) => {
    setSet((cur) => {
      const next = new Set(cur);
      next.has(value) ? next.delete(value) : next.add(value);
      return next;
    });
  }, []);
  const has = useCallback((value: T) => set.has(value), [set]);
  const clear = useCallback(() => setSet(new Set()), []);
  return { set, toggle, has, clear, setSet };
}
