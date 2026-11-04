/* Retained creation IDs and completed evidence recovery never resubmit provider work. */
const test=require("node:test"), assert=require("node:assert/strict");
const React=require("react"), {act,create}=require("react-test-renderer");
const {load}=require("../ts-module.cjs"), {browser}=require("./support/browser.cjs");
const settle=()=>new Promise(resolve=>setImmediate(resolve));
const ID="33333333-3333-4333-8333-333333333333";
const job=status=>({id:ID,status,phase:status,scenario:{highway:"osm:way:1"},created:1,progress:1});

test("lost creation acknowledgement retains its UUID/body across reload; deliberate accepted work gets a new ID",()=>{
  browser();
  const first=load("features/traffic-lab/services/requestIntent.ts");
  const intent=first.retainedIntent("research",{vehicles:50});
  const reloaded=load("features/traffic-lab/services/requestIntent.ts");
  assert.equal(reloaded.retainedIntent("research",{vehicles:50}).requestId,intent.requestId);
  reloaded.acknowledge("research",intent,ID);
  assert.equal(load("features/traffic-lab/services/requestIntent.ts").readIntent("research").jobId,ID);
  assert.notEqual(reloaded.retainedIntent("research",{vehicles:50}).requestId,intent.requestId);
});

test("a retired creation acknowledgement preserves the newer area's retained recovery handle",()=>{
  browser();
  const {retainedIntent,acknowledge,readIntent}=load("features/traffic-lab/services/requestIntent.ts");
  const first=retainedIntent("discovery",{lat:47.6,lon:-122.3,radius_km:4});
  const next=retainedIntent("discovery",{lat:46.6,lon:-122.3,radius_km:4});
  acknowledge("discovery",next,ID);
  acknowledge("discovery",first,"11111111-1111-4111-8111-111111111111");
  assert.deepEqual(readIntent("discovery"),{...next,jobId:ID});
});

test("missing owned research runs clear only their saved handle; temporary reads keep it for reconnect",async()=>{
  for(const status of [404,410,503]){
    const env=browser(`https://lab.example.test/?traffic_run=${ID}`);
    Object.assign(env.events,{history:global.history,setTimeout,clearTimeout});
    const shared=load("shared/services/workspaceAccess.ts"),calls=[];
    const intent=load("features/traffic-lab/services/requestIntent.ts");
    const retained=intent.retainedIntent("research",{vehicles:50});
    intent.acknowledge("research",retained,ID);
    let state,renderer,reads=0,starts=0;
    const {useResearchJob}=load("features/traffic-lab/hooks/useResearchJob.ts",{stubs:{
      "@/shared/services/workspaceAccess":shared,
      "../services/api":{initialize:async()=>{},research:async(path,body)=>{
        calls.push({path,body});
        if(path==="/config")return {};
        if(path==="/jobs")return [];
        if(path===`/jobs/${ID}`){
          if(++reads===1)throw new shared.BrokerError("Saved run unavailable",status,"NOT_FOUND",null);
          return job("failed");
        }
        throw Error(`Unexpected ${path}`);
      }},
    }});
    function Harness(){state=useResearchJob({visible:true,loadStart:async()=>{starts+=1;},onLoaded:()=>{}});return null;}
    try{
      await act(async()=>{renderer=create(React.createElement(Harness));await settle();});
      assert.equal(state.access,"failed");
      assert.equal(intent.readIntent("research")?.jobId,status===503?ID:undefined);
      assert.equal(new URLSearchParams(location.search).get("traffic_run"),status===503?ID:null);
      if(status!==503)assert.match(state.error,/saved research run is no longer available/);
      await act(async()=>{state.reload();await settle();});
      assert.equal(state.access,"ready");
      assert.equal(reads,status===503?2:1);
      assert.equal(starts,status===503?0:1,"Only the existing scenario chooser is restored after definitive expiry");
      assert.equal(calls.some(call=>call.body!==undefined),false,"Reconnect submits no replacement simulation");
    }finally{await act(async()=>renderer?.unmount());}
  }
});

