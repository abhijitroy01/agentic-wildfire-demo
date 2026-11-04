/* 2 · Evacuation & Crew Access: preparation, the guided replay, response plans, refresh recovery and new starts. */
const assert = require("node:assert/strict");
const {activityRows, checkContrast, enterMapFullscreen, exitMapFullscreen, expectNoRequest} = require("./helpers.cjs");
const N = require("./ui-names.cjs");

/** Wait for a prepared draft's review, check it does not submit by itself, then run it. */
async function reviewAndRun(j) {
  const {page, b} = j;
  const previous = b.submitted;
  await page.getByRole("heading", {name:N.PREPARING,exact:true}).waitFor();
  assert.equal(await page.getByRole("button", {name:N.RUN_DRAFT,exact:true}).isDisabled(), true);
  await page.getByRole("heading", {name:N.REVIEW_DRAFT,exact:true}).waitFor();
  assert.equal(b.submitted, previous, "Preparing a smaller area must not submit automatically");
  await page.getByText(N.SMALLER_AREA, {exact:true}).waitFor();
  await page.getByRole("button", {name:N.RUN_DRAFT,exact:true}).click();
}

/** The demo session starts with no sign-in; a failed preparation survives reload and a new one needs review. */
async function failedPreparation(j) {
  const {page, b, stage, STAGE_2} = j;
  await stage(STAGE_2).click();
  await page.getByRole("button", { name: "Research scenarios", exact: true }).waitFor();
  // This button renders only after initialize() resolved and the demo session started; no sign-in prompt exists.
  await page.getByRole("button", { name: "Prepare with current data" }).waitFor();
  assert.equal(await page.getByRole("button", { name: /sign in/i }).count(), 0, "The public app has no sign-in");
  await page.getByRole("button", { name: "Prepare with current data" }).click();
  await page.getByRole("heading", {name:"Preparation failed",exact:true}).waitFor();
  assert.equal(b.submitted, 0);
  await page.reload();
  await page.getByRole("heading", {name:"Preparation failed",exact:true}).waitFor();
  await page.getByText("This saved preparation attempt failed. Reloading keeps its result; it does not retry the road download.", {exact:true}).waitFor();
  assert.equal(b.prepared, 1, "Reload must retain the failed attempt without retrying automatically");
  assert.equal(await page.getByRole("button", {name:N.RUN_DRAFT,exact:true}).count(), 0);
  await page.screenshot({path: j.shot("saved-preparation-failure.png"),fullPage:true});
  const failedPreparation = b.draft;
  await page.getByRole("tab", {name:N.RADIO_TAB,exact:true}).click();
  await page.getByRole("button", {name:"Retry road preparation",exact:true}).click();
  await reviewAndRun(j);
  assert.equal(await page.getByRole("tab", {name:"Reference places",exact:true}).getAttribute("aria-selected"), "true");
  assert.notEqual(b.draft.draft_id, b.failedDraftId);
  assert.equal(failedPreparation.status, "failed", "Starting again must preserve the failed record");
}

