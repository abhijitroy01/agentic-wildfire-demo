/* Native map context generations: retries rebuild the same providers and retire unfinished views. */
const test=require("node:test");
const assert=require("node:assert/strict");
const React=require("react");
const {act,create}=require("react-test-renderer");
const {load}=require("../ts-module.cjs");
const settle=()=>new Promise(resolve=>setImmediate(resolve));
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});
  return {promise,resolve,reject};};

function harness({deferContext=false,deferView=false,deferLoads=false,deferLayers=false}={}) {
  const contexts=[],maps=[],views=[],scales=[],ready=[],errors=[];
  const loadable=properties=>{
    const loading=deferred(),layer={...properties,loading,loadCalls:0,
      load(){this.loadCalls+=1;return loading.promise;}};
    if(!deferLoads)loading.resolve(layer);
    return layer;
  };
  class MapStub {
    constructor(properties) {
      Object.assign(this,properties);
      this.allLayers={toArray:()=>this.layers};
      maps.push(this);
    }
  }
  class ViewStub {
    constructor(properties) {
      Object.assign(this,properties);
      this.readiness=deferred();
      this.ready=false;
      const resolveReadiness=this.readiness.resolve;
      this.readiness.resolve=()=>{this.ready=true;resolveReadiness(this);};
      this.layerReadiness=deferred();
      this.layerViews=[];
      this.destroyCalls=0;
      this.widgets=[];
      this.ui={add:(widget,position)=>this.widgets.push({widget,position})};
      this.popup={};
      this.previousViewDestroyCalls=views.map(view=>view.destroyCalls);
      views.push(this);
      if(!deferView)this.readiness.resolve(this);
      if(!deferLayers)this.layerReadiness.resolve();
    }
    when(){return this.readiness.promise;}
    whenLayerView(layer){this.layerViews.push(layer);return this.layerReadiness.promise;}
    destroy(){this.destroyCalls+=1;}
  }
  class GraphicsStub {constructor(properties){Object.assign(this,loadable(properties));}}
  class ScaleStub {constructor(properties){Object.assign(this,properties);scales.push(this);}}
  const {useMapInitialization}=load("features/map/hooks/useMapInitialization.ts",{stubs:{
    "@arcgis/core/Map":{__esModule:true,default:MapStub},
    "@arcgis/core/views/MapView":{__esModule:true,default:ViewStub},
    "@arcgis/core/layers/GraphicsLayer":{__esModule:true,default:GraphicsStub},
    "@arcgis/core/widgets/ScaleBar":{__esModule:true,default:ScaleStub},
    "@/shared/utils/logger":{logger:{error:(...args)=>errors.push(args)}},
    "../services/mapContext":{createMapContext:publish=>{
      const pending=deferred(),context={publish,pending,disposeCalls:0,
        basemap:{destroyCalls:0,destroy(){this.destroyCalls+=1;}},
        terrain:loadable({visible:true,destroyCalls:0,destroy(){this.destroyCalls+=1;}}),
        dispose(){this.disposeCalls+=1;}};
      contexts.push(context);
      if(!deferContext)pending.resolve(context);
      return pending.promise;
    }},
  }});
  const container={style:{},setAttribute(){},blur(){}},containerRef={current:container};
  const onMapReady=(map,view)=>ready.push({map,view});
  let state,renderer;
  function Harness(){state=useMapInitialization({containerRef,onMapReady});return null;}
  return {
    contexts,maps,views,scales,ready,errors,
    get state(){return state;},
    async mount(){await act(async()=>{renderer=create(React.createElement(Harness));await settle();});},
    async update(action){await act(async()=>{action();await settle();});},
    async unmount(){
      if(renderer){await act(async()=>{renderer.unmount();await settle();});renderer=null;}
    },
  };
}

