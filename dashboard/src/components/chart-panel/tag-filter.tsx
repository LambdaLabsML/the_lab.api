import { useState } from "preact/hooks";
import { activeTagFilters, tagFilterMode, filterText } from "../../state/settings";
import { allExperiments } from "../../state/signals";
import { renameTag } from "../../state/api";
import { refreshChartData } from "../../state/polling";

export function TagFilter() {
  const experiments = allExperiments.value;
  const active = activeTagFilters.value;
  const mode = tagFilterMode.value;

  // Collect all unique tags
  const tagSet = new Set<string>();
  for (const exp of experiments) {
    if (exp.tags) for (const t of exp.tags) tagSet.add(t);
  }
  const filter = filterText.value.toLowerCase().trim();
  const filterTerms = filter ? filter.split(/\s+/) : [];
  const tags = [...tagSet].sort().filter((t) =>
    filterTerms.length === 0 || filterTerms.some((term) => t.toLowerCase().includes(term))
  );

  function toggle(tag: string) {
    const current = new Set(activeTagFilters.value);
    if (current.has(tag)) current.delete(tag);
    else current.add(tag);
    activeTagFilters.value = [...current];
  }

  return (
    <span
      style={{
        display: "inline-flex",
        gap: "4px",
        alignItems: "center",
        marginLeft: "12px",
        flexWrap: "wrap",
      }}
    >
      Tags:
      {tags.length > 0 && active.length > 1 && (
        <span
          class="tag-toggle active"
          style={{ fontSize: "9px", padding: "1px 6px", cursor: "pointer" }}
          onClick={() => { tagFilterMode.value = mode === "and" ? "or" : "and"; }}
          title={mode === "and" ? "AND: experiments must have all selected tags" : "OR: experiments must have any selected tag"}
        >
          {mode.toUpperCase()}
        </span>
      )}
      {tags.length === 0 && (
        <span style={{ color: "#484f58", fontSize: "11px" }}>none</span>
      )}
      {tags.map((tag) => (
        <TagPill
          key={tag}
          tag={tag}
          isActive={active.includes(tag)}
          onToggle={() => toggle(tag)}
        />
      ))}
    </span>
  );
}

function TagPill({
  tag,
  isActive,
  onToggle,
}: {
  tag: string;
  isActive: boolean;
  onToggle: () => void;
}) {
  const [renaming, setRenaming] = useState(false);
  const [value, setValue] = useState(tag);

  function handleContextMenu(e: MouseEvent) {
    e.preventDefault();
    setValue(tag);
    setRenaming(true);
  }

  async function commitRename() {
    setRenaming(false);
    const newTag = value.trim();
    if (!newTag || newTag === tag) return;
    await renameTag(tag, newTag);
    // Update active filters if the old tag was active
    const current = new Set(activeTagFilters.value);
    if (current.has(tag)) {
      current.delete(tag);
      current.add(newTag);
      activeTagFilters.value = [...current];
    }
    refreshChartData();
  }

  function handleKeyDown(e: KeyboardEvent) {
    if (e.key === "Enter") {
      e.preventDefault();
      (e.target as HTMLInputElement).blur();
    }
    if (e.key === "Escape") {
      setRenaming(false);
    }
  }

  if (renaming) {
    return (
      <input
        class="tag-rename-input"
        value={value}
        onInput={(e) => setValue((e.target as HTMLInputElement).value)}
        onBlur={commitRename}
        onKeyDown={handleKeyDown}
        autoFocus
      />
    );
  }

  return (
    <span
      class={`tag-toggle${isActive ? " active" : ""}`}
      onClick={onToggle}
      onContextMenu={handleContextMenu}
    >
      {tag}
    </span>
  );
}
