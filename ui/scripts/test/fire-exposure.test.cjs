/* Saved fire exposure: the browser renders source-bound results and never runs a model.
 * Run: npm --prefix ui run test:metrics */
const test = require("node:test");
const assert = require("node:assert/strict");
const React = require("react");
const { act, create } = require("react-test-renderer");
const { load } = require("../ts-module.cjs");

const FIRST = "11111111-1111-4111-8111-111111111111";
const SECOND = "22222222-2222-4222-8222-222222222222";
const GEOMETRY = { type: "Polygon", coordinates: [
  [[0, 0], [0.02, 0], [0.02, 0.02], [0, 0.02], [0, 0]],
  [[0.005, 0.005], [0.005, 0.01], [0.01, 0.01], [0.01, 0.005], [0.005, 0.005]],
] };
const feature = (kind, elapsed_minutes) => ({ type: "Feature", geometry: GEOMETRY,
  properties: { kind, label: "Retained demo geometry", ...(elapsed_minutes ? { elapsed_minutes } : {}) } });
function assessment(overrides = {}) {
  return { schema_version: "1", assessment_id: FIRST, fire_ref: "owned-fire-observation", input_hash: "a".repeat(64),
    model_version: "wind-exposure-1.0.0", availability: "available", mode: "proximity",
    calculated_at: "2026-10-26T17:00:00Z",
    fire: { latitude: 0, longitude: 0, name: "Reported zero-coordinate fire", source_id: "esri-123",
      source_timestamp: "2026-10-26T16:00:00Z" },
    weather: null, geojson: { type: "FeatureCollection", features: [feature("proximity")] },
    assumptions: ["Distances are illustrative."], warnings: [], scenario: null, ...overrides };
}
function scenario(overrides = {}) {
  return assessment({ assessment_id: SECOND, model_version: "surface-fire-1.0.0", mode: "surface_fire",
    geojson: { type: "FeatureCollection", features: [30, 60, 120].map(minutes => feature("surface_fire", minutes)) },
    scenario: { engine: "Pyrothermel/Behave", fuel_model: 2, moisture_profile: "moderate", slope_degrees: 0,
      wind_height_assumption: "Assumed 10 m reference wind.", initialization: "reported point treated as demo ignition",
      horizons_minutes: [30, 60, 120] }, ...overrides });
}
const locations = [{ latitude: 0, longitude: 0, fire: { name: "Reported zero-coordinate fire", sourceId: "esri-123" } }];
const area = ring => ring.slice(1).reduce((sum, point, index) =>
  sum + ring[index][0] * point[1] - point[0] * ring[index][1], 0) / 2;

test("the parser accepts saved assessments for actual displayed fires, never generates buffers around a search city", async () => {
  const { parseAgentCoreResponse } = load("shared/services/agentResponseParser.ts");
  const reported = assessment();
  const response = await parseAgentCoreResponse({ content: { response: "Reported fire", mapData: { locations },
    location: { name: "Search city", latitude: 47.6, longitude: -122.3 }, fireExposureData: [reported] } });
  assert.deepEqual(response.fireExposureData, [reported]);
  assert.equal(response.riskZones, undefined);
  const old = await parseAgentCoreResponse({ content: { mapData: { locations }, fireExposureData: [
    assessment({ assessment_id: SECOND, fire: { latitude: 47.6, longitude: -122.3 } }),
    assessment({ fire: { ...reported.fire, source_id: "different-fire" } }),
  ] } });
  assert.deepEqual(old.fireExposureData, [], "Neither a city-coordinate result nor another source belongs to this answer");
  const withoutModel = await parseAgentCoreResponse({ content: { mapData: { locations } } });
  assert.deepEqual(withoutModel.locations, locations);
  assert.deepEqual(withoutModel.fireExposureData, [], "Missing model output keeps fires without inventing geometry");
});

test("malformed exposure is isolated and zero/high-latitude source coordinates are valid", () => {
  const { answerAssessments, isAssessmentResult } = load("features/fire-exposure/services/assessment.ts");
  assert.equal(isAssessmentResult(assessment()), true);
  assert.equal(isAssessmentResult(assessment({ fire: { latitude: 90, longitude: 180 } })), true);
  for (const bad of [assessment({ fire: { latitude: NaN, longitude: 0 } }),
    assessment({ fire: { latitude: 0, longitude: 181 } }),
    assessment({ fire: { latitude: 0, longitude: 0, name: {} } }),
    assessment({ geojson: { type: "FeatureCollection", features: [{ ...feature("proximity"),
      geometry: { type: "Polygon", coordinates: [[[0, 0], [1, 0], [1, 1], [0, 1]]] } }] } })]) {
    assert.equal(isAssessmentResult(bad), false);
  }
  assert.equal(answerAssessments([assessment(), null, assessment()], locations).length, 1, "Duplicate IDs do not double draw");
  assert.equal(answerAssessments([assessment()], [{ ...locations[0], latitude: NaN }]).length, 0);
});

