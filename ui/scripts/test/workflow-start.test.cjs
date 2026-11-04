/* Explicit fire workflow starts keep source identity and request receipts through retries and reloads. */
const test = require("node:test");
const assert = require("node:assert/strict");
const {load} = require("../ts-module.cjs");
const {browser,brokerOr,origin} = require("./support/browser.cjs");
const workflow = () => load("features/evacuation/services/workflowStart.ts");
const uuid = () => {let i=0; return () => `request-${++i}`;};
const session = {session_id:"session-1",entry_mode:"radio",revision:1};
const incident = {incident_id:"incident-1",session_id:session.session_id,location_revision:1,preset_id:null,
  location:{status:"resolved",lat:47.5,lon:-121.4,matched_address:"Reported fire",origin:"reported_fire"}};
const source = (id,extra = {}) => ({observation_id:id,source_id:"esri-fire-1",name:"Reported fire",
  latitude:47.5,longitude:-121.4,source_timestamp:"2026-10-26T16:00:00Z",...extra});

test("a session acknowledgement is saved before the incident call; lost replies reuse each request identity", async () => {
  const {storage} = browser(), api = workflow(), requests=[], receipts=new Map();
  const start = api.createWorkflowStart("radio",{fire_observation_id:"saved-esri-observation"},"Reported fire",uuid());
  api.saveWorkflowStart(storage,start);
  let failIncident = true;
  const request = async (path,body) => {
    requests.push({path,body});
    if (path === "/incidents") {
      assert.equal(api.readWorkflowStart(storage).session.session_id,session.session_id,
        "The session is durable before any dependent request");
    }
    const key = `${path}:${body.request_id}`;
    if (!receipts.has(key)) receipts.set(key,path === "/sessions" ? session : incident);
    if (path === "/incidents" && failIncident) {failIncident=false; throw Error("Reply lost after acceptance");}
    return receipts.get(key);
  };
  await assert.rejects(api.acceptWorkflowLocation(start,request,value => api.saveWorkflowStart(storage,value)),
    /Reply lost/);
  const reloaded = api.readWorkflowStart(storage);
  const accepted = await api.acceptWorkflowLocation(reloaded,request,value => api.saveWorkflowStart(storage,value));
  assert.equal(accepted.incident.incident_id,incident.incident_id);
  assert.equal(requests.filter(item => item.path === "/sessions").length,1);
  const sent = requests.filter(item => item.path === "/incidents");
  assert.deepEqual(sent[0],sent[1],"A retry recovers the original incident receipt");
  assert.deepEqual(Object.keys(sent[0].body).sort(),["fire_observation_id","request_id","session_id"]);
  assert.equal(api.readRetainedWorkflows(storage).length,1);
});

test("a lost session reply retries the original session request, not a new UUID", async () => {
  const {storage} = browser(), api=workflow(), sent=[];
  const start=api.createWorkflowStart("radio",{reported_place:"Seattle"},"Seattle",uuid());
  api.saveWorkflowStart(storage,start);
  let lost=true;
  const request=async (path,body) => {
    sent.push({path,body});
    if (lost) {lost=false; throw Error("Session reply lost");}
    return path === "/sessions" ? session : incident;
  };
  await assert.rejects(api.acceptWorkflowLocation(start,request,value => api.saveWorkflowStart(storage,value)));
  await api.acceptWorkflowLocation(api.readWorkflowStart(storage),request,value => api.saveWorkflowStart(storage,value));
  assert.deepEqual(sent[0],sent[1]);
});

test("capacity and lost preparation replies preserve the accepted incident and automatic extent request", async () => {
  const {storage}=browser(), api=workflow(), sent=[];
  const intent=api.roadPreparationIntent(session,incident,uuid());
  api.saveWorkflowStart(storage,intent);
  assert.equal(api.readWorkflowStart(storage).incident.incident_id,incident.incident_id,
    "Accepted preparation-only intents remain resumable with no replacement source");
  let failed=true;
  const request=async (path,body) => {
    sent.push({path,body});
    if (failed) {failed=false; throw Error("Capacity busy");}
    return {draft_id:"draft-1",session_id:session.session_id,incident_id:incident.incident_id};
  };
  await assert.rejects(api.prepareWorkflowRoads(intent,request,value => api.saveWorkflowStart(storage,value)));
  await api.prepareWorkflowRoads(api.readWorkflowStart(storage),request,value => api.saveWorkflowStart(storage,value));
  assert.deepEqual(sent[0],sent[1]);
  assert.equal(sent.every(item => item.path === "/scenario-drafts"),true);
  assert.equal("radius_km" in sent[0].body,false,"Road discovery chooses the initial extent");
  assert.equal(api.readWorkflowStart(storage).draftId,"draft-1");
  api.clearWorkflowStart(storage,"different-intent");
  assert.ok(api.readWorkflowStart(storage),"Late cleanup cannot erase a newer workflow");
  api.clearWorkflowStart(storage,intent.id);
  assert.equal(api.readWorkflowStart(storage),null);
  assert.equal(api.readRetainedWorkflows(storage).length,1);
});

test("an explicit area override is durable and retrying it never returns to automatic defaults", async () => {
  const {storage}=browser(), api=workflow();
  const intent={...api.roadPreparationIntent(session,incident,uuid()),radiusKm:12};
  api.saveWorkflowStart(storage,intent);
  let sent;
  await api.prepareWorkflowRoads(api.readWorkflowStart(storage),async (_path,body) => {
    sent=body; return {draft_id:"fixed-draft"};
  },value => api.saveWorkflowStart(storage,value));
  assert.equal(sent.radius_km,12);
  assert.equal(sent.incident_id,incident.incident_id);
});