/** The guided replay: map expansion keeps the job; clock, policy, crews, bottlenecks and a typed question. */
async function guidedReplay(j) {
  const {page, b, guided, strip} = j;
  await guided.getByText(N.GUIDED_HEADLINE, { exact: true }).waitFor();
  await page.getByRole("button", {name:"Ask agent",exact:true}).click();
  await page.getByRole("button", { name: "Ask simulation agent" }).waitFor();
  await page.locator('.simulation-map[data-ready="true"]').waitFor();
  await page.waitForFunction(busy => !document.querySelector("#simulation-question")?.closest("form").querySelector("button").disabled || !document.body.textContent.includes(busy), N.SIMULATOR_BUSY);
  assert.equal(b.submitted, 1); assert.equal(b.prepared, 2);
  await page.getByRole("button", { name: N.ENLARGE_MAP, exact: true }).click();
  await page.getByRole("button", { name: N.RESTORE_MAP, exact: true }).click();
  assert.equal(b.submitted, 1, "Expansion must not submit another job");
  await page.getByRole("slider").fill("600");
  await page.getByRole("button", { name: N.COORDINATED, exact: true }).click();
  await page.getByRole("button", { name: N.DATA_EVIDENCE, exact: true }).click();
  await page.getByRole("heading", { name: N.ASSUMPTIONS, exact: true }).waitFor();
  await page.getByRole("button", { name: N.FIRE_CREWS, exact: true }).click();
  await page.getByRole("heading", { name: "Fire crews · movement and access" }).waitFor();
  assert.equal(await page.getByLabel("Evacuation and crew outcomes", {exact:true}).count(),0);
  await page.getByLabel("Crew policy comparison", {exact:true}).waitFor();
  assert.equal(await page.getByText("Residents leaving the area", {exact:true}).count(),0);
  await page.getByLabel(N.CREW_TRAILS, {exact:true}).uncheck();
  await page.getByLabel(N.CREW_TRAILS, {exact:true}).check();
  await page.getByLabel(N.CREW_ROUTES, {exact:true}).waitFor();
  await page.getByLabel(N.PLAYBACK_SPEED, {exact:true}).selectOption("60");
  await page.screenshot({path: j.shot("workspace-crews.png"),fullPage:true});
  const crewTimeline = page.locator("details").filter({has:page.locator("summary", {hasText:"Full-run crew timeline"})});
  assert.equal(await crewTimeline.getAttribute("open"),null);
  await crewTimeline.locator("summary").press("Enter");
  assert.ok(await page.getByRole("button", { name: "crew-2", exact: true }).isVisible());
  await page.getByRole("button", { name: "Overview", exact: true }).click();
  await guided.getByLabel("Simulation findings", {exact:true}).waitFor();
  await page.getByRole("button", { name: "Bottlenecks", exact: true }).click();
  await page.getByRole("heading", {name:"Congestion tradeoffs across policies"}).waitFor();
  assert.equal(await guided.getByLabel("Simulation findings", {exact:true}).count(),0);
  assert.equal(await page.getByLabel("Evacuation and crew outcomes", {exact:true}).count(),0);
  await page.getByRole("button", { name: "Overview", exact: true }).click();
  await page.getByLabel("Question", { exact: true }).fill("Show coordinated at ten minutes.");
  await page.getByRole("button", { name: "Ask simulation agent" }).click();
  await page.getByText("Show coordinated at ten minutes.", { exact: true }).waitFor();
  await b.selectionSaved("coordinated", 600);
  // Stage 2's own progress list keeps a distinct name, and the display-only strip shows the selection.
  await guided.getByRole("list", {name: "Scenario progress", exact: true}).waitFor();
  assert.equal(await page.locator('[aria-label="Incident workflow"]').count(), 1);
  j.scenarioLine = await strip.innerText();
  for (const part of [b.run.run_id.slice(0, 8), N.COORDINATED, b.run.status, b.fixtureLocation.lat.toFixed(5),
    b.fixtureLocation.lon.toFixed(5)]) assert.ok(j.scenarioLine.includes(part), `Selected scenario lacks ${part}: ${j.scenarioLine}`);
  await checkContrast(strip, "p");
}

