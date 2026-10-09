// Artifacts: a read-only explorer for the shared .the_lab/artifacts tree.
// Every agent writes there (their own subfolders by convention), so there is
// one tree; the sidebar filter narrows it to an agent or topic. The sidebar
// lists folders lazily; the main pane renders Markdown, images, HTML (sandboxed)
// and text, or lists a folder.
import { signal } from "@preact/signals";
import { useEffect, useMemo, useState } from "preact/hooks";
import { listArtifacts, artifactUrl, getArtifactText, writeArtifactFile, type ArtifactEntry } from "../state/api";
import { renderMarkdown } from "../components/detail-panel";
import { Eyebrow, EmptyState } from "../components/ui";
import { CollabEditor } from "../components/collab-editor";
import { collabActiveCounts } from "../state/collab";

/** Selected artifact path ("" = root), mirrored in the URL as #artifacts/<path>. */
export const artifactPath = signal<string>("");
/** Whether the selection is a folder (decided from its listing entry). */
const selectedIsDir = signal<boolean>(true);
/** Artifact path open in the live collab editor (null = viewing). */
export const editingPath = signal<string | null>(null);
/** Bumped after a save so the read-only view reloads the file. */
const fileRevision = signal(0);
// Text files the dashboard user may create / edit (mirrors the server list).
const EDIT_EXT = new Set(["md", "markdown", "txt", "json", "csv", "tsv", "yaml", "yml", "toml"]);

// Listings by folder path, shared by the tree and the folder view.
const listings = signal<Record<string, ArtifactEntry[] | "loading" | "error">>({});
async function loadListing(path: string, force = false): Promise<void> {
  const cur = listings.value[path];
  if (!force && cur && cur !== "error") return;
  listings.value = { ...listings.value, [path]: "loading" };
  try {
    const r = await listArtifacts(path);
    listings.value = { ...listings.value, [path]: r.entries };
  } catch {
    listings.value = { ...listings.value, [path]: "error" };
  }
}

const join = (dir: string, name: string) => (dir ? `${dir}/${name}` : name);
const parentOf = (path: string) => path.split("/").slice(0, -1).join("/");
const extOf = (path: string) => (path.match(/\.([a-z0-9]+)$/i)?.[1] || "").toLowerCase();
const IMAGE_EXT = new Set(["png", "jpg", "jpeg", "gif", "svg", "webp"]);
const TEXT_EXT = new Set(["txt", "log", "json", "jsonl", "py", "sh", "csv", "tsv", "yaml", "yml", "toml", "ts", "tsx", "js", "mermaid", "cfg", "ini", "out", "err", "diff", "patch"]);

function fmtSize(n: number | null): string {
  if (n == null) return "";
  if (n >= 1_048_576) return `${(n / 1_048_576).toFixed(1)} MB`;
  if (n >= 1024) return `${(n / 1024).toFixed(1)} kB`;
  return `${n} B`;
}
function fmtTime(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  return `${d.toLocaleDateString([], { month: "short", day: "numeric" })} ${d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false })}`;
}

export function openArtifact(path: string, isDir: boolean): void {
  if (editingPath.value && editingPath.value !== path) editingPath.value = null;
  selectedIsDir.value = isDir;
  artifactPath.value = path;
}

/** Select a file and open it directly in the live collab editor (used by the
 *  "Join" toast action and by "+ New file"). */
export function openArtifactForEdit(path: string): void {
  selectedIsDir.value = false;
  artifactPath.value = path;
  editingPath.value = path;
  // From another section (e.g. a toast on the Overview): switch to Artifacts.
  if (!location.hash.startsWith("#artifacts")) {
    location.hash = `#artifacts/${path.split("/").map(encodeURIComponent).join("/")}`;
  }
}

// ── Sidebar tree ─────────────────────────────────────────────────────────────