test("GeoJSON conversion preserves holes and split antimeridian parts without changing saved geometry", () => {
  const { arcgisExposureRings } = load("features/map/utils/fireExposureGraphics.ts");
  const saved = JSON.stringify(GEOMETRY);
  const rings = arcgisExposureRings(GEOMETRY);
  assert.ok(area(rings[0][0]) < 0, "ArcGIS exterior clockwise");
  assert.ok(area(rings[0][1]) > 0, "ArcGIS hole counterclockwise");
  assert.equal(JSON.stringify(GEOMETRY), saved, "Saved geometry remains immutable");
  const parts = { type: "MultiPolygon", coordinates: [
    [[[179.8, 10], [180, 10], [180, 10.1], [179.8, 10.1], [179.8, 10]]],
    [[[-180, 10], [-179.8, 10], [-179.8, 10.1], [-180, 10.1], [-180, 10]]],
  ] };
  assert.equal(arcgisExposureRings(parts).length, 2);
  const cap = { type: "Polygon", coordinates: [[[-180, 89.8], [-90, 89.8], [0, 89.8], [90, 89.8],
    [180, 89.8], [180, 90], [-180, 90], [-180, 89.8]]] };
  assert.equal(arcgisExposureRings(cap).length, 1, "A longitude edge through the same pole correctly closes a cap");
  assert.deepEqual(arcgisExposureRings({ type: "Polygon", coordinates: [
    [[179, 10], [-179, 10], [-179, 11], [179, 11], [179, 10]],
  ] }), [], "A malformed world-spanning edge is not repaired into invented model output");
});

test("surface-fire controls select only returned horizons while historical context stays off until requested", () => {
  const { visibleExposureFeatures, scenarioHorizons, assessmentGroups } = load("features/fire-exposure/services/assessment.ts");
  const baseline = assessment(), timed = scenario();
  assert.deepEqual(scenarioHorizons(baseline), [], "Exposure receives no fabricated time slider");
  assert.deepEqual(scenarioHorizons(timed), [30, 60, 120]);
  assert.deepEqual(visibleExposureFeatures(timed, {}).map(item => item.properties.elapsed_minutes), [120, 60, 30]);
  assert.deepEqual(visibleExposureFeatures(timed, { [SECOND]: 30 }).map(item => item.properties.elapsed_minutes), [30]);
  assert.deepEqual(visibleExposureFeatures(timed, { [SECOND]: null }), []);
  assert.equal(visibleExposureFeatures(baseline, { [SECOND]: null }).length, 0);
  assert.equal(visibleExposureFeatures(baseline, { [FIRST]: 1 }).length, 1);
  assert.equal(assessmentGroups([baseline, timed]).length, 1, "Both modes describe one reported fire");
});

test("exposure popups escape source and model text", () => {
  const { createFireExposurePopup } = load("features/map/utils/fireExposureGraphics.ts");
  const popup = createFireExposurePopup({ kind: "surface_fire", elapsed_minutes: 60,
    fire_name: '<img src=x onerror="alert(1)">', label: "<script>bad()</script>",
    assumptions: ["<svg onload=bad()>"], source_timestamp: "<iframe>" });
  assert.doesNotMatch(popup.content, /<script>|<img|<svg|<iframe>/);
  assert.match(popup.content, /&lt;img/);
  assert.match(popup.content, /60 minutes after assumed initialization/);
});

test("saved assessment reads use only the same-origin GET and reject a mismatched reference", async () => {
  const calls = [];
  let result = assessment();
  const { readFireExposure } = load("features/fire-exposure/services/api.ts", { stubs: {
    "@/shared/services/workspaceAccess": { brokerJson: async (path, options) => {
      calls.push([path, options.method]); return result;
    } },
  } });
  assert.equal((await readFireExposure(FIRST)).assessment_id, FIRST);
  result = assessment({ assessment_id: SECOND });
  await assert.rejects(readFireExposure(FIRST), /unusable result/);
  await assert.rejects(readFireExposure("bad/../reference"), /Invalid saved/);
  assert.deepEqual(calls, Array.from({ length: 2 }, () => [`/fire-exposure/assessments/${FIRST}`, "GET"]));
});

