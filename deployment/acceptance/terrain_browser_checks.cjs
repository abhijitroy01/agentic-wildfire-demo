/* Pure public terrain-browser checks. Reuse the UI's own canonical receipt/source validators. */
const assert = require("node:assert/strict");
const {load} = require("../../ui/scripts/ts-module.cjs");
const {answerJobs,answerAssessments,isFireRiskJobResponse,scenarioHorizons} =
  load("features/fire-exposure/services/assessment.ts");
const {TIMING}=load("shared/constants.ts");
const {POPULATION_SERVICE_URL,VEGETATION_SERVICE_URL,TERRAIN_SERVICE_URL,TERRAIN_RENDERING_FUNCTION}=
  load("shared/config.ts");
const mapFitMilliseconds=TIMING.MAP_DEBOUNCE_MS+TIMING.MAP_CENTER_ANIMATION_MS;
const contextQueries=new Map([
  [`${POPULATION_SERVICE_URL}/query`,{classification:"context_population",limit:{maxRecordCount:"100"}}],
  [`${VEGETATION_SERVICE_URL}/query`,{classification:"context_federal_land",limit:{resultRecordCount:"50"}}],
]);
const contextParameters={f:"json",where:"1=1",outFields:"*",geometryType:"esriGeometryPoint",
  spatialRel:"esriSpatialRelIntersects",distance:String(100*1609.34),units:"esriSRUnit_Meter",
  outSR:"4326",returnGeometry:"true"};
const active = new Set(["queued","running","cancelling"]);
const point = fire => fire && Number.isFinite(fire.latitude) && Number.isFinite(fire.longitude)
  && Math.abs(fire.latitude)<=90 && Math.abs(fire.longitude)<=180;
const sameFire = (fire,location) => location.fire && fire.latitude===location.latitude
  && fire.longitude===location.longitude && fire.source_id===location.fire.sourceId
  && fire.name===location.fire.name && (fire.source_timestamp==null
    ? !Number.isFinite(Date.parse(location.fire.lastUpdated))
    : Date.parse(fire.source_timestamp)===Date.parse(location.fire.lastUpdated));

/** Validate cross-file browser imports before a live browser or source workload can begin. */
function assertBrowserIntegration(helpers,names) {
  assert.equal(typeof helpers.mapsRedrawn,"function","Browser helpers must export callable mapsRedrawn");
  assert.ok(Number.isFinite(helpers.CONDITION_TIMEOUT_MS) && helpers.CONDITION_TIMEOUT_MS>0,
    "Browser helpers must export a positive condition timeout");
  assert.ok(Number.isFinite(mapFitMilliseconds) && mapFitMilliseconds>0,"UI map paint timing is unavailable");
  for(const name of ["STAGE_1","CHAT_INPUT","SEND_MESSAGE"])
    assert.ok(typeof names[name]==="string" && names[name].length,`Browser selector ${name} is unavailable`);
}

/** Runs as an init script: observe one clone of the UI's own POST response, never submit another request. */
function installInvestigationCapture(bindingName) {
  const original=window.fetch.bind(window);
  const notify=value=>{
    try { void Promise.resolve(window[bindingName](value)).catch(()=>{}); } catch {}
  };
  const failed=(error,httpStatus)=>notify({status:"failed",http_status:httpStatus,stream_completed:false,
    error_code:/^[A-Za-z][A-Za-z0-9_]{0,79}$/.test(error?.name) ? error.name : "UnknownError"});
  window.fetch=async(...args)=>{
    const input=args[0],init=args[1];
    const url=new URL(input instanceof Request ? input.url : input,window.location.href);
    const method=String(init?.method || (input instanceof Request ? input.method : "GET")).toUpperCase();
    const observe=url.origin===window.location.origin
      && url.pathname==="/simulation-api/v1/investigation/turns" && method==="POST";
    let response;
    try { response=await original(...args); } catch(error) {
      if(observe)failed(error,null);
      throw error;
    }
    if(observe){
      notify({status:"started",http_status:response.status,stream_completed:false,error_code:null});
      try {
        void response.clone().text().then(text=>notify({
          status:"completed",http_status:response.status,stream_completed:true,error_code:null,text,
        }),error=>failed(error,response.status));
      } catch(error) { failed(error,response.status); }
    }
    return response;
  };
}

