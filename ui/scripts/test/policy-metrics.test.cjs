/* Policy, unit and crew metric rules shared by the Evacuation workspace and the Traffic Lab.
 * Run: npm --prefix ui run test:metrics */
const test = require("node:test");
const assert = require("node:assert/strict");
const { load: loadModule } = require("../ts-module.cjs");

const load = name => loadModule(`features/evacuation/components/${name}.ts`);
const units = load("units"), { congestion, comparison } = load("congestion");
const { missionMetrics, pairedCrewTiming, exitThreshold } = load("mission");
const policy = (queues, times = [0, 60, 120], overrides = {}) => ({
  series: queues.map((queue, i) => ({ queue, t: times[i] })),
  summary: { valid_engine_run: true, valid_accounting: true,
    exited_vehicles: 10, not_departed_vehicles: 2, ...overrides },
});

test("US units use standard conversions and preserve fractional SI inputs", () => {
  assert.equal(units.miles(1.609344), "1.00 mi");
  assert.equal(units.mph(96.56064), "60.0 mph");
  assert.equal(units.speedMph(26.8224), "60.0 mph");
  assert.equal(units.feet(152.4), "500 ft");
  assert.equal(units.temperatureF(20, "C"), "68°F");
  assert.equal(units.temperatureF(72, "F"), "72°F");
  assert(Math.abs(units.fromUSInput("500", units.FEET_PER_METER, 0, 3000) - 152.4) < 1e-10);
  assert(Math.abs(units.fromUSInput("10", units.FEET_PER_METER, 0, 100) - 3.048) < 1e-10);
});
test("rounded input boundaries remain valid without clamping real out-of-range entries", () => {
  for (const [min, max, factor] of [[2, 15, units.MILES_PER_KM], [0, 3000, units.FEET_PER_METER]]) {
    for (const bound of [min, max]) {
      assert.equal(units.fromUSInput((bound * factor).toFixed(2), factor, min, max), bound);
    }
  }
  assert.equal(units.fromUSInput("", units.MILES_PER_KM, 2, 15), "");
  assert(units.fromUSInput("20", units.MILES_PER_KM, 2, 15) > 15);
});
test("invalid or absent source units never coerce to zero or freezing temperature", () => {
  for (const missing of [null, undefined, NaN, Infinity, "0"]) {
    for (const convert of [units.miles, units.feet, units.mph, units.speedMph]) {
      assert.equal(convert(missing), "Unavailable");
    }
    assert.equal(units.temperatureF(missing, "C"), "Temperature unavailable");
  }
  assert.equal(units.temperatureF(-40, "C"), "-40°F");
  assert.equal(units.mph(0), "0.0 mph");
});
test("shared formatters: one U.S. number format, one validated date and time, and the first queue peak", () => {
  assert.equal(units.formatNumber(1234.56), "1,234.6");
  assert.equal(units.formatNumber(2), "2");
  for (const missing of [undefined, null, "", "not a date", NaN, {}]) {
    assert.equal(units.formatDateTime(missing), "Unavailable");
  }
  const epoch = 1_791_000_000; // Early October 2026 in every time zone.
  assert.equal(units.formatDateTime(epoch), units.formatDateTime(new Date(epoch * 1000).toISOString()),
    "Epoch seconds and ISO text name the same instant");
  assert.match(units.formatDateTime(epoch), /^Oct \d{1,2}, 2026, \d{1,2}:\d{2}\s[AP]M \S+$/);
  const { peakSample } = load("replay");
  const series = [{ t: 0, queue: 1 }, { t: 60, queue: 5 }, { t: 120, queue: 5 }, { t: 180, queue: 2 }];
  assert.deepEqual(peakSample(series), { t: 60, queue: 5 }, "A tie keeps the earlier peak");
  assert.deepEqual(peakSample([{ t: 0, queue: 0 }]), { t: 0, queue: 0 });
});
test("slow vehicle-minutes integrate irregular sample intervals, not sample counts", () => {
  const metrics = congestion(policy([0, 10, 0], [0, 30, 120]), 120);
  assert.deepEqual(metrics, { slowMinutes: 10, averageSlow: 5, peak: 10, peakTime: 30, endSlow: 0 });
});
test("missing, out-of-order, nonfinite and incomplete series never become zero congestion", () => {
  for (const p of [
    { series: [] }, policy([0, 1], [5, 120]), policy([0, 1], [0, 119]),
    policy([0, 2, 1], [0, 0, 120]), policy([0, NaN, 1]), policy([0, -1, 1]),
  ]) assert.equal(congestion(p, 120), null);
  const baseline = policy([0, 10, 0]);
  assert.equal(comparison(policy([0, 5, 0], undefined, { valid_engine_run: false }), baseline, 120), null);
  assert.equal(comparison(policy([0, 5, 0]), undefined, 120), null);
});
test("lower queues with fewer exits are explicitly identified as a tradeoff", () => {
  const change = comparison(policy([0, 5, 0], undefined, { exited_vehicles: 8, not_departed_vehicles: 4 }),
    policy([0, 10, 0]), 120);
  assert.equal(change.title, "Less time in slow traffic, but fewer exits");
  assert.equal(change.slowDelta, -5);
  assert.equal(change.exitDelta, -2);
  assert.equal(change.waitingDelta, 2);
  assert.equal(change.percent, 50);
});
test("ties and a zero baseline do not invent a percentage or a winner", () => {
  const zero = policy([0, 0, 0]);
  const tie = comparison(zero, zero, 120);
  assert.equal(tie.percent, null);
  assert.equal(tie.slowDelta, 0);
  assert.match(tie.title, /Same exits/);
  const change = comparison(policy([0, 1, 0]), zero, 120);
  assert.equal(change.percent, null);
  assert.equal(change.slowDelta, 1);
  assert.match(change.title, /More time in slow traffic/);
});

