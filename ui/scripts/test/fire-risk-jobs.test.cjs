/* Read-only durable jobs: source binding, calm, pending/failed outcomes and answer cancellation. */
const test = require("node:test");
const assert = require("node:assert/strict");
const React = require("react");
const {act,create} = require("react-test-renderer");
const {load} = require("../ts-module.cjs");
const {FIRST,SECOND,JOB,landscape,reference,locations,response} = require("./fire-risk-fixtures.cjs");
const settle = () => new Promise(resolve => setImmediate(resolve));

test("schema2 accepts mapped terrain, measured calm and fragmented polygons; failed domains draw no substitute", () => {
  const {isAssessmentResult,scenarioHorizons,visibleExposureFeatures,isExposureGeometry} =
    load("features/fire-exposure/services/assessment.ts");
  const result = landscape();
  assert.equal(isAssessmentResult(result),true);
  assert.deepEqual(scenarioHorizons(result),[30,60]);
  assert.deepEqual(visibleExposureFeatures(result,{}).map(item => item.properties.elapsed_minutes),[60,30]);
  const failed = landscape({availability:"unavailable",landscape:null,
    failure:{code:"DOMAIN_TRUNCATED",message:"Reachable fire footprint exceeds the modeled domain."},
    geojson:{type:"FeatureCollection",features:[]}});
  assert.equal(isAssessmentResult(failed),true);
  assert.deepEqual(visibleExposureFeatures(failed,{}),[]);
  assert.equal(isAssessmentResult({...failed,geojson:result.geojson}),false,"Failures cannot carry success geometry");
  assert.equal(isAssessmentResult({...result,landscape:null}),false,"Terrain success requires mapped input evidence");
  const ring = [[0,0],[.01,0],[.01,.01],[0,.01],[0,0]];
  assert.equal(isExposureGeometry({type:"Polygon",coordinates:Array.from({length:40},()=>ring)}),true,
    "Mapped masks may contain more than sixteen holes");
  assert.equal(isExposureGeometry({type:"MultiPolygon",coordinates:Array.from({length:40},()=>[ring])}),true,
    "Mapped barriers may leave more than thirty-two distinct parts");
  assert.equal(isExposureGeometry({type:"MultiPolygon",coordinates:Array.from({length:1024},()=>
    Array.from({length:5},()=>ring))}),false,"Total vertex budget bounds render work");
});

test("job GET validates the exact retained intent, completion coverage and result identity", async () => {
  let result = response("completed");
  const calls = [];
  const {readFireRiskJob} = load("features/fire-exposure/services/api.ts",{stubs:{
    "@/shared/services/workspaceAccess":{brokerJson:async(path,options)=>{calls.push([path,options]);return result;}},
  }});
  assert.equal((await readFireRiskJob(reference)).assessments[0].assessment_id,FIRST);
  for (const invalid of [
    response("completed",{job_id:SECOND}),response("completed",{fire_refs:["other-fire"]}),
    response("completed",{assessments:[]}),response("completed",{assessments:[landscape(),landscape()]}),
    response("running",{assessments:[landscape()]}),
    response("completed",{assessments:[landscape({fire_ref:"other-fire"})]}),
  ]) {
    result = invalid;
    await assert.rejects(readFireRiskJob(reference),/unusable job receipt/);
  }
  assert.ok(calls.every(([path,options])=>path===`/fire-exposure/jobs/${JOB}` && options.method==="GET"
    && options.timeoutMs===15000 && !options.body),"Status reads never invoke a model or provider");
});

test("the parser preserves pending job references without putting provisional assessment geometry into the answer", async () => {
  const {parseAgentCoreResponse} = load("shared/services/agentResponseParser.ts");
  const result = await parseAgentCoreResponse({content:{mapData:{locations},
    fireExposureData:[],fireExposureJobs:[reference,reference,{job_id:"invalid",fire_refs:["other"]}]}});
  assert.deepEqual(result.fireExposureJobs,[reference]);
  assert.deepEqual(result.fireExposureData,[]);
});

