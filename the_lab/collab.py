"""Live collaborative editing of artifacts: OT core + document manager.

Model: Overleaf / ShareJS. Each artifact file gets one in-memory document
(``CollabDoc``) while at least one dashboard tab is connected to it. Clients
submit ops against a server-assigned version; the server transforms a late
op against the history it missed, applies it, and broadcasts the transformed
result to everyone else. The document is periodically (debounced) flushed to
disk, and disk is polled for external changes (an agent editing the same
file) which get merged in the same way, as ops from a synthetic "disk"
client.

Positions are UTF-16 code units (JavaScript string indices): the server
keeps its text in "u16 form", where every astral character is split into its
two surrogate code points, so a Python index into that string equals a JS
index into the real string. ``to_u16``/``from_u16`` convert at the two
boundaries that matter: text arriving from a client (already u16 form, since
the client is JS) needs no conversion; text read from/written to disk (real
UTF-8 text) is converted with ``to_u16``/``from_u16``. Text sent back out to
clients is the u16-form string, encoded over the wire by
``json.dumps(..., ensure_ascii=True)`` — which escapes each lone surrogate as
its own ``\\uXXXX``, and a JS ``JSON.parse`` recombines an adjacent high/low
pair into the one character it always was.

Transform is a straight port of ShareJS's ``text0`` type — see
``transform_component``/``transform_x`` below; don't "improve" the algorithm,
only the plumbing around it.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class OTError(Exception):
    """A malformed op, a stale/unknown base version, or an unusable path.

    Raised by the pure OT functions and by the document manager; the
    WebSocket handler turns one into an ``{"type":"error",...}`` frame.
    """


# ─────────────────────────────────────────────────────────────────────────
# UTF-16 <-> Python str
# ─────────────────────────────────────────────────────────────────────────

def to_u16(s: str) -> str:
    """Split every astral character (code point > 0xFFFF) in *s* into its
    two surrogate code units, so each Python index counts one JS/UTF-16
    code unit (matching positions the dashboard sends over the wire)."""
    if not any(ord(ch) > 0xFFFF for ch in s):
        return s
    out: list[str] = []
    for ch in s:
        cp = ord(ch)
        if cp > 0xFFFF:
            cp -= 0x10000
            out.append(chr(0xD800 + (cp >> 10)))
            out.append(chr(0xDC00 + (cp & 0x3FF)))
        else:
            out.append(ch)
    return "".join(out)


def from_u16(s: str) -> str:
    """Inverse of ``to_u16``: recombine surrogate pairs back into a single
    astral character. Used only when writing text to disk (real UTF-8)."""
    return s.encode("utf-16-le", "surrogatepass").decode("utf-16-le")


# ─────────────────────────────────────────────────────────────────────────
# OT core — ShareJS text0, ported
# ─────────────────────────────────────────────────────────────────────────

def apply_op(text: str, op: list[dict]) -> str:
    """Apply *op* (a list of insert/delete components, applied in order,
    each relative to the text as modified by the ones before it) to *text*.

    Raises ``OTError`` if a component is out of range or a delete doesn't
    match what's actually in the document — *text* is untouched on error.
    """
    for c in op:
        p = c["p"]
        if "i" in c:
            ins = c["i"]
            if p < 0 or p > len(text):
                raise OTError("insert position out of range")
            text = text[:p] + ins + text[p:]
        elif "d" in c:
            d = c["d"]
            if p < 0 or p + len(d) > len(text):
                raise OTError("delete position out of range")
            if text[p:p + len(d)] != d:
                raise OTError("delete does not match the document")
            text = text[:p] + text[p + len(d):]
        else:
            raise OTError("invalid op component")
    return text


def _transform_position(pos: int, c: dict, insert_after: bool = False) -> int:
    if "i" in c:
        if c["p"] < pos or (c["p"] == pos and insert_after):
            return pos + len(c["i"])
        return pos
    d_len = len(c["d"])
    if pos <= c["p"]:
        return pos
    if pos <= c["p"] + d_len:
        return c["p"]
    return pos - d_len


def _append(dest: list[dict], c: dict) -> None:
    """ShareJS text0's ``append``: drop no-ops, merge with the previous
    component when adjacent and of the same kind, else push."""
    if c.get("i") == "" or c.get("d") == "":
        return
    if not dest:
        dest.append(c)
        return
    last = dest[-1]
    if "i" in c and "i" in last and last["p"] <= c["p"] <= last["p"] + len(last["i"]):
        offset = c["p"] - last["p"]
        dest[-1] = {"i": last["i"][:offset] + c["i"] + last["i"][offset:], "p": last["p"]}
    elif "d" in c and "d" in last and c["p"] <= last["p"] <= c["p"] + len(c["d"]):
        offset = last["p"] - c["p"]
        dest[-1] = {"d": c["d"][:offset] + last["d"] + c["d"][offset:], "p": c["p"]}
    else:
        dest.append(c)


def transform_component(dest: list[dict], c: dict, other_c: dict, side: str) -> list[dict]:
    """Transform a single component *c* against a single concurrent
    component *other_c*, appending the (possibly split) result to *dest*.
    *side* is ``'left'`` or ``'right'`` (which op "wins" a tie at the same
    position — only matters for insert vs insert)."""
    if "i" in c:
        _append(dest, {"i": c["i"], "p": _transform_position(c["p"], other_c, side == "right")})
    elif "i" in other_c:
        # delete vs insert: split the delete around the inserted text.
        s = c["d"]
        p = c["p"]
        if p < other_c["p"]:
            _append(dest, {"d": s[:other_c["p"] - p], "p": p})
            s = s[other_c["p"] - p:]
        if s:
            _append(dest, {"d": s, "p": c["p"] + len(other_c["i"])})
    else:
        # delete vs delete
        od, cd = other_c["d"], c["d"]
        if c["p"] >= other_c["p"] + len(od):
            _append(dest, {"d": cd, "p": c["p"] - len(od)})
        elif c["p"] + len(cd) <= other_c["p"]:
            _append(dest, c)
        else:
            new_d = ""
            if c["p"] < other_c["p"]:
                new_d = cd[:other_c["p"] - c["p"]]
            if c["p"] + len(cd) > other_c["p"] + len(od):
                new_d += cd[other_c["p"] + len(od) - c["p"]:]
            overlap_start = max(c["p"], other_c["p"])
            overlap_end = min(c["p"] + len(cd), other_c["p"] + len(od))
            if cd[overlap_start - c["p"]:overlap_end - c["p"]] != od[overlap_start - other_c["p"]:overlap_end - other_c["p"]]:
                raise OTError("conflicting deletes")
            if new_d:
                new_p = _transform_position(c["p"], other_c)
                _append(dest, {"d": new_d, "p": new_p})
    return dest


def transform_x(left_op: list[dict], right_op: list[dict]) -> tuple[list[dict], list[dict]]:
    """ShareJS's ``bootstrapTransform``: transform two concurrent ops (each
    a list of components) against each other, returning
    ``(left_op', right_op')``."""
    new_right: list[dict] = []
    for rc in right_op:
        new_left: list[dict] = []
        k = 0
        this_rc: dict | None = rc
        while k < len(left_op):
            next_c: list[dict] = []
            transform_component(new_left, left_op[k], this_rc, "left")
            transform_component(next_c, this_rc, left_op[k], "right")
            k += 1
            if len(next_c) == 1:
                this_rc = next_c[0]
            elif len(next_c) == 0:
                for c in left_op[k:]:
                    _append(new_left, c)
                this_rc = None
                break
            else:
                l2, r2 = transform_x(left_op[k:], next_c)
                for c in l2:
                    _append(new_left, c)
                for c in r2:
                    _append(new_right, c)
                this_rc = None
                break
        if this_rc is not None:
            _append(new_right, this_rc)
        left_op = new_left
    return left_op, new_right


def transform(op: list[dict], other: list[dict], side: str) -> list[dict]:
    """Transform *op* against a concurrent *other* op. *side* says which
    side *op* is on ('left' or 'right') — the two sides disagree only when
    an insert in *op* and an insert in *other* land at the same position."""
    if side == "left":
        return transform_x(op, other)[0]
    if side == "right":
        return transform_x(other, op)[1]
    raise ValueError(f"side must be 'left' or 'right', got {side!r}")


def content_hash(text: str) -> str:
    """sha1 of the git blob of *text* (UTF-8) — the same integrity check
    Overleaf uses, computed on the real text (not u16 form)."""
    data = text.encode("utf-8")
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


# ─────────────────────────────────────────────────────────────────────────
# Document manager
# ─────────────────────────────────────────────────────────────────────────

PALETTE = ["#58a6ff", "#f0883e", "#56d364", "#f778ba", "#39c5cf", "#d2a8ff",
           "#ff7b72", "#a5d64c", "#79c0ff", "#ffa198", "#7ee787", "#b392f0"]

HISTORY_LIMIT = 2000
FLUSH_DEBOUNCE_SECONDS = 0.8
POLL_INTERVAL_SECONDS = 2.0
UNLOAD_GRACE_SECONDS = 30.0


@dataclass
class Client:
    """One connected collaborator on a document."""

    id: str
    name: str
    color: str
    websocket: Any
    anchor: int | None = None
    head: int | None = None

    def as_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "color": self.color,
                "anchor": self.anchor, "head": self.head}


class CollabDoc:
    """In-memory collaborative state for one artifact file (text in u16 form)."""

    def __init__(self, path: str, text: str, version: int = 0):
        self.path = path
        self.text = text
        self.version = version
        # One entry per applied op; history[0] was applied at version - len(history).
        self.history: list[dict] = []
        self.clients: dict[str, Client] = {}
        self.flushed_text = text
        self.flushed_version = version
        self.disk_hash: str = ""
        self.disk_stat: tuple[float, int] | None = None
        self.pending_names: set[str] = set()
        self.flush_handle: asyncio.Task | None = None
        self.poll_task: asyncio.Task | None = None
        self.unload_task: asyncio.Task | None = None
        # Serializes flushes (debounced, after a disk merge, on unload).
        self.flush_lock = asyncio.Lock()

    def clients_list(self) -> list[dict]:
        return [c.as_dict() for c in self.clients.values()]


# Keyed by path relative to the artifacts root (posix form).
docs: dict[str, CollabDoc] = {}


def _repo_experiments():
    # Imported at call time (not module scope) so tests can patch
    # ``the_lab.routes.experiments.REPO_DIR`` (and/or ``the_lab.deps.REPO_DIR``,
    # which it was bound from at import) after this module is already loaded.
    from .routes import experiments
    return experiments


def _max_bytes() -> int:
    return _repo_experiments().ARTIFACT_EDIT_MAX_BYTES


def pick_color(doc: CollabDoc) -> str:
    """First palette colour not already used by another client in *doc*."""
    used = {c.color for c in doc.clients.values()}
    for color in PALETTE:
        if color not in used:
            return color
    return PALETTE[len(doc.clients) % len(PALETTE)]


def _resolve_artifact(rel_path: str) -> Path:
    """Resolve *rel_path* to an existing, editable artifact file, or raise
    ``OTError`` with a client-facing message (HTTPException has no meaning
    inside a WebSocket handshake, so every failure mode becomes this one
    exception type)."""
    from fastapi import HTTPException
    experiments = _repo_experiments()
    try:
        target = experiments._artifact_target(rel_path)
    except HTTPException as exc:
        raise OTError(str(exc.detail)) from exc
    if not target.is_file():
        raise OTError("file not found")
    if target.suffix.lower() not in experiments.ARTIFACT_EDIT_EXT:
        raise OTError("file is not editable")
    try:
        size = target.stat().st_size
    except OSError as exc:
        raise OTError(f"could not read file: {exc}") from exc
    if size > experiments.ARTIFACT_EDIT_MAX_BYTES:
        raise OTError("file too large to edit")
    return target


async def get_doc(rel_path: str, target: Path) -> CollabDoc:
    """Return the in-memory doc for *rel_path*, loading it from disk the
    first time anyone joins. Cancels a pending unload (a rejoin during the
    grace period reuses the same in-memory doc, e.g. a page reload)."""
    doc = docs.get(rel_path)
    if doc is not None:
        if doc.unload_task is not None:
            doc.unload_task.cancel()
            doc.unload_task = None
        return doc

    def _read() -> tuple[bytes, tuple[float, int]]:
        data = target.read_bytes()
        st = target.stat()
        return data, (st.st_mtime, st.st_size)

    data, stat_key = await asyncio.to_thread(_read)
    text = to_u16(data.decode("utf-8"))
    doc = CollabDoc(rel_path, text)
    doc.disk_hash = hashlib.sha256(data).hexdigest()
    doc.disk_stat = stat_key
    docs[rel_path] = doc
    return doc


def join(doc: CollabDoc, client: Client) -> None:
    doc.clients[client.id] = client
    if doc.unload_task is not None:
        doc.unload_task.cancel()
        doc.unload_task = None


def leave(doc: CollabDoc, client_id: str) -> None:
    doc.clients.pop(client_id, None)


def _ops_to_u16(raw_ops: Any) -> list[dict]:
    """Validate and convert client-submitted op components: text fields are
    already u16 form coming from a JS client over the wire, but convert
    anyway (a no-op for pure-BMP text) so server-synthesized ops (disk diffs,
    tests) can pass real text through the same path."""
    if not isinstance(raw_ops, list) or not raw_ops:
        raise OTError("ops must be a non-empty list")
    out: list[dict] = []
    for c in raw_ops:
        if not isinstance(c, dict) or not isinstance(c.get("p"), int) or isinstance(c.get("p"), bool):
            raise OTError("invalid op component")
        if isinstance(c.get("i"), str) and "d" not in c:
            out.append({"p": c["p"], "i": to_u16(c["i"])})
        elif isinstance(c.get("d"), str) and "i" not in c:
            out.append({"p": c["p"], "d": to_u16(c["d"])})
        else:
            raise OTError("invalid op component")
    return out


def _apply_ops(doc: CollabDoc, ops: list[dict], base_version: int, client_id: str,
               name: str, color: str | None, enforce_size_limit: bool = True) -> tuple[int, list[dict]]:
    """Transform *ops* (already u16 form) against doc history since
    *base_version*, apply, and record the result in history.

    Returns ``(applied_at_version, transformed_ops)``. Raises ``OTError``
    without mutating the document on any failure.
    """
    if not isinstance(base_version, int) or isinstance(base_version, bool):
        raise OTError("v must be an integer")
    if base_version > doc.version:
        raise OTError("op is based on a version that doesn't exist yet")
    oldest = doc.version - len(doc.history)
    if base_version < oldest:
        raise OTError("base version too old — reconnect and rejoin")
    transformed = ops
    for hv in range(base_version, doc.version):
        hist_ops = doc.history[hv - oldest]["ops"]
        transformed = transform(transformed, hist_ops, "left")
    new_text = apply_op(doc.text, transformed)
    if enforce_size_limit:
        size = len(from_u16(new_text).encode("utf-8"))
        if size > _max_bytes():
            raise OTError("file too large to edit")
    applied_at = doc.version
    doc.text = new_text
    doc.history.append({"ops": transformed, "client": client_id, "name": name, "color": color})
    if len(doc.history) > HISTORY_LIMIT:
        doc.history.pop(0)
    doc.version += 1
    if name:
        doc.pending_names.add(name)
    return applied_at, transformed


def submit_op(doc: CollabDoc, client_id: str, name: str, color: str, base_version: Any,
              raw_ops: Any) -> tuple[int, list[dict], str]:
    """Validate, transform and apply a client's op.

    Returns ``(applied_at_version, transformed_ops, content_hash)``. Raises
    ``OTError`` (document left untouched) on any bad input.
    """
    ops = _ops_to_u16(raw_ops)
    applied_at, transformed = _apply_ops(doc, ops, base_version, client_id, name, color)
    h = content_hash(from_u16(doc.text))
    return applied_at, transformed, h


def _diff_ops(old: str, new: str) -> list[dict]:
    """Minimal diff as OT components: common prefix/suffix, one delete +
    one insert for whatever's left in the middle (u16 positions)."""
    if old == new:
        return []
    n_old, n_new = len(old), len(new)
    max_common = min(n_old, n_new)
    prefix = 0
    while prefix < max_common and old[prefix] == new[prefix]:
        prefix += 1
    max_suffix = max_common - prefix
    suffix = 0
    while suffix < max_suffix and old[n_old - 1 - suffix] == new[n_new - 1 - suffix]:
        suffix += 1
    old_mid = old[prefix:n_old - suffix]
    new_mid = new[prefix:n_new - suffix]
    ops: list[dict] = []
    if old_mid:
        ops.append({"p": prefix, "d": old_mid})
    if new_mid:
        ops.append({"p": prefix, "i": new_mid})
    return ops