function environment({failures=1,permanent=false}={}) {
  const env=browser(`https://lab.example.test/?traffic_run=${ID}`);
  const timers=[],calls=[],loaded=[],shared=load("shared/services/workspaceAccess.ts");
  Object.assign(env.events,{setTimeout:(fn,delay)=>{const timer={fn,delay};timers.push(timer);return timer;},
    clearTimeout:timer=>{if(timer)timer.cancelled=true;}});
  let attempts=0,state;
  const {useResearchJob}=load("features/traffic-lab/hooks/useResearchJob.ts",{stubs:{
    "@/shared/services/workspaceAccess":shared,
    "../services/api":{initialize:async()=>{},research:async(path,body)=>{
      calls.push({path,body});
      if(path==="/config")return {};
      if(path==="/jobs")return [job("completed")];
      if(path===`/jobs/${ID}`)return job("completed");
      if(path.endsWith("/network"))return {type:"FeatureCollection",features:[]};
      if(path.endsWith("/result")){
        if(++attempts<=failures)throw new shared.BrokerError(permanent?"Expired replay":"Storage temporarily unavailable",
          permanent?410:503,permanent?"ARTIFACT_UNAVAILABLE":"ARTIFACT_STORE_UNAVAILABLE",null,!permanent);
        return {saved:true};
      }
      throw Error(`Unexpected ${path}`);
    }},
  }});
  function Harness(){state=useResearchJob({visible:false,loadStart:async()=>{},onLoaded:r=>loaded.push(r)});return null;}
  return {Harness,timers,calls,loaded,state:()=>state};
}

test("completed artifact GET retries recover the same run without creating or cancelling a simulation",async()=>{
  const env=environment();let renderer;
  try{
    await act(async()=>{renderer=create(React.createElement(env.Harness));await settle();});
    assert.equal(env.state().job.status,"completed");
    assert.equal(env.state().loading,true);
    await act(async()=>{env.timers[0].fn();await settle();});
    assert.deepEqual(env.loaded,[{saved:true}]);
    assert.equal(env.state().loadFailed,false);
    assert.equal(env.state().error,"");
    assert.equal(env.calls.some(call=>call.body!==undefined),false);
  }finally{await act(async()=>renderer?.unmount());}
});

test("exhausted artifact recovery exposes same-run reload; expired evidence receives no automatic retries",async()=>{
  for(const permanent of [false,true]){
    const env=environment({failures:10,permanent});let renderer;
    try{
      await act(async()=>{renderer=create(React.createElement(env.Harness));await settle();});
      if(!permanent){
        await act(async()=>{env.timers[0].fn();await settle();});
        await act(async()=>{env.timers[1].fn();await settle();});
      }
      assert.equal(env.state().loadFailed,true);
      assert.equal(env.timers.length,permanent?0:2);
      const before=env.calls.filter(call=>call.path.endsWith("/result")).length;
      await act(async()=>{env.state().reload();await settle();});
      assert.equal(env.calls.filter(call=>call.path.endsWith("/result")).length,before+1);
      assert.equal(env.state().job.id,ID);
      assert.equal(env.calls.some(call=>call.body!==undefined),false);
    }finally{await act(async()=>renderer?.unmount());}
  }
});

test("daily provider budget exhaustion is visible in the saved traffic evidence",()=>{
  browser();
  const {TrafficEvidence}=load("features/evacuation/components/traffic.tsx");
  const {renderToStaticMarkup}=require("react-dom/server");
  const rendered=renderToStaticMarkup(React.createElement(TrafficEvidence,{traffic:{mode:"provider_speed_limits",
    status:"unavailable",reason:"daily_budget_reached",matched_edges:0,highway_edges:10}}));
  assert.match(rendered,/request budget was exhausted/);
  assert.match(rendered,/No provider speed was applied/);
  assert.match(rendered,/retains its OSM speed limit/);
});