test("the parser retains only the fixed public preparation error with original fire markers",async()=>{
  const {parseAgentCoreResponse}=load("shared/services/agentResponseParser.ts");
  const failure={code:"RISK_PREPARATION_FAILED",message:"Terrain assessment preparation could not be queued."};
  const parsed=await parseAgentCoreResponse({content:{mapData:{locations},fireExposureData:[],
    fireExposureJobs:[],fireExposureError:{...failure,providerResponse:"secret-upstream"}}});
  assert.deepEqual(parsed.fireExposureError,failure);
  assert.deepEqual(parsed.locations,locations);
  assert.deepEqual(parsed.fireExposureData,[]);
  const invalid=await parseAgentCoreResponse({content:{mapData:{locations},fireExposureError:{
    ...failure,message:"secret-upstream-response-and-url"}}});
  assert.equal(invalid.fireExposureError,undefined);
});

test("failed answer-to-job handoff is visible even without jobs or geometry and starts no extra work",async()=>{
  const Summary=load("features/fire-exposure/components/FireExposureSummary.tsx",{stubs:{
    "./FireExposureSummary.css":{},"./Accordion.css":{},
  }}).default;
  const originalFetch=global.fetch;
  global.fetch=()=>{throw new Error("Rendering a preparation failure cannot start providers or jobs");};
  let renderer;
  try{
    await act(async()=>{renderer=create(React.createElement(Summary,{assessments:[],jobs:[],selections:{},
      preparationError:{code:"RISK_PREPARATION_FAILED",message:"Terrain assessment preparation could not be queued."},
      onScenarioTime:()=>undefined}));});
    const alerts=renderer.root.findAllByProps({role:"alert"});
    assert.equal(alerts.length,1);
    const text=JSON.stringify(renderer.toJSON());
    assert.match(text,/Terrain assessment preparation could not be queued/);
    assert.match(text,/RISK_PREPARATION_FAILED/);
    assert.match(text,/Reported fires remain on the map/);
    assert.equal(renderer.root.findAllByType("input").length,0,"No simulated outline controls are fabricated");
  }finally{
    if(renderer)await act(async()=>renderer.unmount());
    global.fetch=originalFetch;
  }
});

test("only explicit cancellation posts to an owned job and rejoins saved status without resubmitting calculation", async () => {
  const calls=[];
  const {cancelFireRiskJob}=load("features/fire-exposure/services/api.ts",{stubs:{
    "@/shared/services/workspaceAccess":{brokerJson:async(path,options)=>{
      calls.push([path,options]);return response("cancelling");
    }},
  }});
  assert.equal((await cancelFireRiskJob(reference)).status,"cancelling");
  assert.equal(calls[0][0],`/fire-exposure/jobs/${JOB}/cancel`);
  assert.equal(calls[0][1].method,"POST");
  assert.deepEqual(calls[0][1].body,{});
  assert.ok(!calls[0][1].body.fire && !calls[0][1].body.weather,"Cancellation sends no fresh source/model inputs");
});

test("queued/running jobs poll until completion while the answer and its original fire remain immutable", async () => {
  const originalSetTimeout = global.setTimeout, originalClearTimeout = global.clearTimeout;
  const timers = [], reads = [];
  global.setTimeout = (callback,delay)=>{const timer={callback,delay};timers.push(timer);return timer;};
  global.clearTimeout = timer=>{timer.cancelled=true;};
  const outputs = [response("queued"),response("running"),response("completed")];
  const {useFireRiskJobs} = load("features/fire-exposure/hooks/useFireRiskJobs.ts",{stubs:{
    "../services/api":{readFireRiskJob:async(ref,signal)=>{
      reads.push({ref,signal});return outputs.shift();
    }},
  }});
  const mapData = {locations,fireExposureData:[],fireExposureJobs:[reference]};
  const original = JSON.stringify(mapData);
  let state,renderer;
  function Harness(){state=useFireRiskJobs(mapData);return null;}
  try {
    await act(async()=>{renderer=create(React.createElement(Harness));await settle();});
    assert.equal(state.jobs[0].result.status,"queued");
    assert.deepEqual(state.assessments,[]);
    await act(async()=>{timers[0].callback();await settle();});
    assert.equal(state.jobs[0].result.status,"running");
    assert.deepEqual(state.assessments,[]);
    await act(async()=>{timers[1].callback();await settle();});
    assert.equal(state.jobs[0].result.status,"completed");
    assert.equal(state.assessments[0].assessment_id,FIRST);
    assert.equal(timers.length,2,"Completion stops polling");
    assert.deepEqual(timers.map(timer=>timer.delay),[2000,2000]);
    assert.equal(JSON.stringify(mapData),original,"Hydration never changes a saved chat answer");
    assert.equal(reads.length,3);
  } finally {
    if(renderer)await act(async()=>renderer.unmount());
    global.setTimeout=originalSetTimeout;global.clearTimeout=originalClearTimeout;
  }
});

