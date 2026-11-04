/* fixtureProblems names every requirement a browser fixture misses, on an in-memory copy of a recording's shape,
 * and the tracked synthetic fixture meets them. Every condition wait in the browser checks shares one 60 s limit. */
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const { fixtureProblems, loadFixture } = require("../browser/fixture.cjs");
const { CONDITION_TIMEOUT_MS } = require("../browser/helpers.cjs");
const { createBroker } = require("../browser/mock-broker.cjs");

const root = path.resolve(__dirname, "../../..");
const scripts = path.resolve(__dirname, "..");

/** A complete result and network with the recorded run's shape: schema 4, four crews, three policies. */
function recording() {
  const crews = ["crew-0", "crew-1", "crew-2", "crew-3"].map((id, i) => ({id, origin: `-1029${i}#0`, stage: `1029${i}#13`,
    route: [`-1029${i}#0`, `1029${i}#13`], stage_index: 1, scheduled_departure_s: 120 + 45 * i, service_seconds: 120}));
  const frame = (t, state) => ({t, vehicles: [], closed_edges: [], hazard_radius_m: 150,
    crews: crews.map((crew, i) => ({id: crew.id, state: i === 0 ? state : "waiting_to_depart",
      scheduled_departure_s: crew.scheduled_departure_s, departed_s: null, reached_staging_s: null, returned_s: null}))});
  const policy = (name, state) => ({policy: name, description: name, series: [], summary: {crews_staged: 1},
    frames: [frame(0, "waiting_to_depart"), frame(300, state), frame(420, "returning")]});
  return {
    result: {schema_version: 4, crew_plan: crews, scenario: {lat: 39.75, lon: -121.6, duration_minutes: 30},
      policies: [policy("baseline", "en_route"), policy("coordinated", "staging_stop"), policy("phased", "en_route")],
      input_bundle_id: crypto.randomUUID(),
      // Built at run time so no key-shaped literal sits in the source.
      input_bundle_sha256: crypto.createHash("sha256").update("browser fixture inputs").digest("hex")},
    network: {type: "FeatureCollection", features: []},
  };
}

const PUMPING = "a pumping crew (an Engine, Tanker or Brush position) that makes its staging stop under coordinated";

test("a complete recording meets every requirement", () => {
  const {result, network} = recording();
  assert.deepEqual(fixtureProblems(result, network), []);
});

for (const [missing, strip, expected] of [
  ["crew_plan", ({result}) => { delete result.crew_plan; }, ["a non-empty crew_plan", PUMPING]],
  ["schema version", ({result}) => { result.schema_version = 2; }, ["result schema_version 3 or 4"]],
  ["a policy", ({result}) => { result.policies.pop(); }, ["three policies: baseline, coordinated, phased"]],
  ["the pumping stop", ({result}) => {
    for (const frame of result.policies[1].frames) frame.crews[0].state = "en_route";
  }, [PUMPING]],
  ["the input bundle", ({result}) => { delete result.input_bundle_id; result.input_bundle_sha256 = "fixture"; },
    ["an input_bundle_id", "a SHA-256 input_bundle_sha256"]],
  ["the network", fixture => { fixture.network = {}; }, ["a GeoJSON FeatureCollection network"]],
]) {
  test(`a recording stripped of ${missing} reports what it now lacks`, () => {
    const fixture = recording();
    strip(fixture);
    assert.deepEqual(fixtureProblems(fixture.result, fixture.network), expected);
  });
}

test("with SIMULATION_FIXTURE unset, the harness loads the tracked synthetic fixture and it meets every requirement", () => {
  const fixture = loadFixture(root, undefined);
  assert.equal(fixture.id, "browser-synthetic-grid");
  assert.equal(fixture.synthetic, true);
  assert.match(fixture.label, /^Synthetic .*not real roads, traffic or incidents$/);
  assert.deepEqual(fixtureProblems(fixture.result, fixture.network), []);
});

test("a missing or empty recording reports every requirement", () => {
  assert.equal(fixtureProblems(undefined, undefined).length, 7);
  assert.equal(fixtureProblems({crew_plan: []}, null).includes("a non-empty crew_plan"), true);
});

test("the harness gives every Playwright wait and action 60 s, set on its browser context before the page opens", () => {
  assert.equal(CONDITION_TIMEOUT_MS, 60_000);
  const harness = fs.readFileSync(path.join(scripts, "check_evacuation_workspace.cjs"), "utf8");
  const at = text => harness.indexOf(text);
  const created = at("const context = await browser.newContext("), set = at("context.setDefaultTimeout(CONDITION_TIMEOUT_MS);");
  assert.ok(created >= 0 && created < set && set < at("context.newPage()"),
    "check_evacuation_workspace.cjs must set CONDITION_TIMEOUT_MS on its context between newContext and newPage");
});

test("a mocked-broker wait without its own limit fails after 60 s, not before", async t => {
  t.mock.timers.enable({apis: ["setTimeout"]});
  const {result, network} = loadFixture(root, undefined);
  const capabilities = JSON.parse(fs.readFileSync(path.join(root, "schemas/simulation/capabilities.json"), "utf8"));
  const b = createBroker({result, network, capabilities});
  const settled = () => new Promise(resolve => setImmediate(resolve));
  let outcome = "pending";
  b.until(() => false, "The condition never held").catch(error => { outcome = error.message; });
  t.mock.timers.tick(CONDITION_TIMEOUT_MS - 1);
  await settled();
  assert.equal(outcome, "pending");
  t.mock.timers.tick(1);
  await settled();
  assert.equal(outcome, "The condition never held");
});

