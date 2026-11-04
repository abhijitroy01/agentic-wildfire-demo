/* Map graphics: a mapData change while an earlier load is still running ends with the latest data's graphics,
 * graphics are built before any layer changes, and the overlay queries are bounded.
 * Run: npm --prefix ui run test:metrics */
const test = require("node:test");
const assert = require("node:assert/strict");
const React = require("react");
const { act, create } = require("react-test-renderer");
const { load } = require("../ts-module.cjs");

const LAYERS = ["riskZonesLayer", "locationsLayer", "firesLayer", "windLayer", "demographicLayer", "vegetationLayer"];
/** Graphics layers that record what they hold. */
const recordingLayers = () => Object.fromEntries(LAYERS.map(name => {
  const held = [];
  return [name, { held, add: graphic => held.push(graphic), addMany: graphics => held.push(...graphics),
    removeAll: () => held.splice(0) }];
}));
const settle = () => new Promise(resolve => setImmediate(resolve));

/** A stand-in for ../utils/mapGraphicsLoader whose builds finish when the test says, tagged with their mapData. */
function pausedLoader() {
  const loads = [];
  return { loads, module: {
    buildMapGraphics: mapData => new Promise(resolve =>
      loads.push({ mapData, resolve: () => resolve({ fires: [`${mapData.label} fire`] }) })),
    buildFireExposureGraphics: async () => [],
    showMapGraphics: async (layers, graphics) => {
      layers.firesLayer.removeAll();
      layers.firesLayer.addMany(graphics.fires);
    },
  } };
}

test("a mapData change while a load is running ends with the latest data's graphics", async () => {
  const { loads, module } = pausedLoader();
  const { useMapGraphics } = load("features/map/hooks/useMapGraphics.ts",
    { stubs: { "../utils/mapGraphicsLoader": module } });
  const layers = recordingLayers(), mapView = {};
  let state, renderer;
  function Harness({ mapData }) {
    state = useMapGraphics({ layers, mapData, centerLocation: null, mapView, cameraAvailable: false });
    return null;
  }
  const first = { label: "first answer", locations: [] }, latest = { label: "latest answer", locations: [] };
  await act(async () => { renderer = create(React.createElement(Harness, { mapData: first })); await settle(); });
  try {
    await act(async () => { renderer.update(React.createElement(Harness, { mapData: latest })); await settle(); });
    // The latest load finishes first, then the earlier one: the earlier answer must not replace the latest.
    for (const index of [1, 0]) await act(async () => { loads[index]?.resolve(); await settle(); });
    assert.deepEqual(layers.firesLayer.held, ["latest answer fire"], "The map shows the latest answer");
    assert.deepEqual(loads.map(started => started.mapData.label), ["first answer", "latest answer"],
      "Each mapData starts its own load");
    assert.equal(state.isLoading, false);
  } finally {
    await act(async () => renderer.unmount());
  }
});

test("selecting a retained scenario time reuses the same in-flight base graphics without provider work", async () => {
  const { loads, module } = pausedLoader();
  module.buildFireExposureGraphics = async (_mapData, selections) => [`outline ${selections?.saved ?? 120}`];
  module.showMapGraphics = async (layers, graphics) => {
    layers.firesLayer.removeAll();
    layers.firesLayer.addMany(graphics.fires);
    layers.riskZonesLayer.removeAll();
    layers.riskZonesLayer.addMany(graphics.riskZones);
  };
  const { useMapGraphics } = load("features/map/hooks/useMapGraphics.ts",
    { stubs: { "../utils/mapGraphicsLoader": module } });
  const layers = recordingLayers(), mapView = {}, mapData = { label: "saved fire", locations: [] };
  let renderer;
  function Harness({ exposureSelections }) {
    useMapGraphics({ layers, mapData, centerLocation: null, mapView, cameraAvailable: false, exposureSelections });
    return null;
  }
  try {
    await act(async () => { renderer = create(React.createElement(Harness, { exposureSelections: {} })); await settle(); });
    await act(async () => renderer.update(React.createElement(Harness, { exposureSelections: { saved: 30 } })));
    assert.equal(loads.length, 1, "No duplicate base build while the first load is pending");
    await act(async () => { loads[0].resolve(); await settle(); });
    assert.deepEqual(layers.riskZonesLayer.held, ["outline 30"]);
    await act(async () => renderer.update(React.createElement(Harness, { exposureSelections: { saved: 60 } })));
    assert.equal(loads.length, 1, "Completed base graphics remain reused");
    assert.deepEqual(layers.riskZonesLayer.held, ["outline 60"]);
  } finally {
    if (renderer) await act(async () => renderer.unmount());
  }
});

