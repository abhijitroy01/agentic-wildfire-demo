/* The mocked broker behind the browser checks: every /simulation-api route the page calls, in a table keyed by method
 * and path, with the counters the checks read and waiters for the state they wait on. Providers and agents are
 * fixtures; no AWS job is submitted. */
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const http = require("node:http");
const {CONDITION_TIMEOUT_MS} = require("./helpers.cjs");
const terrainFixture = require("../test/fire-risk-fixtures.cjs");

const id = () => crypto.randomUUID();
/** Server-sent AG-UI frames. */
const sse = frames => frames.map(frame => `data: ${JSON.stringify(frame)}\n\n`).join("");
/** A supervisor tool call as the Runtime streams it. Agent activity must never show its arguments. */
const toolCall = (toolCallId, toolCallName) => [{type:"TOOL_CALL_START",toolCallId,toolCallName},
  {type:"TOOL_CALL_ARGS",toolCallId,delta:JSON.stringify({query:"<b>tool-argument-marker</b>"})},
  {type:"TOOL_CALL_END",toolCallId}];
/** The tool's result envelope, with status "success" or "error". */
const toolResult = (toolCallId, status) => ({type:"TOOL_CALL_RESULT",toolCallId,
  content:JSON.stringify({status,content:[{text:"Fixture receipt."}]})});
const answer = content => [{type:"STATE_SNAPSHOT",snapshot:{type:"structuredResponse",content}},{type:"RUN_FINISHED"}];
/** A route handler that answered the request itself returns HANDLED; any other value is sent as JSON. */
const HANDLED = Symbol("handled");
const VOICE_FIXTURE_URL = "https://fixture.s3.us-west-2.amazonaws.com/voice-messages/fixture/en.mp3";
const TURN_FIELDS = "draft_id,incident_id,intent,location_revision,policy_id,question,replay_time_s,request_id,run_id,session_id";
const REVIEW_ANSWER = "Fixture crew review: staging and travel times come from the saved recording.";
// The review variant whose facts cite every policy, as a crew evidence call without policy_ids returns them.
const REVIEW_EVERY_POLICY = "Fixture crew review citing every policy: all three come from the saved recording.";

/** Serve one AG-UI stream from loopback in parts: the first at once, each later part after one release().
 * The investigation route continues the intercepted request here, so the page reads it frame by frame. */