const scenario = { vehicles: 20, people_per_vehicle: 3, crews: 2, duration_minutes: 10 };
function missionPolicy(crews, overrides = {}) {
  return {
    summary: { valid_engine_run: true, valid_accounting: true, simulated_seconds: 600,
      exited_vehicles: 10, active_vehicles: 8, not_departed_vehicles: 2, exposed_vehicles: 4,
      crews_staged: crews.filter(c => c.reached_staging_s != null).length,
      crews_returned: 0, exposed_crews: 0, crews_not_departed: 0, t95_seconds: null, ...overrides },
    frames: [{ t: 600, crews }],
  };
}
const crew = (id, departed_s, reached_staging_s) =>
  ({ id, scheduled_departure_s: 60, departed_s, reached_staging_s, returned_s: null });

test("resident counts preserve waiting and exposure without inventing safe arrivals", () => {
  const m = missionMetrics(missionPolicy([]), scenario);
  assert.equal(m.peopleExited, 30);
  assert.equal(m.peopleUnresolved, 30);
  assert.equal(m.peopleWaiting, 6);
  assert.equal(m.peopleExposed, 12);
  assert.equal(m.notStaged, 2);
  assert.equal(m.t95, null);
  for (const overrides of [
    { valid_engine_run: false }, { valid_accounting: false }, { active_vehicles: 9 },
    { simulated_seconds: 599 }, { exposed_vehicles: undefined }, { crews_staged: 3 },
    { crews_returned: 1 }, { valid_engine_run: "false" }, { exposed_vehicles: null },
  ]) assert.equal(missionMetrics(missionPolicy([], overrides), scenario), null);
});
test("crew response includes dispatch waiting even when road travel becomes faster", () => {
  const baseline = missionPolicy([crew("a", 60, 180), crew("b", 60, 240)]);
  const current = missionPolicy([crew("a", 150, 210), crew("b", 150, 270)]);
  const timing = pairedCrewTiming(current, baseline, scenario);
  assert.equal(timing.response, 30, "Response worsens by 30 seconds");
  assert.equal(timing.departureWait, 90, "Dispatch waits 90 seconds longer");
  assert.equal(timing.travel, -60, "Road travel improves by 60 seconds");
  assert.equal(timing.later, 2);
  assert.equal(timing.count, 2);
  assert.deepEqual(timing.current, { response: 180, departureWait: 90, travel: 90 });
  assert.deepEqual(timing.baseline, { response: 150, departureWait: 0, travel: 150 });
});
test("faster successful crews cannot hide a crew that never arrived", () => {
  const baseline = missionPolicy([crew("a", 60, 180), crew("b", 60, 500)]);
  const current = missionPolicy([crew("a", 60, 150), crew("b", 60, null)]);
  const timing = pairedCrewTiming(current, baseline, scenario);
  assert.equal(timing.response, -30);
  assert.equal(timing.count, 1);
  assert.equal(timing.total, 2);
  assert.equal(missionMetrics(current, scenario).notStaged, 1);
  assert.equal(missionMetrics(baseline, scenario).notStaged, 0);
  const none = pairedCrewTiming(missionPolicy([crew("a", 60, null), crew("b", 60, null)]), baseline, scenario);
  assert.equal(none.response, null);
  assert.equal(none.count, 0);
});
test("missing or inconsistent crew identities, times and coverage stay unavailable", () => {
  const baseline = missionPolicy([crew("a", 60, 180), crew("b", 60, 240)]);
  for (const current of [
    { ...baseline, frames: [] },
    missionPolicy([crew("a", 60, 180)]),
    missionPolicy([crew("a", 60, 180), crew("a", 60, 240)]),
    missionPolicy([crew("a", 60, 180), crew("c", 60, 240)]),
    missionPolicy([crew("a", 60, 180), crew("b", 250, 240)]),
    missionPolicy([crew("a", 60, 180), crew("b", 60, 240)], { crews_staged: 1 }),
    missionPolicy([crew("a", 60, 700), crew("b", 60, 240)]),
    missionPolicy([crew("a", 60, 180), crew("b", 60, undefined)]),
    missionPolicy([crew("a", 60, 180), { ...crew("b", 60, 240), returned_s: undefined }]),
  ]) assert.equal(pairedCrewTiming(current, baseline, scenario), null);
});
test("95% exit timing distinguishes an unreached threshold from missing or inconsistent evidence", () => {
  assert.deepEqual(exitThreshold(missionPolicy([]), scenario), { seconds: null, status: "not_reached" });
  for (const t95_seconds of [undefined, -1, NaN, 0, 601, "120"]) {
    assert.equal(exitThreshold(missionPolicy([], { t95_seconds }), scenario).status, "unavailable");
  }
  const reached = { exited_vehicles: 19, active_vehicles: 1, not_departed_vehicles: 0 };
  for (const t95_seconds of [null, undefined, NaN, 601]) {
    assert.equal(exitThreshold(missionPolicy([], { ...reached, t95_seconds }), scenario).status, "unavailable");
  }
  assert.deepEqual(exitThreshold(missionPolicy([], { ...reached, t95_seconds: 590 }), scenario),
    { seconds: 590, status: "reached" });
});
/** One crew recorded every 5 s from 0 to 600 s: `state(t)` is its phase and `reached(t)` its saved segment entry. */
const stopRecording = (state, reached = () => null) => ({ frames: Array.from({ length: 121 }, (_, i) => ({
  t: i * 5, hazard_radius_m: 0, closed_edges: [], vehicles: [],
  crews: [{ id: "crew-0", state: state(i * 5), scheduled_departure_s: 0, departed_s: 0,
    reached_staging_s: reached(i * 5), returned_s: null }] })) });
