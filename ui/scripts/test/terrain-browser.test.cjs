/* Contract tests for maintained live UI acceptance; every response here is explicitly synthetic. */
const test=require("node:test");
const assert=require("node:assert/strict");
const fs=require("node:fs");
const path=require("node:path");
const vm=require("node:vm");
const checks=require("../../../deployment/acceptance/terrain_browser_checks.cjs");
const browserHelpers=require("../browser/helpers.cjs");
const browserNames=require("../browser/ui-names.cjs");
const {load}=require("../ts-module.cjs");
const {POPULATION_SERVICE_URL,VEGETATION_SERVICE_URL,TERRAIN_SERVICE_URL,TERRAIN_RENDERING_FUNCTION}=
  load("shared/config.ts");
const fixture=require("./fire-risk-fixtures.cjs");
const fire={...fixture.landscape().fire,source_id:"esri:fixture"};
const assessment=()=>fixture.landscape({fire});
const locations=()=>[{latitude:fire.latitude,longitude:fire.longitude,
  fire:{name:fire.name,sourceId:fire.source_id,lastUpdated:fire.source_timestamp}},
{latitude:47.6,longitude:-122.3,name:"Synthetic search city"}];
const content=()=>({mapData:{locations:locations(),summary:{location:{
  latitude:fire.latitude,longitude:fire.longitude}}},fireExposureJobs:[fixture.reference],fireExposureData:[]});
const receipt=()=>({origin:"https://workspace.example",fire:{status:"passed",
  selected:{source:"fixed official Esri wildfire feed",fire},
  terrain:{available:1,jobs:[{assessments:[assessment()]}]},
  replays:[{status:"passed",numerical_replay:true}]}});
const options=()=>checks.options({WFM_TERRAIN_SUBMIT:"true"},receipt());
const event=(type,value={})=>`data: ${JSON.stringify({type,...value})}\n\n`;
const stream=body=>event("RUN_STARTED")+event("STATE_SNAPSHOT",{snapshot:{content:body}})+event("RUN_FINISHED");
const completed=()=>fixture.response("completed",{assessments:[assessment()]});

test("maintained browser imports export their callable paint helper and fail before live work if it is missing",()=>{
  assert.equal(typeof browserHelpers.mapsRedrawn,"function","The actual maintained module exports mapsRedrawn");
  checks.assertBrowserIntegration(browserHelpers,browserNames);
  assert.throws(()=>checks.assertBrowserIntegration({...browserHelpers,mapsRedrawn:undefined},browserNames),
    /callable mapsRedrawn/);
  assert.throws(()=>checks.assertBrowserIntegration(browserHelpers,{...browserNames,CHAT_INPUT:undefined}),
    /CHAT_INPUT/);
});

test("live browser requires explicit additional work and the same real source proof deployment",()=>{
  assert.equal(options().fire.source_id,"esri:fixture");
  assert.match(options().question,/39\.000000, -121\.000000/);
  assert.doesNotMatch(options().question,/evacuation|simulate|roads/i);
  assert.throws(()=>checks.options({},receipt()),/SUBMIT/);
  const wrong=receipt();wrong.fire.replays=[];
  assert.throws(()=>checks.options({WFM_TERRAIN_SUBMIT:"true"},wrong),/native replay/);
  assert.throws(()=>checks.options({WFM_TERRAIN_SUBMIT:"true",WFM_PUBLIC_URL:"https://another.example"},receipt()),
    /deployment/);
});

