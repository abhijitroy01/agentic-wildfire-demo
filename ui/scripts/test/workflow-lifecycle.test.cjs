/* Mounted workspace regression checks: stale callbacks and reload receipts cannot replace a newer scenario. */
const test=require("node:test"), assert=require("node:assert/strict");
const React=require("react"), {act,create}=require("react-test-renderer");
const {load}=require("../ts-module.cjs"), {browser}=require("./support/browser.cjs");
const settle=() => new Promise(resolve => setImmediate(resolve));
const deferred=() => {let resolve,reject; const promise=new Promise((yes,no) => {resolve=yes;reject=no;});
  return {promise,resolve,reject};};
const scenario={lat:47.5,lon:-121.4,radius_km:8,highway:"auto",vehicles:40,crews:2,
  people_per_vehicle:2,departure_minutes:10,duration_minutes:30,exit_direction:"auto"};
const location=(label) => ({status:"resolved",reported_address:label,matched_address:label,lat:scenario.lat,
  lon:scenario.lon,origin:"geocoded_address",provider:"Esri",source_id:label});
class FixtureBrokerError extends Error {
  constructor(message,code,httpStatus=409) {super(message);this.code=code;this.httpStatus=httpStatus;this.retryAfter=null;}
}

function mountEnvironment(api) {
  const env=browser();
  Object.assign(env.events,{setTimeout,clearTimeout,setInterval,clearInterval});
  const inert=() => {}, containerRef={current:null}, registry={registerFires:inert,publishScenario:inert};
  let radioCallbacks, mapProps;
  const Workspace=load("features/evacuation/components/EvacuationWorkspace.tsx",{stubs:{
    "./workspace.css":{},"./lab-presentation.css":{},
    "@/features/chat/components/CitationsSection":{default:() => null},
    "@/features/weather/components/ForecastSection":{default:() => null},
    "@/features/radio-monitor/hooks/useLiveMonitoring":{useLiveMonitoring:callbacks => {
      radioCallbacks=callbacks; return {clearDetection:inert,status:"idle",isPlaying:false,logs:[]};
    }},
    "@/features/reports/ResponseRegistryContext":{useResponseRegistryContext:() => registry},
    "@/shared/hooks/useMapFullscreen":{useMapFullscreen:() => ({expanded:false,toggle:inert,containerRef})},
    "@/shared/services/workspaceAccess":{BrokerError:FixtureBrokerError,
      isTransientReadError:error=>error instanceof FixtureBrokerError && [408,429,500,502,503,504].includes(error.httpStatus)},
    "./SimulationMap":{REPLAY_MAP_ID:"fixture",SimulationMap:props => {mapProps=props;return null;}},
    "../services/api":{api,initialize:async () => ({access_mode:"local"}),REPORTED_ADDRESS_MIN_LENGTH:4,
      turn:async () => {throw Error("Road preparation must not call an agent turn");}},
  }}).default;
  return {env,Workspace,radio:() => radioCallbacks,map:() => mapProps};
}
const text=renderer => JSON.stringify(renderer.toJSON());
const byText=(renderer,label) => renderer.root.findAllByType("button")
  .find(button => button.props.children === label);