// Staging-stop fixtures: stopped 300–415 s then returning; on the staging segment from 500 s; stopped from 540 s.
const fullStop = t => t < 300 ? "to_staging" : t <= 415 ? "staging_stop" : "returning";
const onSegment = t => t >= 500 ? "on_staging_segment" : "to_staging";
const lateStop = t => t >= 540 ? "staging_stop" : "to_staging";

test("illustrative stop use counts only recorded staging_stop frames, capped at the planned stop", () => {
  const { stopIllustration, crewRole, ILLUSTRATIVE_PUMP_GPM } = load("mission");
  const gpm = ILLUSTRATIVE_PUMP_GPM.ENGINE;
  assert.equal(gpm, 450);
  const at = (policy, t, role = "ENGINE", service = 120) => stopIllustration(policy, "crew-0", role, service, t);
  const stopped = stopRecording(fullStop, t => t >= 300 ? 300 : null);
  // Segment entry never counts: a crew on its staging segment at the horizon has not stopped.
  assert.deepEqual(at(stopRecording(onSegment, t => t >= 500 ? 500 : null), 600),
    { kind: "not_reached", seconds: 0, gallons: 0 });
  assert.deepEqual(at(stopped, 600), { kind: "stopped", seconds: 120, gallons: gpm * 2, gpm, stopSeconds: 120 });
  assert.deepEqual(at(stopped, 360), { kind: "stopped", seconds: 60, gallons: gpm, gpm, stopSeconds: 120 });
  assert.deepEqual(at(stopped, 295), { kind: "not_reached", seconds: 0, gallons: 0 });
  // 125 s of recorded stop frames: capped at the planned 120 s.
  const longer = stopRecording(t => t < 300 ? "to_staging" : t <= 420 ? "staging_stop" : "returning");
  assert.equal(at(longer, 600).seconds, 120);
  // A stop cut off by the horizon: rate × elapsed.
  assert.deepEqual(at(stopRecording(lateStop), 600), { kind: "stopped", seconds: 60, gallons: gpm, gpm, stopSeconds: 120 });
  for (const role of ["RESCUE", "COMMAND"]) assert.deepEqual(at(stopped, 600, role), { kind: "support" }, role);
  // Missing or inconsistent inputs are unavailable, never zero.
  for (const service of [undefined, null, 0, NaN, -120, "120"]) {
    assert.deepEqual(stopIllustration(stopped, "crew-0", "ENGINE", service, 600), { kind: "unavailable" }, String(service));
  }
  const change = (index, frame) => ({ frames: stopped.frames.map((value, i) => i === index ? frame(value) : value) });
  for (const policy of [change(10, ({ crews: _crews, ...frame }) => frame), change(10, frame => ({ ...frame, crews: [] })),
    change(10, frame => ({ ...frame, t: stopped.frames[9].t })), change(10, frame => ({ ...frame, t: NaN })),
    { frames: [] }, {}]) {
    assert.deepEqual(at(policy, 600), { kind: "unavailable" });
  }
  assert.deepEqual([0, 1, 2, 3, 4, 5].map(crewRole), ["ENGINE", "TANKER", "BRUSH", "RESCUE", "COMMAND", "ENGINE"]);
});