test("final SSE snapshot must preserve the exact observed fire and queue a new terrain job before completion",()=>{
  const answer=checks.snapshot(stream(content()),options());
  assert.deepEqual(answer.jobs,[fixture.reference]);
  for(const alter of [
    body=>{body.fireExposureData=[assessment()];},
    body=>{body.fireExposureJobs=[];},
    body=>{body.fireExposureError={code:"RISK_PREPARATION_FAILED",message:"Terrain preparation failed."};},
    body=>{body.mapData.locations[0].latitude=47.6;},
    body=>{body.mapData.locations[0].fire.sourceId="different-source";},
    body=>{body.mapData.summary.location.longitude=-122;},
  ]){
    const body=content();alter(body);
    assert.throws(()=>checks.snapshot(stream(body),options()));
  }
  assert.throws(()=>checks.snapshot(event("STATE_SNAPSHOT",{snapshot:{content:content()}}),options()),/RUN_FINISHED/);
  assert.throws(()=>checks.snapshot(stream(content())+event("RUN_ERROR"),options()),/RUN_ERROR/);
});

test("owner job status rejects another job, fire or provisional geometry without inventing an outcome",()=>{
  assert.equal(checks.jobReceipt(completed(),fixture.reference,locations()).status,"completed");
  for(const raw of [fixture.response("running",{assessments:[assessment()]}),
    completed()]){
    if(raw.status==="completed")raw.job_id=fixture.FIRST;
    assert.throws(()=>checks.jobReceipt(raw,fixture.reference,locations()));
  }
  const wrong=completed();wrong.assessments[0].fire={...fire,latitude:47.6};
  assert.throws(()=>checks.jobReceipt(wrong,fixture.reference,locations()));
});

test("actual returned no-growth geometry, calm measurement and source index supply the screenshot controls",()=>{
  const selected=checks.selection([completed()],locations(),options());
  assert.equal(selected.choice,"0");
  assert.equal(selected.assessment.assessment_id,fixture.FIRST);
  assert.equal(selected.jobId,fixture.JOB);
  assert.equal(selected.outcome,"no_growth");
  assert.deepEqual(selected.horizons,[30,60]);
  assert.equal(selected.assessment.weather.wind_speed_mps,0);
  assert.equal(selected.assessment.weather.wind_from_degrees,null);
});

test("an available neighboring fire cannot certify the requested unavailable fire",()=>{
  const unavailable=assessment();
  unavailable.availability="unavailable";
  unavailable.failure={code:"IGNITION_NONBURNABLE",message:"Synthetic nonburnable point."};
  unavailable.geojson={type:"FeatureCollection",features:[]};
  unavailable.landscape=null;
  const neighbor=assessment();
  neighbor.assessment_id=fixture.SECOND;
  neighbor.fire_ref="esri-observation-2";
  neighbor.fire={...neighbor.fire,name:"Synthetic neighboring fire",source_id:"esri:neighbor"};
  const job=completed();
  job.fire_refs=[...job.fire_refs,neighbor.fire_ref];
  job.assessments=[unavailable,neighbor];
  const points=[...locations(),{latitude:neighbor.fire.latitude,longitude:neighbor.fire.longitude,
    fire:{name:neighbor.fire.name,sourceId:neighbor.fire.source_id,lastUpdated:neighbor.fire.source_timestamp}}];
  checks.jobReceipt(job,{job_id:job.job_id,fire_refs:job.fire_refs},points);
  assert.throws(()=>checks.selection([job],points,options()),/exact requested source fire/);
});

test("an available observation with the same source identity but another point cannot certify the requested fire",()=>{
  const wrong=assessment();
  wrong.fire={...wrong.fire,latitude:wrong.fire.latitude+.001};
  wrong.weather={...wrong.weather,query_latitude:wrong.fire.latitude};
  const job=completed();job.assessments=[wrong];
  const points=[...locations(),{latitude:wrong.fire.latitude,longitude:wrong.fire.longitude,
    fire:{name:wrong.fire.name,sourceId:wrong.fire.source_id,lastUpdated:wrong.fire.source_timestamp}}];
  checks.jobReceipt(job,fixture.reference,points);
  assert.throws(()=>checks.selection([job],points,options()),/exact requested source fire/);
});

