// ------------------------------------------------------------
// Plain-text OT — TypeScript port of ShareJS's `text0` type.
//
// This is a straight port (see the shared collab spec) of the classic
// ShareJS/ot.js algorithm: an op is a list of components applied in
// order, each either an insert `{p, i}` or a delete `{p, d}`. Positions
// are UTF-16 code units (plain JS string indices — astral characters,
// e.g. emoji, are two code units, matching `.length` natively).
//
// The server (Python) ports the exact same algorithm, so this file must
// not "improve" on the pseudocode — bugs here are convergence bugs.
// ------------------------------------------------------------

export interface InsertComponent { p: number; i: string }
export interface DeleteComponent { p: number; d: string }
export type Component = InsertComponent | DeleteComponent;
/** An op: components applied in order to the same (progressively mutating) doc. */
export type Op = Component[];

export function isInsert(c: Component): c is InsertComponent {
  return (c as InsertComponent).i !== undefined;
}

/** Drop empty i/d components; merge with the last component in `dest` when
 *  they are adjacent and of the same kind (ShareJS `append`). Mutates `dest`. */
export function append(dest: Op, c: Component): void {
  if (isInsert(c)) {
    if (c.i === "") return;
  } else if (c.d === "") {
    return;
  }
  const last = dest[dest.length - 1];
  if (last) {
    if (isInsert(c) && isInsert(last) && last.p + last.i.length === c.p) {
      dest[dest.length - 1] = { i: last.i + c.i, p: last.p };
      return;
    }
    if (!isInsert(c) && !isInsert(last) && last.p === c.p) {
      dest[dest.length - 1] = { d: last.d + c.d, p: last.p };
      return;
    }
  }
  dest.push(c);
}

/** Transform a single position through a component `c` (not through a
 *  whole op). `insertAfter` breaks the tie when an insert happens exactly
 *  at `pos` — true keeps `pos` before the insert, moving it along. */
export function transformPosition(pos: number, c: Component, insertAfter = false): number {
  if (isInsert(c)) {
    return c.p < pos || (c.p === pos && insertAfter) ? pos + c.i.length : pos;
  }
  if (pos <= c.p) return pos;
  if (pos <= c.p + c.d.length) return c.p;
  return pos - c.d.length;
}

/** Transform component `c` against component `otherC` (ShareJS `transformComponent`).
 *  `side` is this client's priority relative to `otherC`: 'left' wins ties. */
export function transformComponent(dest: Op, c: Component, otherC: Component, side: "left" | "right"): Op {
  if (isInsert(c)) {
    append(dest, { i: c.i, p: transformPosition(c.p, otherC, side === "right") });
    return dest;
  }
  if (isInsert(otherC)) {
    // delete vs insert: split the delete around the inserted text.
    let s = c.d;
    if (c.p < otherC.p) {
      append(dest, { d: s.slice(0, otherC.p - c.p), p: c.p });
      s = s.slice(otherC.p - c.p);
    }
    if (s) append(dest, { d: s, p: c.p + otherC.i.length });
    return dest;
  }
  // delete vs delete
  if (c.p >= otherC.p + otherC.d.length) {
    append(dest, { d: c.d, p: c.p - otherC.d.length });
  } else if (c.p + c.d.length <= otherC.p) {
    append(dest, c);
  } else {
    let newD = "";
    if (c.p < otherC.p) newD = c.d.slice(0, otherC.p - c.p);
    if (c.p + c.d.length > otherC.p + otherC.d.length) newD += c.d.slice(otherC.p + otherC.d.length - c.p);
    // The overlapping parts of c.d and otherC.d must agree, else the two
    // deletes disagree about what text is at that position — an error.
    const overlapStart = Math.max(c.p, otherC.p);
    const overlapEnd = Math.min(c.p + c.d.length, otherC.p + otherC.d.length);
    if (overlapEnd > overlapStart) {
      const mine = c.d.slice(overlapStart - c.p, overlapEnd - c.p);
      const theirs = otherC.d.slice(overlapStart - otherC.p, overlapEnd - otherC.p);
      if (mine !== theirs) throw new Error("ot: overlapping deletes disagree on deleted text");
    }
    if (newD) append(dest, { d: newD, p: transformPosition(c.p, otherC) });
  }
  return dest;
}

/** ShareJS `bootstrapTransform`: transform leftOp and rightOp (both valid
 *  against the same base doc) against each other, returning the pair that
 *  applies cleanly after the other side. */