function TreeLevel({ dir, depth, filter, open, toggle }: {
  dir: string; depth: number; filter: string;
  open: Set<string>; toggle: (path: string) => void;
}) {
  const listing = listings.value[dir];
  if (listing === "loading" || listing === undefined) return <div class="art-tree-note" style={{ paddingLeft: `${12 + depth * 12}px` }}>loading…</div>;
  if (listing === "error") return <div class="art-tree-note is-error" style={{ paddingLeft: `${12 + depth * 12}px` }}>could not list</div>;
  const f = filter.trim().toLowerCase();
  // The filter applies to the top level (agent / topic folders) and to files
  // already loaded below it.
  const entries = f && depth === 0 ? listing.filter((e) => e.name.toLowerCase().includes(f)) : listing;
  const selected = artifactPath.value;
  return (
    <>
      {entries.map((e) => {
        const path = join(dir, e.name);
        const isOpen = open.has(path);
        return (
          <div key={path}>
            <button
              type="button"
              class={`art-tree-row${selected === path ? " is-active" : ""}${e.external ? " is-external" : ""}`}
              style={{ paddingLeft: `${12 + depth * 12}px` }}
              disabled={e.external}
              title={e.external ? `${e.name} links outside the artifacts folder` : `${path}${e.kind === "file" ? ` · ${fmtSize(e.size)}` : ""}`}
              onClick={() => {
                if (e.kind === "dir") toggle(path);
                openArtifact(path, e.kind === "dir");
              }}
            >
              <span class="art-tree-icon">{e.kind === "dir" ? (isOpen ? "▾" : "▸") : extOf(e.name) === "md" ? "≡" : "·"}</span>
              <span class="art-tree-name">{e.name}</span>
              {e.kind === "file" && collabActiveCounts.value[path] > 0 && (
                <span class="art-collab-badge" title={`${collabActiveCounts.value[path]} editing now`}>
                  ● {collabActiveCounts.value[path]}
                </span>
              )}
            </button>
            {e.kind === "dir" && isOpen && (
              <TreeLevel dir={path} depth={depth + 1} filter="" open={open} toggle={toggle} />
            )}
          </div>
        );
      })}
      {f && depth === 0 && entries.length === 0 && <div class="art-tree-note">no match</div>}
    </>
  );
}

export function ArtifactsSidebar() {
  const [filter, setFilter] = useState("");
  const [open, setOpen] = useState<Set<string>>(() => {
    // Open the folders on the way to a deep-linked artifact.
    const parts = artifactPath.value.split("/").filter(Boolean);
    // A deep link to a file opens its folders, not the file itself.
    if (parts.length && /\.[a-z0-9]{1,8}$/i.test(parts[parts.length - 1])) parts.pop();
    return new Set(parts.map((_, i) => parts.slice(0, i + 1).join("/")));
  });
  useEffect(() => { void loadListing(""); }, []);
  useEffect(() => { for (const p of open) void loadListing(p); }, [open]);
  const toggle = (path: string) => setOpen((cur) => {
    const next = new Set(cur);
    if (next.has(path)) next.delete(path); else next.add(path);
    return next;
  });
  return (
    <div class="art-sidebar">
      <div class="nav-secondary-head"><Eyebrow>.the_lab/artifacts</Eyebrow></div>
      <div class="art-filter">
        <input
          class="art-filter-input"
          value={filter}
          placeholder="filter folders (agent, topic)…"
          onInput={(e) => setFilter((e.target as HTMLInputElement).value)}
        />
        <button type="button" class="art-refresh" title="Reload the listings" onClick={() => {
          void loadListing("", true);
          for (const p of open) void loadListing(p, true);
        }}>↺</button>
      </div>
      <div class="art-tree">
        <TreeLevel dir="" depth={0} filter={filter} open={open} toggle={toggle} />
      </div>
    </div>
  );
}

// ── Main pane ────────────────────────────────────────────────────────────────