test("answer changes and unmount abort pending reads; a late response never belongs to the next fire", async () => {
  const reads = [];
  const {useFireRiskJobs} = load("features/fire-exposure/hooks/useFireRiskJobs.ts",{stubs:{
    "../services/api":{readFireRiskJob:(ref,signal)=>new Promise(resolve=>reads.push({ref,signal,resolve}))},
  }});
  const first = {locations,fireExposureData:[],fireExposureJobs:[reference]};
  const second = {locations:[{latitude:47,longitude:-122,fire:{name:"Second fire"}}],fireExposureData:[]};
  let state,renderer;
  function Harness({mapData}){state=useFireRiskJobs(mapData);return null;}
  try {
    await act(async()=>{renderer=create(React.createElement(Harness,{mapData:first}));await settle();});
    await act(async()=>renderer.update(React.createElement(Harness,{mapData:second})));
    assert.equal(reads[0].signal.aborted,true);
    await act(async()=>{reads[0].resolve(response("completed"));await settle();});
    assert.deepEqual(state.assessments,[]);
    assert.deepEqual(state.jobs,[]);
    await act(async()=>renderer.update(React.createElement(Harness,{mapData:first})));
    await act(async()=>renderer.unmount());renderer=null;
    assert.equal(reads[1].signal.aborted,true);
    await act(async()=>{reads[1].resolve(response("completed"));await settle();});
    assert.equal(reads.length,2,"Restoring an answer reads only its existing receipt");
  } finally {if(renderer)await act(async()=>renderer.unmount());}
});

test("cancelled terrain jobs keep original markers and no completed geometry, even after a late active status",async t=>{
  t.mock.timers.enable({apis:["setTimeout"]});
  let gets=0,late,cancels=0,state,renderer;
  const {useFireRiskJobs}=load("features/fire-exposure/hooks/useFireRiskJobs.ts",{stubs:{
    "../services/api":{
      readFireRiskJob:async()=>{
        gets+=1;
        if(gets===1)return response("queued");
        if(gets===2)return new Promise(resolve=>{late=resolve;});
        return response("cancelled");
      },
      cancelFireRiskJob:async()=>{cancels+=1;return response("cancelling");},
    },
  }});
  const mapData={locations,fireExposureData:[],fireExposureJobs:[reference]};
  function Harness(){state=useFireRiskJobs(mapData);return null;}
  try{
    await act(async()=>{renderer=create(React.createElement(Harness));await settle();});
    await act(async()=>{t.mock.timers.tick(2000);await settle();});
    await act(async()=>{await state.cancelJob(JOB);await settle();});
    assert.equal(state.jobs[0].result.status,"cancelled");
    await act(async()=>{late(response("running"));await settle();});
    assert.equal(state.jobs[0].result.status,"cancelled","Cancellation retires the earlier polling controller");
    assert.deepEqual(state.assessments,[]);
    assert.deepEqual(mapData.locations,locations);
    assert.equal(cancels,1);
  }finally{
    if(renderer)await act(async()=>renderer.unmount());
    t.mock.timers.reset();
  }
});

test("read failure is recoverable status uncertainty, and mismatched fire results never become retained geometry", async () => {
  const reads = [];
  let output = new Error("Temporary status read timeout");
  const {useFireRiskJobs} = load("features/fire-exposure/hooks/useFireRiskJobs.ts",{stubs:{
    "../services/api":{readFireRiskJob:async()=>{reads.push("GET");if(output instanceof Error)throw output;return output;}},
  }});
  const mapData = {locations,fireExposureData:[],fireExposureJobs:[reference]};
  let state,renderer;
  function Harness(){state=useFireRiskJobs(mapData);return null;}
  try {
    await act(async()=>{renderer=create(React.createElement(Harness));await settle();});
    assert.match(state.jobs[0].readError,/timeout/);
    assert.equal(state.jobs[0].result,null,"Status-read failure is not a terminal model failure");
    output=response("completed",{assessments:[landscape({fire:{latitude:47,longitude:-122,source_id:"different-fire"}})]});
    await act(async()=>{state.refreshStatus();await settle();});
    assert.match(state.jobs[0].readError,/does not belong/);
    assert.deepEqual(state.assessments,[]);
    output=response("failed",{error:"LANDFIRE request did not complete."});
    await act(async()=>{state.refreshStatus();await settle();});
    assert.equal(state.jobs[0].result.status,"failed");
    assert.deepEqual(state.assessments,[]);
    output=response("completed");
    await act(async()=>{state.refreshStatus();await settle();});
    assert.equal(state.assessments[0].assessment_id,FIRST);
    assert.equal(reads.length,4);
  } finally {if(renderer)await act(async()=>renderer.unmount());}
});