/** Response plans: recorded policy deltas, folded sources and congestion navigation; a mismatched receipt fails closed. */
async function responsePlans(j) {
  const {page, b, result, guided} = j;
  await page.getByRole("button", { name: "Guided scenario", exact: true }).click();
  j.analysesBeforeReload = b.analyzed;
  await page.getByRole("button", {name:"Response plans",exact:true}).click();
  const assessment = page.locator(".updated-assessment");
  await assessment.getByLabel("Simulation findings", {exact:true}).waitFor();
  const coordinated = assessment.locator('[data-finding-policy="coordinated"]');
  const baselineResult = result.policies.find(p=>p.policy==="baseline");
  const coordinatedResult = result.policies.find(p=>p.policy==="coordinated");
  const exits = `${(coordinatedResult.summary.exited_vehicles*result.scenario.people_per_vehicle).toLocaleString("en-US")} vs ${(baselineResult.summary.exited_vehicles*result.scenario.people_per_vehicle).toLocaleString("en-US")}`;
  assert((await coordinated.innerText()).includes(exits));
  const initialAssessment = page.locator(".initial-assessment");
  assert.equal(await initialAssessment.getAttribute("open"),null);
  await initialAssessment.locator(":scope > summary").press("Enter");
  await page.getByText("Initial fixture plan: sourced fire and weather observations; official guidance.", {exact:true}).waitFor();
  const sources = initialAssessment.locator(".planning-sources");
  assert.equal(await sources.getAttribute("open"),null);
  await sources.locator(":scope > summary").press("Enter");
  const article = sources.locator("details.citation-item");
  assert.equal(await article.getAttribute("open"),null);
  await article.locator(":scope > summary").press("Enter");
  assert.equal((await article.locator(".citation-excerpt").innerText()).match(/Fixture guidance for UI verification\./g).length,12);
  assert.match(await article.innerText(),/Collection reviewed: 2026-09-28/);
  await checkContrast(initialAssessment, ".planning-assessment-text, .citation-document a, .citation-content > div");
  await page.screenshot({path: j.shot("planning-sources.png"),fullPage:true});
  await initialAssessment.locator(":scope > summary").click();
  await assessment.locator("summary", {hasText:"Full saved assessment and guidance"}).press("Enter");
  await page.getByText("Updated fixture plan: review resident delays and crews that did not arrive.", {exact:true}).waitFor();
  assert.equal(await assessment.locator(".planning-sources").getAttribute("open"),null);
  await checkContrast(assessment, ".planning-assessment-text, .mission-findings dt, .mission-findings dd, .mission-findings .mission-change, .mission-findings b");
  await assessment.locator("summary", {hasText:"Full saved assessment and guidance"}).click();
  await page.screenshot({path: j.shot("planning-findings.png"),fullPage:true});
  await coordinated.getByRole("button", {name:"Inspect congestion",exact:true}).click();
  assert.equal(await page.getByRole("button",{name:"Bottlenecks",exact:true}).getAttribute("aria-pressed"),"true");
  assert.equal(await page.getByRole("button",{name:N.COORDINATED,exact:true}).getAttribute("aria-pressed"),"true");
  assert.equal(await page.getByRole("slider").inputValue(),String(coordinatedResult.series.reduce((a,b)=>b.queue>a.queue?b:a).t));
  await page.getByRole("slider").fill("600");
  b.mismatchedPlan = true;
  await page.reload();
  await page.getByRole("button", {name:"Response plans",exact:true}).click();
  await page.getByText(/Matching run and frozen-input evidence is unavailable/).waitFor();
  assert.equal(await guided.getByLabel("Simulation findings", {exact:true}).count(),0,"A mismatched plan receipt must not show policy highlights");
  b.mismatchedPlan = false;
  await page.reload();
  await page.getByRole("button", {name:"Response plans",exact:true}).click();
  await guided.getByLabel("Simulation findings", {exact:true}).waitFor();
}