async function steppedStream(parts, onPart = () => {}) {
  let released = 0, wake = () => {}, activeResponse;
  const server = http.createServer(async (request, response) => {
    activeResponse = response;
    request.resume();
    response.writeHead(200, {"Content-Type":"text/event-stream","Cache-Control":"no-store",Connection:"close"});
    for (const [index, part] of parts.entries()) {
      while (released < index) await new Promise(resolve => { wake = resolve; });
      onPart(part);
      if (!response.writableEnded && !response.destroyed) response.write(sse(part));
    }
    if (!response.writableEnded && !response.destroyed) response.end();
    server.close();
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  return {url:`http://127.0.0.1:${server.address().port}/`, release: () => { released++; wake(); },
    // The connection can end while the accepted owner work continues and saves its result independently.
    disconnect: () => activeResponse.end()};
}

/** Fixture data the routes serve and the checks compare against. */
function fixtureData(result) {
  const fixtureLocation = {
    status: "resolved", reported_address: "Verification fixture address, Paradise, California",
    matched_address: "Verification fixture — Paradise, California", lat: result.scenario.lat,
    lon: result.scenario.lon, coordinate_system: "WGS84", address_type: "Locality", provider: "Amazon Location",
    score: 100, resolved_at: new Date().toISOString(), resolver_version: "amazon-location-v1", source_id: "test-fixture",
    meaning: "Browser verification fixture; not a live provider lookup.",
  };
  return {fixtureLocation, firetruckId: id(), reportId: id(),
    researchJob: {id:id(),status:"completed",progress:1,phase:"Completed retained recording",
      created:Date.now()/1000,scenario:result.scenario},
    detection: { type: "detection", data: { location: "123 Fixture Road, Paradise, California",
      confidence: 0.98, keywords: ["fire"], transcript_excerpt: "Exercise: fire at 123 Fixture Road, Paradise, California." } },
    investigationEvidence: {
      response: "Public investigation broker fixture.",
      location: {name:"Investigation fixture point",latitude:fixtureLocation.lat,longitude:fixtureLocation.lon},
      mapData: {locations:[{name:"Investigation fixture point",latitude:fixtureLocation.lat,longitude:fixtureLocation.lon,
        currentWeather:{temperature:{value:72,unit:"degF"},relativeHumidity:{value:30,unit:"percent"},
          weatherDescription:"Clear fixture conditions",timestamp:"2026-10-07T17:00:00Z"}}]},
      knowledgeBaseCitations:[{documentName:"Investigation source fixture",publisher:"Fixture agency",
        sourceUrl:"https://www.ready.gov/wildfires",excerpt:"Retained source passage for the first answer."}],
    }};
}

/** Retained model-output contract fixtures. Shapes are synthetic UI evidence, not a live spread calculation. */
function fireExposureFixture(latitude, longitude) {
  const fire = {latitude, longitude, name:"Later fixture fire",source_id:"fixture-fire-6",
    source_timestamp:"2026-10-26T16:00:00Z"};
  const weather = {query_latitude:latitude,query_longitude:longitude,source:"DTN",availability:"available",
    observed_at:"2026-10-26T16:05:00Z",wind_speed_mps:4,wind_from_degrees:270};
  const geometry = size => ({type:"Polygon",coordinates:[[
    [longitude-size,latitude-size],[longitude+size,latitude-size],
    [longitude+size,latitude+size],[longitude-size,latitude+size],[longitude-size,latitude-size],
  ]]});
  const base = {schema_version:"1",fire_ref:"fixture-fire-6",input_hash:"a".repeat(64),model_version:"ui-fixture-v1",
    calculated_at:"2026-10-26T16:10:00Z",availability:"available",fire,weather,
    assumptions:["Synthetic retained UI contract fixture."],warnings:[]};
  return [{...base,assessment_id:"0f684c63-58a4-4e3f-9667-b4e12d7a563a",mode:"wind_informed",scenario:null,
    geojson:{type:"FeatureCollection",features:[{type:"Feature",geometry:geometry(.015),
      properties:{kind:"downwind",label:"Retained illustrative exposure"}}]}},
  {...base,assessment_id:"cb92d45a-9250-4213-8410-aac5cc4f8795",mode:"surface_fire",
    scenario:{engine:"Pyrothermel/Behave",fuel_model:1,moisture_profile:"moderate",slope_degrees:0,
      wind_height_assumption:"DTN wind treated as 10 m above ground; open unsheltered fuel assumed",
      initialization:"reported point treated as demo ignition",horizons_minutes:[30,60,120]},
    geojson:{type:"FeatureCollection",features:[30,60,120].map((minute,index) => ({
      type:"Feature",geometry:geometry(.003 * (index+1)),
      properties:{kind:"surface_fire",label:`Retained ${minute}-minute scenario`,elapsed_minutes:minute},
    }))}}];
}

/** The supervisor turn for the `count`th investigation, as the Runtime streams it: specialist tool calls, its own
 * completion call, then the outcome. A turn of several parts is released step by step. */
function investigationParts(count, result, evidence) {
  // A final explicit handoff check reuses the same synthetic contract answer after the recovery reload.
  if (count === 8) return investigationParts(6,result,evidence);
  if (count === 9) return [[{type:"RUN_STARTED"}, ...toolCall("terrain-detect","detection_agent_tool"),
    toolResult("terrain-detect","success"), ...toolCall("terrain-final","PublicAnswerResponse"),
    toolResult("terrain-final","success"), ...answer({response:"Synthetic pending terrain job contract fixture.",
      location:{name:"Seattle search area",latitude:47.6,longitude:-122.3},
      mapData:{locations:terrainFixture.locations},fireExposureData:[],fireExposureJobs:[terrainFixture.reference]})]];
  const expires = Math.floor(Date.now()/1000)+3600;
  const voiceDraft = {response:"Reviewable communication fixture. No alert has been sent.",
    audioData:{success:true,messageId:id(),generatedAt:new Date().toISOString(),
      expiresAt:new Date(expires*1000).toISOString(),summary:{totalLanguages:1,successful:1,failed:0},
      languages:{en:{languageCode:"en",languageName:"English",voice:"Joanna",audioUrl:VOICE_FIXTURE_URL,
        expires,translatedText:"Demonstration message for review.",success:true}}}};
  return {
    // Observations, then guidance, then the synthesis, in the order SUPERVISOR_PROMPT requires.
    // Released in steps so the page is checked mid-run.
    1: [[{type:"RUN_STARTED"}, ...toolCall("detect-1","detection_agent_tool"), toolResult("detect-1","success"),
      ...toolCall("guide-1","public_response_agent_tool")],
    [toolResult("guide-1","success"), ...toolCall("final-1","PublicAnswerResponse")],
    [toolResult("final-1","success"), ...answer(evidence)]],
    // No specialist call: the supervisor answers directly.
    2: [[{type:"RUN_STARTED"}, ...toolCall("final-2","PublicAnswerResponse"), toolResult("final-2","success"),
      ...answer({response:"Second answer with separate evidence.",
        location:{name:"Second point",latitude:38.58,longitude:-121.49}})]],
    // A failed guidance call still reaches the answer beside the communication draft.
    3: [[{type:"RUN_STARTED"}, ...toolCall("guide-3","public_response_agent_tool"), toolResult("guide-3","error"),
      ...toolCall("voice-3","communication_agent_tool"), toolResult("voice-3","success"),
      ...toolCall("final-3","PublicAnswerResponse"), toolResult("final-3","success"), ...answer(voiceDraft)]],
    // The run fails while a specialist is still working, with the broker's replacement message.
    4: [[{type:"RUN_STARTED"}, ...toolCall("detect-4","detection_agent_tool"),
      {type:"RUN_ERROR",code:"INVESTIGATION_FAILED",message:"Investigation could not complete. Please retry."}]],
    // After the reloads, a fresh answer at the fixture point for "Simulate this search area" (UI-04).
    5: [[{type:"RUN_STARTED"}, ...toolCall("final-5","PublicAnswerResponse"), toolResult("final-5","success"),
      ...answer(evidence)]],
    // A later answer with a fire near the fixture incident and retained baseline and scenario geometry.
    6: [[{type:"RUN_STARTED"}, ...toolCall("detect-6","detection_agent_tool"), toolResult("detect-6","success"),
      ...toolCall("final-6","PublicAnswerResponse"), toolResult("final-6","success"),
      ...answer({response:"Later answer with a nearby fixture fire.",
        location:{name:"Later search place",latitude:result.scenario.lat+.03,longitude:result.scenario.lon+.03},
        mapData:{locations:[{name:"Later fixture fire",latitude:result.scenario.lat+.02,
          longitude:result.scenario.lon+.02,fire:{name:"Later fixture fire",sourceId:"fixture-fire-6",
            lastUpdated:"2026-10-26T16:00:00Z",acresBurned:40,percentContained:10}}]},
        fireExposureData:fireExposureFixture(result.scenario.lat+.02,result.scenario.lon+.02)})]],
    // A provider place name that is not text, as the runtime may copy it from a tool result.
    7: [[{type:"RUN_STARTED"}, ...toolCall("final-7","PublicAnswerResponse"), toolResult("final-7","success"),
      ...answer({response:"Answer with a place name that is not text.",
        location:{name:42,latitude:result.scenario.lat,longitude:result.scenario.lon}})]],
  }[count];
}

/** The completed incident task snapshot: the initial plan, or the updated plan for a selected run. */
function plannedSnapshot(result, selectedRun) {
  return {type:"structuredResponse",content:{
    response:selectedRun ? "Updated fixture plan: review resident delays and crews that did not arrive." :
      "Initial fixture plan: sourced fire and weather observations; official guidance.",
    mapData:{locations:[{name:"Fixture observation",latitude:result.scenario.lat+.01,
      longitude:result.scenario.lon+.01,currentWeather:{
        temperature:{value:72,unit:"F"},wind:{speed:{value:8,unit:"mph"}}},
      fire:{name:"Nearby fixture fire",sourceId:"fixture-nearby-1",lastUpdated:"2026-10-26T16:00:00Z",
        acresBurned:120,percentContained:35,distanceMiles:12}},
    {latitude:result.scenario.lat+.02,longitude:result.scenario.lon+.02,
      fire:{name:"Unknown-size fixture fire",sourceId:"fixture-nearby-2",lastUpdated:"2026-10-26T16:00:00Z"},
      currentWeather:{temperature:{value:48,unit:"degF"}}},
    {name:"Scenario weather",latitude:result.scenario.lat,longitude:result.scenario.lon,
      currentWeather:{temperature:{value:80,unit:"degF"},relativeHumidity:{value:20,unit:"percent"},
        timestamp:"2026-10-05T17:00:00Z"}}]},
    location:{name:"Verified scenario forecast",latitude:result.scenario.lat,longitude:result.scenario.lon},
    forecastData:{days:[{dayName:"Tuesday",date:"2026-10-06",periods:[
      {timestamp:"2026-10-06T06:00:00Z",temperature:{value:70,unit:"degF"}},
      {timestamp:"2026-10-06T00:00:00Z",temperature:{value:75,unit:"degF"}},
      {timestamp:"2026-10-06T12:00:00Z",temperature:{value:68,unit:"degF"}}]}]},
    knowledgeBaseCitations:[{documentName:"Official browser fixture",publisher:"Fixture agency",
      sourceUrl:"https://www.ready.gov/wildfires",sourceDate:"2026-08-01",retrievedAt:"2026-09-28",
      excerpt:"Fixture guidance for UI verification. ".repeat(12)}],
  }};
}

/** Firetruck exercise routes: start, stop, status and reports for the one fixture run. */
function firetruckRoutes(b) {
  const run = `/firetruck/runs/${b.firetruckId}`;
  return [
    ["GET", "/firetruck/health", () => ({status: "ok"})],
    ["POST", "/firetruck/runs", ({body}) => {
      b.firetruckStarts++;
      b.firetruckState = "running";
      assert.equal(body.fire_location.latitude, b.fixtureLocation.lat);
      assert.ok(body.request_id && !body.url && !body.telemetry);
      return {session_id: b.firetruckId, status: "running"};
    }],
    ["DELETE", run, () => {
      b.firetruckStops++;
      // Like the service, the run stops at its next update, and its final snapshot reaches only a stream still open.
      const started = b.firetruckStarts;
      setTimeout(() => {
        if (started !== b.firetruckStarts) return;
        b.firetruckState = "stopped";
        b.changed();
        if (!b.truckSocketOpen) return;
        b.truckSocket.send(JSON.stringify({...b.truckEvidence(), event_type: "TRUCK_UPDATE"}));
        b.truckSocket.send(JSON.stringify({event_type: "SIMULATION_STOPPED", status: "stopped"}));
      }, 1000);
      return {session_id: b.firetruckId, status: "stopping"};
    }],
    ["GET", run, () => {
      b.firetruckStatusReads++;
      return b.truckEvidence();
    }],
    ["POST", `${run}/reports`, ({body}) => {
      b.reportRequests++;
      assert.deepEqual(Object.keys(body).sort(), ["question", "request_id"]);
      return {report_id: b.reportId, status: "running"};
    }],
    ["GET", `/firetruck/reports/${b.reportId}`, () => ({report_id: b.reportId, status: "completed"})],
  ];
}

/** Access, research, radio and investigation-turn routes. */
function workspaceRoutes(b, {result, network, capabilities}) {
  const job = `/research/jobs/${b.researchJob.id}`;
  return [
    ["GET", "/workspace-config", () => ({ agent_configured: true, access_mode: "public_demo" })],
    ["POST", "/demo/session", () => ({ ready: true, access_mode: "public_demo", session_created: ++b.bootstraps === 1 })],
    ["GET", "/research/config", () => ({ deployment: "AWS", traffic_key_configured: true })],
    ["GET", "/research/jobs", () => [b.researchJob]],
    ["GET", job, () => b.researchJob],
    ["GET", `${job}/artifacts/result`, () => result],
    ["GET", `${job}/artifacts/network`, () => network],
    ["GET", "/research/examples/paradise", () => ({ area: result.scenario,
      geometry: { type: "FeatureCollection", features: [] }, highways: [result.network.selected_route] })],
    ["POST", "/research/discover", async ({body,route}) => {
      assert.match(body.request_id,/^[a-f0-9-]{36}$/i);
      const {request_id,...area}=body;
      b.roadDiscoveryJob={id:id(),kind:"road_discovery",status:"queued",scenario:area,created:Date.now()/1000,
        phase:"Synthetic regional road extraction pending",progress:.2};
      await route.fulfill({status:202,json:b.roadDiscoveryJob});
      return HANDLED;
    }],
    ["GET", /^\/research\/discover\/[^/]+$/, ({endpoint}) => {
      assert.equal(endpoint.split("/").at(-1),b.roadDiscoveryJob.id);
      const area=b.roadDiscoveryJob.scenario;
      const result={area,source_mode:"Synthetic UI road contract fixture; no provider extraction.",
        geometry:{type:"FeatureCollection",features:[{type:"Feature",properties:{corridor_id:"osm:way:fixture"},
          geometry:{type:"LineString",coordinates:[[area.lon-.01,area.lat],[area.lon+.01,area.lat]]}}]},
        highways:[{corridor_id:"osm:way:fixture",label:"Synthetic Seattle road corridor",source:"OpenStreetMap",
          road_class:"primary",source_way_ids:[1],approx_distance_km:.1}]};
      return {...b.roadDiscoveryJob,status:b.completeRoadDiscovery?"completed":"running",
        phase:b.completeRoadDiscovery?"Synthetic roads retained":"Synthetic regional road extraction pending",
        progress:b.completeRoadDiscovery?1:.4,...(b.completeRoadDiscovery?{result}:{})};
    }],
    ["GET", `/fire-exposure/jobs/${terrainFixture.JOB}`, () => {
      b.fireRiskJobReads++;
      return terrainFixture.response(b.completeFireRiskJob?"completed":"running");
    }],
    ["POST", "/investigation/turns", async ({body, route}) => {
      assert.match(body.request_id, /^[a-f0-9]{8}-[a-f0-9]{4}-[1-5][a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/i);
      assert.ok(typeof body.thread_id === "string" && body.thread_id.length > 0);
      assert.ok(body.question);
      assert.ok(!body.forwardedProps && !body.agentRuntimeArn);
      const existing = b.investigationReceipts[body.request_id];
      if (existing) {
        if (existing.question !== body.question || existing.receipt.thread_id !== body.thread_id) {
          await route.fulfill({status:409,json:{error:{code:"IDEMPOTENCY_CONFLICT",
            message:"Request ID belongs to a different investigation.",retryable:false}}});
          return HANDLED;
        }
        const {receipt} = existing;
        if (receipt.status === "running") await existing.finished;
        const frames = receipt.status === "completed"
          ? [{type:"STATE_SNAPSHOT",snapshot:receipt.snapshot},{type:"RUN_FINISHED"}]
          : [{type:"RUN_ERROR",...receipt.error}];
        await route.fulfill({contentType:"text/event-stream",body:sse(frames)});
        return HANDLED;
      }
      b.investigations++;
      const receipt = {request_id:body.request_id,thread_id:body.thread_id,attempt_id:id(),status:"running",
        created_at:Date.now()/1000,completed_at:null,lease_until:Date.now()/1000+210,snapshot:null,error:null};
      let finish;
      const retained = {question:body.question,receipt,finished:new Promise(resolve => {finish = resolve;})};
      b.investigationReceipts[body.request_id] = retained;
      const retainPart = frames => {
        for (const frame of frames) {
          if (frame.type === "STATE_SNAPSHOT") receipt.snapshot = frame.snapshot;
          if (frame.type === "RUN_FINISHED") Object.assign(receipt,{status:"completed",completed_at:Date.now()/1000});
          if (frame.type === "RUN_ERROR") Object.assign(receipt,{status:"failed",completed_at:Date.now()/1000,
            snapshot:null,error:{code:frame.code,message:frame.message}});
        }
        if (receipt.status !== "running") finish();
        b.changed();
      };
      const parts = investigationParts(b.investigations, result, b.investigationEvidence);
      if (parts.length > 1) {
        b.heldTurn = await steppedStream(parts, retainPart);
        await route.continue({url:b.heldTurn.url});
      } else {
        retainPart(parts[0]);
        await route.fulfill({ contentType: "text/event-stream", body: sse(parts[0]) });
      }
      return HANDLED;
    }],
    ["GET", /^\/investigation\/turns\/[^/]+$/, async ({endpoint,route}) => {
      // Each broker belongs to one browser visitor; a new visitor gets an empty owned receipt collection.
      const retained = b.investigationReceipts[endpoint.split("/").at(-1)];
      if (retained) return structuredClone(retained.receipt);
      await route.fulfill({status:404,json:{error:{code:"NOT_FOUND",
        message:"Investigation turn not found.",retryable:false}}});
      return HANDLED;
    }],
    ["POST", "/radio/stream", async ({body, route}) => {
      b.radioRequests++; assert.equal(body.playback, true);
      const frames = [{ type: "connected", data: { session_id: id() } },
        { type: "simulation_audio", data: { url: "https://simulation-audio.invalid/fixture.wav",
          file: "8cc27aab6fe8b511d6ca2ed4d06ec1453740c29c7ed779bdf784e17ce5673441.mp3" } },
        b.detection, b.detection, {type:"complete",data:{first_detection:b.detection.data}}];
      await route.fulfill({ contentType: "text/event-stream", body: sse(frames) });
      return HANDLED;
    }],
    ["GET", /^(?:\/.*)?\/capabilities$/, () => capabilities],
  ];
}

/** Session, incident, incident-task and scenario-draft routes. */
function sessionRoutes(b, {result, network}) {
  return [
    ["POST", "/sessions", ({body}) => {
      b.sessionPosts.push(body);
      if (b.sessionReceipts[body.request_id]) return b.sessionReceipts[body.request_id];
      b.session = { session_id: id(), revision: 1, entry_mode: body.entry_mode, selected_run_id: null,
        policy_id: "baseline", replay_time_s: 0 };
      b.sessions[b.session.session_id] = b.session;
      b.sessionReceipts[body.request_id] = b.session; return b.session;
    }],
    ["GET", /^\/sessions\/[^/]+\/turns$/, () => b.outputs],
    ["GET", /^\/sessions\/[^/]+$/, ({endpoint}) => b.sessions[endpoint.split("/")[2]]],
    ["PATCH", /^\/sessions\/[^/]+$/, ({body, endpoint}) => {
      const response = b.sessions[endpoint.split("/")[2]];
      // A selection update saves only the selection: no demo role or illustrative number is ever saved.
      b.sessionUpdates++;
      const fields = Object.keys(body).sort().join(",");
      if (fields !== "expected_revision,policy_id,replay_time_s,request_id,selected_run_id")
        b.errors.push(`Session update sent unexpected fields: ${fields}`);
      Object.assign(response, { revision: response.revision+1, selected_run_id: body.selected_run_id,
        policy_id: body.policy_id, replay_time_s: body.replay_time_s });
      return response;
    }],
    ["POST", "/incidents", async ({body,route}) => {
      b.incidentPosts.push(body);
      if (b.incidentReceipts[body.request_id]) return b.incidentReceipts[body.request_id];
      assert.equal(["reported_address","reported_place","coordinates","fire_assessment_id","fire_observation_id","preset_id"]
        .filter(field => body[field] != null).length,1,"One exclusive location input goes to the broker");
      if (b.rejectNextLocation) {
        const message = b.rejectNextLocation;
        b.rejectNextLocation = null;
        await route.fulfill({status:422,json:{error:{message}}});
        return HANDLED;
      }
      let location = b.fixtureLocation;
      if (body.fire_observation_id) {
        const source = Object.values(b.incidentTasks).flatMap(task => task.fire_observations || [])
          .find(item => item.observation_id === body.fire_observation_id);
        assert.ok(source,"The selected nearby fire references an owned immutable source fixture");
        location = {...b.fixtureLocation,lat:source.latitude,lon:source.longitude,
          reported_address:source.name,matched_address:source.name,provider:"Esri",origin:"reported_fire",
          source_observation_id:source.observation_id,source_id:source.source_id,
          source_timestamp:source.source_timestamp,resolver_version:"fire-observation-v1"};
      } else if (body.fire_assessment_id) {
        const assessment = fireExposureFixture(result.scenario.lat+.02,result.scenario.lon+.02)
          .find(item => item.assessment_id === body.fire_assessment_id);
        assert.ok(assessment,"The scenario references an actual retained assessment fixture");
        location = {...b.fixtureLocation,lat:assessment.fire.latitude,lon:assessment.fire.longitude,
          reported_address:assessment.fire.name,matched_address:assessment.fire.name,provider:"Esri",
          address_type:"Reported fire observation",score:null,origin:"reported_fire",
          source_assessment_id:assessment.assessment_id,source_timestamp:assessment.fire.source_timestamp,
          resolver_version:"fire-observation-v1",meaning:"Retained browser contract fixture; no provider lookup."};
      } else if (body.coordinates) {
        location = {...b.fixtureLocation,...body.coordinates,provider:"User input",score:null,origin:"user_coordinates",
          resolver_version:"coordinates-v1",meaning:"Explicit browser fixture scenario coordinate."};
      } else if (body.reported_place) location = {...location,origin:"geocoded_place"};
      b.incident = { incident_id: id(), session_id: body.session_id, location_revision: 1,
        preset_id: body.preset_id || null, location };
      Object.assign(b.sessions[body.session_id], {incident_id: b.incident.incident_id,
        revision: b.sessions[body.session_id].revision+1});
      b.incidents[b.incident.incident_id] = b.incident;
      b.incidentReceipts[body.request_id] = b.incident; return b.incident;
    }],
    ["POST", /^\/incidents\/[^/]+\/investigation$/, async args => {
      if (b.rejectNextInitialInvestigation && !args.body?.run_id) {
        const error=b.rejectNextInitialInvestigation; b.rejectNextInitialInvestigation=null;
        await args.route.fulfill({status:409,json:{error}}); return HANDLED;
      }
      return incidentTask(b,result,args);
    }],
    ["GET", /^\/incidents\/[^/]+\/investigation$/, args => incidentTask(b, result, args)],
    ["GET", /^\/incidents\//, ({endpoint}) => b.incidents[endpoint.split("/")[2]]],
    ["POST", /^\/incidents\/[^/]+\/investigation\/cancel$/, ({endpoint}) => b.incidents[endpoint.split("/")[2]]],
    ["POST", "/scenario-drafts", async ({body,route}) => {
      b.preparationPosts.push(body);
      if (b.preparationReceipts[body.request_id]) return b.preparationReceipts[body.request_id];
      if (b.rejectNextPreparation) {
        const error = b.rejectNextPreparation; b.rejectNextPreparation = null;
        await route.fulfill({status:503,headers:{"Retry-After":"5"},json:{error}});
        return HANDLED;
      }
      const hold = b.heldPreparation;
      if (hold) {hold.reached = true; b.changed(); await hold.released;}
      const prepared = createPreparation(b,result,body);
      b.preparationReceipts[body.request_id] = prepared; return prepared;
    }],
    ["GET", /^\/scenario-drafts\/[^/]+\/network$/, () => network],
    ["GET", /^\/scenario-drafts\/[^/]+$/, ({endpoint}) => {
      const draft = b.drafts[endpoint.split("/")[2]];
      assert.ok(draft,"GET rejoins a specific saved draft");
      b.preparationPolls++;
      if (draft.draft_id === b.failedDraftId && b.preparationPolls > 1) Object.assign(draft, {
        status:"failed", phase:"Run failed", error:"Both public OSM network services are unavailable. Retry later."});
      else if (draft.status === "preparing" && b.preparationPolls > 1) Object.assign(draft, {status:"ready", draft_revision:2,
        phase:"Road network validated; review before running",
        scenario:{...draft.scenario,highway:result.scenario.highway,exit_direction:result.scenario.exit_direction},
        network_validation:{requested_radius_km:15,effective_radius_km:result.scenario.radius_km,
          edge_count:result.network.edge_count,gate_count:result.network.gates.length,
          adjusted:true,gates:result.network.gates}});
      return draft;
    }],
    ["GET", "/runs", () => Object.values(b.runs)],
    ["GET", /^\/runs\/[^/]+$/, ({endpoint}) => b.runs[endpoint.split("/")[2]]],
    ["GET", /\/artifacts\/result$/, () => result],
    ["GET", /\/artifacts\/network$/, () => network],
  ];
}

/** Direct REST road preparation is independent of the Simulation agent's conversation. */
function createPreparation(b, result, body) {
  b.prepared++; b.preparationPolls = 0;
  const incident = b.incidents[body.incident_id];
  b.draft = {draft_id:id(),draft_revision:1,status:"preparing",preparation_id:id(),
    phase:"Queued for road-network validation",session_id:body.session_id,incident_id:body.incident_id,
    location_revision:1,scenario:{...result.scenario,lat:incident.location.lat,lon:incident.location.lon,
      radius_km:body.radius_km ?? result.scenario.radius_km,highway:"auto",exit_direction:"auto"},
    scenario_hash:"fixture",input_mode:"current",changes:{},parent_run_id:null};
  if (b.prepared === 1) b.failedDraftId = b.draft.draft_id;
  b.drafts[b.draft.draft_id] = b.draft;
  Object.assign(b.sessions[body.session_id],{latest_draft_id:b.draft.draft_id,selected_run_id:null});
  return b.draft;
}

/** Start (POST) or read (GET) an incident's investigation task; a read after the first completes it. */
function incidentTask(b, result, {body, endpoint, url}) {
  const incidentId = endpoint.split("/")[2], selectedRun = body?.run_id || url.searchParams.get("run_id");
  const key = `${incidentId}:${selectedRun || "initial"}`;
  // Saved calls as each supervisor makes them: the planning supervisor registers only the guidance specialist.
  const savedCalls = [...(selectedRun ? [] : [["detect","detection_agent_tool"]]),
    ["guidance","public_response_agent_tool"], ["final","PublicAnswerResponse"]];
  if (body && !b.incidentTasks[key]) {
    if (selectedRun) b.updatedPlans++; else b.incidentStarts++;
    b.incidentTasks[key] = {task_id:id(), incident_id:incidentId, location_revision:1,
      run_id:selectedRun || null, stage:selectedRun ? "simulation" : "initial", status:"running",
      input_bundle_id:selectedRun ? result.input_bundle_id : null,
      input_bundle_sha256:selectedRun ? result.input_bundle_sha256 : null,
      fire_observations:[],created_at:Date.now()/1000, activity:[{tool_call_id:savedCalls[0][0],name:savedCalls[0][1],
        status:"running",started_at:Date.now()/1000}], polls:0, snapshot:null};
  }
  const task = b.incidentTasks[key], now = Date.now()/1000;
  if (task && !body && ++task.polls > 1) Object.assign(task,{status:"completed", completed_at:now,
    // One call after another, as the sequential executor runs them, across the task's recorded time.
    activity:savedCalls.map(([tool_call_id, name], index) => ({tool_call_id, name, status:"result_received",
      started_at:task.created_at + index*(now-task.created_at)/savedCalls.length,
      ended_at:task.created_at + (index+1)*(now-task.created_at)/savedCalls.length})),
    snapshot:plannedSnapshot(result, selectedRun)});
  if (task?.snapshot && !task.fire_observations?.length) task.fire_observations = task.snapshot.content.mapData.locations
    .filter(item => item.fire?.sourceId).map(item => ({observation_id:id(),source_id:item.fire.sourceId,
      name:item.fire.name,latitude:item.latitude,longitude:item.longitude,source_timestamp:item.fire.lastUpdated}));
  return task && selectedRun && b.mismatchedPlan ? {...task,input_bundle_sha256:"different-inputs"} : task || null;
}

/** The Simulation agent's turn route: questions, crew reviews, prepare, submit and analyze. */
function agentRoutes(b, {result, crewQuestion}) {
  return [["POST", "/agent/turns", async ({body, route}) => {
    b.agentTurns++;
    const fields = Object.keys(body).sort().join(",");
    if (fields !== TURN_FIELDS) b.errors.push(`An agent turn sent unexpected fields: ${fields}`);
    const output = { schema_version: 1, session_id: body.session_id, incident_id: body.incident_id,
      location_revision: 1, turn_id: id(), question: body.question, status: "answer",
      answer: "Browser verification fixture: residents and crew outcomes use the retained recording.",
      evidence: [], ui_actions: [], capability_version: "1.0.0", warnings: [] };
    // The broker forwards only the Simulation agent's tool names and call IDs as CUSTOM notices.
    const notices = [];
    if (body.question === crewQuestion) {
      b.reviewTurns++;
      const sent = JSON.stringify(body);
      if (body.intent !== "question" || body.policy_id !== "coordinated"
          || body.run_id !== b.sessions[body.session_id].selected_run_id) b.errors.push(`Review turn selection: ${sent}`);
      if (/Illustrative|gal|gpm|Engine|Tanker|Brush|Rescue|Command/.test(sent)) b.errors.push(`Review turn carries demo text: ${sent}`);
      const cited = b.reviewCitesEveryPolicy ? result.policies.map(p => p.policy) : [body.policy_id];
      output.answer = b.reviewCitesEveryPolicy ? REVIEW_EVERY_POLICY : REVIEW_ANSWER;
      output.evidence = [{ evidence_id: id(), view: "crew", availability: "available",
        artifact_refs: [{ run_id: body.run_id, sha256: "fixture" }], comparisons: [], missing_reasons: [],
        pagination: { has_more: false }, facts: cited.map(policy => ({ fact_id: id(), run_id: body.run_id,
          metric: "crews_staged", value: result.policies.find(p => p.policy === policy).summary.crews_staged,
          unit: null, availability: "available", policy_id: policy })) }];
      notices.push(...["TOOL_CALL_START", "TOOL_CALL_END", "TOOL_CALL_RESULT"].map(event => ({ type: "CUSTOM",
        name: "simulation_activity", value: { event, tool_name: "get_simulation_evidence", tool_call_id: "review-1" } })));
    } else if (body.intent === "prepare") {
      const hold = b.heldPreparation;
      if (hold) { hold.reached = true; b.changed(); await hold.released; }
      Object.assign(output, { draft: createPreparation(b,result,body), status: "needs_input" });
    } else if (body.intent === "submit") {
      assert.equal(b.draft.status, "ready");
      b.submitted++;
      b.run = { run_id: id(), session_id: body.session_id, incident_id: body.incident_id,
        location_revision: 1, status: "completed", progress: 1, phase: "Verification fixture complete",
        scenario: result.scenario, created_at: new Date().toISOString(), draft_id: body.draft_id, parent_run_id: null };
      b.runs[b.run.run_id] = b.run; b.sessions[body.session_id].selected_run_id = b.run.run_id;
      Object.assign(output, { run: b.run, status: "submitted" });
    } else if (body.intent === "analyze") {
      b.analyzed++;
      output.evidence = [{ evidence_id: id(), view: "comparison", facts: [], comparisons: [],
        artifact_refs: [{ run_id: body.run_id }], pagination: { has_more: false } }];
      // A replay receipt can legitimately include these actions. Reusing the
      // saved analysis on reload must not apply them over a user's selection.
      output.ui_actions = [{ type: "seek_time", time_s: 0 }, { type: "select_policy", policy_id: "baseline" }];
    }
    else output.ui_actions = [{ type: "seek_time", time_s: 600 }, { type: "select_policy", policy_id: "coordinated" }];
    const existing = body.intent === "analyze" ? b.outputs.find(o => o.analysis_run === body.run_id) : null;
    if (!existing) b.outputs.push({ ...output, ...(body.intent === "analyze" ? { analysis_run: body.run_id } : {}) });
    const frames = [{ type: "RUN_STARTED" }, ...notices, { type: "STATE_SNAPSHOT",
      snapshot: { type: "structuredResponse", content: { response: output.answer, simulationData: existing || output } } },
    { type: "RUN_FINISHED" }];
    await route.fulfill({ status: 200, contentType: "text/event-stream", body: sse(frames) });
    return HANDLED;
  }]];
}

/** Create the mocked broker for one fixture. Its fields are the counters and state the checks read. */
function createBroker({result, network, capabilities}) {
  // "Review response findings" asks the crew_details suggestion; every turn sends exactly the TurnRequest fields.
  const crewQuestion = capabilities.operations.find(op => op.id === "crew_details").suggested_question;
  const waiters = new Set();
  const b = {...fixtureData(result), crewQuestion,
    submitted: 0, prepared: 0, analyzed: 0, investigations: 0, radioRequests: 0, bootstraps: 0,
    incidentStarts: 0, updatedPlans: 0, agentTurns: 0, sessionUpdates: 0, reviewTurns: 0, preparationPolls: 0,
    firetruckState: "running", firetruckStarts: 0, firetruckStops: 0, firetruckStatusReads: 0, reportRequests: 0,
    // Closes of the exercise stream by the page; a frame sent after a close is not delivered.
    truckStreamCloses: 0, truckSocketOpen: false, truckSocket: null, delayedLayerLoads: 0, layerLoadsInFlight: 0,
    mismatchedPlan: false, reviewCitesEveryPolicy: false,
    fireRiskJobReads:0,completeFireRiskJob:false,roadDiscoveryJob:null,completeRoadDiscovery:false,
    session: null, incident: null, draft: null, run: null, failedDraftId: null, heldTurn: null, heldPreparation: null,
    sessions:{}, runs:{}, incidents:{}, incidentTasks:{}, drafts:{}, outputs:[], sessionPosts:[], incidentPosts:[],
    sessionReceipts:{},incidentReceipts:{},preparationReceipts:{},preparationPosts:[],investigationReceipts:{},
    // Every request and exercise stream the page opened, in order, as {method, endpoint, body}.
    requests: [], errors: [],
    // E-101 streams beside the fixture incident, so its marker is on the exercise map. truckChanges holds later telemetry.
    truckChanges: {},
  };
  b.truckEvidence = () => ({session_id: b.firetruckId, status: b.firetruckState,
    phase: "INITIAL_ATTACK", incident_elapsed_seconds: 300, trucks: [{
      truck_id: "E-101", unit_type: "ENGINE", latitude: b.fixtureLocation.lat + .004, longitude: b.fixtureLocation.lon + .004,
      heading: 90, speed_mph: 5, activity_status: "SUPPRESSION", water_tank_level_pct: 75,
      water_tank_level_gal: 750, pump_status: "ACTIVE", flow_rate_gpm: 100,
      crew_count: 4, hours_on_duty: 1, in_danger_zone: false, entrapment_risk: "LOW",
      distance_to_fire_ft: 200, estimated_water_remaining_min: 7.5, escape_route_clear: true, ...b.truckChanges,
    }]});
  /** Settle every waiter whose condition now holds; called after each change of broker state. */
  b.changed = () => {
    for (const waiter of [...waiters]) if (waiter.condition()) {
      waiters.delete(waiter); clearTimeout(waiter.timer); waiter.resolve();
    }
  };
  /** Resolve once `condition()` holds for the broker's state, or fail with `message` after `ms`. */
  b.until = (condition, message, ms = CONDITION_TIMEOUT_MS) => condition() ? Promise.resolve() : new Promise((resolve, reject) => {
    const waiter = {condition, resolve};
    waiter.timer = setTimeout(() => { waiters.delete(waiter); reject(Error(message)); }, ms);
    waiters.add(waiter);
  });
  /** Resolve once the page's debounced session write has saved the current run with `policy_id` at `replay_time_s`. */
  b.selectionSaved = (policy_id, replay_time_s) => b.until(() => {
    const saved = b.sessions[b.run.session_id];
    return saved.selected_run_id === b.run.run_id && saved.policy_id === policy_id && saved.replay_time_s === replay_time_s;
  }, `The session write never saved ${policy_id} at ${replay_time_s} s`);
  /** Hold the next REST preparation until release(), so the page can act while its start is busy. */
  b.holdPreparation = () => {
    let release;
    const released = new Promise(resolve => { release = resolve; });
    b.heldPreparation = {reached: false, released, release: () => { b.heldPreparation = null; release(); }};
    return b.heldPreparation;
  };
  b.workCounters = () => ({submitted: b.submitted, prepared: b.prepared, analyzed: b.analyzed,
    investigations: b.investigations, incidentStarts: b.incidentStarts, updatedPlans: b.updatedPlans,
    firetruckStarts: b.firetruckStarts, firetruckStops: b.firetruckStops, reportRequests: b.reportRequests,
    agentTurns: b.agentTurns});
  b.routes = [...firetruckRoutes(b), ...workspaceRoutes(b, {result, network, capabilities}),
    ...sessionRoutes(b, {result, network}), ...agentRoutes(b, {result, crewQuestion})];
  return b;
}

/** Serve the broker's routes and the exercise stream to `page`, and delay ArcGIS layer-view modules. */
async function installBroker(page, b, fabricUrl) {
  // Exercise unmount while ArcGIS's layer-view modules are still loading.
  await page.route(/\/(?:assets|node_modules\/\.vite\/deps)\/(?:GraphicsLayerView2D|OpenStreetMapLayerView2D)-[^/]+\.js(?:\?.*)?$/, async route => {
    b.delayedLayerLoads++; b.layerLoadsInFlight++;
    await new Promise(resolve => setTimeout(resolve, 1500));
    await route.continue();
    b.layerLoadsInFlight--; b.changed();
  });
  await page.routeWebSocket("**", ws => {
    const address = new URL(ws.url());
    if (address.host === new URL(fabricUrl).host && address.pathname === "/" && address.searchParams.has("token")) {
      ws.close(); return; // Vite development HMR; absent from the published build.
    }
    if (ws.url().includes(`/simulation-api/v1/firetruck/runs/${b.firetruckId}/stream`)) {
      b.truckSocket = ws;
      b.truckSocketOpen = true;
      b.requests.push({method: "WEBSOCKET", endpoint: address.pathname.replace("/simulation-api/v1", ""), body: null});
      ws.onClose(() => {
        b.truckStreamCloses++;
        if (b.truckSocket === ws) b.truckSocketOpen = false;
        b.changed();
      });
      ws.send(JSON.stringify({event_type: "CONNECTED", session_id: b.firetruckId}));
      ws.send(JSON.stringify({...b.truckEvidence(), event_type: "TRUCK_UPDATE"}));
      b.changed();
    } else { b.errors.push("Unexpected direct WebSocket: "+ws.url()); ws.close(); }
  });
  await page.route("**/simulation-api/**", async route => {
    const request = route.request(), method = request.method();
    const url = new URL(request.url());
    const endpoint = url.pathname.replace("/simulation-api/v1", "");
    const body = request.postDataJSON();
    b.requests.push({method, endpoint, body});
    const entry = b.routes.find(([verb, pattern]) => verb === method &&
      (typeof pattern === "string" ? pattern === endpoint : pattern.test(endpoint)));
    if (!entry) {
      await route.fulfill({ status: 404, json: { error: { message: `Unexpected mock path ${method} ${endpoint}` } } });
      return;
    }
    const response = await entry[2]({body, url, endpoint, route});
    if (response !== HANDLED) await route.fulfill({ json: response });
    b.changed();
  });
}

module.exports = {createBroker, installBroker, REVIEW_ANSWER, REVIEW_EVERY_POLICY, VOICE_FIXTURE_URL};
