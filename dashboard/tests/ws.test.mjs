import { after, test } from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { build } from "esbuild";

const directory = await mkdtemp(join(tmpdir(), "lab-ws-test-"));
const bundle = join(directory, "ws.mjs");
await build({ entryPoints: ["src/state/ws.ts"], outfile: bundle, bundle: true, platform: "node", format: "esm" });
const { __wsTest, subscribeWsEvents, wsCatchingUp } = await import(pathToFileURL(bundle));
const originalFetch = globalThis.fetch;
after(async () => { globalThis.fetch = originalFetch; __wsTest.reset(); await rm(directory, { recursive: true }); });

function snapshotResponse(url) {
  if (String(url).startsWith("/api/v1/chart-data")) return { ok: true, json: async () => ({ experiments: [], running: [], metric_keys: [], history: { loaded: 0, total: 0, has_more: false } }) };
  if (url === "/api/v1/graph") return { ok: true, json: async () => ({ nodes: [], edges: [] }) };
  return { ok: true, json: async () => ({}) };
}

test("resync snapshots before applying queued frames, deduplicates, and restarts on overflow", async () => {
  __wsTest.reset();
  const deferred = [];
  globalThis.fetch = (url) => new Promise((resolve) => deferred.push(() => resolve(snapshotResponse(url))));
  __wsTest.receive({ type: "resync", reason: "gap", seq: 10 });
  await Promise.resolve();
  assert.equal(wsCatchingUp.value, true);
  assert.equal(deferred.length, 3);
  __wsTest.receive({ type: "message_received", seq: 11 });
  assert.equal(__wsTest.queued(), 1);
  __wsTest.receive({ type: "resync", reason: "overflow", seq: 20 });
  assert.equal(__wsTest.queued(), 0);
  deferred.splice(0, 3).forEach((resolve) => resolve());
  for (let i = 0; i < 10 && deferred.length < 3; i++) await Promise.resolve();
  assert.equal(deferred.length, 3);
  deferred.splice(0, 3).forEach((resolve) => resolve());
  await __wsTest.waitForSnapshot();
  assert.equal(wsCatchingUp.value, false);
  assert.equal(__wsTest.lastSeq(), 20);
  let delivered = 0;
  const unsubscribe = subscribeWsEvents(() => { delivered++; });
  __wsTest.receive({ type: "message_received", seq: 21 });
  __wsTest.receive({ type: "message_received", seq: 21 });
  unsubscribe();
  assert.equal(delivered, 1);
});

test("a local gap resyncs and a failed snapshot remains visibly catching up", async () => {
  __wsTest.reset();
  const pending = [];
  globalThis.fetch = (url) => new Promise((resolve) => pending.push(() => resolve(snapshotResponse(url))));
  __wsTest.receive({ type: "message_received", seq: 30 });
  __wsTest.receive({ type: "message_received", seq: 32 });
  assert.equal(__wsTest.lastSeq(), 32);
  assert.equal(wsCatchingUp.value, true);
  pending.splice(0).forEach((resolve) => resolve());
  await __wsTest.waitForSnapshot();
  assert.equal(wsCatchingUp.value, false);
  __wsTest.reset();
  globalThis.fetch = async () => ({ ok: false, status: 400 });
  __wsTest.receive({ type: "resync", reason: "overflow", seq: 40 });
  await __wsTest.waitForSnapshot();
  assert.equal(wsCatchingUp.value, true);
  // Failed snapshots retry later. Events arriving in that window stay queued
  // rather than patching stale state ahead of the next canonical snapshot.
  __wsTest.receive({ type: "message_received", seq: 41 });
  assert.equal(__wsTest.queued(), 1);
  __wsTest.reset();
});
