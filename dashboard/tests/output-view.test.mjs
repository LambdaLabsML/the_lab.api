import { after, test } from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { build } from "esbuild";

const directory = await mkdtemp(join(tmpdir(), "lab-output-view-test-"));
const bundle = join(directory, "output-view.mjs");
const controllerBundle = join(directory, "output-controller.mjs");
await build({ entryPoints: ["src/state/output-view.ts"], outfile: bundle, bundle: true, platform: "node", format: "esm" });
await build({ entryPoints: ["src/state/output-controller.ts"], outfile: controllerBundle, bundle: true, platform: "node", format: "esm" });
const policy = await import(pathToFileURL(bundle));
const { OutputController } = await import(pathToFileURL(controllerBundle));
after(() => rm(directory, { recursive: true }));

test("output is actionable before it exists for live and failed Slurm runs", () => {
  assert.equal(policy.canShowExperimentOutput("starting", false), true);
  assert.equal(policy.canShowExperimentOutput("running", false), true);
  assert.equal(policy.canShowExperimentOutput("failed", false), true);
  assert.equal(policy.canShowExperimentOutput("completed", false), false);
  assert.equal(policy.canShowExperimentOutput("completed", true), true);
});

test("a 404 is waiting for live output, not a global failure", () => {
  let latestStatus = "running";
  assert.equal(policy.outputUnavailableState(latestStatus, 404), "waiting");
  // The same late 404 must be reclassified once a terminal event updates
  // the selected experiment, rather than leaving a stale "still running" UI.
  latestStatus = "failed";
  assert.equal(policy.outputUnavailableState(latestStatus, 404), "unavailable");
  assert.equal(policy.outputUnavailableState("starting", 404), "waiting");
  assert.equal(policy.outputUnavailableState("failed", 404), "unavailable");
  assert.equal(policy.outputUnavailableState("completed", 404), "unavailable");
  assert.equal(policy.outputUnavailableState("completed", 500), "error");
});

test("output refresh reacts to all output-producing events", () => {
  for (const type of ["experiment_log_updated", "experiment_output_updated", "experiment_progress_updated", "experiment_finished"]) {
    assert.equal(policy.isOutputRefreshEvent(type), true);
  }
  assert.equal(policy.isOutputRefreshEvent("idea_changed"), false);
});

test("output controller coalesces, ignores stale selection, and permits close/reopen", async () => {
  const controller = new OutputController();
  const pending = [];
  const load = (signal) => new Promise((resolve, reject) => pending.push({ signal, resolve, reject }));
  const received = [];
  const failed = [];
  controller.open("a");
  const first = controller.fetch("a", load, (x) => received.push(x), (x) => failed.push(x));
  assert.equal(controller.fetch("a", load, (x) => received.push(x), (x) => failed.push(x)), first);
  assert.equal(pending.length, 1);
  controller.open("b");
  const second = controller.fetch("b", load, (x) => received.push(x), (x) => failed.push(x));
  assert.equal(pending[0].signal.aborted, true);
  pending[0].resolve("stale");
  pending[1].resolve("fresh");
  await Promise.all([first, second]);
  assert.deepEqual(received, ["fresh"]);
  controller.close();
  controller.open("b");
  const third = controller.fetch("b", load, (x) => received.push(x), (x) => failed.push(x));
  assert.equal(pending.length, 3);
  pending[2].reject(new Error("404"));
  await third;
  assert.equal(failed.length, 1);
  assert.deepEqual(received, ["fresh"]); // failed refresh preserves displayed content
});