/** Saved tasks in the incident's Agent activity tab: the completion call becomes the synthesis row. */
async function savedActivity(j) {
  const {page, received} = j;
  await page.getByRole("button", {name:"Agent activity",exact:true}).click();
  await page.getByRole("heading", {name:"Fire, weather & guidance agents",exact:true}).waitFor();
  await page.getByText("Guidance agent: finding published guidance", {exact:true}).first().waitFor();
  // Saved tasks: the completion call becomes the synthesis row, so only the specialist rows read Response received.
  const savedList = title => page.locator(`.agent-activity[aria-label="${title}"]`);
  await savedList("Simulation-informed planning agent")
    .getByText("Supervisor: combined results from 1 specialist into the answer", {exact:true}).waitFor();
  assert.deepEqual(await activityRows(savedList("Fire, weather & guidance agents")), [received,
    ["delegation", "result_received", "Detection agent: checking requested observations", "Response received"],
    ["delegation", "result_received", "Guidance agent: finding published guidance", "Response received"],
    ["synthesis", "done", "Supervisor: combined results from 2 specialists into the answer", "Completed"]]);
  assert.deepEqual(await activityRows(savedList("Simulation-informed planning agent")), [received,
    ["delegation", "result_received", "Guidance agent: finding published guidance", "Response received"],
    ["synthesis", "done", "Supervisor: combined results from 1 specialist into the answer", "Completed"]]);
  assert.equal(await page.getByText("Supervisor: received the request", {exact:true}).count(), 2);
  assert.equal(await page.getByText("PublicAnswerResponse", {exact:true}).count(), 0);
  assert.equal(await page.getByText("Result unverified", {exact:true}).count(), 0);
  assert.equal(await page.getByText("Response received", {exact:true}).count(), 3);
  assert.equal(await page.locator(".agent-activity").getByText("Completed", {exact:true}).count(), 2);
  await page.locator(".incident-investigation").screenshot({path: j.shot("agent-activity-saved.png")});
}

/** A reload recovers the run, selection and saved analysis, and restarts no investigation or plan. */
async function refreshRecovery(j) {
  const {page, b, guided} = j;
  const initialStartsBeforeReload = b.incidentStarts, plansBeforeReload = b.updatedPlans;
  await page.reload();
  await guided.getByText(N.GUIDED_HEADLINE, { exact: true }).waitFor();
  await page.waitForFunction(line => document.querySelector('[aria-label="Selected scenario"]')?.innerText === line,
    j.scenarioLine);
  assert.equal(b.submitted, 1, "Refresh must recover the existing run");
  assert.equal(await page.getByRole("slider").inputValue(), "600");
  assert.equal(await page.getByRole("button", { name: N.COORDINATED, exact: true }).getAttribute("aria-pressed"), "true");
  assert.equal(b.analyzed, j.analysesBeforeReload, "Refresh must reuse saved analysis without changing selection");
  assert.equal(b.incidentStarts, initialStartsBeforeReload, "Refresh must not restart incident investigation");
  assert.equal(b.updatedPlans, plansBeforeReload, "Refresh must not rebuild response plans");
}

/** Conditions keep each place's weather, unknown fire measurements stay unavailable, the forecast regroups by UTC
 * day; the evacuation map enters full screen and keeps the replay. */
async function conditions(j) {
  const {page} = j;
  await page.getByRole("button", {name:"Conditions",exact:true}).click();
  await page.getByRole("heading", {name:"Nearby fixture fire",exact:true}).waitFor();
  const details = page.getByRole("article", {name:"Selected location details"});
  assert.match(await details.innerText(), /72 °F/);
  await page.getByRole("button", {name:/^Unknown-size fixture fire/}).click();
  assert.match(await details.innerText(), /Area unavailable.*Containment unavailable/);
  assert.match(await details.innerText(), /48 °F/);
  assert.doesNotMatch(await details.innerText(), /72 °F|0 acres/);
  await page.getByRole("button", {name:/^Scenario point weather/}).click();
  assert.match(await details.innerText(), /80 °F/);
  const forecast = page.locator(".incident-forecast");
  assert.match(await forecast.innerText(), /Verified scenario forecast/);
  const forecastDays = forecast.locator(".accordion");
  assert.equal(await forecastDays.count(),1,"UTC fallback does not apply California's date offset");
  assert.match(await forecastDays.nth(0).locator(".accordion-title-text").innerText(), /Tuesday.*2026-10-06/);
  assert.deepEqual(await forecastDays.nth(0).locator(".period-time-compact").allTextContents(),
    ["12:00 AM","6:00 AM","12:00 PM"]);
  await checkContrast(forecast, ".accordion-title-text, .period-time-compact, .metric-compact dt, .metric-compact dd");
  await page.getByRole("button", {name:"Show nearby fires & weather",exact:true}).click();
  await page.getByRole("button", {name:"Reported fire: Nearby fixture fire",exact:true}).click();
  await page.getByText("Containment:", {exact:true}).waitFor();
  await page.getByRole("heading", {name:"Nearby fixture fire",exact:true}).first().waitFor();
  assert.equal(await page.getByRole("button", {name:/^Nearby fixture fire/}).getAttribute("aria-pressed"), "true");
  const toggle = await enterMapFullscreen(page, page.getByRole("button", {name:N.ENLARGE_MAP,exact:true}), j.guided, j.t);
  assert.equal(await page.locator(".map-undocked").count(),1);
  await page.screenshot({path: j.shot("evacuation-fullscreen.png")});
  await exitMapFullscreen(page, toggle, j.guided);
  assert.equal(await page.locator(".map-undocked").count(),0);
  assert.equal(await page.getByRole("slider").inputValue(), "600");
  assert.equal(await page.getByRole("button", {name:N.COORDINATED,exact:true}).getAttribute("aria-pressed"), "true");
  await page.waitForFunction(name => document.activeElement?.textContent === name, N.ENLARGE_MAP);
  await page.screenshot({ path: j.shot("workspace-desktop.png"), fullPage: true });
}