/** Persist diagnostic codes and completion metadata only; the SSE body remains transient validation input. */
function streamStatus(value) {
  assert.ok(value && ["started","completed","failed"].includes(value.status),"Invalid cloned stream status");
  assert.ok(value.http_status===null || Number.isInteger(value.http_status)
    && value.http_status>=100 && value.http_status<=599,"Invalid cloned stream HTTP status");
  assert.equal(typeof value.stream_completed,"boolean");
  return {capture:"browser_fetch_response_clone",status:value.status,http_status:value.http_status,
    stream_completed:value.stream_completed,error_code:typeof value.error_code==="string"
      && /^[A-Za-z][A-Za-z0-9_]{0,79}$/.test(value.error_code) ? value.error_code : null};
}

function networkFailureCode(value) {
  return typeof value==="string" && /^net::ERR_[A-Z0-9_]+$/.test(value) ? value : "unknown";
}

/** Accept only the current map overlay's bounded point query, never arbitrary queries to the same service. */
function isContextQuery(target,limit) {
  const fixed={...contextParameters,...limit};
  const parameters=target.searchParams;
  const keys=[...parameters.keys()];
  const expected=[...Object.keys(fixed),"geometry"];
  if(keys.length!==expected.length || new Set(keys).size!==keys.length
    || keys.some(key=>!expected.includes(key))
    || Object.entries(fixed).some(([key,value])=>parameters.get(key)!==value))return false;
  let geometry;
  try{geometry=JSON.parse(parameters.get("geometry"));}catch{return false;}
  return geometry && Object.keys(geometry).length===3
    && Object.keys(geometry).every(key=>["x","y","spatialReference"].includes(key))
    && point({latitude:geometry.y,longitude:geometry.x})
    && geometry.spatialReference?.wkid===4326 && Object.keys(geometry.spatialReference).length===1;
}

function isHillshadeRule(value) {
  let rule;
  try{rule=JSON.parse(value);}catch{return false;}
  return rule && !Array.isArray(rule) && rule.rasterFunction===TERRAIN_RENDERING_FUNCTION
    && Object.keys(rule).length===1;
}

function isMapSpatialReference(value) {
  if(["3857","102100"].includes(value))return true;
  let reference;
  try{reference=JSON.parse(value);}catch{return false;}
  return reference && !Array.isArray(reference) && [3857,102100].includes(reference.wkid)
    && Object.keys(reference).every(key=>["wkid","latestWkid"].includes(key))
    && (reference.latestWkid===undefined || reference.latestWkid===3857);
}

/** The public context layer requests only the exact PNG export; no unavailable metadata operations are used. */
function terrainRequest(target,method) {
  const service=new URL(TERRAIN_SERVICE_URL);
  if(target.origin!==service.origin || !target.pathname.startsWith(service.pathname)
    || method!=="GET" || target.username || target.password || target.hash)return null;
  const operation=target.pathname.slice(service.pathname.length);
  const parameters=target.searchParams,keys=[...parameters.keys()];
  if(new Set(keys).size!==keys.length)return null;
  if(operation!=="/exportImage")return null;
  const required=["f","bbox","bboxSR","imageSR","size","format","renderingRule"];
  if(required.some(key=>!parameters.has(key))
    || keys.some(key=>!required.includes(key))
    || parameters.get("f")!=="image" || parameters.get("format")!=="png32"
    || !isHillshadeRule(parameters.get("renderingRule"))
    || !isMapSpatialReference(parameters.get("bboxSR"))
    || !isMapSpatialReference(parameters.get("imageSR")))return null;
  const bboxText=parameters.get("bbox").split(","),bbox=bboxText.map(Number);
  // The context map uses Web Mercator. Allow adjacent wrapped world copies, retaining its actual latitude bound.
  if(bbox.length!==4 || bboxText.some(value=>!value.trim()) || !bbox.every(Number.isFinite)
    || bbox[0]>=bbox[2] || bbox[1]>=bbox[3] || Math.abs(bbox[0])>3*20037508.342789244
    || Math.abs(bbox[2])>3*20037508.342789244 || Math.abs(bbox[1])>20037508.342789244
    || Math.abs(bbox[3])>20037508.342789244)return null;
  const size=parameters.get("size").split(",").map(Number);
  // The USGS export service accepts at most 8000 pixels in either image dimension.
  if(size.length!==2 || !size.every(value=>Number.isInteger(value) && value>0 && value<=8000))return null;
  return "context_terrain_image";
}