test("an earlier conditions rejection cannot overwrite the new incident's lease-waiting state or activity", async () => {
  const earlier=deferred(); let number=0, renderer;
  const api=async (path,body) => {
    if (path === "/capabilities") return {operations:[]};
    if (path === "/runs") return [];
    if (path === "/sessions") return {session_id:`session-${++number}`,entry_mode:"radio",revision:1};
    if (path === "/incidents") return {incident_id:`incident-${number}`,session_id:body.session_id,
      location_revision:1,preset_id:null,location:location(body.reported_address)};
    if (path === "/scenario-drafts") return {draft_id:`draft-${number}`,session_id:body.session_id,
      incident_id:body.incident_id,location_revision:1,status:"preparing",preparation_id:`prep-${number}`,
      scenario,phase:"Preparing",input_mode:"current"};
    if (path.endsWith("/investigation")) {
      if (!body) return null;
      if (path.includes("incident-1")) return earlier.promise;
      throw new FixtureBrokerError("Earlier owner investigation still running","INVESTIGATION_IN_PROGRESS");
    }
    if (path.includes("/scenario-drafts/")) return {draft_id:`draft-${number}`,status:"preparing",
      preparation_id:`prep-${number}`,scenario,phase:"Preparing",input_mode:"current"};
    throw Error(`Unexpected route ${path}`);
  };
  const harness=mountEnvironment(api);
  try {
    await act(async () => {renderer=create(React.createElement(harness.Workspace));await settle();});
    await act(async () => {await harness.radio().onDetected("First verified address");await settle();});
    await act(async () => {await harness.radio().onDetected("Second verified address");await settle();});
    assert.match(text(renderer),/Waiting for earlier investigation/);
    assert.match(text(renderer),/Resume conditions/);
    await act(async () => {earlier.reject(Error("Old conditions request failed"));await settle();});
    assert.match(text(renderer),/Second verified address/);
    assert.match(text(renderer),/Waiting for earlier investigation/);
    assert.doesNotMatch(text(renderer),/Old conditions request failed/);
    assert.equal(harness.map().showStudyArea,false,"Unvalidated preparation draws no guessed boundary");
  } finally {await act(async () => renderer?.unmount());}
});

test("a pending selection GET is fenced before PATCH; reload selects the new REST draft while old runs remain retained", async () => {
  const oldSession={session_id:"session-0",entry_mode:"radio",revision:1,incident_id:"incident-0",
    selected_run_id:"run-0",policy_id:"baseline",replay_time_s:0,latest_draft_id:null};
  const oldIncident={incident_id:"incident-0",session_id:"session-0",location_revision:1,preset_id:null,
    location:location("Accepted incident")};
  const oldRun={run_id:"run-0",session_id:"session-0",incident_id:"incident-0",location_revision:1,
    status:"failed",progress:0,phase:"Retained failed run",scenario:{...scenario,exit_direction:"south"},
    created_at:"2026-10-26T16:00:00Z",draft_id:"old-draft"};
  const heldGet=deferred(), getReached=deferred(), sent=[]; let sessionGets=0, newDraft, renderer;
  const api=async (path,body,options) => {
    sent.push({path,body,method:options?.method});
    if (path === "/capabilities") return {operations:[]};
    if (path === "/runs") return [oldRun];
    if (path === "/sessions/session-0/turns") return [];
    if (path === "/sessions/session-0") {
      if (body) {Object.assign(oldSession,body);return {...oldSession};}
      if (++sessionGets === 2) {getReached.resolve();return heldGet.promise;}
      return {...oldSession};
    }
    if (path === "/incidents/incident-0") return oldIncident;
    if (path === "/runs/run-0") return oldRun;
    if (path.endsWith("/investigation")) return null;
    if (path === "/scenario-drafts") {
      newDraft={draft_id:"new-draft",session_id:"session-0",incident_id:"incident-0",location_revision:1,
        status:"preparing",preparation_id:"new-preparation",scenario,phase:"New REST draft",input_mode:"current"};
      Object.assign(oldSession,{latest_draft_id:newDraft.draft_id,selected_run_id:null,revision:2}); return newDraft;
    }
    if (path === "/scenario-drafts/new-draft") return newDraft;
    throw Error(`Unexpected route ${path}`);
  };
  const harness=mountEnvironment(api);
  harness.env.storage.setItem("fireguard_simulation_session",oldSession.session_id);
  try {
    await act(async () => {renderer=create(React.createElement(harness.Workspace));await settle();await settle();});
    await act(async () => {await getReached.promise;});
    await act(async () => {byText(renderer,"Validate new area").props.onClick();await settle();await settle();});
    assert.equal(sent.filter(item => item.path === "/scenario-drafts").length,1);
    assert.equal(oldSession.selected_run_id,null);
    await act(async () => {heldGet.resolve({...oldSession,selected_run_id:"run-0",revision:1});await settle();});
    assert.equal(sent.filter(item => item.method === "PATCH").length,0,
      "A stale response cannot issue the old selection write after preparation began");
    assert.equal(oldRun.status,"failed","Preparing a new study area preserves the earlier retained run");
    await act(async () => {renderer.unmount();await settle();
      renderer=create(React.createElement(harness.Workspace));await settle();await settle();});
    assert.match(text(renderer),/Preparing road network/);
    assert.match(text(renderer),/New REST draft/);
    assert.equal(harness.map().scenario.radius_km,scenario.radius_km);
    assert.equal(sent.filter(item => item.path === "/scenario-drafts").length,1,
      "A reload GETs latest_draft_id instead of posting another preparation or requiring an agent receipt");
    assert.ok(sent.some(item => item.path === "/scenario-drafts/new-draft" && item.body === undefined));
  } finally {await act(async () => renderer?.unmount());}
});

