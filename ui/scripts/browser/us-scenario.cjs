/* Native explicit location handoff with mocked server receipts. No provider/model/SUMO work is created. */
const assert = require("node:assert/strict");
const {expectNoRequest} = require("./helpers.cjs");
const N = require("./ui-names.cjs");

/** A rejected location preserves the selected recording; a fire action sends only its immutable assessment ID. */
async function explicitLocationHandoff(j) {
  const {page,b,guided,stage,strip,currentStage,STAGE_1,STAGE_2} = j;
  await stage(STAGE_2).click();
  await guided.waitFor();
  await page.getByRole("button",{name:"Scenario",exact:true}).click();
  await page.getByRole("tab",{name:N.RADIO_TAB,exact:true}).click();
  const selectedSession = await page.evaluate(() => sessionStorage.getItem("fireguard_simulation_session"));
  const previousStrip = await strip.innerText(), selectedRun = b.sessions[selectedSession].selected_run_id;
  const before = b.workCounters(), incidentsBefore = b.incidentPosts.length;
  await page.getByText(N.ADDRESS_ENTRY,{exact:true}).click();
  await page.getByLabel(N.ADDRESS_INPUT,{exact:true}).fill("123 Example Road, Seattle, WA");
  b.rejectNextLocation = "The location provider is temporarily unavailable. Try again later.";
  await page.getByRole("button",{name:"Resolve & prepare",exact:true}).click();
  await guided.getByRole("alert").filter({hasText:"location provider is temporarily unavailable"}).waitFor();
  assert.equal(b.incidentPosts.length,incidentsBefore+1);
  assert.deepEqual(Object.keys(b.incidentPosts.at(-1)).sort(),["reported_address","request_id","session_id"]);
  assert.equal(await strip.innerText(),previousStrip,"Failed resolution keeps the previously selected scenario");
  assert.equal(await page.evaluate(() => sessionStorage.getItem("fireguard_simulation_session")),selectedSession);
  assert.equal(b.sessions[selectedSession].selected_run_id,selectedRun);
  assert.deepEqual(b.workCounters(),before,"Rejected resolution creates no preparation or investigation");
  await page.screenshot({path:j.shot("rejected-us-location-preserves-recording.png"),fullPage:true});

  await stage(STAGE_1).click();
  await page.getByLabel(N.CHAT_INPUT,{exact:true}).fill("Show the retained fire for explicit scenario review.");
  await page.getByRole("button",{name:N.SEND_MESSAGE,exact:true}).click();
  await page.getByText("Later answer with a nearby fixture fire.",{exact:false}).waitFor();
  assert.equal(b.investigations,8);
  const summary = page.getByRole("complementary",{name:"Fire exposure evidence",exact:true});
  await summary.getByRole("button",{name:/^Fire exposure evidence/}).click();
  const action = summary.getByRole("button",{name:"Plan evacuation for this fire",exact:true});
  assert.equal(await action.count(),1,"Two assessment modes give one reported-fire action");
  const beforeHandoff = b.workCounters(), postsBefore = b.incidentPosts.length;
  await action.click();
  await currentStage(STAGE_2);
  await page.getByRole("heading",{name:N.REVIEW_DRAFT,exact:true}).waitFor();
  const body = b.incidentPosts.at(-1);
  assert.equal(b.incidentPosts.length,postsBefore+1);
  assert.deepEqual(Object.keys(body).sort(),["fire_assessment_id","request_id","session_id"]);
  assert.equal(body.fire_assessment_id,"0f684c63-58a4-4e3f-9667-b4e12d7a563a");
  assert.equal(b.incident.location.origin,"reported_fire");
  assert.equal(b.incident.location.source_assessment_id,body.fire_assessment_id);
  await guided.locator(".resolved-location").getByText("Later fixture fire",{exact:true}).waitFor();
  assert.equal(b.submitted,beforeHandoff.submitted,"An explicit handoff prepares a draft; running still requires review");
  assert.equal(b.prepared,beforeHandoff.prepared+1);
  assert.equal(b.incidentStarts,beforeHandoff.incidentStarts+1);
  await expectNoRequest(page,b,500,r => r.endpoint.startsWith("/fire-exposure"),
    "The scenario handoff does not rerun the model or fetch source data from the browser");
  await page.screenshot({path:j.shot("reported-fire-assessment-handoff-review.png"),fullPage:true});

  // Road retry uses the already accepted incident. It never re-resolves its fire name or creates a new session.
  b.incident.location.source_assessment_id = null;
  b.draft.status = "failed";
  b.failedDraftId = b.draft.draft_id;
  await page.reload();
  await page.getByRole("heading",{name:"Preparation failed",exact:true}).waitFor();
  const requestsBeforeRetry=b.requests.length, sessionsBefore=b.sessionPosts.length, incidentsBeforeRetry=b.incidentPosts.length;
  await page.getByRole("button",{name:"Retry road preparation",exact:true}).click();
  await page.getByRole("heading",{name:N.REVIEW_DRAFT,exact:true}).waitFor();
  assert.equal(b.sessionPosts.length,sessionsBefore); assert.equal(b.incidentPosts.length,incidentsBeforeRetry);
  const posted=b.requests.slice(requestsBeforeRetry).filter(request => request.method === "POST");
  assert.deepEqual(posted.map(request => request.endpoint),["/scenario-drafts"]);
  assert.equal(posted[0].body.incident_id,b.incident.incident_id);
  assert.equal("radius_km" in posted[0].body,false);

  // Coverage absence is a completed search result; identical automatic discovery is not offered again.
  b.draft.status="unavailable";
  b.draft.road_coverage={reason_code:"NO_DRIVABLE_ROADS",message:"No usable roads in the checked automatic extents.",
    extent_mode:"auto",requested_radius_km:null,searched_radius_km:15,study_bounds:[],attempts:[{radius_km:15}]};
  await page.reload();
  await page.getByRole("heading",{name:"Road coverage unavailable",exact:true}).waitFor();
  assert.equal(await page.getByRole("button",{name:"Retry road preparation",exact:true}).count(),0);
  assert.equal(await page.getByRole("button",{name:"Run this draft",exact:true}).count(),0);
  await page.getByRole("button",{name:"Conditions",exact:true}).click();
  const cards=page.getByRole("group",{name:"Observed locations",exact:true});
  await cards.getByRole("button",{name:/Unknown-size fixture fire/}).click();
  const originalTitle=await guided.locator(".evac-map-toolbar > div").first().innerText();
  const beforeNearbyPosts=b.incidentPosts.length, beforeNearbySessions=b.sessionPosts.length;
  assert.equal(await guided.locator(".evac-map-toolbar > div").first().innerText(),originalTitle,
    "Preview selection leaves the active scenario unchanged");
  const beforeNearbyTurn=b.agentTurns;
  b.rejectNextInitialInvestigation={code:"INVESTIGATION_IN_PROGRESS",message:"Earlier initial investigation is running."};
  b.rejectNextPreparation={code:"SIMULATION_CAPACITY",message:"Road preparation capacity is busy."};
  await page.getByRole("button",{name:"Start workflow for Unknown-size fixture fire",exact:true}).click();
  await page.getByRole("heading",{name:"Road preparation awaiting retry",exact:true}).waitFor();
  assert.equal(b.incidentPosts.length,beforeNearbyPosts+1);
  assert.equal(b.sessionPosts.length,beforeNearbySessions+1);
  assert.deepEqual(Object.keys(b.incidentPosts.at(-1)).sort(),["fire_observation_id","request_id","session_id"]);
  assert.equal(b.incident.location.origin,"reported_fire");
  const acceptedIncident=b.incident.incident_id, acceptedSession=b.incident.session_id;
  assert.equal(b.agentTurns,beforeNearbyTurn,"Road preparation never requires a Simulation agent turn");
  await page.getByRole("button",{name:"Resume conditions",exact:true}).waitFor();
  assert.match(await guided.innerText(),/earlier initial investigation is still running/i);
  await page.reload();
  await page.getByRole("heading",{name:"Road preparation awaiting retry",exact:true}).waitFor();
  const resume=page.getByRole("button",{name:"Resume road preparation",exact:true});
  await page.waitForFunction(() => [...document.querySelectorAll("button")]
    .some(button => button.textContent === "Resume road preparation" && !button.disabled));
  await resume.click();
  await page.getByRole("heading",{name:N.REVIEW_DRAFT,exact:true}).waitFor();
  assert.equal(b.incident.incident_id,acceptedIncident); assert.equal(b.incident.session_id,acceptedSession);
  assert.equal(b.incidentPosts.length,beforeNearbyPosts+1); assert.equal(b.sessionPosts.length,beforeNearbySessions+1);
  assert.equal(b.preparationPosts.at(-1).request_id,b.preparationPosts.at(-2).request_id,
    "Capacity retry reuses the saved request intent");
  await page.getByRole("button",{name:"Investigate conditions & plan",exact:true}).click();
  await page.getByRole("button",{name:"Conditions",exact:true}).click();
  await cards.getByRole("button",{name:/Nearby fixture fire/}).first().waitFor();
  assert.equal(await cards.getByRole("button",{name:/Unknown-size fixture fire/}).count(),1,
    "Reload and a new investigation do not multiply the same sourced fire");
  assert.equal(await cards.getByRole("button",{name:/Nearby fixture fire/}).count(),1);
  await page.setViewportSize({width:390,height:844});
  await cards.getByRole("button",{name:/Nearby fixture fire/}).click();
  const nearby=page.getByRole("button",{name:"Start workflow for Nearby fixture fire",exact:true});
  await nearby.focus(); await nearby.press("Enter");
  await page.getByRole("heading",{name:N.REVIEW_DRAFT,exact:true}).waitFor();
  assert.equal(b.incidentPosts.length,beforeNearbyPosts+2,"Keyboard start changes to the selected nearby fire once");
  await page.reload();
  await page.getByRole("heading",{name:N.REVIEW_DRAFT,exact:true}).waitFor();
  await page.getByRole("button",{name:"Conditions",exact:true}).click();
  assert.equal(await cards.getByRole("button",{name:/Unknown-size fixture fire/}).count(),1);
  await page.setViewportSize({width:1512,height:1100});
  await page.screenshot({path:j.shot("nearby-fire-workflow-capacity-and-reload.png"),fullPage:true});
}

module.exports = {explicitLocationHandoff};