export function transformX(leftOp: Op, rightOpIn: Op): [Op, Op] {
  let newRight: Op = [];
  for (const rc0 of rightOpIn) {
    let rc: Component | null = rc0;
    let newLeft: Op = [];
    let k = 0;
    while (k < leftOp.length) {
      const nextC: Op = [];
      transformComponent(newLeft, leftOp[k], rc!, "left");
      transformComponent(nextC, rc!, leftOp[k], "right");
      k += 1;
      if (nextC.length === 1) {
        rc = nextC[0];
      } else if (nextC.length === 0) {
        for (const comp of leftOp.slice(k)) append(newLeft, comp);
        rc = null;
        break;
      } else {
        const [l2, r2] = transformX(leftOp.slice(k), nextC);
        for (const comp of l2) append(newLeft, comp);
        for (const comp of r2) append(newRight, comp);
        rc = null;
        break;
      }
    }
    if (rc !== null) append(newRight, rc);
    leftOp = newLeft;
  }
  return [leftOp, newRight];
}

/** Transform `op` against `other`. `side` is which op "wins" simultaneous
 *  inserts at the same position — pass 'left' for the client's own op
 *  being transformed against a server/history op (clients are always
 *  'left' relative to server ops, per the spec). */
export function transform(op: Op, other: Op, side: "left" | "right"): Op {
  return side === "left" ? transformX(op, other)[0] : transformX(other, op)[1];
}

/** Apply an op's components, in order, to `doc`. Throws if a delete's text
 *  doesn't match, or a position is out of range. */
export function applyOp(doc: string, op: Op): string {
  for (const c of op) {
    if (isInsert(c)) {
      if (c.p < 0 || c.p > doc.length) throw new Error(`ot: insert position ${c.p} out of range (len ${doc.length})`);
      doc = doc.slice(0, c.p) + c.i + doc.slice(c.p);
    } else {
      if (c.p < 0 || c.p + c.d.length > doc.length) throw new Error(`ot: delete position ${c.p} out of range (len ${doc.length})`);
      if (doc.slice(c.p, c.p + c.d.length) !== c.d) throw new Error(`ot: delete text mismatch at ${c.p}`);
      doc = doc.slice(0, c.p) + doc.slice(c.p + c.d.length);
    }
  }
  return doc;
}

// ---------------------------------------------------------------------------
// CodeMirror <-> Op bridges.
//
// These use a minimal structural type instead of importing @codemirror/state,
// so this module (and its Node test) have no editor dependency. CodeMirror's
// ChangeSet and ChangeSpec both satisfy these shapes.
// ---------------------------------------------------------------------------

/** Structural subset of CodeMirror's `ChangeSet`. */
export interface ChangeSetLike {
  iterChanges(f: (fromA: number, toA: number, fromB: number, toB: number, inserted: { toString(): string }) => void): void;
}

/** Structural subset of CodeMirror's `ChangeSpec` (one change). */
export interface TextChangeSpec { from: number; to?: number; insert?: string }

/**
 * Convert a CodeMirror ChangeSet into an Op (sequential components).
 * Changes are visited from the END of the document backwards: each change's
 * `fromA`/`toA` are positions in `oldDoc`, and since later changes never
 * affect positions before them, processing in that order means every
 * position we read is still valid against the (conceptually still-being-
 * mutated) document.
 */
export function opsFromChanges(changes: ChangeSetLike, oldDoc: string): Op {
  const raw: { fromA: number; toA: number; text: string }[] = [];
  changes.iterChanges((fromA, toA, _fromB, _toB, inserted) => {
    raw.push({ fromA, toA, text: inserted.toString() });
  });
  const op: Op = [];
  for (let i = raw.length - 1; i >= 0; i--) {
    const { fromA, toA, text } = raw[i];
    if (toA > fromA) append(op, { d: oldDoc.slice(fromA, toA), p: fromA });
    if (text) append(op, { i: text, p: fromA });
  }
  return op;
}

/** Convert an Op into a CodeMirror `changes:` array (applied in sequence —
 *  CodeMirror dispatches a ChangeSpec[] against the doc as progressively
 *  mutated by earlier entries, same semantics as an Op's components). */
export function changesFromOps(op: Op): TextChangeSpec[] {
  return op.map((c) => (isInsert(c) ? { from: c.p, insert: c.i } : { from: c.p, to: c.p + c.d.length }));
}