/** A Palisades 2025 preset selection submits one fresh run. */
async function presetRun(j) {
  const {page, b, guided, stage, STAGE_2} = j;
  await stage(STAGE_2).click();
  await guided.getByText(N.GUIDED_HEADLINE, { exact: true }).waitFor();
  assert.equal(await page.getByRole("button", {name:N.COORDINATED,exact:true}).getAttribute("aria-pressed"), "true");
  await page.getByRole("button", {name:"Scenario",exact:true}).click();
  await page.getByLabel("Scenario reference").selectOption("palisades-2025");
  await page.getByRole("button", { name: "Prepare with current data" }).click();
  await reviewAndRun(j);
  await page.waitForFunction(() => document.body.textContent.includes("Verification fixture complete"));
  await guided.getByText(N.GUIDED_HEADLINE, { exact: true }).waitFor();
  assert.equal(b.submitted, 2, "A new preset selection must submit one fresh run");
  assert.equal(b.incident.preset_id, "palisades-2025");
}

/** Radio playback goes through the broker and repeated detection starts one run; the layout keeps the audio element. */
async function radioRun(j) {
  const {page, b} = j;
  await page.getByRole("button", { name: "Prepare with current data" }).waitFor({ state: "visible" });
  await page.waitForFunction(() => ![...document.querySelectorAll("button")].find(b => b.textContent === "Prepare with current data").disabled);
  await page.route("https://simulation-audio.invalid/fixture.wav", route =>
    route.fulfill({ status: 200, contentType: "audio/wav", body: Buffer.alloc(44) }));
  await page.getByRole("tab", { name: N.RADIO_TAB, exact: true }).click();
  // The typed-address entry, queried as check_prepared_simulation_browser.cjs queries it after deployment. Resolving
  // is not clicked here: that would start another run, which this journey's run counts do not expect.
  const addressEntry = page.getByText(N.ADDRESS_ENTRY, {exact: true});
  await addressEntry.click();
  const address = page.getByLabel(N.ADDRESS_INPUT, {exact: true});
  const resolve = page.getByRole("button", {name: N.RESOLVE_AND_PREPARE, exact: true});
  await address.fill("525 Market St, San Francisco, CA");
  assert.equal(await resolve.isEnabled(), true, `A typed address enables "${N.RESOLVE_AND_PREPARE}"`);
  await address.fill("");
  assert.equal(await resolve.isDisabled(), true, `An empty address leaves "${N.RESOLVE_AND_PREPARE}" disabled`);
  await addressEntry.click();
  await address.waitFor({state: "hidden"});
  await page.getByRole("button", { name: "Play radio & investigate" }).click();
  await reviewAndRun(j);
  await b.until(() => b.radioRequests > 0, "The radio stream request never reached the broker");
  assert.equal(b.radioRequests, 1, "Radio must use the broker without a browser provider key");
  await page.getByLabel("Prerecorded emergency radio").waitFor();
  await page.getByText(b.detection.data.transcript_excerpt, { exact: true }).waitFor();
  await page.waitForFunction(() => ![...document.querySelectorAll("button")].find(b => b.textContent === "Play radio & investigate").disabled);
  assert.equal(b.submitted, 3, "Repeated radio detection must start exactly one run");
  assert.equal(b.incident.preset_id, null);
  assert.equal(await page.getByLabel("Prerecorded emergency radio").getAttribute("src"), "https://simulation-audio.invalid/fixture.wav");
  const audioNode = await page.getByLabel("Prerecorded emergency radio").elementHandle();
  await page.getByRole("button", {name:"Conditions",exact:true}).click();
  await page.getByRole("heading", {name:"Locations & conditions",exact:true}).waitFor();
  assert.equal(await page.getByLabel("Prerecorded emergency radio").evaluate((node, original) => node === original, audioNode), true);
  for (const size of [{width:1366,height:900}, {width:1024,height:768}, {width:820,height:1180}, {width:683,height:450}]) {
    await page.setViewportSize(size);
    const overflow = await page.evaluate(testId => {
      const root = document.querySelector(`[data-testid="${testId}"]`);
      const pane = root.querySelector('.evac-investigation'), player = root.querySelector('audio');
      const nav = document.querySelector('nav[aria-label="Incident workflow"]');
      const p = pane.getBoundingClientRect(), a = player.getBoundingClientRect();
      return {page:document.documentElement.scrollWidth > innerWidth,
        player:a.left < p.left || a.right > p.right + 1, pane:pane.scrollWidth > pane.clientWidth + 1,
        nav:nav.scrollWidth > nav.clientWidth + 1 || nav.getBoundingClientRect().right > innerWidth + 1};
    }, N.GUIDED_TEST_ID);
    assert.deepEqual(overflow, {page:false,player:false,pane:false,nav:false}, JSON.stringify({size,overflow}));
    await page.screenshot({path: j.shot(`unified-${size.width}.png`),fullPage:true});
  }
  await page.setViewportSize({width:1512,height:1100});
}