test("an unfinished answer's graphics never reach layers after its map unmounts", async () => {
  const { loads, module } = pausedLoader();
  const { useMapGraphics } = load("features/map/hooks/useMapGraphics.ts",
    { stubs: { "../utils/mapGraphicsLoader": module } });
  const layers = recordingLayers();
  function Harness() {
    useMapGraphics({ layers, mapData:{label:"departed answer",locations:[]},
      centerLocation:null,mapView:{},cameraAvailable:false });
    return null;
  }
  let renderer;
  await act(async () => { renderer = create(React.createElement(Harness)); await settle(); });
  await act(async () => renderer.unmount());
  await act(async () => { loads[0].resolve(); await settle(); });
  assert.deepEqual(layers.firesLayer.held, [], "Unmounting fences the pending graphics before the layers change");
});

test("job hydration rebuilds only local fire/weather geometry while base provider overlays remain reused", async () => {
  const {loads,module}=pausedLoader(),observations=[];
  module.buildFireObservationGraphics=async data=>{
    observations.push(data.fireExposureData);
    return {fires:[`weather ${data.fireExposureData.length}`],wind:[]};
  };
  const {useMapGraphics}=load("features/map/hooks/useMapGraphics.ts",{stubs:{"../utils/mapGraphicsLoader":module}});
  const layers=recordingLayers(),mapView={},mapData={label:"pending answer",locations:[]};
  let renderer;
  function Harness({assessments}){
    useMapGraphics({layers,mapView,mapData,centerLocation:null,cameraAvailable:false,assessments});
    return null;
  }
  try{
    await act(async()=>{renderer=create(React.createElement(Harness,{assessments:[]}));await settle();});
    await act(async()=>{loads[0].resolve();await settle();});
    await act(async()=>renderer.update(React.createElement(Harness,{assessments:[{assessment_id:"saved"}]})));
    assert.equal(loads.length,1,"Completed status never triggers another source-overlay build");
    assert.equal(observations.at(-1).length,1);
    assert.deepEqual(layers.firesLayer.held,["weather 1"]);
  }finally{if(renderer)await act(async()=>renderer.unmount());}
});