test("no ui/scripts/browser module or journey crew check gives a condition wait its own numeric time limit", () => {
  const files = fs.readdirSync(path.join(scripts, "browser")).filter(name => name.endsWith(".cjs"))
    .map(name => path.join("browser", name)).concat(path.join("traffic-lab", "check_crew_ui.cjs"));
  assert.ok(files.includes(path.join("browser", "exercise.cjs")) && files.includes(path.join("browser", "stage-2-evacuation.cjs")),
    `Scanned only ${files}`);
  const limited = files.filter(file => /\btimeout\s*:\s*\d/.test(fs.readFileSync(path.join(scripts, file), "utf8")));
  assert.deepEqual(limited, [], "These modules pass a numeric timeout instead of inheriting CONDITION_TIMEOUT_MS");
});

function broker() {
  return createBroker({...loadFixture(root,undefined),
    capabilities:JSON.parse(fs.readFileSync(path.join(root,"schemas/simulation/capabilities.json"),"utf8"))});
}

async function routeFixture(b,method,endpoint,body) {
  const entry = b.routes.find(([verb,pattern]) => verb === method
    && (typeof pattern === "string" ? pattern === endpoint : pattern.test(endpoint)));
  assert.ok(entry,`Missing fixture route ${method} ${endpoint}`);
  let fulfilled;
  const response = await entry[2]({body,endpoint,route:{fulfill:async value => {fulfilled = value;}}});
  return fulfilled ?? {json:response};
}

const framesOf = response => response.body.trim().split("\n\n").map(frame => JSON.parse(frame.slice(6)));

test("investigation fixtures retain exact completed owner snapshots and same-ID replays never invoke again", async () => {
  const b = broker();
  b.investigations = 1; // The second answer is one immediate stream, requiring no loopback socket.
  const body = {request_id:crypto.randomUUID(),thread_id:crypto.randomUUID(),question:"Fixture question",context_envelope:null};
  const first = await routeFixture(b,"POST","/investigation/turns",body);
  const snapshot = framesOf(first).find(frame => frame.type === "STATE_SNAPSHOT").snapshot;
  const receipt = (await routeFixture(b,"GET",`/investigation/turns/${body.request_id}`)).json;
  assert.deepEqual(receipt.snapshot,snapshot);
  assert.equal(receipt.status,"completed");
  assert.equal(receipt.request_id,body.request_id);
  assert.equal(receipt.thread_id,body.thread_id);
  const replay = await routeFixture(b,"POST","/investigation/turns",body);
  assert.equal(b.investigations,2);
  assert.deepEqual(framesOf(replay),[{type:"STATE_SNAPSHOT",snapshot},{type:"RUN_FINISHED"}]);
  const conflict = await routeFixture(b,"POST","/investigation/turns",{...body,question:"Different question"});
  assert.equal(conflict.status,409);
  assert.equal(conflict.json.error.code,"IDEMPOTENCY_CONFLICT");
  const foreign = await routeFixture(broker(),"GET",`/investigation/turns/${body.request_id}`);
  assert.equal(foreign.status,404);
  assert.equal(foreign.json.error.code,"NOT_FOUND");
});

test("investigation fixtures keep authoritative failure receipts and running reads never invent results", async () => {
  const b = broker();
  b.investigations = 3;
  const body = {request_id:crypto.randomUUID(),thread_id:crypto.randomUUID(),question:"Fixture failed question"};
  const first = await routeFixture(b,"POST","/investigation/turns",body);
  const failure = framesOf(first).at(-1);
  assert.equal(failure.type,"RUN_ERROR");
  const receipt = (await routeFixture(b,"GET",`/investigation/turns/${body.request_id}`)).json;
  assert.equal(receipt.status,"failed");
  assert.equal(receipt.snapshot,null);
  assert.deepEqual(receipt.error,{code:failure.code,message:failure.message});
  const replay = await routeFixture(b,"POST","/investigation/turns",body);
  assert.deepEqual(framesOf(replay),[failure]);
  assert.equal(b.investigations,4);
  const requestId = crypto.randomUUID();
  b.investigationReceipts[requestId] = {receipt:{...receipt,request_id:requestId,status:"running",error:null,
    completed_at:null,lease_until:Date.now()/1000+210}};
  const pending = (await routeFixture(b,"GET",`/investigation/turns/${requestId}`)).json;
  assert.equal(pending.status,"running");
  assert.equal(pending.snapshot,null);
  assert.equal(pending.error,null);
  pending.status = "completed";
  assert.equal(b.investigationReceipts[requestId].receipt.status,"running","HTTP reads do not mutate retained work");
});

test("investigation fixtures require a stable UUID before accepting work", async () => {
  const b = broker();
  await assert.rejects(routeFixture(b,"POST","/investigation/turns",{question:"Missing request ID"}));
  assert.equal(b.investigations,0);
});

test("radio fixtures complete after duplicate detections instead of simulating a broken transport", async () => {
  const b = broker();
  const response = await routeFixture(b,"POST","/radio/stream",{playback:true});
  const frames = framesOf(response);
  assert.equal(frames.filter(frame => frame.type === "detection").length,2);
  assert.equal(frames.filter(frame => frame.type === "complete").length,1);
  assert.equal(frames.at(-1).type,"complete");
  assert.deepEqual(frames.at(-1).data.first_detection,b.detection.data);
});
