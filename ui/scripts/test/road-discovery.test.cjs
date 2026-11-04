/* Traffic Lab discovers roads through an area-bound worker job, never a long provider request in the API. */
const test=require("node:test");
const assert=require("node:assert/strict");
const React=require("react");
const {act,create}=require("react-test-renderer");
const {load}=require("../ts-module.cjs");
const ID="55555555-5555-4555-8555-555555555555";
const area={lat:47.60395,lon:-122.3305,radius_km:4};
const result={area,geometry:{type:"FeatureCollection",features:[]},
  highways:[{corridor_id:"osm:way:1",label:"Seattle fixture corridor",source:"OpenStreetMap"}]};
const job=(status,overrides={})=>({id:ID,kind:"road_discovery",status,scenario:area,created:1,
  phase:status==="completed"?"Roads retained":"Extracting regional roads",progress:status==="completed"?1:.3,
  ...(status==="completed"?{result}:{}),...overrides});
const settle=()=>new Promise(resolve=>setImmediate(resolve));
test.beforeEach(()=>{
  const values=new Map();
  global.sessionStorage={getItem:key=>values.get(key)??null,setItem:(key,value)=>values.set(key,String(value)),
    removeItem:key=>values.delete(key)};
});

test("discovery receipts bind job identity, area and saved completion without requiring a selected highway",()=>{
  const {discoveryJob}=load("features/traffic-lab/services/api.ts");
  assert.equal(discoveryJob(job("running"),area).scenario.highway,undefined);
  assert.deepEqual(discoveryJob(job("completed"),area,ID).result,result);
  for(const invalid of [job("running",{scenario:{...area,lat:0}}),job("completed",{result:null}),
    job("completed",{result:{...result,area:{...area,lon:0}}}),job("running",{id:"wrong"}),
    job("running",{kind:"research"})]){
    assert.throws(()=>discoveryJob(invalid,area,ID),/unusable|no usable/);
  }
});

test("discovery POST returns promptly; queued/running progress uses bounded GETs until the exact area completes",async()=>{
  const timers=[],calls=[],loaded=[];
  const originalSetTimeout=global.setTimeout,originalClearTimeout=global.clearTimeout;
  global.setTimeout=(callback,delay)=>{const timer={callback,delay};timers.push(timer);return timer;};
  global.clearTimeout=timer=>{if(timer)timer.cancelled=true;};
  const reads=[job("queued"),job("running"),job("completed")];
  const {useRoadDiscovery}=load("features/traffic-lab/hooks/useRoadDiscovery.ts",{stubs:{
    "../services/api":{...load("features/traffic-lab/services/api.ts"),research:async(path,body,options)=>{
      calls.push({path,body,options});return body?job("queued"):reads.shift();
    }},
  }});
  let state,renderer;
  function Harness(){state=useRoadDiscovery(area,value=>loaded.push(value));return null;}
  try{
    await act(async()=>{renderer=create(React.createElement(Harness));});
    await act(async()=>{await state.start();await settle();});
    assert.equal(state.pending,true);
    assert.equal(state.job.status,"queued");
    assert.deepEqual(loaded,[]);
    await act(async()=>{timers[0].callback();await settle();});
    assert.equal(state.job.status,"running");
    await act(async()=>{timers[1].callback();await settle();});
    assert.equal(state.pending,false);
    assert.deepEqual(loaded,[result]);
    assert.equal(calls.filter(call=>call.body).length,1,"One explicit submission; status does not resubmit provider work");
    assert.ok(calls.slice(1).every(call=>call.path===`/discover/${ID}` && !call.body
      && call.options.timeoutMs===15000));
    assert.equal(timers.length,2);
  }finally{
    if(renderer)await act(async()=>renderer.unmount());
    global.setTimeout=originalSetTimeout;global.clearTimeout=originalClearTimeout;
  }
});

test("a changed study area or unmounted lab aborts old reads and rejects late geometry without cancelling server work",async()=>{
  const reads=[],loaded=[],calls=[];
  const {useRoadDiscovery}=load("features/traffic-lab/hooks/useRoadDiscovery.ts",{stubs:{
    "../services/api":{...load("features/traffic-lab/services/api.ts"),research:(path,body,options)=>{
      calls.push(path);return body?Promise.resolve(job("queued")):new Promise(resolve=>reads.push({resolve,options}));
    }},
  }});
  let state,renderer;
  function Harness({target}){state=useRoadDiscovery(target,value=>loaded.push(value));return null;}
  try{
    await act(async()=>{renderer=create(React.createElement(Harness,{target:area}));});
    await act(async()=>{await state.start();await settle();});
    await act(async()=>renderer.update(React.createElement(Harness,{target:{...area,lat:0}})));
    assert.equal(reads[0].options.signal.aborted,true);
    await act(async()=>{reads[0].resolve(job("completed"));await settle();});
    assert.equal(state.job,null);
    assert.deepEqual(loaded,[]);
    await act(async()=>renderer.update(React.createElement(Harness,{target:area})));
    await act(async()=>{await state.start();await settle();});
    await act(async()=>renderer.unmount());renderer=null;
    assert.equal(reads[1].options.signal.aborted,true);
    await act(async()=>{reads[1].resolve(job("completed"));await settle();});
    assert.deepEqual(loaded,[]);
    assert.equal(calls.some(path=>path.endsWith("/cancel")),false,"Area/navigation changes stop reads only");
  }finally{if(renderer)await act(async()=>renderer.unmount());}
});