test("slow saved-session restoration gates every workflow entry and cannot overwrite an early source selection", async () => {
  const held=deferred(), requested=deferred(), sent=[]; let renderer;
  const saved={session_id:"saved-session",entry_mode:"radio",revision:1,incident_id:"saved-incident",selected_run_id:null};
  const api=async (path,body) => {
    sent.push({path,body});
    if (path === "/capabilities") return {operations:[]};
    if (path === "/runs") return [];
    if (path === "/sessions/saved-session") {requested.resolve();return held.promise;}
    if (path === "/incidents/saved-incident") return {incident_id:"saved-incident",session_id:saved.session_id,
      location_revision:1,preset_id:null,location:location("Saved verified incident")};
    if (path.endsWith("/turns")) return [];
    if (path.endsWith("/investigation")) return null;
    throw Error(`Unexpected route ${path}`);
  };
  const harness=mountEnvironment(api);
  harness.env.storage.setItem("fireguard_simulation_session",saved.session_id);
  try {
    await act(async () => {renderer=create(React.createElement(harness.Workspace));await requested.promise;});
    assert.match(text(renderer),/Restoring saved workflow/);
    assert.equal(byText(renderer,"Prepare with current data"),undefined);
    assert.equal(byText(renderer,"Resolve & prepare"),undefined);
    assert.equal(byText(renderer,"Resume workflow start"),undefined);
    await act(async () => {await harness.radio().onDetected("Early new fire");await settle();});
    assert.equal(sent.some(item => item.path === "/sessions" || item.path === "/incidents"),false,
      "Even a strategy callback cannot enter a workflow while the saved context is restoring");
    await act(async () => {held.resolve(saved);await settle();await settle();});
    assert.match(text(renderer),/Saved verified incident/);
    assert.doesNotMatch(text(renderer),/Restoring saved workflow/);
    assert.ok(byText(renderer,"Resolve & prepare"));
    assert.equal(harness.map().scenario.lat,scenario.lat);
  } finally {await act(async () => renderer?.unmount());}
});