async def flush_doc(doc: CollabDoc) -> dict | None:
    """Write *doc*'s current text to disk (atomically) if it differs from
    what was last flushed, append one line to ``artifact_edits.jsonl``, and
    return the ``saved`` broadcast payload — or ``None`` if there was
    nothing to flush."""
    async with doc.flush_lock:
        return await _flush_locked(doc)


async def _flush_locked(doc: CollabDoc) -> dict | None:
    if doc.text == doc.flushed_text:
        return None
    experiments = _repo_experiments()
    target = experiments._artifact_target(doc.path)
    # Snapshot before the (threaded) write: ops applied while it runs are not
    # on disk yet, so they must not be recorded as flushed.
    snapshot_text = doc.text
    snapshot_version = doc.version
    text_out = from_u16(snapshot_text)
    data = text_out.encode("utf-8")

    def _write() -> tuple[float, int]:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, target)
        st = target.stat()
        return (st.st_mtime, st.st_size)

    disk_stat = await asyncio.to_thread(_write)
    flushed_version = snapshot_version
    names = sorted(doc.pending_names)
    doc.pending_names.clear()
    doc.flushed_text = snapshot_text
    doc.flushed_version = flushed_version
    doc.disk_hash = hashlib.sha256(data).hexdigest()
    doc.disk_stat = disk_stat
    at = datetime.now(timezone.utc).isoformat()

    def _log() -> None:
        try:
            with open(experiments.REPO_DIR / ".the_lab" / "artifact_edits.jsonl", "a") as f:
                f.write(json.dumps({
                    "ts": at, "user": "user", "path": doc.path, "version": flushed_version,
                    "bytes": len(data), "collab": True, "names": names,
                }) + "\n")
        except OSError:
            pass

    await asyncio.to_thread(_log)
    return {"type": "saved", "v": flushed_version, "at": at}