test("terrain evidence shows calm, no modeled growth and missing-cell uncertainty with no extra work", async () => {
  const realFetch = global.fetch;
  global.fetch=()=>{throw new Error("Evidence rendering performs no provider work");};
  const Summary=load("features/fire-exposure/components/FireExposureSummary.tsx",{stubs:{
    "./FireExposureSummary.css":{},"./Accordion.css":{},
  }}).default;
  let renderer;
  try {
    await act(async()=>{renderer=create(React.createElement(Summary,{assessments:[landscape()],selections:{},
      jobs:[{reference,result:response("completed"),readError:null}],onScenarioTime:()=>undefined}));});
    const text=JSON.stringify(renderer.toJSON());
    assert.match(text,/DTN measured calm wind/);
    assert.match(text,/No modeled growth beyond the ignition cell/);
    assert.match(text,/cells have missing inputs/);
    assert.match(text,/LANDFIRE LF2024 FBFM40/);
    const checkbox=renderer.root.findAllByType("input").find(input=>input.props.type==="checkbox");
    assert.equal(checkbox.props.checked,true);
    const range=renderer.root.findAllByType("input").find(input=>input.props.type==="range");
    assert.equal(range.props["aria-valuetext"],"60 minutes after assumed initialization");
  } finally {
    if(renderer)await act(async()=>renderer.unmount());
    global.fetch=realFetch;
  }
});

test("selected-fire terrain framing excludes the search city, stays stable on time changes and drives the visible legend", async () => {
  const graphics=[],fits=[],legends=[],result=landscape(),jobState={assessments:[result],jobs:[],refreshStatus:()=>undefined};
  const mapView={on:()=>({remove(){}})},layers={};
  const MapContainer=load("features/map/components/MapContainer.tsx",{stubs:{
    "./MapContainer.css":{},"./FireExposureSummary.css":{},"./Accordion.css":{},
    "@/features/camera/hooks/useCameraAvailable":{useCameraAvailable:()=>false},
    "@/features/fire-exposure/hooks/useFireRiskJobs":{useFireRiskJobs:()=>jobState},
    "../hooks/useMapInitialization":{useMapInitialization:()=>({mapView,layers,isLoading:false,error:null})},
    "../hooks/useMapGraphics":{useMapGraphics:options=>{graphics.push(options);return {isLoading:false};}},
    "../hooks/useMapCentering":{useMapCentering:options=>{fits.push(options);return {};}},
    "../hooks/useMapPopups":{useMapPopups:()=>({handleMapClick:()=>undefined})},
    "./MapLegend":{__esModule:true,default:props=>{legends.push(props);return null;}},
  }}).default;
  const mapData={locations:[...locations,{latitude:47,longitude:-122,fire:{name:"Second fire",sourceId:"esri-2"}}],
    fireExposureData:[]};
  let renderer;
  try {
    await act(async()=>{renderer=create(React.createElement(MapContainer,{mapData,
      centerLocation:{latitude:47.6,longitude:-122.3,name:"Seattle search area"}}));});
    await act(async()=>renderer.root.findAllByType("button")[0].props.onClick());
    const firstFit=fits.at(-1).mapData;
    assert.deepEqual(firstFit.locations,[locations[0]],"Terrain focus fits the actual selected fire alone");
    assert.equal(fits.at(-1).centerLocation,null,"The search city is not added to terrain bounds");
    assert.equal(fits.at(-1).terrainFocus,true);
    assert.equal(graphics.at(-1).mapData,mapData,"All reported markers and base overlays retain original answer identity");
    assert.deepEqual(graphics.at(-1).displayedAssessments,[result]);
    assert.equal(legends.at(-1).landscapeMinute,60);
    const slider=renderer.root.findAllByType("input").find(input=>input.props.type==="range");
    await act(async()=>slider.props.onChange({currentTarget:{value:"0"}}));
    assert.equal(fits.at(-1).mapData,firstFit,"Time controls leave the camera target identity unchanged");
    assert.equal(legends.at(-1).landscapeMinute,30);
    const checkbox=renderer.root.findAllByType("input").find(input=>input.props.type==="checkbox");
    await act(async()=>checkbox.props.onChange({currentTarget:{checked:false}}));
    assert.equal(legends.at(-1).showLandscape,false,"Hidden geometry disappears from the legend too");
    await act(async()=>renderer.root.findByType("select").props.onChange({currentTarget:{value:"1"}}));
    assert.deepEqual(fits.at(-1).mapData.locations,[mapData.locations[1]]);
    assert.deepEqual(graphics.at(-1).displayedAssessments,[],"Selecting another fire cannot carry the first fire's shape");
    assert.equal(legends.at(-1).showLandscape,false);
  } finally {if(renderer)await act(async()=>renderer.unmount());}
});