test("a rejected replacement source keeps the prior active run polling under its accepted context", async () => {
  const timers=[], sent=[]; let renderer, progressed=false;
  const saved={session_id:"old-session",entry_mode:"radio",revision:1,incident_id:"old-incident",selected_run_id:"old-run"};
  const job={run_id:"old-run",session_id:saved.session_id,incident_id:"old-incident",location_revision:1,
    status:"running",progress:0,phase:"Existing run",scenario:{...scenario,exit_direction:"south"},
    created_at:"2026-10-26T16:00:00Z",draft_id:"old-draft"};
  const api=async (path,body) => {
    sent.push({path,body});
    if (path === "/capabilities") return {operations:[]};
    if (path === "/runs") return [job];
    if (path === "/sessions/old-session") return saved;
    if (path === "/incidents/old-incident") return {incident_id:"old-incident",session_id:saved.session_id,
      location_revision:1,preset_id:null,location:location("Prior accepted point")};
    if (path === "/runs/old-run") return {...job,phase:progressed ? "Existing run progressed" : job.phase};
    if (path.endsWith("/turns")) return [];
    if (path.endsWith("/investigation")) return null;
    if (path === "/sessions") return {session_id:"rejected-source-session",entry_mode:"radio",revision:1};
    if (path === "/incidents") throw new FixtureBrokerError("New source could not be verified","GEOCODER_UNAVAILABLE",503);
    throw Error(`Unexpected route ${path}`);
  };
  const harness=mountEnvironment(api);
  Object.assign(harness.env.events,{setTimeout:(fn,delay)=>{const timer={fn,delay};timers.push(timer);return timer;},
    clearTimeout:timer=>{if(timer)timer.cancelled=true;}});
  harness.env.storage.setItem("fireguard_simulation_session",saved.session_id);
  try {
    await act(async () => {renderer=create(React.createElement(harness.Workspace));await settle();await settle();});
    await act(async () => {await assert.rejects(harness.radio().onDetected("Rejected replacement"),/could not be verified/);await settle();});
    progressed=true;
    await act(async () => {timers.findLast(timer=>timer.delay===2500&&!timer.cancelled).fn();await settle();});
    assert.match(text(renderer),/Existing run progressed/);
    assert.match(text(renderer),/Prior accepted point/);
    assert.equal(sent.some(item => item.path === "/scenario-drafts"),false);
    assert.equal(harness.env.storage.getItem("fireguard_simulation_session"),saved.session_id);
  } finally {await act(async () => renderer?.unmount());}
});

test("capacity rejection restarts prior ready geometry loading after the same-incident generation advances", async () => {
  const oldGeometry=deferred(); let renderer, networkReads=0;
  const saved={session_id:"geometry-session",entry_mode:"radio",revision:1,incident_id:"geometry-incident",
    selected_run_id:null,latest_draft_id:"ready-draft"};
  const draft={draft_id:"ready-draft",session_id:saved.session_id,incident_id:saved.incident_id,
    location_revision:1,status:"ready",preparation_id:"ready-preparation",scenario,phase:"Ready retained draft",
    input_mode:"current",network_validation:{requested_radius_km:8,effective_radius_km:8,edge_count:10,gate_count:2,
      adjusted:false,attempts:[],study_bounds:[[-121.5,47.4,-121.3,47.6]]}};
  const fresh={type:"FeatureCollection",features:[],name:"Fresh retained geometry"};
  const api=async (path,body) => {
    if (path === "/capabilities") return {operations:[]};
    if (path === "/runs") return [];
    if (path === "/sessions/geometry-session") return saved;
    if (path === "/incidents/geometry-incident") return {incident_id:saved.incident_id,session_id:saved.session_id,
      location_revision:1,preset_id:null,location:location("Retained ready point")};
    if (path.endsWith("/turns")) return [];
    if (path.endsWith("/investigation")) return null;
    if (path === "/scenario-drafts/ready-draft") return draft;
    if (path === "/scenario-drafts/ready-draft/network") return ++networkReads === 1 ? oldGeometry.promise : fresh;
    if (path === "/scenario-drafts" && body) throw new FixtureBrokerError("Preparation capacity busy","SIMULATION_CAPACITY",503);
    throw Error(`Unexpected route ${path}`);
  };
  const harness=mountEnvironment(api);
  harness.env.storage.setItem("fireguard_simulation_session",saved.session_id);
  try {
    await act(async () => {renderer=create(React.createElement(harness.Workspace));await settle();await settle();});
    assert.equal(networkReads,1);
    await act(async () => {byText(renderer,"Validate new area").props.onClick();await settle();await settle();});
    assert.equal(networkReads,2,"The accepted-context revision restarts the unchanged draft's pending geometry effect");
    assert.deepEqual(harness.map().network,fresh);
    await act(async () => {oldGeometry.resolve({type:"FeatureCollection",features:[],name:"Stale geometry"});await settle();});
    assert.deepEqual(harness.map().network,fresh,"The prior generation cannot overwrite the fresh geometry");
    assert.match(text(renderer),/Road preparation awaiting retry/);
  } finally {await act(async () => renderer?.unmount());}
});

