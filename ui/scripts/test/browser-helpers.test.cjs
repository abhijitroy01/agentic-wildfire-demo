/* Native map paint boundaries: synthetic view states reproduce the observed viewport-resize screenshot race. */
const test = require("node:test");
const assert = require("node:assert/strict");
const vm = require("node:vm");
const {mapsRedrawn,nativeMapViewIdle,observedMapViewModules} = require("../browser/helpers.cjs");

const idleView = () => ({
  ready:true,stationary:true,resizing:false,updating:false,destroyed:false,width:984,height:792,
  container:{clientWidth:984,clientHeight:792},
  allLayerViews:{toArray:()=>[{updating:false},{updating:false}]},
});

test("matching canvas dimensions do not certify a native view while its resize frame remains pending",() => {
  const view = idleView();
  view.stationary = false;
  view.resizing = true;
  assert.equal(nativeMapViewIdle(view),false);
  view.resizing = false;
  assert.equal(nativeMapViewIdle(view),false);
  view.stationary = true;
  assert.equal(nativeMapViewIdle(view),true);
});

test("native paint requires the current container size, ready view and completed visible layer updates",() => {
  for(const change of [
    view=>{view.width=983;},
    view=>{view.height=791;},
    view=>{view.ready=false;},
    view=>{view.updating=true;},
    view=>{view.destroyed=true;},
    view=>{view.container=null;},
    view=>{view.allLayerViews={toArray:()=>[{updating:false},{updating:true}]};},
  ]) {
    const view = idleView();
    change(view);
    assert.equal(nativeMapViewIdle(view),false);
  }
  assert.equal(nativeMapViewIdle(null),false);
  assert.equal(nativeMapViewIdle(idleView()),true);
});

test("public native view inspection imports only actually observed same-origin MapView script URLs",() => {
  const origin = "https://demo.example";
  const production = `${origin}/assets/MapView-BAouQVWr.js`;
  const development = `${origin}/node_modules/.vite/deps/@arcgis_core_views_MapView.js?v=recorded`;
  assert.deepEqual(observedMapViewModules([
    production,production,development,
    "https://unrelated.example/assets/MapView-external.js",
    `${origin}/assets/index-main.js`,
    `${origin}/simulation-api/v1/MapView`,
    `${origin}/assets/MapView-BAouQVWr.js#different`,
    "https://user:password@demo.example/assets/MapView-secret.js",
    "blob:https://demo.example/assets/MapView-worker.js",
    "not a URL",null,
  ],origin),[production,development]);
  assert.deepEqual(observedMapViewModules([],origin),[],
    "No URL is synthesized when the browser has not observed a native module");
});

test("Playwright string expressions execute native readiness checks instead of returning a truthy function",async () => {
  const view = idleView();
  view.resizing = true;
  view.stationary = false;
  const surface = {dataset:{},clientWidth:984,clientHeight:792};
  const canvas = {width:984,height:792,clientWidth:984,clientHeight:792,getClientRects:()=>[{}],
    closest:()=>surface};
  view.container.contains = node=>node===canvas;
  let predicateCalls = 0,frames = 0;
  const context = vm.createContext({window:{__wfmBrowserNativeViews:[{toArray:()=>[view]}]},
    document:{querySelectorAll:()=>[canvas]},devicePixelRatio:1,
    requestAnimationFrame:callback=>{frames++;callback();}});
  const page = {
    evaluate:expression=>vm.runInContext(typeof expression==="function" ? `(${expression.toString()})()` : expression,
      context),
    waitForFunction:async expression=>{
      const result = vm.runInContext(expression,context);
      assert.equal(typeof result,"boolean","A string predicate must evaluate to a boolean, not a function value");
      assert.equal(result,false,"The observed pending resize cannot certify paint readiness");
      view.resizing=false;
      view.stationary=true;
      assert.equal(vm.runInContext(expression,context),true);
      predicateCalls++;
    },
  };
  await mapsRedrawn(page);
  assert.equal(predicateCalls,1);
  assert.equal(frames,2);
});
