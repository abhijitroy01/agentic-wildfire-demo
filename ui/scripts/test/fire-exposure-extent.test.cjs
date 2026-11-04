/* Initial camera framing uses the answer's retained geometry; selection controls never compute or fetch a model. */
const test = require("node:test");
const assert = require("node:assert/strict");
const React = require("react");
const { act, create } = require("react-test-renderer");
const { load } = require("../ts-module.cjs");
const { fireExposureInitialExtent, BASEMAP_LATITUDE_LIMIT } = load("features/map/utils/fireExposureExtent.ts");

const shape = coordinates => ({type:"Polygon",coordinates:[coordinates]});
const polygon = (west, south, east, north) => shape([
  [west,south],[east,south],[east,north],[west,north],[west,south],
]);
function mapData(geometry, longitude = 0, latitude = 0) {
  return {locations:[{longitude,latitude,fire:{name:"Reported fire"}}],fireExposureData:[{
    availability:"available",geojson:{features:[{geometry,properties:{kind:"proximity"}}]},
  }]};
}
const viewport = {width:1000,height:800};

test("initial framing contains saved exposure rather than only the fire marker or search city", () => {
  const data = mapData(polygon(-.3,-.2,.4,.5));
  const saved = JSON.stringify(data);
  const bounds = fireExposureInitialExtent(data,{longitude:.02,latitude:.01},viewport);
  assert.ok(bounds.xmin < -.3 && bounds.xmax > .4 && bounds.ymin < -.2 && bounds.ymax > .5);
  assert.ok(bounds.zoom >= 0 && bounds.zoom <= 12);
  assert.equal(JSON.stringify(data), saved, "Camera framing changes no source evidence");
  assert.ok(Object.values(fireExposureInitialExtent({...data,locations:[
    {longitude:NaN,latitude:91,fire:{}},...data.locations,
  ]},null,viewport)).every(Number.isFinite), "An invalid reported coordinate never becomes the camera anchor");
  assert.equal(fireExposureInitialExtent({...data,fireExposureData:[]},null,viewport), null);
  assert.equal(fireExposureInitialExtent({...data,fireExposureData:[{
    ...data.fireExposureData[0],availability:"unavailable",
  }]},null,viewport), null, "Unavailable output contributes no invented camera geometry");
});

test("split antimeridian geometry frames adjacent fires in the shortest longitude arc", () => {
  const geometry = {type:"MultiPolygon",coordinates:[
    polygon(179.6,10,180,10.3).coordinates,polygon(-180,10,-179.7,10.3).coordinates,
  ]};
  for (const longitude of [179.8,-179.8]) {
    const data = mapData(geometry,longitude,10.1);
    const before = JSON.stringify(data);
    const bounds = fireExposureInitialExtent(data,null,viewport);
    assert.ok(bounds.xmax - bounds.xmin < 2, "Dateline neighbors do not zoom to the whole world");
    assert.ok(bounds.xmin <= longitude && bounds.xmax >= longitude, "Choose the fire's nearest world copy");
    assert.equal(JSON.stringify(data),before);
  }
});

test("camera latitude and world width are bounded without changing high-latitude fire coordinates", () => {
  const geometry = shape([[-180,89.8],[-90,89.8],[0,89.8],[90,89.8],[180,89.8],
    [180,90],[-180,90],[-180,89.8]]);
  const data = mapData(geometry,179,89.9), before = JSON.stringify(data);
  const bounds = fireExposureInitialExtent(data,null,viewport);
  assert.ok(Object.values(bounds).every(Number.isFinite));
  assert.ok(bounds.xmax - bounds.xmin <= 360);
  assert.ok(bounds.ymin >= -BASEMAP_LATITUDE_LIMIT - 1e-9 && bounds.ymax <= BASEMAP_LATITUDE_LIMIT + 1e-9);
  assert.ok(bounds.ymax > bounds.ymin);
  assert.equal(JSON.stringify(data),before, "Projection limits apply only to the camera, never the fire or polygon");
  assert.equal(fireExposureInitialExtent(mapData(polygon(0,0,.00001,.00001)),null,viewport).zoom,12,
    "Tiny scenario geometry never causes excessive initial zoom");
  const terrain=mapData(polygon(-121,39,-120.999,39.001),-121,39),beforeTerrain=JSON.stringify(terrain);
  const selected=fireExposureInitialExtent(terrain,null,{...viewport,terrainFocus:true});
  assert.equal(selected.zoom,15,"Selected 30m-scale output keeps neighboring terrain and roads in view");
  assert.ok(selected.xmax-selected.xmin<.002 && selected.ymax-selected.ymin<.002,
    "Camera framing does not inflate the physical modeled footprint");
  assert.equal(JSON.stringify(terrain),beforeTerrain);
});

test("the map fits returned geometry once; time-selection rerenders neither refit nor fetch it", async t => {
  t.mock.timers.enable({apis:["setTimeout"]});
  const calls = [], mapView = {...viewport,goTo:async (...args) => calls.push(args)};
  const data = mapData(polygon(-.3,-.3,.3,.3));
  const {useMapCentering} = load("features/map/hooks/useMapCentering.ts", {stubs:{
    "@arcgis/core/geometry/Extent":{__esModule:true,default:class {
      constructor(value) {Object.assign(this,value);}
    }},
  }});
  function Harness({selectedMinute}) {
    void selectedMinute;
    useMapCentering({mapView,mapData:data,centerLocation:null});
    return null;
  }
  let renderer;
  const settle = () => new Promise(resolve => setImmediate(resolve));
  try {
    await act(async () => {renderer = create(React.createElement(Harness,{selectedMinute:120}));});
    await act(async () => {t.mock.timers.tick(1000);await settle();});
    assert.equal(calls.length,1);
    assert.ok(calls[0][0].target.xmin < -.3 && calls[0][0].target.xmax > .3);
    await act(async () => {renderer.update(React.createElement(Harness,{selectedMinute:30}));});
    await act(async () => {t.mock.timers.tick(1000);await settle();});
    assert.equal(calls.length,1,"Changing only scenario time leaves the initial camera framing alone");
  } finally {
    if (renderer) await act(async () => renderer.unmount());
    t.mock.timers.reset();
  }
});