/** Classify provider requests without retaining query parameters, headers or credentials. */
function classifyBrowserRequest(value,method) {
  const target=new URL(value);
  const overlay=contextQueries.get(`${target.origin}${target.pathname}`);
  let classification;
  if(target.hostname.includes("bedrock-agentcore"))classification="forbidden_agentcore";
  else if(/(?:^|\.)dtn\.com$/.test(target.hostname))classification="forbidden_weather_provider";
  else if(/\/v1\/(?:landscape|exposure)\/assessments\/?$/i.test(target.pathname))
    classification="forbidden_private_modeler";
  else if(overlay && method==="GET" && !target.username && !target.password && !target.hash
    && isContextQuery(target,overlay.limit))classification=overlay.classification;
  else if(/\/ImageServer(?:\/|$)/i.test(target.pathname))
    classification=terrainRequest(target,method) || "forbidden_image_service";
  else if(/\/FeatureServer(?:\/[^/]+)?\/query\/?$/i.test(target.pathname))
    classification="forbidden_feature_service_query";
  return classification ? {method,host:target.host,path:target.pathname,classification} : null;
}

/** Snapshot sanitized classifications and counts before any acceptance assertion can stop the receipt. */
function requestClassifications(requests) {
  const counts={context_population:0,context_federal_land:0,context_terrain_image:0,
    forbidden_image_service:0,forbidden_feature_service_query:0,
    forbidden_weather_provider:0,forbidden_agentcore:0,forbidden_private_modeler:0};
  for(const request of requests)counts[request.classification]++;
  return {requests:requests.map(request=>({...request})),counts,
    forbidden_total:requests.filter(request=>request.classification.startsWith("forbidden_")).length};
}

function capturedSnapshot(value,expected) {
  const status=streamStatus(value);
  assert.ok(status.status==="completed" && status.stream_completed,
    "UI Investigation cloned stream did not complete");
  assert.ok(status.http_status>=200 && status.http_status<300,
    "UI Investigation transport returned a failed status");
  assert.equal(typeof value.text,"string","UI Investigation cloned stream returned no text");
  return snapshot(value.text,expected);
}

function options(env, receipt) {
  assert.equal(env.WFM_TERRAIN_SUBMIT,"true","Set WFM_TERRAIN_SUBMIT=true for this one additional real Investigation");
  const proof=receipt.fire, source=proof?.selected;
  assert.ok(proof?.status==="passed" && proof.terrain?.available>0
    && proof.replays?.some(replay=>replay.status==="passed" && replay.numerical_replay===true),
  "Use the successful real terrain CLI receipt with at least one sealed-input native replay");
  assert.equal(source?.source,"fixed official Esri wildfire feed");
  assert.ok(point(source.fire) && source.fire.source_id?.startsWith("esri:"),"Receipt must select an actual Esri point");
  const origin = new URL(env.WFM_PUBLIC_URL || receipt.origin);
  assert.ok(origin.protocol==="https:" && !origin.username && !origin.password && !origin.search
    && !origin.hash && origin.pathname==="/","Use the published HTTPS origin without credentials or a path");
  assert.equal(origin.origin,receipt.origin,"Browser deployment must equal the source-proof deployment");
  const versions=[...new Set(proof.terrain.jobs.flatMap(job=>job.assessments)
    .filter(result=>result.availability==="available").map(result=>result.model_version))];
  assert.equal(versions.length,1,"Source proof needs one declared landscape model identity");
  assert.ok(typeof versions[0]==="string" && versions[0].length);
  const fire=source.fire;
  return {url:origin.href,fire,modelVersion:versions[0],question:
    `Check currently reported wildfires within 5 miles of ${fire.latitude.toFixed(6)}, `+
    `${fire.longitude.toFixed(6)}. Include current weather.`};
}

function snapshot(text, expected) {
  const events=text.split(/\r?\n/).filter(line=>line.startsWith("data: "))
    .map(line=>JSON.parse(line.slice(6)));
  assert.ok(!events.some(event=>event.type==="RUN_ERROR"),"UI Investigation returned RUN_ERROR");
  const finished=events.findLastIndex(event=>event.type==="RUN_FINISHED");
  const last=events.findLastIndex(event=>event.type==="STATE_SNAPSHOT");
  assert.ok(last>=0 && finished>last,"UI Investigation needs its authoritative final snapshot before RUN_FINISHED");
  const content=events[last].snapshot.content;
  assert.ok(!content.fireExposureError,"Terrain preparation could not be queued; no success may be substituted");
  assert.ok(Array.isArray(content.fireExposureData) && content.fireExposureData.length===0,
    "A new answer cannot substitute immediate historical distance-circle results");
  const jobs=answerJobs(content.fireExposureJobs);
  assert.ok(jobs.length && jobs.length===content.fireExposureJobs.length,"New source answer requires valid terrain jobs");
  const locations=content.mapData?.locations || [];
  assert.ok(locations.some(location=>location.fire?.sourceId===expected.fire.source_id
    && location.latitude===expected.fire.latitude && location.longitude===expected.fire.longitude),
  "The new answer must retain the exact CLI-selected reported fire, rather than the search area");
  const center=content.mapData?.summary?.location;
  assert.ok(point(center) && Math.abs(center.latitude-Number(expected.fire.latitude.toFixed(6)))<=1e-6
    && Math.abs(center.longitude-Number(expected.fire.longitude.toFixed(6)))<=1e-6,
  "The current fire search must use the explicit six-decimal source coordinate");
  return {content,jobs,locations};
}

