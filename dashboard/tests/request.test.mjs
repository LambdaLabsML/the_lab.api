import { test, after } from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { build } from "esbuild";

const directory = await mkdtemp(join(tmpdir(), "lab-request-test-"));
const bundle = join(directory, "request.mjs");
const pollingBundle = join(directory, "polling.mjs");
await build({ entryPoints: ["src/state/request.ts"], outfile: bundle, bundle: true, platform: "node", format: "esm" });
await build({ entryPoints: ["src/state/polling.ts"], outfile: pollingBundle, bundle: true, platform: "node", format: "esm" });
const { createInFlightGuard, requestJson, requestText } = await import(pathToFileURL(bundle));
const { __pollingTest } = await import(pathToFileURL(pollingBundle));
const originalFetch = globalThis.fetch;
after(async () => { globalThis.fetch = originalFetch; await rm(directory, { recursive: true }); });

test("deadline covers a hung response body and later requests recover", async () => {
  let calls = 0;
  globalThis.fetch = async () => {
    calls++;
    if (calls === 1) return { ok: true, json: () => new Promise(() => {}) };
    return { ok: true, json: async () => ({ recovered: true }) };
  };
  await assert.rejects(requestJson("/slow", undefined, { timeoutMs: 10, retries: 0 }), { name: "TimeoutError" });
  assert.deepEqual(await requestJson("/slow"), { recovered: true });
});

test("text responses have the same body deadline", async () => {
  globalThis.fetch = async () => ({ ok: true, text: () => new Promise(() => {}) });
  await assert.rejects(requestText("/slow-text", undefined, { timeoutMs: 10, retries: 0 }), { name: "TimeoutError" });
});

test("transient reads retry but mutations never retry", async () => {
  let calls = 0;
  globalThis.fetch = async () => { calls++; return { ok: false, status: 503 }; };
  await assert.rejects(requestJson("/read", undefined, { retryDelayMs: 0 }));
  assert.equal(calls, 3);
  calls = 0;
  await assert.rejects(requestJson("/create", { method: "POST" }, { retries: 2 }));
  assert.equal(calls, 1);
});

test("authentication and malformed JSON errors do not retry", async () => {
  let calls = 0;
  globalThis.fetch = async () => { calls++; return { ok: false, status: 401 }; };
  await assert.rejects(requestJson("/private"));
  assert.equal(calls, 1);
  globalThis.fetch = async () => { calls++; return { ok: true, json: async () => { throw new SyntaxError("bad JSON"); } }; };
  await assert.rejects(requestJson("/bad-json"));
  assert.equal(calls, 2);
});

test("caller cancellation settles a hanging request without retry", async () => {
  let calls = 0;
  globalThis.fetch = () => { calls++; return new Promise(() => {}); };
  const controller = new AbortController();
  const request = requestJson("/cancel", { signal: controller.signal });
  controller.abort();
  await assert.rejects(request, { name: "AbortError" });
  assert.equal(calls, 1);
});

test("mutations are never retried and a timeout says their outcome is unknown", async () => {
  let calls = 0;
  globalThis.fetch = () => { calls++; return new Promise(() => {}); };
  await assert.rejects(
    requestJson("/write", { method: "PUT" }, { timeoutMs: 10, retries: 2 }),
    (error) => error.name === "TimeoutError" && error.message.includes("outcome is unknown"),
  );
  assert.equal(calls, 1);
});

test("shared poll guard coalesces and releases after a failed request", async () => {
  __pollingTest.reset();
  let resolve;
  let calls = 0;
  globalThis.fetch = () => {
    calls++;
    return new Promise((done) => { resolve = done; });
  };
  const first = __pollingTest.pollBacklog();
  const second = __pollingTest.pollBacklog();
  assert.equal(first, second);
  assert.equal(calls, 1);
  resolve({ ok: true, json: async () => ({ current_branch: "main" }) });
  await first;

  globalThis.fetch = async () => { calls++; return { ok: false, status: 400 }; };
  await __pollingTest.pollBacklog();
  globalThis.fetch = async () => { calls++; return { ok: true, json: async () => ({ current_branch: "recovered" }) }; };
  await __pollingTest.pollBacklog();
  assert.equal(calls, 3);
  assert.equal(__pollingTest.backlog()?.current_branch, "recovered");
});

test("slow recurring output work survives later poll ticks", async () => {
  let calls = 0;
  let resolve;
  const pollOutput = createInFlightGuard(() => {
    calls++;
    return new Promise((done) => { resolve = done; });
  });
  const first = pollOutput();
  assert.equal(pollOutput(), first);
  assert.equal(pollOutput(), first);
  assert.equal(calls, 1);
  resolve("complete output");
  assert.equal(await first, "complete output");
});

test("a chart query changed in flight cannot publish the old selection", async () => {
  __pollingTest.reset();
  __pollingTest.setChartHistoryLimit(250);
  let firstResponse;
  let calls = 0;
  globalThis.fetch = () => {
    calls++;
    if (calls === 1) return new Promise((done) => { firstResponse = done; });
    return Promise.resolve({ ok: true, json: async () => ({
      experiments: [{ id: 2, label: "new", status: "completed", metrics: { score: 2 } }],
      running: [], metric_keys: ["score"], history: { loaded: 500, total: 500, has_more: false },
    }) });
  };
  const pending = __pollingTest.pollChartData();
  __pollingTest.setChartHistoryLimit(500);
  firstResponse({ ok: true, json: async () => ({
    experiments: [{ id: 1, label: "old", status: "completed", metrics: { score: 1 } }],
    running: [], metric_keys: ["score"], history: { loaded: 250, total: 250, has_more: false },
  }) });
  await pending;
  assert.equal(calls, 2);
  assert.equal(__pollingTest.experiments()[0].label, "new");
});