test("browser capacity cannot pass an unavailable, circle, different model, fabricated growth or missing wind result",()=>{
  const variants=[
    value=>{value.status="failed";value.assessments=[];},
    value=>{value.assessments[0].availability="unavailable";},
    value=>{value.assessments[0].mode="wind_informed";},
    value=>{value.assessments[0].model_version="another-model";},
    value=>{value.assessments[0].geojson.features[1].geometry=structuredClone(fixture.GEOMETRY);
      value.assessments[0].geojson.features[1].geometry.coordinates[0][1][0]+=.01;},
    value=>{value.assessments[0].weather.wind_speed_mps=null;},
    value=>{value.assessments[0].geojson.features[0].properties.outer_km=25;},
  ];
  for(const alter of variants){const value=completed();alter(value);
    assert.throws(()=>checks.selection([value],locations(),options()));}
});

test("time and layer controls may read saved data but cannot submit another turn, sources, road or SUMO work",()=>{
  const requests=[{method:"POST",path:"/simulation-api/v1/demo/session"},
    {method:"POST",path:"/simulation-api/v1/investigation/turns"},
    {method:"GET",path:`/simulation-api/v1/fire-exposure/jobs/${fixture.JOB}`}];
  checks.noExtraWork(requests);
  for(const path of ["/simulation-api/v1/research/discover","/simulation-api/v1/research/jobs",
    "/simulation-api/v1/research/sources","/simulation-api/v1/fire-exposure/jobs/job/cancel",
    "/simulation-api/v1/investigation/turns"])
    assert.throws(()=>checks.noExtraWork([...requests,{method:"POST",path}]));
});

const contextQuery=(service,limit)=>`${service}/query?${new URLSearchParams({
  f:"json",where:"1=1",outFields:"*",geometry:JSON.stringify({x:-121,y:39,spatialReference:{wkid:4326}}),
  geometryType:"esriGeometryPoint",spatialRel:"esriSpatialRelIntersects",distance:"160934",
  units:"esriSRUnit_Meter",outSR:"4326",returnGeometry:"true",...limit,
})}`;

test("only the exact population and federal-land context query contracts are allowed provider reads",()=>{
  for(const [service,limit,classification] of [
    [POPULATION_SERVICE_URL,{maxRecordCount:"100"},"context_population"],
    [VEGETATION_SERVICE_URL,{resultRecordCount:"50"},"context_federal_land"],
  ]){
    const url=contextQuery(service,limit);
    assert.deepEqual(checks.classifyBrowserRequest(url,"GET"),{
      method:"GET",host:new URL(service).host,path:`${new URL(service).pathname}/query`,classification,
    });
    assert.ok(!JSON.stringify(checks.classifyBrowserRequest(url,"GET")).includes("geometry"),
      "Saved classifications retain no query input");
  }
});

test("an exact context endpoint is not permission for arbitrary geometry, filters, limits, authentication or methods",()=>{
  const normal=contextQuery(POPULATION_SERVICE_URL,{maxRecordCount:"100"});
  const changes=[
    url=>url.searchParams.set("where","OBJECTID > 0"),
    url=>url.searchParams.set("outFields","NAME"),
    url=>url.searchParams.set("distance","999999999"),
    url=>url.searchParams.set("maxRecordCount","1000"),
    url=>url.searchParams.set("resultRecordCount","50"),
    url=>url.searchParams.set("token","Synthetic private credential"),
    url=>url.searchParams.append("where","1=1"),
    url=>url.searchParams.set("geometry",JSON.stringify({x:-181,y:39,spatialReference:{wkid:4326}})),
    url=>url.searchParams.set("geometry",JSON.stringify({x:-121,y:"39",spatialReference:{wkid:4326}})),
    url=>url.searchParams.set("geometry",JSON.stringify({x:-121,y:39,spatialReference:{wkid:3857}})),
    url=>url.searchParams.set("geometry",JSON.stringify({x:-121,y:39,z:0,spatialReference:{wkid:4326}})),
    url=>url.searchParams.set("geometry","malformed"),
    url=>{url.username="Synthetic-user";url.password="Synthetic-password";},
    url=>{url.hash="Synthetic-fragment";},
    url=>{url.protocol="http:";},
    url=>{url.pathname=url.pathname.replace("/FeatureServer/0/","/FeatureServer/1/");},
    url=>{url.hostname="services.arcgis.com.example";},
  ];
  for(const alter of changes){
    const url=new URL(normal);alter(url);
    const result=checks.classifyBrowserRequest(url.href,"GET");
    assert.equal(result.classification,"forbidden_feature_service_query",url.pathname);
    assert.ok(!JSON.stringify(result).includes("Synthetic"),"Query, credentials and fragment are not recorded");
  }
  assert.equal(checks.classifyBrowserRequest(normal,"POST").classification,"forbidden_feature_service_query");
  assert.equal(checks.classifyBrowserRequest(`${POPULATION_SERVICE_URL}/query?where=1%3D1`,"GET")
    .classification,"forbidden_feature_service_query");
});