test("retry rebuilds the same context and view, preserving terrain visibility and a native dual-unit scale",async()=>{
  const h=harness();
  try{
    await h.mount();
    assert.equal(h.contexts.length,1);
    assert.equal(h.maps[0].basemap,h.contexts[0].basemap);
    assert.equal(h.maps[0].layers[0],h.contexts[0].terrain);
    assert.equal(h.state.mapView,h.views[0]);
    assert.deepEqual(h.views[0].constraints,{maxZoom:18});
    assert.equal(h.scales.length,1);
    assert.equal(h.scales[0].view,h.views[0]);
    assert.equal(h.scales[0].unit,"dual");
    assert.equal(h.scales[0].style,"line");
    assert.deepEqual(h.views[0].widgets,[{widget:h.scales[0],position:"bottom-left"}]);
    await h.update(()=>{h.contexts[0].publish("streets","failed");h.contexts[0].publish("terrain","failed");});
    assert.deepEqual(h.state.contextStatus,{streets:"failed",terrain:"failed"});
    await h.update(()=>h.state.setTerrainVisible(false));
    assert.equal(h.contexts[0].terrain.visible,false);
    await h.update(()=>h.state.retryContext());
    assert.equal(h.contexts.length,2,"Retry calls the provider factory again, not refresh on failed layers");
    assert.equal(h.maps.length,2);
    assert.equal(h.views.length,2);
    assert.equal(h.contexts[0].disposeCalls,1,"The previous provider status generation is disposed");
    assert.equal(h.views[0].destroyCalls,1);
    assert.ok(h.maps[0].layers.every(layer=>layer.loadCalls===1),"Layer metadata drains independently of DOM readiness");
    assert.deepEqual(h.views[0].layerViews,h.maps[0].layers,"Cleanup drains native layer creation before destroy");
    assert.equal(h.state.mapView,h.views[1]);
    assert.equal(h.state.terrainVisible,false);
    assert.equal(h.contexts[1].terrain.visible,false,"Retry retains the user's requested visibility");
    assert.deepEqual(h.state.contextStatus,{streets:"loading",terrain:"loading"});
    const current=h.state;
    await h.update(()=>{h.contexts[0].publish("streets","ready");h.contexts[0].publish("terrain","ready");});
    assert.equal(h.state,current,"Old provider notifications cannot change the replacement map status");
    await h.update(()=>h.state.setTerrainVisible(true));
    assert.equal(h.contexts[1].terrain.visible,true);
    assert.equal(h.contexts[0].terrain.visible,false,"Visibility updates target only the replacement provider");
    assert.equal(h.scales.length,2,"Each replacement view receives its own native scale widget");
    assert.equal(h.ready.length,2);
    assert.equal(h.errors.length,0);
  }finally{await h.unmount();}
  assert.equal(h.contexts[1].disposeCalls,1);
  assert.equal(h.views[1].destroyCalls,1);
});

test("a provider factory resolving after retry cannot create a stale view or change replacement status",async()=>{
  const h=harness({deferContext:true});
  try{
    await h.mount();
    assert.equal(h.views.length,0);
    await h.update(()=>{h.state.setTerrainVisible(false);h.state.retryContext();});
    assert.equal(h.contexts.length,2);
    await h.update(()=>h.contexts[1].pending.resolve(h.contexts[1]));
    assert.equal(h.views.length,1);
    assert.equal(h.state.mapView,h.views[0]);
    assert.equal(h.contexts[1].terrain.visible,false);
    await h.update(()=>h.contexts[1].publish("streets","ready"));
    const current=h.state;
    await h.update(()=>{
      h.contexts[0].publish("streets","failed");
      h.contexts[0].pending.resolve(h.contexts[0]);
    });
    assert.equal(h.state,current);
    assert.equal(h.views.length,1,"A retired factory result cannot build another native view");
    assert.equal(h.contexts[0].disposeCalls,1);
    assert.equal(h.contexts[0].basemap.destroyCalls,1);
    assert.equal(h.contexts[0].terrain.destroyCalls,1);
    assert.equal(h.ready.length,1);
    assert.equal(h.errors.length,0);
  }finally{await h.unmount();}
});

test("a factory resolving after unmount disposes unused providers without publishing ready or error",async()=>{
  const h=harness({deferContext:true});
  await h.mount();
  const current=h.state;
  await h.unmount();
  await h.update(()=>{
    h.contexts[0].publish("terrain","failed");
    h.contexts[0].pending.resolve(h.contexts[0]);
  });
  assert.equal(h.state,current);
  assert.equal(h.contexts[0].disposeCalls,1);
  assert.equal(h.contexts[0].basemap.destroyCalls,1);
  assert.equal(h.contexts[0].terrain.destroyCalls,1);
  assert.equal(h.views.length,0);
  assert.equal(h.scales.length,0);
  assert.equal(h.ready.length,0);
  assert.equal(h.errors.length,0);
});