/** UI-04: stage 1's selected answer starts stage 2 at its reported place, after any busy
 * start; walkthrough stages 2, 3 and 4 follow that one run under Coordinated. */
async function searchAreaStart(j) {
  const {page, b, guided, stage, currentStage, searchArea, findings, STAGE_1, STAGE_2} = j;
  // Stage 1 keeps no answers across the reloads above, so ask again; a completed answer is selected.
  await stage(STAGE_1).click();
  await page.getByLabel(N.CHAT_INPUT, {exact: true}).fill("Where should evacuation planning focus?");
  await page.getByRole("button", {name: N.SEND_MESSAGE, exact: true}).click();
  await page.getByText(b.investigationEvidence.response, {exact: false}).waitFor();
  assert.equal(b.investigations, 5);
  await searchArea.getByText("Search area: Investigation fixture point", {exact: true}).waitFor();
  // A click while another start is busy waits for it, then starts once: hold the REST preparation acknowledgement.
  await stage(STAGE_2).click();
  await page.getByRole("button", {name: "Scenario", exact: true}).click();
  await page.getByRole("tab", {name: "Reference places", exact: true}).click();
  const preparation = b.holdPreparation();
  await page.getByRole("button", {name: "Prepare with current data", exact: true}).click();
  await b.until(() => preparation.reached, "The reference-place start never sent its REST preparation request");
  // The busy status, which check_prepared_simulation_browser.cjs waits out after deployment, shows while the start runs.
  await page.getByRole("button", {name: "Ask agent", exact: true}).click();
  await page.getByRole("status").filter({hasText: N.SIMULATOR_BUSY}).waitFor();
  await page.getByRole("button", {name: "Scenario", exact: true}).click();
  const postsBefore = [b.sessionPosts.length, b.incidentPosts.length];
  await stage(STAGE_1).click();
  await searchArea.getByRole("button", {name: "Simulate this search area", exact: true}).click();
  await currentStage(STAGE_2);
  await expectNoRequest(page, b, 500, r => r.method === "POST" && ["/sessions", "/incidents"].includes(r.endpoint),
    "While the reference-place start is busy, the search area must not start a session or incident");
  assert.deepEqual([b.sessionPosts.length, b.incidentPosts.length], postsBefore, "The search area must wait for the busy start");
  preparation.release();
  await b.until(() => b.sessionPosts.length > postsBefore[0], "A search-area click during another start was dropped");
  await guided.getByRole("heading", {name: "Verified search area", exact: true}).waitFor();
  // The search area replaces the earlier radio report, so the context panel shows one address; the recording stays.
  const scenarioView = guided.locator("#incident-context-scenario");
  for (const earlier of ["Detected radio report", "Reported address:"]) {
    assert.equal(await scenarioView.getByText(earlier, {exact: true}).count(), 0, `The earlier radio report shows: ${earlier}`);
  }
  assert.ok(!(await scenarioView.innerText()).includes(b.detection.data.location), "Only the search area's address shows");
  assert.equal(await page.getByLabel("Prerecorded emergency radio").count(), 1, "The radio recording stays available");
  assert.deepEqual([b.sessionPosts.length, b.incidentPosts.length], [postsBefore[0] + 1, postsBefore[1] + 1]);
  assert.deepEqual(Object.keys(b.sessionPosts.at(-1)).sort(), ["entry_mode", "request_id"]);
  assert.equal(b.sessionPosts.at(-1).entry_mode, "radio");
  // Only the place goes to the server, which geocodes it: no coordinates and no preset.
  assert.deepEqual(Object.keys(b.incidentPosts.at(-1)).sort(), ["reported_place", "request_id", "session_id"]);
  assert.equal(b.incidentPosts.at(-1).reported_place, "Investigation fixture point");
  await guided.getByText("Search area from 1 · Investigate: Investigation fixture point. No specific fire was selected.",
    {exact: true}).waitFor();
  assert.equal(b.incident.location.origin,"geocoded_place","The search-area receipt retains its place origin");
  assert.equal(await guided.getByText(/Extracted address/).count(), 0, "Each start clears the activity log");
  await reviewAndRun(j);
  await page.waitForFunction(id => document.querySelector('[aria-label="Selected scenario"]')?.innerText
    .includes(`Run ${id}`), b.run.run_id.slice(0, 8));
  await checkContrast(guided.locator(".resolved-location"), "h3, b, p, small");
  // Walkthrough stages 2, 3 and 4 follow this one search-area run under Coordinated.
  await page.getByRole("button", {name: N.COORDINATED, exact: true}).click();
  await j.walkthrough("2-search-area", page);
  await j.replayPolicy(N.COORDINATED);
  await j.checkReplayAt(j.stopFrame.t + 60);
  // A fire popup opened in stage 2 stays open on the shared map; close it so the capture shows the crews.
  for (const close of await guided.locator(".maplibregl-popup-close-button").all()) await close.click();
  await j.walkthrough("3-response-replay", page);
  await findings.getByRole("button", {name: "Review response findings", exact: true}).click();
  await findings.getByText(j.REVIEW_ANSWER, {exact: true}).waitFor();
  await findings.locator("summary", {hasText: "Evidence: crew"}).click();
  await findings.getByText(`${N.COORDINATED} · crews staged: ${j.replayed.summary.crews_staged}`, {exact: false}).waitFor();
  await j.walkthrough("4-review-findings", findings);
}

module.exports = {failedPreparation, guidedReplay, responsePlans, savedActivity, refreshRecovery, conditions,
  presetRun, radioRun, searchAreaStart};