const terrainImage=()=>`${TERRAIN_SERVICE_URL}/exportImage?${new URLSearchParams({
  f:"image",bbox:"-13500000,4700000,-13400000,4800000",bboxSR:"102100",imageSR:"102100",
  size:"1512,1100",format:"png32",
  renderingRule:JSON.stringify({rasterFunction:TERRAIN_RENDERING_FUNCTION}),
})}`;

test("terrain context permits only the declared USGS PNG32 hillshade export",()=>{
  const url=new URL(terrainImage());
  assert.deepEqual(checks.classifyBrowserRequest(url.href,"GET"),{
    method:"GET",host:url.host,path:url.pathname,classification:"context_terrain_image",
  });
  for(const [key,value] of [["bboxSR",JSON.stringify({wkid:102100,latestWkid:3857})],
    ["imageSR","3857"],["size","8000,8000"]]){
    const permitted=new URL(terrainImage());permitted.searchParams.set(key,value);
    assert.equal(checks.classifyBrowserRequest(permitted.href,"GET").classification,"context_terrain_image");
  }
  assert.ok(!JSON.stringify(checks.classifyBrowserRequest(url.href,"GET")).includes("renderingRule"),
    "Terrain diagnostics omit request parameters");
});

test("terrain visualization never permits unsupported metadata, arbitrary renderers, credentials or image services",()=>{
  const changes=[
    url=>url.searchParams.set("token","Synthetic private token"),
    url=>url.searchParams.set("where","1=1"),
    url=>url.searchParams.set("f","json"),
    url=>url.searchParams.set("format","lerc"),
    url=>url.searchParams.set("format","png"),
    url=>url.searchParams.set("adjustAspectRatio","true"),
    url=>url.searchParams.set("interpolation","RSP_BilinearInterpolation"),
    url=>url.searchParams.set("size","0,1100"),
    url=>url.searchParams.set("size","1512.5,1100"),
    url=>url.searchParams.set("size","8001,8000"),
    url=>url.searchParams.set("bbox",""),
    url=>url.searchParams.set("bbox","1,2,3,"),
    url=>url.searchParams.set("bbox","3,2,1,4"),
    url=>url.searchParams.set("bbox","-13500000,4700000,-13400000,Infinity"),
    url=>url.searchParams.set("bboxSR","4326"),
    url=>url.searchParams.set("imageSR",JSON.stringify({wkid:102100,private:"Synthetic"})),
    url=>url.searchParams.set("renderingRule",JSON.stringify({rasterFunction:"Slope"})),
    url=>url.searchParams.set("renderingRule",JSON.stringify({
      rasterFunction:TERRAIN_RENDERING_FUNCTION,rasterFunctionArguments:{Raster:"Synthetic private raster"}})),
    url=>url.searchParams.set("renderingRule","malformed"),
    url=>url.searchParams.append("f","image"),
    url=>url.searchParams.delete("renderingRule"),
    url=>{url.username="Synthetic-user";url.password="Synthetic-password";},
    url=>{url.hash="Synthetic-fragment";},
    url=>{url.protocol="http:";},
    url=>{url.hostname="elevation.nationalmap.gov.example";},
    url=>{url.pathname=url.pathname.replace("/3DEPElevation/","/OtherElevation/");},
    url=>{url.pathname=url.pathname.replace("/exportImage","/identify");},
  ];
  for(const alter of changes){
    const url=new URL(terrainImage());alter(url);
    const result=checks.classifyBrowserRequest(url.href,"GET");
    assert.equal(result.classification,"forbidden_image_service",url.pathname);
    assert.ok(!JSON.stringify(result).includes("Synthetic"),"Imagery diagnostics exclude sensitive query input");
  }
  assert.equal(checks.classifyBrowserRequest(terrainImage(),"POST").classification,"forbidden_image_service");
  for(const operation of ["query","identify","getSamples","computeStatisticsHistograms","download","exportImage/",
    "keyProperties","histograms","rasterFunctionInfos"]){
    assert.equal(checks.classifyBrowserRequest(`${TERRAIN_SERVICE_URL}/${operation}?f=json`,"GET").classification,
      "forbidden_image_service");
  }
  for(const value of [`${TERRAIN_SERVICE_URL}?f=json`,`${TERRAIN_SERVICE_URL}?f=json&token=Synthetic`,
    `${TERRAIN_SERVICE_URL}/keyProperties?f=json&where=1%3D1`,
    `${TERRAIN_SERVICE_URL}/rasterFunctionInfos?${new URLSearchParams({f:"json",
      renderingRule:JSON.stringify({rasterFunction:TERRAIN_RENDERING_FUNCTION})})}`])
    assert.equal(checks.classifyBrowserRequest(value,"GET").classification,"forbidden_image_service");
});

