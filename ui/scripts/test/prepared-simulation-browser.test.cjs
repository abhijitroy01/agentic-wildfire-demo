/* Offline assertions used by the maintained real-browser SUMO release acceptance. */
const test = require("node:test");
const assert = require("node:assert/strict");
const {acceptanceOptions,verifyIncidentPoint,verifyCompletedRun} =
  require("../../../deployment/acceptance/prepared_simulation_checks.cjs");

function fixture(mode = "synthetic") {
  const options = acceptanceOptions({WFM_SIMULATION_TRAFFIC_MODE:mode});
  const incident = {location:{status:"resolved",origin:"geocoded_address",reported_address:options.location.address,
    lat:47.603,lon:-122.33,address_type:"PointAddress",provider:"Amazon Location"}};
  const scenario = {lat:47.603,lon:-122.33,vehicles:400,crews:4,radius_km:3,highway:"osm:name:fixture",
    exit_direction:"south",traffic_mode:mode};
  const draft = {scenario,network_validation:{network_sha256:"actual-network"}};
  const traffic = mode === "synthetic" ? {mode,status:"not_requested",matched_edges:0}
    : {mode,provider:"TomTom",status:"applied",matched_edges:2};
  const result = {scenario,network:{net_sha256:"actual-network"},engine:"Eclipse SUMO",traffic,
    assumptions:["All demand is synthetic.","The analyst-defined hazard is not a wildfire forecast."],
    policies:["baseline","coordinated","phased"].map(policy => ({policy,summary:{
      valid_engine_run:true,valid_accounting:true,exited_vehicles:250,active_vehicles:100,not_departed_vehicles:50}}))};
  return {options,incident,draft,result};
}

test("prepared browser acceptance defaults to Seattle and synthetic traffic with exclusive configurable input", () => {
  assert.deepEqual(acceptanceOptions({}),{location:{kind:"address",address:"600 4th Ave, Seattle, WA"},
    trafficMode:"synthetic",expectedPoint:null});
  assert.deepEqual(acceptanceOptions({WFM_SIMULATION_LATITUDE:"0",WFM_SIMULATION_LONGITUDE:"0"}).location,
    {kind:"coordinates",coordinates:{lat:0,lon:0}});
  assert.equal(acceptanceOptions({WFM_SIMULATION_PRESET:"palisades-2025"}).location.preset,"palisades-2025");
  for (const env of [{WFM_SIMULATION_LATITUDE:"47"}, {WFM_SIMULATION_LATITUDE:"NaN",WFM_SIMULATION_LONGITUDE:"0"},
    {WFM_SIMULATION_ADDRESS:"Seattle",WFM_SIMULATION_PRESET:"paradise-2018"},
    {WFM_SIMULATION_TRAFFIC_MODE:"pretend_live"}]) assert.throws(() => acceptanceOptions(env));
});

test("live receipt validation requires the same exact point in the draft and keeps city matches out of street cases", () => {
  const {options,incident,draft} = fixture();
  verifyIncidentPoint(incident,draft.scenario,options);
  assert.throws(() => verifyIncidentPoint(incident,{...draft.scenario,lat:47.61},options),/latitude must equal/);
  assert.throws(() => verifyIncidentPoint({...incident,location:{...incident.location,address_type:"Locality"}},
    draft.scenario,options),/city centroid/);
  const coordinates = acceptanceOptions({WFM_SIMULATION_LATITUDE:"0",WFM_SIMULATION_LONGITUDE:"0"});
  verifyIncidentPoint({location:{status:"resolved",lat:0,lon:0,origin:"user_coordinates",provider:"User input",score:null}},
    {lat:0,lon:0},coordinates);
  assert.throws(() => verifyIncidentPoint({location:{status:"resolved",lat:0.00001,lon:0,origin:"user_coordinates",
    provider:"User input",score:null}},{lat:0.00001,lon:0},coordinates),/latitude must stay unchanged/);
});

test("synthetic release acceptance still checks real engine, accounting, reviewed geometry and hazard assumptions", () => {
  const {options,incident,draft,result} = fixture();
  verifyCompletedRun(result,draft,incident,options);
  assert.throws(() => verifyCompletedRun({...result,engine:"Mock simulator"},draft,incident,options));
  assert.throws(() => verifyCompletedRun({...result,traffic:{...result.traffic,segments:[]}},draft,incident,options));
  assert.throws(() => verifyCompletedRun({...result,network:{net_sha256:"another-network"}},draft,incident,options));
  assert.throws(() => verifyCompletedRun({...result,assumptions:["Synthetic demand"]},draft,incident,options),/hazards/);
  const broken = structuredClone(result);
  broken.policies[1].summary.active_vehicles = 99;
  assert.throws(() => verifyCompletedRun(broken,draft,incident,options));
});

test("explicit provider acceptance still requires actual TomTom matches rather than assuming synthetic is enough", () => {
  const {options,incident,draft,result} = fixture("provider_speed_limits");
  verifyCompletedRun(result,draft,incident,options);
  assert.throws(() => verifyCompletedRun({...result,traffic:{...result.traffic,matched_edges:0}},draft,incident,options),
    /actual matched roads/);
  assert.throws(() => verifyCompletedRun({...result,traffic:{...result.traffic,status:"unavailable"}},draft,incident,options));
});
