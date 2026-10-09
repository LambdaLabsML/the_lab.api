// ------------------------------------------------------------
// Human (non-agent) message identities — the interim stand-in for real
// accounts (see the_lab/messages.py). The catch-all "user" reaches every
// person using the dashboard; a named "user:<name>" reaches one personally.
// Mirrors the server's messages.is_human / messages.human_id.
// ------------------------------------------------------------

const HUMAN_RE = /^(?:agent:)?user(?::([a-z0-9][a-z0-9_-]{0,31}))?$/;

/** True for "user", the legacy "agent:user" alias, or a named "user:<name>". */
export function isHuman(id: string | null | undefined): boolean {
  return !!id && HUMAN_RE.test(id);
}

/** The slug of a named human id ("user:david" -> "david"); null for the
 *  catch-all "user" / "agent:user", or a non-human id. */
export function humanSlug(id: string | null | undefined): string | null {
  const m = id ? HUMAN_RE.exec(id) : null;
  return m ? m[1] ?? null : null;
}

/** Display label for a human id: the slug, title-cased, or "user" for the
 *  catch-all. Not meaningful for a non-human id (callers check isHuman first). */
export function humanDisplayName(id: string | null | undefined): string {
  const slug = humanSlug(id);
  if (!slug) return "user";
  return slug.replace(/[_-]+/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

/** A display name ("David") turned into the slug the server expects
 *  ([a-z0-9][a-z0-9_-]{0,31}); "" if nothing usable survives. */
export function nameToSlug(name: string): string {
  return (name || "")
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, "-")
    .replace(/^[-_]+/, "")
    .replace(/-+$/, "")
    .slice(0, 32);
}

/** The full "user:<name>" id for a display name, or null if blank. */
export function humanIdForName(name: string): string | null {
  const slug = nameToSlug(name);
  return slug ? `user:${slug}` : null;
}