test("fire and other FeatureServer queries remain forbidden alongside DTN, AgentCore and private modeling",()=>{
  for(const url of [
    "https://services9.arcgis.com/example/arcgis/rest/services/WFIGS_Incident_Locations_Current/FeatureServer/0/query",
    "https://services.arcgis.com/P3ePLMYs2RVChkJx/arcgis/rest/services/Other_Service/FeatureServer/0/query",
    "https://services.arcgis.com/example/FeatureServer/query",
    "https://services.arcgis.com/example/FeatureServer/layer/query",
    "https://another.example/FeatureServer/0/query/",
  ])assert.equal(checks.classifyBrowserRequest(url,"GET").classification,"forbidden_feature_service_query");
  const requests=[
    checks.classifyBrowserRequest(contextQuery(POPULATION_SERVICE_URL,{maxRecordCount:"100"}),"GET"),
    checks.classifyBrowserRequest(contextQuery(VEGETATION_SERVICE_URL,{resultRecordCount:"50"}),"GET"),
    checks.classifyBrowserRequest("https://weather.api.dtn.com/conditions?token=Synthetic-secret","GET"),
    checks.classifyBrowserRequest("https://api.auth.dtn.com/v1/tokens/authorize","POST"),
    checks.classifyBrowserRequest("https://bedrock-agentcore.us-west-2.amazonaws.com/runtimes/fixture/invocations","POST"),
    checks.classifyBrowserRequest("http://fire-risk-modeler.local:8000/v1/landscape/assessments","POST"),
    checks.classifyBrowserRequest("http://fire-risk-modeler.local:8000/v1/exposure/assessments/","POST"),
  ];
  const summary=checks.requestClassifications(requests);
  assert.deepEqual(summary.counts,{context_population:1,context_federal_land:1,
    context_terrain_image:0,forbidden_image_service:0,
    forbidden_feature_service_query:0,forbidden_weather_provider:2,forbidden_agentcore:1,forbidden_private_modeler:2});
  assert.equal(summary.forbidden_total,5);
  assert.ok(!JSON.stringify(summary).includes("Synthetic-secret"),"Receipt diagnostics exclude query values");
  assert.equal(checks.classifyBrowserRequest("https://workspace.example/simulation-api/v1/fire-exposure/jobs/fixture",
    "GET"),null,"Owner-scoped broker reads are not private modeler calls");
  assert.equal(checks.classifyBrowserRequest("https://workspace.example/simulation-api/v1/fire-exposure/assessments/fixture",
    "GET"),null);
  assert.equal(checks.classifyBrowserRequest("https://services.arcgis.com/example/MapServer/tile/0/0/0","GET"),null,
    "Ordinary basemap tiles are outside the source-query classifier");
  requests.push(checks.classifyBrowserRequest("https://another.example/FeatureServer/0/query","GET"));
  assert.equal(summary.requests.length,7,"Persisted snapshots cannot change with later captured requests");
  assert.equal(checks.requestClassifications(requests).forbidden_total,6);
});