async def check_and_merge_external(doc: CollabDoc) -> dict | None:
    """Poll the file for *doc* once. If an agent changed it on disk since
    the last flush, diff ``flushed_text`` -> the new disk text and submit
    that as an op based on ``flushed_version`` from client "disk" (so it's
    transformed against any unflushed, in-memory user edits), then flush
    the merge back to disk.

    Returns ``None`` if the file is unchanged, ``{"status": "deleted"}`` if
    it's gone, or ``{"status": "merged", "op": {...} | None, "saved": {...}
    | None}`` after a merge (``op`` is the broadcast payload for other
    clients, or ``None`` if the diff was empty; ``saved`` likewise mirrors
    ``flush_doc``'s return).
    """
    # Under the flush lock: reading the file between our own os.replace and
    # the flush recording disk_hash would look like an agent edit and apply
    # our text a second time.
    async with doc.flush_lock:
        return await _check_and_merge_locked(doc)


async def _check_and_merge_locked(doc: CollabDoc) -> dict | None:
    experiments = _repo_experiments()
    target = experiments._artifact_target(doc.path)

    def _stat_and_maybe_read():
        try:
            st = target.stat()
        except OSError:
            return None
        key = (st.st_mtime, st.st_size)
        if key == doc.disk_stat:
            return (key, None)
        return (key, target.read_bytes())

    result = await asyncio.to_thread(_stat_and_maybe_read)
    if result is None:
        return {"status": "deleted"}
    key, data = result
    doc.disk_stat = key
    if data is None:
        return None
    new_hash = hashlib.sha256(data).hexdigest()
    if new_hash == doc.disk_hash:
        return None
    try:
        new_text = to_u16(data.decode("utf-8"))
    except UnicodeDecodeError:
        return None  # can't merge a non-UTF-8 external write; leave it for next poll
    ops = _diff_ops(doc.flushed_text, new_text)
    doc.disk_hash = new_hash
    op_payload = None
    if ops:
        applied_at, transformed = _apply_ops(doc, ops, doc.flushed_version, "disk", "disk", None,
                                              enforce_size_limit=False)
        op_payload = {"type": "op", "v": applied_at, "ops": transformed, "client": "disk",
                      "name": "disk", "color": None}
    saved = await _flush_locked(doc)
    return {"status": "merged", "op": op_payload, "saved": saved}