test("retry of a ready view drains its layer views before attaching a replacement to the same container",async()=>{
  const h=harness({deferLayers:true});
  try{
    await h.mount();
    assert.equal(h.views.length,1);
    assert.equal(h.state.mapView,h.views[0]);
    await h.update(()=>{h.state.setTerrainVisible(false);h.state.retryContext();});
    assert.equal(h.views.length,1,"The replacement cannot attach before old layer views finish creation");
    assert.equal(h.contexts.length,1,"Providers for the replacement wait for the previous container owner");
    assert.equal(h.contexts[0].disposeCalls,1);
    const current=h.state;
    await h.update(()=>h.contexts[0].publish("terrain","failed"));
    assert.equal(h.state,current);
    assert.equal(h.state.mapView,h.views[0]);
    assert.equal(h.views.length,1,"Readiness alone does not release the shared DOM container");
    assert.equal(h.ready.length,1,"The old view's original ready notification is not repeated");
    assert.equal(h.scales.length,1,"No replacement widget is added before retirement finishes");
    assert.deepEqual(h.views[0].layerViews,h.maps[0].layers);
    assert.equal(h.views[0].destroyCalls,0,"In-flight layer work is drained before native destroy");
    await h.update(()=>h.views[0].layerReadiness.resolve());
    assert.equal(h.views[0].destroyCalls,1);
    assert.equal(h.contexts.length,2);
    assert.equal(h.views.length,2);
    assert.deepEqual(h.views[1].previousViewDestroyCalls,[1],"Native destroy precedes replacement attachment");
    assert.equal(h.contexts[1].terrain.visible,false,"The requested terrain toggle survives the retirement wait");
    assert.equal(h.state.terrainVisible,false);
    assert.equal(h.state.mapView,h.views[1]);
    assert.deepEqual(h.ready,[{map:h.maps[0],view:h.views[0]},{map:h.maps[1],view:h.views[1]}]);
    assert.equal(h.scales.length,2);
    const replacement=h.state;
    await h.update(()=>h.contexts[0].publish("streets","failed"));
    assert.equal(h.state,replacement,"Retired provider notifications remain fenced after replacement activation");
    assert.equal(h.errors.length,0);
  }finally{
    h.maps.forEach(map=>map.layers.forEach(layer=>layer.loading.resolve(layer)));
    h.views.forEach(view=>view.layerReadiness.resolve());
    await h.unmount();
  }
});

test("retry drains layer metadata without waiting for a never-ready view and fences its late readiness",async()=>{
  const h=harness({deferView:true,deferLoads:true});
  try{
    await h.mount();
    await h.update(()=>{h.state.setTerrainVisible(false);h.state.retryContext();});
    assert.equal(h.views.length,1);
    assert.equal(h.views[0].ready,false);
    assert.equal(h.views[0].destroyCalls,0,"Layer metadata is still loading");
    assert.ok(h.maps[0].layers.every(layer=>layer.loadCalls===1));
    assert.equal(h.views[0].layerViews.length,0,"Never-ready views have no started layer views to drain");
    await h.update(()=>h.maps[0].layers.forEach(layer=>layer.loading.resolve(layer)));
    assert.equal(h.views[0].destroyCalls,1,"Destroy finishes while initial view readiness remains unresolved");
    assert.equal(h.views[0].ready,false);
    assert.equal(h.views.length,2);
    assert.deepEqual(h.views[1].previousViewDestroyCalls,[1]);
    assert.equal(h.contexts[1].terrain.visible,false);
    assert.equal(h.ready.length,0);
    assert.equal(h.scales.length,0);
    await h.update(()=>h.views[1].readiness.resolve());
    assert.equal(h.state.mapView,h.views[1]);
    const current=h.state;
    await h.update(()=>{h.contexts[0].publish("terrain","failed");h.views[0].readiness.resolve();});
    assert.equal(h.state,current);
    assert.deepEqual(h.ready,[{map:h.maps[1],view:h.views[1]}],"Retired readiness cannot publish a stale map");
    assert.equal(h.scales.length,1);
    assert.equal(h.errors.length,0);
  }finally{
    h.maps.forEach(map=>map.layers.forEach(layer=>layer.loading.resolve(layer)));
    await h.unmount();
  }
});

test("late view rejection after unmount is drained without reactivating the map or surfacing a stale error",async()=>{
  const h=harness({deferView:true});
  await h.mount();
  const current=h.state;
  await h.unmount();
  assert.equal(h.contexts[0].disposeCalls,1);
  await h.update(()=>h.views[0].readiness.reject(new Error("Retired view creation failed")));
  assert.equal(h.state,current);
  assert.equal(h.views[0].destroyCalls,1);
  assert.equal(h.ready.length,0);
  assert.equal(h.scales.length,0);
  assert.equal(h.errors.length,0);
});

test("unmount during retirement cancels replacement attachment while the old native view still drains",async()=>{
  const h=harness({deferView:true,deferLoads:true});
  await h.mount();
  await h.update(()=>h.state.retryContext());
  assert.equal(h.contexts.length,1);
  const current=h.state;
  await h.unmount();
  await h.update(()=>h.contexts[0].publish("terrain","ready"));
  assert.equal(h.views[0].destroyCalls,0);
  await h.update(()=>h.maps[0].layers.forEach(layer=>layer.loading.resolve(layer)));
  assert.equal(h.views[0].destroyCalls,1);
  assert.equal(h.views[0].ready,false,"An unmounted map retires without ever obtaining DOM readiness");
  assert.equal(h.views[0].layerViews.length,0);
  assert.equal(h.contexts[0].disposeCalls,1);
  assert.equal(h.contexts.length,1,"An unmounted replacement never starts another provider generation");
  assert.equal(h.views.length,1);
  assert.equal(h.state,current);
  assert.equal(h.ready.length,0);
  assert.equal(h.scales.length,0);
  assert.equal(h.errors.length,0);
});