test("Response Tracker roster shows demo roles and labels every illustrative number", () => {
  const { createElement } = require("react");
  const { renderToStaticMarkup } = require("react-dom/server");
  const { CrewExplanation } = loadModule("features/evacuation/components/explanations.tsx");
  // Five crews by position: Engine and Brush stop, Tanker never reaches staging, Rescue and Command stop as support.
  const phases = [fullStop, onSegment, lateStop, fullStop, fullStop];
  const ids = phases.map((_, i) => `crew-${i}`);
  const frames = Array.from({ length: 121 }, (_, i) => ({ t: i * 5, hazard_radius_m: 0, closed_edges: [], vehicles: [],
    crews: ids.map((id, c) => ({ id, state: phases[c](i * 5), scheduled_departure_s: 120 + 45 * c, departed_s: null,
      reached_staging_s: null, returned_s: null })) }));
  const replayPolicy = { policy: "coordinated", summary: {}, series: [], frames };
  const result = { demand: [], policies: [replayPolicy], crew_plan: ids.map((id, c) => ({ id, origin: `origin-${c}`,
    stage: `stage-${c}`, route: [], stage_index: c, scheduled_departure_s: 120 + 45 * c, service_seconds: 120 })) };
  const render = props => renderToStaticMarkup(createElement(CrewExplanation, { result, frame: frames.at(-1),
    showRoutes: true, showTrails: true, setRoutes: () => {}, setTrails: () => {}, locate: () => {}, ...props }));
  const html = render({ replayPolicy });
  const rows = [...html.matchAll(/<tr><th scope="row">(.*?)<\/tr>/g)].map(match => match[1].replace(/<[^>]+>/g, " "));
  assert.equal(rows.length, 5);
  assert.deepEqual(rows.map(row => ["Engine", "Tanker", "Brush", "Rescue", "Command"].find(role => row.includes(role))),
    ["Engine", "Tanker", "Brush", "Rescue", "Command"]);
  assert.ok(html.includes("Demo role · illustrative use"), html);
  assert.ok(rows[0].includes("Illustrative · not simulated: about 900 gal at 450 gpm over the 2:00 recorded stop"), rows[0]);
  assert.ok(rows[1].includes("Stop not reached") && !rows[1].includes("gal"), rows[1]);
  assert.ok(rows[2].includes("Illustrative · not simulated: about 450 gal at 450 gpm, 1:00 into the 2:00 recorded stop"),
    rows[2]);
  for (const row of rows.slice(3)) assert.ok(row.includes("Support role · no pumping") && !row.includes("gal"), row);
  const amounts = [...html.matchAll(/>([^<]*\d[\d,]*\s*(?:gal|gpm)[^<]*)</g)].map(match => match[1]);
  assert.equal(amounts.length, 2);
  for (const text of amounts) assert.ok(text.startsWith("Illustrative · not simulated: "), text);
  // Stage 2 and Research render the same roster without roles or illustration.
  const plain = render({});
  assert.ok(!/Demo role|Illustrative|gal\b|gpm/.test(plain), plain);
});