async def _broadcast(doc: CollabDoc, message: dict, exclude: str | None = None) -> None:
    text = json.dumps(message, ensure_ascii=True)
    for cid, client in list(doc.clients.items()):
        if cid == exclude:
            continue
        try:
            await client.websocket.send_text(text)
        except Exception:
            pass  # a dead socket is cleaned up by its own connection's receive loop


async def _send_json(websocket: Any, message: dict) -> None:
    await websocket.send_text(json.dumps(message, ensure_ascii=True))


def _notify_presence(doc: CollabDoc, client: Client, action: str) -> None:
    from . import ws as ws_mod
    ws_mod.broadcaster.broadcast({
        "type": "collab_presence", "path": doc.path, "client": client.id,
        "name": client.name, "color": client.color, "action": action,
        "count": len(doc.clients),
    })


def _notify_invite(doc: CollabDoc, client: Client, message: str | None) -> None:
    from . import ws as ws_mod
    ws_mod.broadcaster.broadcast({
        "type": "collab_invite", "path": doc.path,
        "from": {"client": client.id, "name": client.name, "color": client.color},
        "message": message,
    })


def schedule_flush(doc: CollabDoc) -> None:
    """Debounce a flush ~``FLUSH_DEBOUNCE_SECONDS`` after the last op."""
    if doc.flush_handle is not None and not doc.flush_handle.done():
        doc.flush_handle.cancel()

    async def _debounced() -> None:
        try:
            await asyncio.sleep(FLUSH_DEBOUNCE_SECONDS)
        except asyncio.CancelledError:
            return
        saved = await flush_doc(doc)
        if saved is not None:
            await _broadcast(doc, saved)

    doc.flush_handle = asyncio.create_task(_debounced())


