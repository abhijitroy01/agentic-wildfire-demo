/* Pure checks shared by live browser acceptance and offline regression tests. */
const assert = require("node:assert/strict");

function point(env, prefix) {
  const lat = env[`${prefix}_LATITUDE`], lon = env[`${prefix}_LONGITUDE`];
  if (lat == null && lon == null) return null;
  assert.ok(lat?.trim() && lon?.trim(),`${prefix}: provide both latitude and longitude`);
  const value = {lat:Number(lat),lon:Number(lon)};
  assert.ok(Number.isFinite(value.lat) && Number.isFinite(value.lon)
    && Math.abs(value.lat)<=90 && Math.abs(value.lon)<=180,`${prefix}: invalid WGS84 coordinate`);
  return value;
}

function acceptanceOptions(env) {
  const coordinates = point(env,"WFM_SIMULATION");
  const address = env.WFM_SIMULATION_ADDRESS?.trim(), preset = env.WFM_SIMULATION_PRESET?.trim();
  assert.ok([coordinates,address,preset].filter(Boolean).length<=1,"Choose one address, preset or coordinate");
  assert.ok(!preset || ["paradise-2018","palisades-2025"].includes(preset),"Unknown reference preset");
  const trafficMode = env.WFM_SIMULATION_TRAFFIC_MODE || "synthetic";
  assert.ok(["synthetic","provider_speed_limits"].includes(trafficMode),"Invalid requested traffic mode");
  const location = coordinates ? {kind:"coordinates",coordinates} : preset ? {kind:"preset",preset}
    : {kind:"address",address:address || "600 4th Ave, Seattle, WA"};
  return {location,trafficMode,expectedPoint:point(env,"WFM_SIMULATION_EXPECTED")};
}

function verifyIncidentPoint(incident, scenario, options) {
  const location = incident.location;
  assert.equal(location.status,"resolved");
  assert.ok(Number.isFinite(location.lat) && Number.isFinite(location.lon)
    && Math.abs(location.lat)<=90 && Math.abs(location.lon)<=180,"Verified WGS84 incident point");
  assert.equal(location.coordinate_system || "WGS84","WGS84");
  assert.equal(scenario.lat,location.lat,"Scenario latitude must equal the resolved incident receipt");
  assert.equal(scenario.lon,location.lon,"Scenario longitude must equal the resolved incident receipt");
  if (options.location.coordinates) {
    assert.equal(location.lat,options.location.coordinates.lat,"Explicit latitude must stay unchanged");
    assert.equal(location.lon,options.location.coordinates.lon,"Explicit longitude must stay unchanged");
  }
  const expected = options.expectedPoint || options.location.coordinates;
  if (expected) assert.ok(Math.abs(location.lat-expected.lat)<0.0001 &&
    Math.abs(location.lon-expected.lon)<0.0001,"Incident point must match the configured acceptance coordinate");
  if (options.location.kind === "address") {
    assert.equal(location.origin || "geocoded_address","geocoded_address");
    assert.equal(location.reported_address,options.location.address);
    assert.ok(!/locality|region|postal/i.test(location.address_type),"Street-address case cannot resolve to a city centroid");
  } else if (options.location.kind === "coordinates") {
    assert.equal(location.origin,"user_coordinates");
    assert.equal(location.provider,"User input");
    assert.equal(location.score,null,"An explicit coordinate has no geocoder match score");
  } else assert.equal(incident.preset_id,options.location.preset);
}

function verifyCompletedRun(result, draft, incident, options) {
  verifyIncidentPoint(incident,result.scenario,options);
  assert.equal(result.network.net_sha256,draft.network_validation.network_sha256);
  assert.equal(result.scenario.radius_km,draft.scenario.radius_km);
  assert.equal(result.scenario.highway,draft.scenario.highway);
  assert.ok(draft.scenario.highway !== "auto" && ["north","south","east","west"]
    .includes(draft.scenario.exit_direction),"Only a concrete reviewed road and direction may run");
  assert.equal(result.scenario.vehicles,400);
  assert.equal(result.scenario.crews,4);
  assert.equal(result.scenario.traffic_mode,options.trafficMode);
  assert.equal(result.traffic.mode,options.trafficMode);
  assert.match(result.engine,/SUMO/);
  if (options.trafficMode === "provider_speed_limits") {
    assert.equal(result.traffic.provider,"TomTom");
    assert.equal(result.traffic.status,"applied");
    assert.ok(result.traffic.matched_edges>0,"Explicit provider mode must have actual matched roads");
  } else {
    assert.equal(result.traffic.status,"not_requested");
    assert.equal(result.traffic.matched_edges,0);
    assert.equal(result.traffic.segments,undefined);
    assert.equal(result.traffic.matches,undefined);
    assert.ok(result.assumptions.some(value => /synthetic|assumed/i.test(value) && /traffic|demand/i.test(value)),
      "Synthetic demand must be stated as an assumption");
  }
  assert.ok(result.assumptions.some(value => /hazard/i.test(value) && /not a wildfire forecast/i.test(value)),
    "Assumed simulation hazards stay separate from observed fire evidence");
  assert.deepEqual(result.policies.map(policy => policy.policy),["baseline","coordinated","phased"]);
  for (const policy of result.policies) {
    assert.ok(policy.summary.valid_engine_run,`${policy.policy}: engine integrity`);
    assert.ok(policy.summary.valid_accounting,`${policy.policy}: accounting integrity`);
    assert.equal(policy.summary.exited_vehicles+policy.summary.active_vehicles+policy.summary.not_departed_vehicles,400);
  }
}

module.exports = {acceptanceOptions,verifyIncidentPoint,verifyCompletedRun};