function Breadcrumb({ path, isDir }: { path: string; isDir: boolean }) {
  const parts = path.split("/").filter(Boolean);
  return (
    <div class="art-crumbs">
      <button type="button" class="art-crumb" onClick={() => openArtifact("", true)}>artifacts</button>
      {parts.map((p, i) => {
        const sub = parts.slice(0, i + 1).join("/");
        const last = i === parts.length - 1;
        return (
          <span key={sub}>
            <span class="art-crumb-sep">/</span>
            {last
              ? <span class="art-crumb is-current">{p}</span>
              : <button type="button" class="art-crumb" onClick={() => openArtifact(sub, true)}>{p}</button>}
          </span>
        );
      })}
      <span class="art-actions">
        {path && !isDir && EDIT_EXT.has(extOf(path)) && editingPath.value !== path && (
          <button type="button" class="art-btn" onClick={() => { editingPath.value = path; }} title="Edit this file">✎ Edit</button>
        )}
        <NewFile folder={isDir ? path : parentOf(path)} />
        {path && !isDir && (
          <a class="art-raw" href={artifactUrl(path)} target="_blank" rel="noopener noreferrer">raw ↗</a>
        )}
      </span>
    </div>
  );
}

/** "+ New file" in the current folder: a name, then the file opens in the editor. */
function NewFile({ folder }: { folder: string }) {
  const [name, setName] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const create = async () => {
    let n = (name || "").trim().replace(/^\/+/, "");
    if (!n) return;
    if (!/\.[a-z0-9]+$/i.test(n)) n += ".md";
    if (!EDIT_EXT.has(extOf(n))) { setError(`use one of: ${[...EDIT_EXT].map((e) => "." + e).join(" ")}`); return; }
    const path = join(folder, n);
    const title = n.replace(/\.[a-z0-9]+$/i, "").split("/").pop()!.replace(/[-_]+/g, " ");
    const content = extOf(n) === "md" ? `# ${title}\n\n` : "";
    try {
      const r = await writeArtifactFile(path, content, { create: true });
      if (r.conflict) { setError("a file with that name exists"); return; }
      setName(null); setError(null);
      void loadListing(parentOf(path), true);
      openArtifactForEdit(path);
    } catch (e) {
      setError(String((e as Error).message || e));
    }
  };
  if (name === null) {
    return <button type="button" class="art-btn" onClick={() => { setName(""); setError(null); }} title={`Create a file in ${folder || "artifacts"}/`}>+ New file</button>;
  }
  return (
    <span class="art-new">
      <span class="art-new-dir">{folder ? `${folder}/` : ""}</span>
      <input
        class="art-new-input"
        autoFocus
        value={name}
        placeholder="name.md (folders: notes/plan.md)"
        onInput={(e) => setName((e.target as HTMLInputElement).value)}
        onKeyDown={(e) => { if (e.key === "Enter") void create(); if (e.key === "Escape") { setName(null); setError(null); } }}
      />
      <button type="button" class="art-btn" onClick={() => void create()}>Create</button>
      <button type="button" class="art-btn is-quiet" onClick={() => { setName(null); setError(null); }}>Cancel</button>
      {error && <span class="art-new-error">{error}</span>}
    </span>
  );
}

// ── Editor ───────────────────────────────────────────────────────────────────
// Live collaborative editor (see ../components/collab-editor + ../state/collab):
// edits are synced over a websocket (OT), saved server-side on a debounce, and
// never lost to a "someone else changed it" conflict — there's no REST
// save/draft/conflict path any more.

function ArtifactEditor({ path }: { path: string }) {
  return (
    <CollabEditor
      key={path}
      path={path}
      onClose={() => {
        editingPath.value = null;
        fileRevision.value += 1; // pick up the latest saved content in the read-only view
      }}
    />
  );
}