test("guided initialization can reconnect after a read failure without starting provider work",async()=>{
  let renderer,attempts=0;const calls=[];
  const harness=mountEnvironment(async(path,body)=>{
    calls.push({path,body});
    if(path==="/capabilities"){
      if(++attempts===1)throw new FixtureBrokerError("Workspace temporarily unavailable","STORE_UNAVAILABLE",503);
      return {operations:[]};
    }
    if(path==="/runs")return [];
    throw Error(`Unexpected ${path}`);
  });
  try{
    await act(async()=>{renderer=create(React.createElement(harness.Workspace));await settle();});
    assert.match(text(renderer),/Workspace temporarily unavailable/);
    const retry=byText(renderer,"Reconnect to workspace");
    assert.ok(retry);
    await act(async()=>{retry.props.onClick();await settle();});
    assert.equal(attempts,2);
    assert.doesNotMatch(text(renderer),/Workspace temporarily unavailable/);
    assert.equal(calls.some(call=>call.body!==undefined),false);
  }finally{await act(async()=>renderer?.unmount());}
});

test("a missing owned saved session releases its handle, while temporary restoration failure retains it",async()=>{
  for(const status of [404,410,503]){
    let renderer,reads=0;const calls=[];
    const harness=mountEnvironment(async(path,body)=>{
      calls.push({path,body});
      if(path==="/capabilities")return {operations:[]};
      if(path==="/runs")return [];
      if(path==="/sessions/retained-session"){
        if(++reads===1)throw new FixtureBrokerError("Saved session unavailable","NOT_FOUND",status);
        return {session_id:"retained-session",entry_mode:"lab",revision:1,selected_run_id:null};
      }
      if(path.endsWith("/turns"))return [];
      throw Error(`Unexpected ${path}`);
    });
    harness.env.storage.setItem("fireguard_simulation_session","retained-session");
    try{
      await act(async()=>{renderer=create(React.createElement(harness.Workspace));await settle();});
      assert.equal(harness.env.storage.getItem("fireguard_simulation_session"),
        status===503?"retained-session":null);
      if(status!==503)assert.match(text(renderer),/saved simulation session is no longer available/);
      await act(async()=>{byText(renderer,"Reconnect to workspace").props.onClick();await settle();});
      assert.equal(reads,status===503?2:1,"Only a transient failure reads the same saved handle again");
      assert.doesNotMatch(text(renderer),/Restoring saved workflow/);
      assert.equal(calls.some(call=>call.body!==undefined),false,"Recovery creates no replacement paid work");
    }finally{await act(async()=>renderer?.unmount());}
  }
});