test("Crews returned counts recorded crew phases; samples never carry it (UIWS-2)", () => {
  const { createElement } = require("react");
  const { renderToStaticMarkup } = require("react-dom/server");
  const { MetricCards } = loadModule("features/evacuation/components/explanations.tsx");
  const { crewsReturned } = load("crews");
  const card = (frame, replayPolicy) => {
    const html = renderToStaticMarkup(createElement(MetricCards, { items: [
      ["Crews returned", crewsReturned(frame, replayPolicy, 600), "Returned to a model exit after staging."]] }));
    return html.match(/<b>(.*?)<\/b>/)[1];
  };
  const record = (id, state, returned_s = null) => ({ id, state, scheduled_departure_s: 0, departed_s: 0,
    reached_staging_s: 100, returned_s });
  // The simulator writes these eight sample keys and no crews_returned (simulation.py).
  const series = [0, 300, 600].map(t => ({ t, exited: 1, active: 1, not_departed: 0, queue: 0, exposed: 0,
    crews_staged: 2, closed_edges: 0 }));
  const frame = { t: 300, hazard_radius_m: 0, closed_edges: [], vehicles: [],
    crews: [record("crew-0", "returned", 280), record("crew-1", "returning"), record("crew-2", "returned", 290)] };
  const replay = { policy: "baseline", summary: { crews_returned: 3 }, series, frames: [frame] };
  assert.equal(card(frame, replay), "2");
  assert.equal(card({ ...frame, crews: [] }, replay), "0");
  // Older recordings save only the final total: unavailable mid-run, the summary at the end, never zero.
  const older = ({ crews: _crews, ...rest }) => rest;
  assert.equal(card(older(frame), replay), "Unavailable");
  assert.equal(card(older({ ...frame, t: 600 }), replay), "3");
  assert.equal(card(null, replay), "Unavailable");
});

