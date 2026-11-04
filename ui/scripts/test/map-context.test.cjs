/* Public map context reports provider failures without altering retained simulation data. */
const test=require("node:test");
const assert=require("node:assert/strict");
const React=require("react");
const {act,create}=require("react-test-renderer");
const {load}=require("../ts-module.cjs");
const {ContextRequests,hasVisibleTerrainPixels,isMapRequestAbort}=load("features/map/services/mapContext.ts");
const abort=error=>error.name==="AbortError";
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});
  return {promise,resolve,reject};};

test("missing hillshade pixels remain unavailable; a valid flat pixel block is retained",()=>{
  assert.equal(hasVisibleTerrainPixels([]),false);
  assert.equal(hasVisibleTerrainPixels([255,255,255,0,0,0,0,0]),false);
  assert.equal(hasVisibleTerrainPixels([0,0,0,255]),true,"Valid uniformly shaded flat terrain is retained");
  assert.equal(isMapRequestAbort({name:"AbortError"}),true);
  assert.equal(isMapRequestAbort(new Error("Provider failed")),false);
});

test("one successful tile cannot conceal another failed tile; the same tile must succeed or be retried",async()=>{
  const states=[],monitor=new ContextRequests(status=>states.push(status));
  await assert.rejects(monitor.run("bad",async()=>{throw new Error("Provider failed");},abort));
  await monitor.run("good",async()=>"tile",abort);
  assert.equal(states.at(-1),"failed");
  await monitor.run("bad",async()=>"retried tile",abort);
  assert.equal(states.at(-1),"ready");
});

test("cancelled map requests and stale completions after retry cannot fabricate success or failure",async()=>{
  const states=[],monitor=new ContextRequests(status=>states.push(status)),old=deferred();
  const pending=monitor.run("old",()=>old.promise,abort);
  monitor.reset();
  old.resolve("old tile");await pending;
  assert.equal(states.at(-1),"loading","Only a new successful request establishes readiness");
  await assert.rejects(monitor.run("pan",async()=>{
    throw Object.assign(new Error("Pan cancelled"),{name:"AbortError"});},abort));
  assert.notEqual(states.at(-1),"failed");
  await monitor.run("new",async()=>"new tile",abort);
  assert.equal(states.at(-1),"ready");
});

test("context controls keep failures explicit and terrain toggle changes only the requested layer visibility",async()=>{
  const Controls=load("features/map/components/MapContextControls.tsx").default;
  const visibility=[],retry=[],props={status:{streets:"failed",terrain:"failed"},terrainVisible:true,
    onTerrainVisible:value=>visibility.push(value),onRetry:()=>retry.push(true)};
  let renderer;
  try{
    await act(async()=>{renderer=create(React.createElement(Controls,props));});
    const text=()=>JSON.stringify(renderer.toJSON());
    assert.match(text(),/Street map tiles unavailable/);assert.match(text(),/Terrain shading unavailable/);
    await act(async()=>renderer.root.findByType("input").props.onChange({target:{checked:false}}));
    assert.deepEqual(visibility,[false]);
    await act(async()=>renderer.root.findByType("button").props.onClick());
    assert.equal(retry.length,1);
    await act(async()=>renderer.update(React.createElement(Controls,{...props,terrainVisible:false,
      status:{streets:"ready",terrain:"failed"}})));
    assert.doesNotMatch(text(),/Terrain shading unavailable/);
    assert.equal(renderer.root.findAllByType("button").length,0);
  }finally{if(renderer)await act(async()=>renderer.unmount());}
});

test("reported perimeter uses only supplied geometry, preserves holes and cannot become simulated spread",()=>{
  class Record{constructor(properties){Object.assign(this,properties);}}
  const api={Graphic:Record,Polygon:Record,SimpleFillSymbol:Record};
  const {reportedPerimeterGraphics}=load("features/map/utils/reportedPerimeterGraphics.ts");
  const ring=[[-121,39],[-120.9,39],[-120.9,39.1],[-121,39.1],[-121,39]];
  const hole=[[-120.98,39.02],[-120.96,39.02],[-120.96,39.04],[-120.98,39.04],[-120.98,39.02]];
  const source={fire:{name:"<script>source</script>",latitude:39,longitude:-121,source_id:"observed",
    source_timestamp:"2026-10-27T12:00:00Z",perimeter:{type:"Polygon",coordinates:[ring,hole]}}};
  const before=JSON.stringify(source);
  const graphics=reportedPerimeterGraphics(api,[source,source,{fire:{latitude:39,longitude:-121}}]);
  assert.equal(graphics.length,1,"One source perimeter, no geometry invented for the point-only fire");
  assert.equal(graphics[0].geometry.rings.length,2);
  assert.equal(graphics[0].symbol.style,"none");
  assert.equal(graphics[0].symbol.outline.style,"dash");
  assert.equal(graphics[0].attributes.type,"firePerimeter");
  assert.match(graphics[0].popupTemplate.content,/capture time was not supplied/);
  assert.doesNotMatch(graphics[0].popupTemplate.content,/<script>/);
  assert.equal(JSON.stringify(source),before,"Source rings stay unchanged");
});
