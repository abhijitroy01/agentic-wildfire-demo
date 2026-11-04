/* Time contours use retained modeled geometry, never invented bands or a model request. */
const test = require("node:test");
const assert = require("node:assert/strict");
const React = require("react");
const {act,create} = require("react-test-renderer");
const {load} = require("../ts-module.cjs");
const {FIRST,landscape,GEOMETRY} = require("./fire-risk-fixtures.cjs");

const features = minutes => minutes.map(elapsed_minutes => ({type:"Feature",geometry:structuredClone(GEOMETRY),
  properties:{kind:"landscape",label:"Retained modeled contour",elapsed_minutes}}));
const result = () => landscape({geojson:{type:"FeatureCollection",features:features([15,30,60])}});
const immutable = value => {
  if (value && typeof value === "object") { Object.freeze(value); Object.values(value).forEach(immutable); }
  return value;
};
const text = value => typeof value === "string" ? value
  : Array.isArray(value) ? value.map(text).join("") : value?.children ? text(value.children) : "";
class ArcgisValue { constructor(properties) { Object.assign(this,properties); } }
const arcgis = {Graphic:ArcgisValue,Polygon:ArcgisValue,SimpleFillSymbol:ArcgisValue};

test("selected time reveals only real earlier contours, in cumulative draw order", () => {
  const {visibleExposureFeatures,scenarioHorizons} = load("features/fire-exposure/services/assessment.ts");
  const saved = immutable(result()), original = JSON.stringify(saved);
  assert.deepEqual(scenarioHorizons(saved),[15,30,60]);
  assert.deepEqual(visibleExposureFeatures(saved,{}).map(item => item.properties.elapsed_minutes),[60,30,15]);
  assert.deepEqual(visibleExposureFeatures(saved,{[FIRST]:30}).map(item => item.properties.elapsed_minutes),[30,15]);
  assert.deepEqual(visibleExposureFeatures(saved,{[FIRST]:null}),[]);
  assert.equal(JSON.stringify(saved),original,"Selection never changes retained output");
});

test("contour colors stay stable across slider changes and no geometry is smoothed or expanded", () => {
  const {fireExposureGraphics} = load("features/map/utils/fireExposureGraphics.ts");
  const saved = immutable(result()), original = JSON.stringify(saved);
  const realFetch = globalThis.fetch;
  globalThis.fetch = () => { throw new Error("Time selection must never fetch"); };
  try {
    const all = fireExposureGraphics(arcgis,[saved],{});
    const earlier = fireExposureGraphics(arcgis,[saved],{[FIRST]:30});
    assert.deepEqual(all.map(item => item.attributes.elapsed_minutes),[60,30,15]);
    assert.deepEqual(earlier.map(item => item.attributes.elapsed_minutes),[30,15]);
    assert.deepEqual(all[1].symbol,earlier[0].symbol,"The 30-minute contour retains its color");
    assert.deepEqual(all[2].symbol,earlier[1].symbol,"The 15-minute contour retains its color");
    assert.equal(new Set(all.map(item => JSON.stringify(item.symbol.outline.color))).size,3);
    assert.deepEqual(all[0].geometry,all[1].geometry,"Coincident real footprints remain coincident");
    assert.ok(all.every(item => item.symbol.color[3] <= .15),"Fills preserve basemap context");
    assert.equal(JSON.stringify(saved),original);
  } finally { globalThis.fetch = realFetch; }
});

test("no-growth statement requires equal retained geometry at successive returned times", () => {
  const {noAdditionalScenarioGrowth} = load("features/fire-exposure/services/assessment.ts");
  const saved = result();
  assert.equal(noAdditionalScenarioGrowth(saved,{}),true);
  assert.equal(noAdditionalScenarioGrowth(saved,{[FIRST]:30}),true);
  assert.equal(noAdditionalScenarioGrowth(saved,{[FIRST]:15}),false,"There is no previous returned time");
  assert.equal(noAdditionalScenarioGrowth(saved,{[FIRST]:null}),false);
  saved.geojson.features[2].geometry.coordinates[0][1][0] += .001;
  assert.equal(noAdditionalScenarioGrowth(saved,{}),false,"Growth is never suppressed by a label");
  assert.equal(noAdditionalScenarioGrowth(landscape({availability:"unavailable",geojson:{features:[]}}),{}),false);
});

test("legend identifies modeled arrival times and keeps wind details collapsed until opened", async () => {
  const Legend = load("features/map/components/MapLegend.tsx",{stubs:{"./MapLegend.css":{},"./Accordion.css":{}}}).default;
  let renderer;
  try {
    await act(async () => { renderer = create(React.createElement(Legend,{
      showWind:true,showLandscape:true,showObservedPerimeter:true,landscapeMinute:30,landscapeHorizons:[15,30,60],
    })); });
    const content = text(renderer.toJSON());
    assert.match(content,/Reported fire location/);
    assert.match(content,/Reported perimeter/);
    assert.match(content,/Modeled spread · 30 min/);
    assert.match(content,/Reached by 15 min/);
    assert.match(content,/Reached by 30 min/);
    assert.doesNotMatch(content,/Reached by 60 min/);
    assert.match(content,/Minutes after assumed ignition/);
    assert.doesNotMatch(content,/High risk|Low risk|burn probability/i);
    assert.doesNotMatch(content,/0-6 mph/,"Wind ranges are hidden by default");
    const toggle = renderer.root.findAllByType("button").find(button => button.props["aria-expanded"] === false);
    await act(async () => toggle.props.onClick());
    assert.match(text(renderer.toJSON()),/0-6 mph/);
  } finally { if (renderer) await act(async () => renderer.unmount()); }
});

test("uniform-fuel legend has real time keys with the same stable colors as its cumulative graphics", async () => {
  const Legend = load("features/map/components/MapLegend.tsx",{stubs:{"./MapLegend.css":{},"./Accordion.css":{}}}).default;
  const {scenarioContourStyle} = load("features/map/utils/fireExposureGraphics.ts");
  let renderer;
  try {
    await act(async () => { renderer = create(React.createElement(Legend,{
      showSurfaceFire:true,surfaceFireMinute:60,surfaceFireHorizons:[30,60,120],
    })); });
    const content = text(renderer.toJSON());
    assert.match(content,/Uniform-fuel spread · 60 min/);
    assert.match(content,/Reached by 30 min/);
    assert.match(content,/Reached by 60 min/);
    assert.doesNotMatch(content,/Reached by 120 min|Modeled spread/);
    const swatches = renderer.root.findAllByType("span").filter(span =>
      span.props.className === "legend-icon scenario-contour-icon");
    assert.deepEqual(swatches.map(span => span.props.style.borderColor),[30,60].map(minutes =>
      `rgb(${scenarioContourStyle(minutes,[30,60,120]).outline.slice(0,3).join(" ")})`));
    assert.match(content,/Minutes after assumed ignition/);
  } finally { if (renderer) await act(async () => renderer.unmount()); }
});