function captureWindow(fetch,onReceipt) {
  const window={location:{href:"https://workspace.example/",origin:"https://workspace.example"},
    fetch,receiveCapture:onReceipt};
  const context=vm.createContext({window,URL,Request,Promise});
  vm.runInContext(`(${checks.installInvestigationCapture.toString()})("receiveCapture")`,context);
  return window;
}

test("browser captures a clone of the single UI request while the original response remains readable",async()=>{
  const text=stream(content()), original=new Response(text,{status:200});
  const calls=[],statuses=[];
  let complete;
  const captured=new Promise(resolve=>{complete=resolve;});
  const window=captureWindow(async(...args)=>{calls.push(args);return original;},value=>{
    statuses.push(value);
    if(value.status==="completed")complete(value);
  });
  const input="/simulation-api/v1/investigation/turns", init={method:"POST",body:"Synthetic question"};
  const response=await window.fetch(input,init);
  assert.equal(response,original,"The UI receives its own untouched Response object");
  assert.equal(await response.text(),text,"Diagnostic capture does not consume the UI stream");
  const value=await captured;
  assert.equal(calls.length,1,"Cloning never submits an alternate Investigation request");
  assert.equal(calls[0][0],input);
  assert.equal(calls[0][1],init);
  assert.deepEqual(statuses.map(value=>value.status),["started","completed"]);
  assert.deepEqual(checks.capturedSnapshot(value,options()).jobs,[fixture.reference]);
  assert.deepEqual(checks.streamStatus(value),{capture:"browser_fetch_response_clone",
    status:"completed",http_status:200,stream_completed:true,error_code:null});
  assert.ok(!Object.hasOwn(checks.streamStatus(value),"text"),"Saved diagnostics contain no SSE body");
});

test("a completed cloned body still requires the latest final snapshot followed by RUN_FINISHED and no RUN_ERROR",()=>{
  const receipt=text=>({status:"completed",http_status:200,stream_completed:true,error_code:null,text});
  const early=content();early.fireExposureJobs=[];
  const final=event("STATE_SNAPSHOT",{snapshot:{content:early}})+stream(content());
  assert.deepEqual(checks.capturedSnapshot(receipt(final),options()).jobs,[fixture.reference]);
  for(const text of [event("STATE_SNAPSHOT",{snapshot:{content:content()}}),
    stream(content())+event("RUN_ERROR"),event("RUN_FINISHED")+event("STATE_SNAPSHOT",{snapshot:{content:content()}})])
    assert.throws(()=>checks.capturedSnapshot(receipt(text),options()));
  assert.throws(()=>checks.capturedSnapshot({...receipt(final),http_status:503},options()),/failed status/);
  assert.throws(()=>checks.capturedSnapshot({...receipt(final),stream_completed:false},options()),/did not complete/);
});