test("the evidence panel groups modes by fire and its timeline controls perform no fetch", async () => {
  const realFetch = globalThis.fetch;
  globalThis.fetch = () => { throw new Error("Rendering or selecting time must not fetch"); };
  const selected = [], simulationRequests = [];
  const Summary = load("features/fire-exposure/components/FireExposureSummary.tsx", {
    stubs: { "./FireExposureSummary.css": {}, "./Accordion.css": {} },
  }).default;
  let renderer;
  try {
    await act(async () => { renderer = create(React.createElement(Summary, {
      assessments: [assessment(), scenario()], selections: {},
      onScenarioTime: (...args) => selected.push(args),
      onSimulateReportedFire: id => simulationRequests.push(id),
    })); });
    const toggle = renderer.root.findAllByType("button")[0];
    await act(async () => toggle.props.onClick());
    assert.equal(renderer.root.findAllByType("strong").length, 1, "One fire, not one duplicate card per mode");
    const actions = renderer.root.findAllByType("button").filter(button =>
      button.props.children === "Plan evacuation for this fire");
    assert.equal(actions.length, 1, "A fire with two modes offers one explicit scenario action");
    await act(async () => actions[0].props.onClick());
    assert.deepEqual(simulationRequests, [FIRST], "The action sends the immutable baseline assessment ID");
    const timeline = renderer.root.findAllByType("input").find(input => input.props.type === "range");
    await act(async () => timeline.props.onChange({ currentTarget: { value: "0" } }));
    assert.deepEqual(selected, [[SECOND, 30]]);
    const checkbox = renderer.root.findAllByType("input").find(input => input.props.type === "checkbox");
    await act(async () => checkbox.props.onChange({ currentTarget: { checked: false } }));
    assert.deepEqual(selected, [[SECOND, 30], [SECOND, null]]);
    await act(async () => renderer.update(React.createElement(Summary,{assessments:[assessment(),scenario()],
      selections:{},onScenarioTime:() => undefined})));
    assert.equal(renderer.root.findAllByType("button").filter(button =>
      button.props.children === "Plan evacuation for this fire").length,0,
    "Views without the explicit scenario callback offer no mutation action");
  } finally {
    if (renderer) await act(async () => renderer.unmount());
    globalThis.fetch = realFetch;
  }
});

test("late chat completion preserves its evidence without replacing the latest or a manually selected answer", async () => {
  const started = [];
  const { useAgentSubmission } = load("features/layout/hooks/useAgentSubmission.ts", { stubs: {
    "@/features/reports/ResponseRegistryContext": { useResponseRegistryContext: () => ({ registerFires: () => undefined }) },
    "@/shared/services/AgentService": { invokeAgentStreaming: (question, callbacks) => {
      started.push({ question, callbacks }); return { cancel: () => undefined };
    } },
  } });
  let state, renderer;
  const onEvidence = () => undefined;
  function Harness() { state = useAgentSubmission({ onEvidence }); return null; }
  const answer = (response, result) => ({ response, locations, fireExposureData: [result],
    location: null, audioData: null, forecastData: null });
  const first = answer("First", assessment()), second = answer("Second", assessment({ assessment_id: SECOND }));
  const submit = async question => {
    await act(async () => state.setInputText(question));
    await act(async () => state.handleSubmit({ preventDefault() {} }));
  };
  try {
    await act(async () => { renderer = create(React.createElement(Harness)); });
    await submit("First fire");
    await submit("Second fire");
    await act(async () => started[1].callbacks.onComplete(second));
    await act(async () => started[0].callbacks.onComplete(first));
    assert.equal(state.mapData.fireExposureData[0].assessment_id, SECOND);
    assert.deepEqual(state.messages.filter(message => message.evidence).map(message => message.evidence.response),
      ["First", "Second"], "Both completed answers retain their own evidence");

    await submit("Third fire");
    await act(async () => state.selectAnswer(first));
    await act(async () => started[2].callbacks.onComplete(second));
    assert.equal(state.mapData.fireExposureData[0].assessment_id, FIRST, "Completion respects the user's answer selection");
    await act(async () => state.selectAnswer({ ...first, locations: [], fireExposureData: [] }));
    assert.deepEqual(state.mapData.locations, [], "An answer without fires clears previous fire geometry");
    assert.deepEqual(state.mapData.fireExposureData, []);
    assert.equal(started.length, 3, "Restoring evidence started no extra request");
  } finally {
    if (renderer) await act(async () => renderer.unmount());
  }
});