function jobReceipt(raw, reference, locations) {
  assert.ok(isFireRiskJobResponse(raw,reference),"Saved terrain receipt changed its job, fire set or terminal contract");
  assert.equal(answerAssessments(raw.assessments,locations).length,raw.assessments.length,
    "Saved terrain assessments must belong to this new answer");
  assert.ok(raw.assessments.every(assessment=>locations.some(location=>sameFire(assessment.fire,location))),
    "Saved terrain must retain exact source coordinates, name, identity and timestamp");
  return raw;
}

function selection(receipts, locations, expected) {
  assert.ok(receipts.every(job=>job.status==="completed"),"Only completed owned jobs can demonstrate terrain capacity");
  const assessments=receipts.flatMap(job=>job.assessments);
  assert.ok(assessments.every(result=>result.schema_version==="2" && result.mode==="landscape"
    && result.model_version===expected.modelVersion),"New terrain cannot use historical or different-model geometry");
  const available=assessments.filter(result=>result.availability==="available");
  assert.ok(available.length,"No actual available terrain assessment demonstrated browser capacity");
  const assessment=available.find(result=>result.fire.source_id===expected.fire.source_id
    && result.fire.latitude===expected.fire.latitude && result.fire.longitude===expected.fire.longitude);
  assert.ok(assessment,"The exact requested source fire has no available terrain assessment");
  const index=locations.findIndex(location=>sameFire(assessment.fire,location));
  assert.ok(index>=0,"Available assessment must map to the exact returned source fire choice");
  const horizons=scenarioHorizons(assessment);
  assert.deepEqual(assessment.geojson.features.map(feature=>feature.properties.elapsed_minutes),
    assessment.scenario.horizons_minutes,"Use only actual returned time horizons");
  assert.deepEqual(horizons,assessment.scenario.horizons_minutes);
  assert.ok(horizons.length && assessment.geojson.features.every(feature=>feature.properties.kind==="landscape"
    && feature.properties.inner_km==null && feature.properties.outer_km==null),"Terrain must not inflate a distance ring");
  const weather=assessment.weather, grid=assessment.landscape;
  assert.ok(weather?.availability==="available" && weather.observed_at
    && weather.query_latitude===assessment.fire.latitude && weather.query_longitude===assessment.fire.longitude
    && Number.isFinite(weather.wind_speed_mps)
    && (weather.wind_speed_mps===0 || Number.isFinite(weather.wind_from_degrees)),
  "Available terrain requires measured fire-point wind; missing wind cannot be called calm");
  assert.equal(grid.spread_cells,grid.reached_cells-1);
  assert.equal(grid.modeled_cells+grid.nonburnable_cells+grid.nodata_cells,
    grid.manifest.width*grid.manifest.height);
  if(grid.spread_cells===0)for(const feature of assessment.geojson.features)
    assert.deepEqual(feature.geometry,assessment.geojson.features[0].geometry,
      "No-growth outcome must not expand the ignition footprint between horizons");
  const job=receipts.find(job=>job.assessments.some(result=>result.assessment_id===assessment.assessment_id));
  return {assessment,jobId:job.job_id,choice:String(index),horizons,
    outcome:grid.spread_cells===0?"no_growth":"spread"};
}

function noExtraWork(requests, allowedTurns=1) {
  const turns=requests.filter(request=>request.method==="POST"
    && request.path==="/simulation-api/v1/investigation/turns");
  assert.equal(turns.length,allowedTurns,"One explicit browser Investigation, with no navigation/control resubmission");
  assert.ok(requests.every(request=>request.method!=="POST"
    || ["/simulation-api/v1/demo/session","/simulation-api/v1/investigation/turns"].includes(request.path)),
  "Terrain visualization must not submit road, simulation, source, cancellation or private model work");
}

module.exports={options,snapshot,jobReceipt,selection,noExtraWork,active,mapFitMilliseconds,
  installInvestigationCapture,streamStatus,networkFailureCode,capturedSnapshot,assertBrowserIntegration,
  classifyBrowserRequest,requestClassifications};
