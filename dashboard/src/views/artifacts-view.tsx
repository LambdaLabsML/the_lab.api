// Artifacts: a read-only explorer for the shared .the_lab/artifacts tree.
// Every agent writes there (their own subfolders by convention), so there is
// one tree; the sidebar filter narrows it to an agent or topic. The sidebar
// lists folders lazily; the main pane renders Markdown, images, HTML (sandboxed)
// and text, or lists a folder.
import { signal } from "@preact/signals";
import { useEffect, useMemo, useState } from "preact/hooks";
import { listArtifacts, artifactUrl, getArtifactText, type ArtifactEntry } from "../state/api";
import { renderMarkdown } from "../components/detail-panel";
import { Eyebrow, EmptyState } from "../components/ui";

/** Selected artifact path ("" = root), mirrored in the URL as #artifacts/<path>. */
export const artifactPath = signal<string>("");
/** Whether the selection is a folder (decided from its listing entry). */
const selectedIsDir = signal<boolean>(true);

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
  selectedIsDir.value = isDir;
  artifactPath.value = path;
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

function Breadcrumb({ path }: { path: string }) {
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
      {path && !selectedIsDir.value && (
        <a class="art-raw" href={artifactUrl(path)} target="_blank" rel="noopener noreferrer">raw ↗</a>
      )}
    </div>
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

export function ArtifactsView() {
  const path = artifactPath.value;
  // A deep link to a folder or file: decide by its parent's listing.
  useEffect(() => {
    if (!path) { selectedIsDir.value = true; return; }
    const parent = parentOf(path);
    void loadListing(parent).then(() => {
      const listing = listings.value[parent];
      const name = path.split("/").pop();
      if (Array.isArray(listing)) {
        const entry = listing.find((e) => e.name === name);
        if (entry) selectedIsDir.value = entry.kind === "dir";
      }
    });
  }, [path]);
  return (
    <main class="artifacts-page">
      <Breadcrumb path={path} />
      <div class="art-content">
        {selectedIsDir.value ? <FolderView path={path} /> : <FileView key={path} path={path} />}
      </div>
    </main>
  );
}