async def _handle_deleted_file(doc: CollabDoc) -> None:
    await _broadcast(doc, {"type": "error", "message": "file was deleted"})
    for client in list(doc.clients.values()):
        try:
            await client.websocket.close(code=1011)
        except Exception:
            pass
    doc.clients.clear()
    docs.pop(doc.path, None)


def start_polling(doc: CollabDoc) -> None:
    """Start the background task that watches the file for external
    (agent) edits while *doc* has clients. A no-op if already running."""
    if doc.poll_task is not None and not doc.poll_task.done():
        return

    async def _poll_loop() -> None:
        try:
            while doc.clients:
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
                if not doc.clients:
                    break
                result = await check_and_merge_external(doc)
                if result is None:
                    continue
                if result["status"] == "deleted":
                    await _handle_deleted_file(doc)
                    return
                if result["op"] is not None:
                    await _broadcast(doc, result["op"])
                if result["saved"] is not None:
                    await _broadcast(doc, result["saved"])
        except asyncio.CancelledError:
            pass

    doc.poll_task = asyncio.create_task(_poll_loop())


def schedule_unload(doc: CollabDoc) -> None:
    """After the last client leaves: flush, then drop the doc from memory
    — unless someone rejoins within the grace period (e.g. a page reload),
    which cancels this via ``join``/``get_doc``."""
    if doc.poll_task is not None:
        doc.poll_task.cancel()
        doc.poll_task = None
    if doc.unload_task is not None:
        doc.unload_task.cancel()

    async def _unload() -> None:
        try:
            await asyncio.sleep(UNLOAD_GRACE_SECONDS)
        except asyncio.CancelledError:
            return
        if doc.clients:
            return
        await flush_doc(doc)
        docs.pop(doc.path, None)

    doc.unload_task = asyncio.create_task(_unload())