test("a genuine fetch failure is preserved without changing the UI rejection or logging the error message",async()=>{
  const error=new TypeError("Synthetic private detail"), receipts=[];
  const window=captureWindow(async()=>{throw error;},value=>receipts.push(value));
  await assert.rejects(window.fetch("/simulation-api/v1/investigation/turns",{method:"POST"}),
    value=>value===error);
  assert.deepEqual(checks.streamStatus(receipts[0]),{capture:"browser_fetch_response_clone",
    status:"failed",http_status:null,stream_completed:false,error_code:"TypeError"});
  assert.ok(!JSON.stringify(receipts).includes(error.message));
  assert.equal(checks.networkFailureCode("net::ERR_ABORTED"),"net::ERR_ABORTED");
  assert.equal(checks.networkFailureCode("Unknown network detail with a synthetic token"),"unknown");
});

test("a genuinely interrupted cloned stream is a failed capture, not a terminal success",async()=>{
  const bytes=new TextEncoder().encode(event("STATE_SNAPSHOT",{snapshot:{content:content()}}));
  const original=new Response(new ReadableStream({start(controller){
    controller.enqueue(bytes);
    controller.error(new DOMException("Synthetic private interruption detail","AbortError"));
  }}));
  let finish;
  const failed=new Promise(resolve=>{finish=resolve;});
  const window=captureWindow(async()=>original,value=>{if(value.status==="failed")finish(value);});
  assert.equal(await window.fetch("/simulation-api/v1/investigation/turns",{method:"POST"}),original);
  const value=await failed;
  assert.deepEqual(checks.streamStatus(value),{capture:"browser_fetch_response_clone",status:"failed",
    http_status:200,stream_completed:false,error_code:"AbortError"});
  assert.ok(!JSON.stringify(value).includes("private"));
  assert.throws(()=>checks.capturedSnapshot(value,options()),/did not complete/);
});

test("capture ignores saved GETs, other work and a different origin",async()=>{
  const calls=[],receipts=[];
  const window=captureWindow(async(...args)=>{calls.push(args);return new Response("Synthetic read");},
    value=>receipts.push(value));
  await window.fetch("/simulation-api/v1/fire-exposure/jobs/fixture");
  await window.fetch("/simulation-api/v1/investigation/turns");
  await window.fetch("https://other.example/simulation-api/v1/investigation/turns",{method:"POST"});
  assert.equal(calls.length,3);
  assert.deepEqual(receipts,[]);
});

test("a browser launch failure seals a failed diagnostic receipt before any source or browser request",async()=>{
  const writes=[],errors=[];
  const fakeProcess={env:{WFM_TERRAIN_SUBMIT:"true",WFM_TERRAIN_RECEIPT:"Synthetic-proof.json"},exitCode:0};
  const fakeFs={mkdirSync(){},readFileSync(){return JSON.stringify(receipt());},
    writeFileSync(filename,text){writes.push(JSON.parse(text));}};
  const fakeChromium={async launch(){throw Error("Synthetic Chrome launch failure");}};
  const fakeRequire=name=>{
    if(name==="node:fs")return fakeFs;
    if(name==="playwright")return {chromium:fakeChromium};
    if(name.endsWith("ui-names.cjs"))return browserNames;
    if(name.endsWith("helpers.cjs"))return browserHelpers;
    if(name==="./terrain_browser_checks.cjs")return checks;
    return require(name);
  };
  const source=fs.readFileSync(path.resolve(__dirname,"../../../deployment/acceptance/check_terrain_browser.cjs"),"utf8");
  await vm.runInNewContext(source,{require:fakeRequire,process:fakeProcess,setTimeout,URL,
    console:{log(){},error(value){errors.push(value);}}});
  assert.deepEqual(writes.map(value=>value.status),["running","failed"]);
  assert.equal(writes.at(-1).error,"Synthetic Chrome launch failure");
  assert.equal(fakeProcess.exitCode,1);
  assert.equal(errors.length,1);
});