test("explicit terrain retry retains the request identity after a lost response and keeps old evidence", async () => {
  const calls=[];
  const saved=new Map();
  const retryIntent=load("features/fire-exposure/services/retryIntent.ts",{stubs:{
    "@/shared/services/tabStorage":{tabStorage:{
      getItem:key=>saved.get(key)??null,setItem:(key,value)=>saved.set(key,value),
    }},
  }});
  const retryReference={job_id:SECOND,fire_refs:reference.fire_refs};
  const old=response("completed",{assessments:[landscape({availability:"unavailable",landscape:null,
    failure:{code:"LANDSCAPE_REQUEST_FAILED",message:"Canonical source unavailable."},
    geojson:{type:"FeatureCollection",features:[]}})]});
  const {useFireRiskJobs}=load("features/fire-exposure/hooks/useFireRiskJobs.ts",{stubs:{
    "../services/retryIntent":retryIntent,
    "../services/api":{
      readFireRiskJob:async ref=>ref.job_id===JOB ? old
        : response("completed",{job_id:SECOND,assessments:[landscape({assessment_id:SECOND})]}),
      retryFireRiskJob:async(ref,requestId)=>{
        calls.push({ref,requestId});
        if(calls.length===1)throw new TypeError("Lost retry acknowledgement");
        return {...response("queued"),...retryReference};
      },
    },
  }});
  const mapData={locations,fireExposureData:[],fireExposureJobs:[reference]};
  const before=JSON.stringify(mapData);
  let state,renderer;
  function Harness(){state=useFireRiskJobs(mapData);return null;}
  try{
    await act(async()=>{renderer=create(React.createElement(Harness));await settle();});
    await act(async()=>{await state.retryJob(JOB);await settle();});
    assert.match(state.jobs[0].retryError,/Lost retry acknowledgement/);
    await act(async()=>renderer.unmount());
    await act(async()=>{renderer=create(React.createElement(Harness));await settle();});
    assert.match(state.jobs[0].retryError,/earlier terrain retry/);
    assert.equal(calls.length,1,"Restoring an unresolved submission starts no extra provider work");
    await act(async()=>{await state.retryJob(JOB);await settle();});
    assert.equal(calls[0].requestId,calls[1].requestId,"An uncertain POST rejoins the same attempt");
    assert.equal(state.jobs.length,2);
    assert.equal(state.assessments.length,2,"Original unavailable evidence remains alongside recovered evidence");
    assert.equal(state.assessments[1].availability,"available");
    assert.equal(JSON.stringify(mapData),before,"Retry does not rewrite the saved answer or original evidence");
    await act(async()=>renderer.unmount());
    await act(async()=>{renderer=create(React.createElement(Harness));await settle();});
    assert.equal(calls.length,2,"Restoring an acknowledged retry only reads its saved receipt");
    assert.equal(state.jobs.length,2);
    assert.equal(state.assessments[1].availability,"available");
  }finally{if(renderer)await act(async()=>renderer.unmount());}
});