async def _handle_op(doc: CollabDoc, client: Client, msg: dict) -> bool:
    try:
        applied_at, transformed, h = submit_op(doc, client.id, client.name, client.color,
                                                msg.get("v"), msg.get("ops"))
    except OTError as exc:
        await _send_json(client.websocket, {"type": "error", "message": str(exc)})
        return False
    await _send_json(client.websocket, {"type": "ack", "v": applied_at, "hash": h})
    await _broadcast(doc, {"type": "op", "v": applied_at, "ops": transformed, "client": client.id,
                           "name": client.name, "color": client.color}, exclude=client.id)
    schedule_flush(doc)
    return True


async def handle_collab_socket(websocket: Any, path: str, name: str, client_id: str) -> None:
    """Run one collab WebSocket connection end-to-end. The caller has
    already accepted the socket and checked auth; *path* is the artifact
    path relative to the artifacts root, as given by the client."""
    try:
        target = _resolve_artifact(path)
    except OTError as exc:
        await _send_json(websocket, {"type": "error", "message": str(exc)})
        await websocket.close(code=1008)
        return

    experiments = _repo_experiments()
    rel_path = target.relative_to(experiments._artifacts_root()).as_posix()

    try:
        doc = await get_doc(rel_path, target)
    except OSError as exc:
        await _send_json(websocket, {"type": "error", "message": f"could not read file: {exc}"})
        await websocket.close(code=1011)
        return

    color = pick_color(doc)
    client = Client(id=client_id, name=name or client_id, color=color, websocket=websocket)
    join(doc, client)
    start_polling(doc)

    await _send_json(websocket, {
        "type": "joined", "client": client.id, "color": color, "v": doc.version,
        "text": doc.text, "hash": content_hash(from_u16(doc.text)), "clients": doc.clients_list(),
    })
    await _broadcast(doc, {"type": "presence", "clients": doc.clients_list()}, exclude=client.id)
    _notify_presence(doc, client, "joined")

    from fastapi import WebSocketDisconnect
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if not isinstance(msg, dict):
                continue
            mtype = msg.get("type")
            if mtype == "op":
                ok = await _handle_op(doc, client, msg)
                if not ok:
                    await websocket.close(code=1008)
                    return
            elif mtype == "cursor":
                client.anchor = msg.get("anchor")
                client.head = msg.get("head")
                await _broadcast(doc, {"type": "cursor", "client": client.id, "anchor": client.anchor,
                                       "head": client.head}, exclude=client.id)
            elif mtype == "invite":
                _notify_invite(doc, client, msg.get("message"))
            elif mtype == "ping":
                await _send_json(websocket, {"type": "pong"})
            # unknown message types are ignored
    except WebSocketDisconnect:
        pass
    finally:
        leave(doc, client.id)
        if doc.clients:
            await _broadcast(doc, {"type": "presence", "clients": doc.clients_list()})
        _notify_presence(doc, client, "left")
        if not doc.clients:
            schedule_unload(doc)