test("the catalog retains distinct source IDs and revised evidence without multiplying identical observations", () => {
  const {storage}=browser(), api=workflow();
  api.retainFireCatalog(storage,[source("old-1"),source("other-source",{source_id:"esri-fire-2"})]);
  const newest=source("new-1",{source_timestamp:"2026-10-26T16:00:00+00:00"});
  const kept=api.retainFireCatalog(storage,[newest]);
  assert.deepEqual(kept.map(item => item.observation_id),["new-1","other-source"]);
  const changed=source("revised-1",{longitude:-121.5,source_timestamp:"2026-10-26T17:00:00Z"});
  assert.equal(api.retainFireCatalog(storage,[changed]).length,3,
    "A materially changed provider observation remains distinct");
  assert.equal(api.retainFireCatalog(storage,[newest,changed]).length,3);
  const current=[{id:"display",name:newest.name,latitude:newest.latitude,longitude:newest.longitude,
    fire:{sourceId:newest.source_id,name:newest.name,lastUpdated:"2026-10-26T16:00:00Z"},
    weather:{temperature:{value:72,unit:"F"}}}];
  const shown=api.workflowObservations(current,kept);
  assert.equal(shown[0].id,"fire:new-1");
  assert.equal(shown[0].sourceObservationId,"new-1");
  assert.equal(shown[1].weather,undefined);
  assert.equal(api.workflowObservations([],kept).every(item => item.weather === undefined),true,
    "Switching scenarios retains fire receipts but never the earlier point weather");
});

test("expiry or owner denial belongs to the server and never turns a reported fire into name geocoding", async () => {
  const {storage}=browser(), api=workflow(), sent=[];
  const intent={...api.createWorkflowStart("radio",{fire_observation_id:"expired-source"},"Reported fire",uuid()),
    session};
  api.saveWorkflowStart(storage,intent);
  await assert.rejects(api.acceptWorkflowLocation(intent,async (path,body) => {
    sent.push({path,body}); throw Error("Saved source expired");
  },value => api.saveWorkflowStart(storage,value)),/expired/);
  assert.deepEqual(sent,[{path:"/incidents",body:{request_id:intent.incidentRequestId,
    session_id:session.session_id,fire_observation_id:"expired-source"}}]);
});

test("typed broker failures retain code, status and Retry-After without changing their message", async () => {
  let response=new Response(JSON.stringify({error:{code:"SIMULATION_CAPACITY",message:"Preparation capacity is busy."}}),
    {status:503,headers:{"Content-Type":"application/json","Retry-After":"7"}});
  browser(`${origin}/`,brokerOr(() => response));
  const {brokerJson,BrokerError}=load("shared/services/workspaceAccess.ts");
  await assert.rejects(brokerJson("/scenario-drafts",{body:{request_id:"same"}}),error => {
    assert.equal(error instanceof BrokerError,true);
    assert.equal(error.message,"Preparation capacity is busy.");
    assert.equal(error.code,"SIMULATION_CAPACITY"); assert.equal(error.httpStatus,503); assert.equal(error.retryAfter,7);
    return true;
  });
  response=new Response("Unavailable",{status:502});
  await assert.rejects(brokerJson("/scenario-drafts"),error => error.httpStatus === 502 &&
    error.code === null && error.retryAfter === null && error.message === "Simulation request failed (502).");
  response=new Response(JSON.stringify({error:{message:"Wait for admission"}}),
    {status:429,headers:{"Retry-After":new Date(Date.now()+60_000).toUTCString()}});
  await assert.rejects(brokerJson("/scenario-drafts"),error => error.retryAfter >= 59 && error.retryAfter <= 60);
});

test("malformed or conflicting saved source inputs cannot resume a different location", () => {
  const {storage}=browser(), api=workflow();
  const intent=api.createWorkflowStart("radio",{reported_place:"Seattle",fire_observation_id:"fire"},"Bad",uuid());
  api.saveWorkflowStart(storage,intent);
  assert.equal(api.readWorkflowStart(storage),null);
  storage.setItem("fireguard_workflow_start","broken-json");
  assert.equal(api.readWorkflowStart(storage),null);
});

test("nearby fire preview has an explicit keyboard action and weather-only selections have none", () => {
  const React=require("react"), {renderToStaticMarkup}=require("react-dom/server");
  const {IncidentConditions}=load("features/evacuation/components/IncidentInvestigation.tsx",{stubs:{
    "@/features/chat/components/CitationsSection":{default:() => null},
    "@/features/weather/components/ForecastSection":{default:() => null},
  }});
  const fire={id:"fire:source",sourceObservationId:"source",name:"Duplicate name",latitude:47.5,longitude:-121.4,
    fire:{name:"Duplicate name",distanceMiles:100}};
  const render=props => renderToStaticMarkup(React.createElement(IncidentConditions,{
    task:null,observations:[fire],selectedId:fire.id,select:() => {},startFire:() => {},
    point:{lat:fire.latitude,lon:fire.longitude},...props})).replace(/<!-- -->/g,"");
  const html=render({});
  assert.match(html,/<button[^>]*>Start workflow for Duplicate name<\/button>/);
  assert.match(html,/0 miles from scenario point/);
  assert.doesNotMatch(html,/100 miles away/);
  assert.match(render({busy:true}),/<button disabled=""[^>]*>Start workflow for/);
  assert.doesNotMatch(render({observations:[{...fire,fire:undefined,weather:{}}]}),/Start workflow for/);
  assert.doesNotMatch(render({observations:[{...fire,sourceObservationId:undefined}]}),/Start workflow for/);
});