test("explicit cancellation is monotonic and a late running snapshot cannot undo cancelling",async()=>{
  const reads=[],calls=[];
  const {useRoadDiscovery}=load("features/traffic-lab/hooks/useRoadDiscovery.ts",{stubs:{
    "../services/api":{...load("features/traffic-lab/services/api.ts"),research:(path,body,options)=>{
      calls.push({path,body});return path.endsWith("/cancel")?Promise.resolve(job("cancelling"))
        :body?Promise.resolve(job("queued")):new Promise(resolve=>reads.push({resolve,options}));
    }},
  }});
  let state,renderer;
  function Harness(){state=useRoadDiscovery(area,()=>{});return null;}
  try{
    await act(async()=>{renderer=create(React.createElement(Harness));});
    await act(async()=>{await state.start();await settle();});
    await act(async()=>{await state.cancel();await settle();});
    assert.equal(state.job.status,"cancelling");
    await act(async()=>{reads[0].resolve(job("running"));await settle();});
    assert.equal(state.job.status,"cancelling");
    assert.equal(calls.filter(call=>call.path.endsWith("/cancel")).length,1);
  }finally{if(renderer)await act(async()=>renderer.unmount());}
});

test("completion winning a cancel race loads its saved roads once and fences the older running read",async()=>{
  const reads=[],calls=[],loaded=[];
  const {useRoadDiscovery}=load("features/traffic-lab/hooks/useRoadDiscovery.ts",{stubs:{
    "../services/api":{...load("features/traffic-lab/services/api.ts"),research:(path,body,options)=>{
      calls.push({path,body});return path.endsWith("/cancel")?Promise.resolve(job("completed"))
        :body?Promise.resolve(job("queued")):new Promise(resolve=>reads.push({resolve,options}));
    }},
  }});
  let state,renderer;
  function Harness(){state=useRoadDiscovery(area,value=>loaded.push(value));return null;}
  try{
    await act(async()=>{renderer=create(React.createElement(Harness));});
    await act(async()=>{await state.start();await settle();});
    assert.equal(reads.length,1,"The earlier status read is still in flight");
    await act(async()=>{await state.cancel();await settle();});
    assert.equal(state.job.status,"completed");
    assert.equal(state.pending,false);
    assert.deepEqual(loaded,[result],"An actual completed cancel response supplies the saved road map");
    assert.equal(reads[0].options.signal.aborted,true);
    await act(async()=>{reads[0].resolve(job("running"));await settle();});
    assert.equal(state.job.status,"completed");
    assert.deepEqual(loaded,[result],"The late older read cannot replace completed roads");
    assert.equal(calls.filter(call=>call.path.endsWith("/cancel")).length,1);
  }finally{if(renderer)await act(async()=>renderer.unmount());}
});

test("Traffic Lab never enables a run with a previous/default corridor after an empty discovery or area change",async()=>{
  let completion,currentArea,starts=0,renderer;
  const presentation=new Proxy({}, {get:(_target,key)=>["miles","clock","feet"].includes(key)
    ? value=>String(value):()=>null});
  const TrafficLab=load("features/traffic-lab/components/TrafficLab.tsx",{stubs:{
    "@/features/evacuation/presentation":presentation,
    "@/shared/hooks/useMapFullscreen":{useMapFullscreen:()=>({containerRef:{current:null},expanded:false,toggle(){}})},
    "./SourceCoverage":{SourceCoverage:()=>null},
    "../hooks/useResearchJob":{useResearchJob:()=>({access:"ready",config:null,job:null,history:[],result:null,
      network:null,running:false,error:"",setError(){},clear(){},start:async()=>{starts+=1;}})},
    "../hooks/useRoadDiscovery":{useRoadDiscovery:(target,onCompleted)=>{
      currentArea=target;completion=onCompleted;return {pending:false,error:"",job:null,clear(){},start:async()=>{},cancel:async()=>{}};
    }},
  }}).default;
  const run=()=>renderer.root.findAllByType("button")
    .find(button=>String(button.props.children).includes("Run three-policy"));
  try{
    await act(async()=>{renderer=create(React.createElement(TrafficLab));});
    assert.equal(run().props.disabled,true,"The starting highway string alone is not ready evidence");
    await act(async()=>completion({...result,area:currentArea,highways:[]}));
    assert.equal(run().props.disabled,true);
    assert.match(JSON.stringify(renderer.toJSON()),/No usable road corridors returned/);
    await act(async()=>completion({...result,area:currentArea}));
    assert.equal(run().props.disabled,false,"An actual returned corridor becomes selectable");
    const latitude=renderer.root.findAllByType("input").find(input=>input.props.value===currentArea.lat);
    await act(async()=>latitude.props.onChange({target:{value:"0"}}));
    assert.equal(currentArea.lat,0,"Zero is preserved as an explicit area coordinate");
    assert.equal(run().props.disabled,true,"Area changes cannot submit the previous corridor");
    assert.equal(starts,0,"Neither discovery rendering nor editing a point creates a SUMO run");
  }finally{if(renderer)await act(async()=>renderer.unmount());}
});
