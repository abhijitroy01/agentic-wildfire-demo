/* Which workflow stage and simulation mode a reload or a ?traffic_run= link restores.
 * Run: npm --prefix ui run test:metrics */
const test = require("node:test");
const assert = require("node:assert/strict");
const { load } = require("../ts-module.cjs");
const { browser } = require("./support/browser.cjs");

test("workflow stages 2 and 3 survive reloads until the user returns to Investigation", () => {
  const { restoredView, rememberView, restoredSimulationMode, rememberSimulationMode, VIEW_KEY } = load("shared/services/viewPersistence.ts");
  const { storage } = browser();
  assert.equal(restoredView(storage, true), "chat");
  storage.setItem(VIEW_KEY, "traffic-lab"); // A tab opened before the combined workspace existed.
  assert.equal(restoredView(storage, true), "evacuation");
  assert.equal(restoredSimulationMode(storage), "research");
  assert.equal(restoredView(storage, false), "chat", "Builds without simulations ignore a remembered view");
  rememberView(storage, "evacuation");
  assert.equal(restoredView(storage, true), "evacuation");
  rememberSimulationMode(storage, "guided");
  assert.equal(restoredSimulationMode(storage), "guided");
  rememberSimulationMode(storage, "research");
  assert.equal(restoredSimulationMode(storage), "research");
  assert.equal(restoredSimulationMode(storage, "?traffic_run=run-7&simulation_mode=guided"), "guided");
  rememberView(storage, "response");
  assert.equal(restoredView(storage, true), "response");
  assert.equal(restoredView(storage, false), "response", "3 · Response Tracker exists in builds without simulations");
  assert.equal(restoredView(storage, true, "?traffic_run=run-7"), "response",
    "A research run's link left in the address does not pull a reload of stage 3 back to stage 2");
  rememberView(storage, "chat");
  assert.equal(storage.getItem(VIEW_KEY), null, "Returning to Investigation forgets the remembered stage");
  assert.equal(restoredView(storage, true), "chat");
  storage.setItem(VIEW_KEY, "radio");
  assert.equal(restoredView(storage, true), "chat", "Only workflow stages 2 and 3 are restored");
});

test("a ?traffic_run= deep link names the saved run to open", () => {
  browser();
  const { savedRunId } = load("features/traffic-lab/hooks/useResearchJob.ts");
  const { restoredView, restoredSimulationMode } = load("shared/services/viewPersistence.ts");
  assert.equal(restoredView(sessionStorage, true, "?traffic_run=run-7"), "evacuation",
    "A fresh tab opens the linked capability before its history has been restored.");
  assert.equal(restoredSimulationMode(sessionStorage, "?traffic_run=run-7"), "research");
  assert.equal(savedRunId("?traffic_run=run-7"), "run-7");
  assert.equal(savedRunId("?view=lab&traffic_run=run-8"), "run-8");
  assert.equal(savedRunId("?view=lab"), null);
  assert.equal(savedRunId(""), null);
});