function FolderView({ path }: { path: string }) {
  useEffect(() => { void loadListing(path); }, [path]);
  const listing = listings.value[path];
  if (listing === undefined || listing === "loading") return <div class="art-note">Loading…</div>;
  if (listing === "error") return <div class="art-note is-error">Could not list this folder.</div>;
  if (!listing.length) return <EmptyState icon="▤" title="Empty folder" body="Nothing in here yet." />;
  return (
    <table class="art-table">
      <thead><tr><th>name</th><th class="num">size</th><th>modified</th></tr></thead>
      <tbody>
        {listing.map((e) => (
          <tr key={e.name}>
            <td>
              <button type="button" class="art-link" disabled={e.external} onClick={() => openArtifact(join(path, e.name), e.kind === "dir")}>
                {e.kind === "dir" ? "▸ " : ""}{e.name}{e.external ? " (external link)" : ""}
              </button>
            </td>
            <td class="num">{fmtSize(e.size)}</td>
            <td>{fmtTime(e.mtime)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function FileView({ path }: { path: string }) {
  const ext = extOf(path);
  const [state, setState] = useState<{ text: string; truncated: boolean } | "loading" | { error: string }>("loading");
  const isText = ext === "md" || TEXT_EXT.has(ext) || !ext;
  useEffect(() => {
    if (!isText) return;
    let cancelled = false;
    setState("loading");
    getArtifactText(path)
      .then((r) => { if (!cancelled) setState(r); })
      .catch((e) => { if (!cancelled) setState({ error: String(e?.message || e) }); });
    return () => { cancelled = true; };
  }, [path]);
  const dir = parentOf(path);
  const html = useMemo(
    () => (ext === "md" && typeof state === "object" && "text" in state
      ? renderMarkdown(state.text, `.the_lab/artifacts${dir ? `/${dir}` : ""}`)
      : ""),
    [state, ext, dir],
  );

  if (IMAGE_EXT.has(ext)) return <div class="art-image"><img src={artifactUrl(path)} alt={path} /></div>;
  if (ext === "html" || ext === "htm") {
    // Agent-written HTML: shown sandboxed (no scripts, no same-origin access).
    return <iframe class="art-iframe" src={artifactUrl(path)} sandbox="" title={path} />;
  }
  if (!isText) {
    return <div class="art-note">No preview for .{ext} files. <a href={artifactUrl(path)} target="_blank" rel="noopener noreferrer">Open raw ↗</a></div>;
  }
  if (state === "loading") return <div class="art-note">Loading…</div>;
  if ("error" in state) return <div class="art-note is-error">{state.error}</div>;
  const cut = state.truncated && <div class="art-note">Showing the first 2 MB. <a href={artifactUrl(path)} target="_blank" rel="noopener noreferrer">Open raw ↗</a></div>;
  if (ext === "md") {
    return (
      <>
        {cut}
        <div
          class="md-output art-md"
          dangerouslySetInnerHTML={{ __html: html }}
          onClick={(e) => {
            // Links to other artifacts open here instead of a new tab.
            const a = (e.target as HTMLElement).closest("a");
            const href = a?.getAttribute("href") || "";
            const prefix = "/api/v1/files/.the_lab/artifacts/";
            if (!href.startsWith(prefix)) return;
            e.preventDefault();
            const target = decodeURIComponent(href.slice(prefix.length).split("#")[0]).replace(/\/+$/, "");
            const parts: string[] = [];
            for (const seg of target.split("/")) {
              if (seg === "..") parts.pop(); else if (seg && seg !== ".") parts.push(seg);
            }
            openArtifact(parts.join("/"), href.endsWith("/"));
          }}
        />
      </>
    );
  }
  return <>{cut}<pre class="art-pre">{state.text}</pre></>;
}

/** Folder or file? From the parent's listing once loaded, else by extension. */
function isDirPath(path: string): boolean {
  if (!path) return true;
  const listing = listings.value[parentOf(path)];
  const name = path.split("/").pop() || "";
  if (Array.isArray(listing)) {
    const entry = listing.find((e) => e.name === name);
    if (entry) return entry.kind === "dir";
  }
  return selectedIsDir.value && !/\.[a-z0-9]{1,8}$/i.test(name);
}

export function ArtifactsView() {
  const path = artifactPath.value;
  useEffect(() => { if (path) void loadListing(parentOf(path)); }, [path]);
  const isDir = isDirPath(path);
  return (
    <main class="artifacts-page">
      <Breadcrumb path={path} isDir={isDir} />
      <div class="art-content">
        {isDir ? <FolderView path={path} />
          : editingPath.value === path ? <ArtifactEditor path={path} />
          : <FileView key={`${path}@${fileRevision.value}`} path={path} />}
      </div>
    </main>
  );
}