test("one crew description feeds both the crew table row and the Locate popup", () => {
  const { createElement } = require("react");
  const { renderToStaticMarkup } = require("react-dom/server");
  const { CrewExplanation } = loadModule("features/evacuation/components/explanations.tsx");
  const { crewDetails, crewDetailLines, STOPPED_M_S } = load("crews");
  const plan = ["crew-0", "crew-1", "crew-2"].map((id, i) => ({ id, origin: `origin-${i}`, stage: `stage-${i}`, route: [],
    stage_index: i, scheduled_departure_s: 60 * (i + 1), service_seconds: 120 }));
  const result = { demand: [], crew_plan: plan };
  const frame = { t: 400, hazard_radius_m: 0, closed_edges: [],
    vehicles: [["crew-0", -121, 39, STOPPED_M_S / 2, 1], ["crew-1", -121, 39, 10, 1], ["civ-1", -121, 39, 0, 0]],
    crews: [
      { id: "crew-0", state: "staging_stop", scheduled_departure_s: 60, departed_s: 75, reached_staging_s: 300, returned_s: null },
      { id: "crew-1", state: "returned", scheduled_departure_s: 120, departed_s: 130, reached_staging_s: 200, returned_s: 390 },
      { id: "crew-2", state: "waiting_to_depart", scheduled_departure_s: 180, departed_s: null, reached_staging_s: null, returned_s: null },
    ] };
  const stopped = crewDetails(result, frame, "crew-0");
  assert.equal(stopped.name, "Crew 1");
  assert.equal(stopped.speed, "stopped or very slow");
  assert.deepEqual(crewDetailLines(stopped), [["Phase", "Making the assigned staging stop"],
    ["Recorded speed", "stopped or very slow"], ["Road entry", "1:00 scheduled · Entered at 1:15"],
    ["Staging", "Reached segment 5:00"]]);
  const moving = crewDetails(result, frame, "crew-1");
  assert.equal(moving.number, 2);
  assert.equal(moving.speed, "22.4 mph");
  assert.deepEqual(crewDetailLines(moving).at(-1), ["Staging", "Reached segment 3:20 · Returned 6:30"]);
  const waiting = crewDetails(result, frame, "crew-2");
  assert.equal(waiting.speed, null);
  assert.equal(waiting.plannedOrigin, true);
  assert.deepEqual(crewDetailLines(waiting).map(([label]) => label), ["Phase", "Road entry", "Staging"]);
  assert.equal(crewDetails(result, frame, "civ-1"), null);
  // A recording without per-crew timing says so rather than inventing times.
  const older = crewDetails(result, { ...frame, crews: undefined }, "crew-1");
  assert.deepEqual([older.phase, older.entry, older.staging], ["On road; phase not recorded",
    "Actual entry time not saved", "Individual timing not saved"]);
  // The table row shows exactly the popup's values.
  const html = renderToStaticMarkup(createElement(CrewExplanation, { result, frame, showRoutes: true, showTrails: true,
    setRoutes: () => {}, setTrails: () => {}, locate: () => {} }));
  const rows = [...html.matchAll(/<tr><th scope="row">(.*?)<\/tr>/g)].map(match => match[1].replace(/<[^>]+>/g, " "));
  for (const [i, details] of [stopped, moving, waiting].entries()) {
    assert.ok(rows[i].startsWith(details.name), rows[i]);
    for (const [, value] of crewDetailLines(details)) {
      for (const part of value.split(" · ")) assert.ok(rows[i].includes(part), `${part} in ${rows[i]}`);
    }
  }
});

test("run evidence shows a reused result's basis and simulation date only when the result was reused", () => {
  const { createElement } = require("react");
  const { renderToStaticMarkup } = require("react-dom/server");
  const { RunEvidence } = loadModule("features/evacuation/components/explanations.tsx");
  const result = { engine: "SUMO", engine_version: "1.27.1", sources: { sources: [] },
    network: { edge_count: 10, retrieved_at: 1790000000, osm_sha256: "a", net_sha256: "b" },
    scenario: { vehicles: 20, crews: 2, seed: 7 }, policies: [missionPolicy([])] };
  const fresh = renderToStaticMarkup(createElement(RunEvidence, { result }));
  assert.doesNotMatch(fresh, /REUSED RESULT/);
  const reused = renderToStaticMarkup(createElement(RunEvidence, { result: { ...result,
    reused_result: { basis: "Simulated outcomes reused from an identical earlier run.", simulated_at: 1790000000 } } }));
  assert.match(reused, /REUSED RESULT/);
  assert.match(reused, /Simulated outcomes reused from an identical earlier run\./);
  assert.ok(reused.includes(`Simulated ${units.formatDateTime(1790000000)}.`), reused);
});
