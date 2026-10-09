// Node test for the OT port (run with `node --test src/lib/ot.test.ts`).
// Keep to erasable TypeScript syntax: no enums, no parameter properties.
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  applyOp, transform, transformPosition, append, opsFromChanges, changesFromOps,
  type Op, type Component, type ChangeSetLike,
} from "./ot.ts";

// ---------------------------------------------------------------------------
// Deterministic PRNG (mulberry32) — fixed seed so the fuzz test is reproducible.
// ---------------------------------------------------------------------------
function mulberry32(seed: number): () => number {
  let a = seed | 0;
  return () => {
    a = (a + 0x6D2B79F5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
const randInt = (rng: () => number, maxExclusive: number): number => Math.floor(rng() * maxExclusive);

const ALPHABET = "abcdefgh \n";
const EMOJI = ["😀", "🚀", "✨", "👩‍💻", "🧪"]; // some multi-code-unit (surrogate pairs / ZWJ sequences)

function randomChar(rng: () => number): string {
  // ~15% chance of an astral/emoji grapheme so the fuzz regularly exercises
  // multi-UTF-16-code-unit text (surrogate pairs), matching the "u16 form"
  // positions used on the wire.
  if (rng() < 0.15) return EMOJI[randInt(rng, EMOJI.length)];
  return ALPHABET[randInt(rng, ALPHABET.length)];
}

function randomString(rng: () => number, maxLen: number): string {
  const n = 1 + randInt(rng, maxLen);
  let s = "";
  for (let i = 0; i < n; i++) s += randomChar(rng);
  return s;
}

function randomDoc(rng: () => number): string {
  return randomString(rng, 24);
}

/** A random, internally-consistent op against `doc` (components valid in
 *  sequence against the progressively-mutated local copy). */
function randomOp(rng: () => number, doc: string): Op {
  let cur = doc;
  const op: Op = [];
  const n = 1 + randInt(rng, 3);
  for (let i = 0; i < n; i++) {
    const canDelete = cur.length > 0;
    const doInsert = !canDelete || rng() < 0.5;
    if (doInsert) {
      const p = randInt(rng, cur.length + 1);
      const text = randomString(rng, 4);
      append(op, { i: text, p });
      cur = cur.slice(0, p) + text + cur.slice(p);
    } else {
      const p = randInt(rng, cur.length);
      const len = 1 + randInt(rng, cur.length - p);
      const d = cur.slice(p, p + len);
      append(op, { d, p });
      cur = cur.slice(0, p) + cur.slice(p + len);
    }
  }
  return op;
}

// ---------------------------------------------------------------------------
// Basic unit behaviour
// ---------------------------------------------------------------------------

test("applyOp: insert and delete", () => {
  assert.equal(applyOp("hello world", [{ i: "!", p: 5 }]), "hello! world");
  assert.equal(applyOp("hello world", [{ d: "world", p: 6 }]), "hello ");
});

test("applyOp: sequential components compose", () => {
  // delete "world", then insert "there" at the same spot
  const op: Op = [{ d: "world", p: 6 }, { i: "there", p: 6 }];
  assert.equal(applyOp("hello world", op), "hello there");
});

test("applyOp: delete text mismatch throws", () => {
  assert.throws(() => applyOp("hello world", [{ d: "xxxxx", p: 6 }]));
});

test("transformPosition: insert before/after/at", () => {
  const ins: Component = { i: "XY", p: 3 };
  assert.equal(transformPosition(5, ins), 7); // insert strictly before pos
  assert.equal(transformPosition(1, ins), 1); // insert after pos
  assert.equal(transformPosition(3, ins, false), 3); // tie, insertAfter=false: stay
  assert.equal(transformPosition(3, ins, true), 5); // tie, insertAfter=true: move
});

test("transformPosition: delete before/inside/after", () => {
  const del: Component = { d: "abc", p: 3 }; // removes [3,6)
  assert.equal(transformPosition(1, del), 1); // before
  assert.equal(transformPosition(4, del), 3); // inside -> clamps to start
  assert.equal(transformPosition(8, del), 5); // after -> shifted back by len
});

test("append: merges adjacent inserts and deletes, drops empties", () => {
  const dest: Op = [];
  append(dest, { i: "ab", p: 0 });
  append(dest, { i: "cd", p: 2 }); // continues the previous insert
  append(dest, { i: "", p: 99 }); // dropped
  assert.deepEqual(dest, [{ i: "abcd", p: 0 }]);

  const dest2: Op = [];
  append(dest2, { d: "xy", p: 5 });
  append(dest2, { d: "z", p: 5 }); // same p as last delete -> merges
  assert.deepEqual(dest2, [{ d: "xyz", p: 5 }]);
});

// ---------------------------------------------------------------------------
// TP1 convergence fuzz (fixed seed)
// ---------------------------------------------------------------------------

test("TP1 convergence: random concurrent ops converge, fixed seed", () => {
  const rng = mulberry32(0xC0FFEE);
  const ITERATIONS = 500;
  for (let i = 0; i < ITERATIONS; i++) {
    const doc = randomDoc(rng);
    const a = randomOp(rng, doc);
    const b = randomOp(rng, doc);
    const left = applyOp(applyOp(doc, a), transform(b, a, "right"));
    const right = applyOp(applyOp(doc, b), transform(a, b, "left"));
    assert.equal(left, right, `diverged at iteration ${i}: doc=${JSON.stringify(doc)} a=${JSON.stringify(a)} b=${JSON.stringify(b)}`);
  }
});

// ---------------------------------------------------------------------------
// UTF-16 / emoji specific cases — positions are code units, astral chars
// (incl. the ZWJ "👩‍💻") are more than one code unit, same as the wire form.
// ---------------------------------------------------------------------------

test("emoji: surrogate pair length is counted in code units", () => {
  const rocket = "🚀"; // U+1F680 — one code point, two UTF-16 code units
  assert.equal(rocket.length, 2);
  const doc = "a" + rocket + "b"; // length 4
  assert.equal(doc.length, 4);
  const op: Op = [{ d: rocket, p: 1 }];
  assert.equal(applyOp(doc, op), "ab");
});

test("emoji: concurrent inserts around and across a surrogate pair converge", () => {
  const doc = "x🚀y"; // 'x', hi, lo, 'y' — length 4
  const a: Op = [{ i: "A", p: 0 }]; // insert at the very start
  const b: Op = [{ d: "🚀", p: 1 }, { i: "B", p: 1 }]; // replace the emoji with 'B'
  const left = applyOp(applyOp(doc, a), transform(b, a, "right"));
  const right = applyOp(applyOp(doc, b), transform(a, b, "left"));
  assert.equal(left, right);
  assert.equal(left, "AxBy");
});

test("emoji: ZWJ sequence (multi-codepoint grapheme) treated as plain code units", () => {
  const woman = "👩‍💻"; // woman + ZWJ + laptop — several UTF-16 code units
  const doc = "pre" + woman + "post";
  const delOp: Op = [{ d: woman, p: 3 }];
  assert.equal(applyOp(doc, delOp), "prepost");
});

// ---------------------------------------------------------------------------
// CodeMirror ChangeSet <-> Op bridges
// ---------------------------------------------------------------------------

/** Minimal fake of CodeMirror's ChangeSet, for testing opsFromChanges without
 *  an editor dependency. `changes` are given in ascending fromA order, as
 *  CodeMirror's iterChanges would deliver them. */
function fakeChangeSet(changes: { fromA: number; toA: number; insert: string }[]): ChangeSetLike {
  return {
    iterChanges(f) {
      let bOffset = 0;
      for (const c of changes) {
        const fromB = c.fromA + bOffset;
        f(c.fromA, c.toA, fromB, fromB + c.insert.length, { toString: () => c.insert });
        bOffset += c.insert.length - (c.toA - c.fromA);
      }
    },
  };
}

test("opsFromChanges: single replacement round-trips through applyOp", () => {
  const oldDoc = "abcdefgh";
  const cs = fakeChangeSet([{ fromA: 2, toA: 4, insert: "XYZ" }]); // "cd" -> "XYZ"
  const op = opsFromChanges(cs, oldDoc);
  assert.equal(applyOp(oldDoc, op), "abXYZefgh");
});

test("opsFromChanges: multiple non-overlapping changes, processed end-to-start", () => {
  const oldDoc = "0123456789";
  // Replace [1,3) with "AA", and delete [7,9) entirely — given in ascending order,
  // as CodeMirror would deliver them.
  const cs = fakeChangeSet([
    { fromA: 1, toA: 3, insert: "AA" },
    { fromA: 7, toA: 9, insert: "" },
  ]);
  const op = opsFromChanges(cs, oldDoc);
  // Expect the tail (pos 7) processed before the head (pos 1).
  assert.equal(applyOp(oldDoc, op), "0AA3456" + "9");
});

test("opsFromChanges: pure insert (no deletion) at a position", () => {
  const oldDoc = "hello";
  const cs = fakeChangeSet([{ fromA: 5, toA: 5, insert: " world" }]);
  const op = opsFromChanges(cs, oldDoc);
  assert.deepEqual(op, [{ i: " world", p: 5 }]);
  assert.equal(applyOp(oldDoc, op), "hello world");
});

test("changesFromOps: maps insert/delete components to CM-like ChangeSpecs", () => {
  const op: Op = [{ d: "foo", p: 2 }, { i: "bar", p: 2 }];
  assert.deepEqual(changesFromOps(op), [
    { from: 2, to: 5 },
    { from: 2, insert: "bar" },
  ]);
});