test("guided status reads wait for the preceding request and cannot undo a cancellation",async()=>{
  const slow=deferred(),timers=[];let renderer,reads=0;
  const saved={session_id:"slow-session",entry_mode:"radio",revision:1,incident_id:"slow-incident",selected_run_id:"slow-run"};
  const job={run_id:"slow-run",session_id:saved.session_id,incident_id:saved.incident_id,location_revision:1,
    status:"running",progress:0,phase:"Long retained operation",scenario:{...scenario,exit_direction:"south"},
    created_at:"2026-10-30T16:00:00Z",draft_id:"slow-draft"};
  const harness=mountEnvironment(async(path,body)=>{
    if(path==="/capabilities")return {operations:[]};
    if(path==="/runs")return [job];
    if(path==="/sessions/slow-session")return saved;
    if(path==="/incidents/slow-incident")return {incident_id:saved.incident_id,session_id:saved.session_id,
      location_revision:1,preset_id:null,location:location("Retained slow point")};
    if(path==="/runs/slow-run")return ++reads===1?job:slow.promise;
    if(path==="/runs/slow-run/cancel")return {...job,status:"cancelling",phase:"Cancellation requested"};
    if(path.endsWith("/turns"))return [];
    if(path.endsWith("/investigation"))return null;
    throw Error(`Unexpected ${path} ${!!body}`);
  });
  Object.assign(harness.env.events,{setTimeout:(fn,delay)=>{const timer={fn,delay};timers.push(timer);return timer;},
    clearTimeout:timer=>{if(timer)timer.cancelled=true;}});
  harness.env.storage.setItem("fireguard_simulation_session",saved.session_id);
  try{
    await act(async()=>{renderer=create(React.createElement(harness.Workspace));await settle();await settle();});
    assert.equal(reads,2,"One restoration read and one in-flight poll");
    assert.equal(timers.some(timer=>timer.delay===2500),false,"No overlapping status request is scheduled");
    await act(async()=>{byText(renderer,"Cancel simulation").props.onClick();await settle();});
    await act(async()=>{slow.resolve({...job,phase:"Late running reply"});await settle();});
    assert.match(text(renderer),/Cancellation requested/);
    assert.doesNotMatch(text(renderer),/Late running reply/);
    assert.equal(timers.filter(timer=>timer.delay===2500&&!timer.cancelled).length,1);
  }finally{await act(async()=>renderer?.unmount());}
});

test("guided ready-road reads recover the retained geometry without submitting preparation again",async()=>{
  const timers=[],calls=[];let renderer,reads=0;
  const saved={session_id:"ready-session",entry_mode:"radio",revision:1,incident_id:"ready-incident",
    latest_draft_id:"ready-draft",selected_run_id:null};
  const draft={draft_id:"ready-draft",session_id:saved.session_id,incident_id:saved.incident_id,location_revision:1,
    status:"ready",preparation_id:"ready-preparation",phase:"Roads ready",scenario:{...scenario,highway:"osm:way:1"},
    input_mode:"current",network_validation:{requested_radius_km:8,effective_radius_km:8,edge_count:10,gate_count:2,
      adjusted:false,attempts:[],study_bounds:[[-121.5,47.4,-121.3,47.6]]}};
  const geometry={type:"FeatureCollection",features:[],name:"Exact retained network"};
  const harness=mountEnvironment(async(path,body)=>{
    calls.push({path,body});
    if(path==="/capabilities")return {operations:[]};
    if(path==="/runs")return [];
    if(path==="/sessions/ready-session")return saved;
    if(path==="/incidents/ready-incident")return {incident_id:saved.incident_id,session_id:saved.session_id,
      location_revision:1,preset_id:null,location:location("Retained ready point")};
    if(path.endsWith("/turns"))return [];
    if(path.endsWith("/investigation"))return null;
    if(path==="/scenario-drafts/ready-draft")return draft;
    if(path==="/scenario-drafts/ready-draft/network"){
      if(++reads===1)throw new FixtureBrokerError("Retained storage temporarily unavailable","ARTIFACT_STORE_UNAVAILABLE",503);
      return geometry;
    }
    throw Error(`Unexpected ${path}`);
  });
  Object.assign(harness.env.events,{setTimeout:(fn,delay)=>{const timer={fn,delay};timers.push(timer);return timer;},
    clearTimeout:timer=>{if(timer)timer.cancelled=true;}});
  harness.env.storage.setItem("fireguard_simulation_session",saved.session_id);
  try{
    await act(async()=>{renderer=create(React.createElement(harness.Workspace));await settle();await settle();});
    assert.equal(reads,1);
    await act(async()=>{timers.find(timer=>timer.delay===2500).fn();await settle();});
    assert.equal(reads,2);
    assert.deepEqual(harness.map().network,geometry);
    assert.doesNotMatch(text(renderer),/Retained storage temporarily unavailable/);
    assert.equal(calls.some(call=>call.body!==undefined),false);
  }finally{await act(async()=>renderer?.unmount());}
});