test("graphics are built off the layers: saved exposure renders and wind icons follow the measured mph", async () => {
  // Each ArcGIS class becomes a record of its name and constructor properties.
  const stubs = Object.fromEntries(["Graphic", "geometry/Point", "geometry/Polygon", "symbols/SimpleFillSymbol",
    "symbols/SimpleMarkerSymbol", "symbols/PictureMarkerSymbol"].map(name => [`@arcgis/core/${name}`, { __esModule: true,
    default: class { constructor(properties) { Object.assign(this, { kind: name.split("/").pop() }, properties); } } }]));
  const { buildMapGraphics, buildFireExposureGraphics, buildFireObservationGraphics, showMapGraphics, windIcon } =
    load("features/map/utils/mapGraphicsLoader.ts", { stubs });
  const { SYMBOLS } = load("shared/constants.ts");
  const { CAMERA_FEED_ACTION } = load("features/map/utils/popupGenerators.ts");
  const { light, moderate, strong, veryStrong, extreme } = SYMBOLS.wind;
  assert.deepEqual([5.9, 6, 11.9, 12, 18.9, 19, 30.9, 31].map(mph => windIcon(mph).url),
    [light, moderate, moderate, strong, strong, veryStrong, veryStrong, extreme]);
  assert.deepEqual([0, 10, 30].map(mph => windIcon(mph).size), [30, 30, 40]);
  const assessment = { assessment_id: "saved-exposure", fire_ref: "fire-observation", availability: "available",
    mode: "proximity", fire: { latitude: 39, longitude: -121, name: "Fixture fire" }, assumptions: [],
    geojson: { type: "FeatureCollection", features: [{ type: "Feature",
      properties: { kind: "proximity", label: "Saved spatial context" },
      geometry: { type: "Polygon", coordinates: [[[-121, 39], [-120.9, 39], [-121, 39.1], [-121, 39]]] } }] } };
  const mapData = { fireExposureData: [assessment], locations: [
    { latitude: 39, longitude: -121, fire: { name: "Fixture fire" },
      currentWeather: { wind: { speed: { value: 10, unit: "m/s" }, direction: { value: 90 } } } },
    { latitude: 38, longitude: -120, name: "Station", currentWeather: {} }] };
  const layers = recordingLayers();
  const graphics = await buildMapGraphics(mapData, null, { cameraAction: true });
  assert.ok(Object.values(layers).every(layer => layer.held.length === 0), "Building changes no layer");
  assert.deepEqual(graphics.riskZones, [], "Historical distance context defaults off");
  assert.deepEqual((await buildFireExposureGraphics(mapData, {"saved-exposure":1}))
    .map(graphic => graphic.attributes.assessment_id), ["saved-exposure"]);
  assert.deepEqual([graphics.center, graphics.demographic, graphics.vegetation], [[], [], []], "No search area");
  // The fire's assessment reports no suitable weather, so the old unrelated location weather is not reused.
  const [diamond] = graphics.wind;
  assert.equal(graphics.wind.length, 1);
  assert.equal(graphics.fires[0].attributes.currentWeather, null,
    "The fire popup also avoids unrelated weather when the assessment has no suitable observation");
  const withWeather = await buildMapGraphics({ ...mapData, fireExposureData: [{
    ...assessment, weather: { availability: "available", source: "DTN", wind_speed_mps: 10,
      wind_from_degrees: 90, query_latitude: 39, query_longitude: -121 },
  }] }, null, { cameraAction: true });
  const [arrow] = withWeather.wind;
  // 10 m/s is about 22.4 mph; the arrow points where the wind blows to.
  assert.deepEqual([arrow.symbol.kind, arrow.symbol.url, arrow.symbol.angle], ["PictureMarkerSymbol", veryStrong, 270]);
  assert.deepEqual([arrow.geometry.longitude, arrow.geometry.latitude, arrow.symbol.xoffset], [-121, 39, 24]);
  assert.deepEqual(withWeather.fires[0].attributes.currentWeather, arrow.attributes.currentWeather,
    "Fire and weather popups share the retained per-fire weather");
  const calm = await buildFireObservationGraphics({...mapData,fireExposureData:[{
    ...assessment,weather:{availability:"available",source:"DTN",wind_speed_mps:0,wind_from_degrees:null,
      query_latitude:39,query_longitude:-121},
  }]});
  assert.equal(calm.wind[0].symbol.kind,"SimpleMarkerSymbol","Calm has no invented direction arrow");
  assert.equal(calm.fires[0].attributes.currentWeather.wind.speed.value,0);
  const mismatched = await buildMapGraphics({ ...mapData, fireExposureData:[{
    ...assessment, weather:{availability:"available",source:"DTN",wind_speed_mps:10,wind_from_degrees:90,
      query_latitude:38,query_longitude:-120},
  }] }, null);
  assert.equal(mismatched.fires[0].attributes.currentWeather, null);
  assert.equal(mismatched.wind.length, 1, "Weather queried at a different place never becomes the fire's arrow");
  const rawMismatch = await buildMapGraphics({locations:[{...mapData.locations[0],
    currentWeather:{...mapData.locations[0].currentWeather,queryLocation:{latitude:38,longitude:-120}},
  }]}, null);
  assert.equal(rawMismatch.wind.length, 0, "An explicit query coordinate also fences unassessed weather");
  assert.equal(rawMismatch.fires[0].attributes.currentWeather, null);
  assert.deepEqual([diamond.symbol.kind, diamond.symbol.style, diamond.attributes.name],
    ["SimpleMarkerSymbol", "diamond", "Station"]);
  assert.deepEqual(graphics.fires[0].popupTemplate.actions, [{ ...CAMERA_FEED_ACTION, type: "button" }]);
  await showMapGraphics(layers, graphics);
  assert.deepEqual(layers.firesLayer.held, graphics.fires);
  assert.deepEqual(layers.riskZonesLayer.held, graphics.riskZones);
});

test("both overlay queries are bounded by a 15-second timeout signal (S-F3)", async () => {
  const { fetch: realFetch } = globalThis, { timeout } = AbortSignal;
  const calls = [], timeouts = [], errors = [];
  globalThis.fetch = async (url, init) => {
    calls.push([new URL(url).host, init?.signal instanceof AbortSignal && !init.signal.aborted]);
    return { ok: true, json: async () => ({ features: [] }) };
  };
  AbortSignal.timeout = milliseconds => { timeouts.push(milliseconds); return timeout.call(AbortSignal, milliseconds); };
  try {
    const { fetchDemographicAreas, fetchFederalLand } = load("features/map/services/overlayData.ts", { stubs: {
      "@/shared/config": { POPULATION_SERVICE_URL: "https://population.invalid/layer",
        VEGETATION_SERVICE_URL: "https://federal-land.invalid/layer" },
      "@/shared/utils/logger": { logger: { error: (...args) => errors.push(args) } } } });
    const center = { latitude: 39.76, longitude: -121.62 };
    assert.deepEqual([await fetchDemographicAreas(center), await fetchFederalLand(center)], [[], []]);
  } finally {
    globalThis.fetch = realFetch;
    AbortSignal.timeout = timeout;
  }
  assert.deepEqual(errors, []);
  assert.deepEqual(calls, [["population.invalid", true], ["federal-land.invalid", true]]);
  assert.deepEqual(timeouts, [15000, 15000]);
});
