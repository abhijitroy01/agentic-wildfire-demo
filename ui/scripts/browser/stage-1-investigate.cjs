/* 1 · Investigate: the opening page, the workflow navigation and the live and failed supervisor answers. */
const assert = require("node:assert/strict");
const {activityRows, checkContrast, enterMapFullscreen, exitMapFullscreen, nextFrames} = require("./helpers.cjs");
const {VOICE_FIXTURE_URL} = require("./mock-broker.cjs");
const N = require("./ui-names.cjs");

/** The page opens on stage 1 as a demonstration, with one workflow landmark; rapid stage changes survive pending
 * ArcGIS layer loads. */
async function opening(j) {
  const {page, b, stage, workflow, STAGE_1, STAGE_2, STAGE_3} = j;
  await page.goto(j.fabricUrl);
  await stage(STAGE_3).waitFor();
  // Demo framing: the page description and the header tagline.
  assert.equal(await page.locator('meta[name="description"]').getAttribute("content"), "Agentic wildfire planning demonstration");
  assert.equal(await page.locator(".tagline").textContent(), "Agentic wildfire planning demonstration");
  // One "Incident workflow" landmark: an ordered list of the three numbered steps, with stage 1 current.
  assert.equal(await page.locator('[aria-label="Incident workflow"]').count(), 1);
  assert.deepEqual(await workflow.locator(":scope > ol > li").allTextContents(), [STAGE_1, STAGE_2, STAGE_3]);
  assert.deepEqual(await workflow.locator('[aria-current="step"]').allTextContents(), [STAGE_1]);
  assert.equal(await page.getByRole("button", { name: "Traffic Lab", exact: true }).count(), 0);
  await page.getByText("OpenStreetMap · USGS terrain", {exact:true}).waitFor();
  await page.waitForFunction(() => document.querySelector(".esri-view-root"));
  for (let index = 0; index < 3; index++) {
    await stage(STAGE_3).click();
    await j.openExercise();
    await page.waitForFunction(() => document.querySelector(".response-workspace .esri-view-root"));
    await stage(STAGE_1).click();
    await page.waitForFunction(() => document.querySelector(".investigation-workspace .esri-view-root"));
  }
  // Let every delayed layer-view module arrive after its map unmounted, and run.
  await b.until(() => b.delayedLayerLoads > 0 && b.layerLoadsInFlight === 0,
    "The delayed ArcGIS layer-view modules never finished loading");
  await nextFrames(page);
  assert.ok(b.delayedLayerLoads > 0, "The pending ArcGIS module regression must execute");
}

/** UI-05: keyboard use, no overflow and contrast at desktop, tablet and phone widths; reduced motion. */
async function workflowKeyboard(j) {
  const {page, stage, workflow, currentStage, STAGE_1, STAGE_2} = j;
  for (const [width, height] of [[1440, 1000], [820, 1180], [390, 844]]) {
    await page.setViewportSize({width, height});
    assert.equal(await workflow.evaluate(nav => document.documentElement.scrollWidth > innerWidth ||
      nav.scrollWidth > nav.clientWidth + 1 || nav.getBoundingClientRect().right > innerWidth + 1), false,
    `The workflow navigation overflows at ${width}px`);
    await stage(STAGE_1).focus();
    await page.keyboard.press("Tab");
    assert.equal(await page.evaluate(() => document.activeElement?.textContent), STAGE_2, `Tab order at ${width}px`);
    await page.keyboard.press("Enter");
    await currentStage(STAGE_2);
    await page.keyboard.press("Shift+Tab");
    await page.keyboard.press("Enter");
    await currentStage(STAGE_1);
    await checkContrast(workflow, "button");
    await page.locator(".app-header").screenshot({path: j.shot(`workflow-navigation-${width}.png`)});
  }
  await page.setViewportSize({width: 1512, height: 1100});
  await page.waitForFunction(() => document.querySelector(".investigation-workspace .esri-view-root"));
  const investigationView = await page.locator(".investigation-workspace .esri-view-root").elementHandle();
  await stage(STAGE_1).click();
  assert.equal(await page.evaluate(view => view === document.querySelector(".investigation-workspace .esri-view-root"),
    investigationView), true, "Selecting the current step changes nothing");
  await page.emulateMedia({reducedMotion: "reduce"});
  assert.deepEqual(await page.evaluate(() => [...document.querySelectorAll(".App *")].filter(node => {
    const style = getComputedStyle(node);
    return [style.animationDuration, style.transitionDuration].some(value => value.split(",").some(part => parseFloat(part) > .001));
  }).map(node => node.getAttribute("class") || node.tagName)), [], "Reduced motion leaves no animation or transition");
  await page.emulateMedia({reducedMotion: null});
}

/** Agent activity at a desktop and a phone width, for the review record; neither may overflow the panel. */
async function captureActivity(j, state) {
  const {page, activity} = j;
  for (const [width, height] of [[1440, 1000], [390, 844]]) {
    await page.setViewportSize({width, height});
    assert.equal(await activity.evaluate(node => node.scrollWidth > node.clientWidth), false, `${state} at ${width}px`);
    // A status label stays on one line (its text has one line top); a long row title wraps instead.
    assert.deepEqual(await activity.evaluate(node => [...node.querySelectorAll("li > div > span")].filter(span => {
      const range = document.createRange();
      range.selectNodeContents(span);
      return new Set([...range.getClientRects()].map(rect => Math.round(rect.top))).size > 1;
    }).map(span => span.textContent)), [], `${state} status labels wrap at ${width}px`);
    await activity.screenshot({path: j.shot(`agent-activity-${state}-${width}.png`)});
  }
  await page.setViewportSize({width:1512, height:1100});
}

/** The first answer's supervisor rows mid-run and on completion. */
async function liveActivity(j) {
  const {page, b, activity, steps, received} = j;
  // The busy status and the answer article, queried as check_public_sop_browser.cjs queries them after deployment.
  const processing = page.getByRole("status", {name: N.PROCESSING, exact: true});
  const answers = page.getByRole("article", {name: N.AGENT_MESSAGE, exact: true});
  const answersBefore = await answers.count();
  // Guidance is selected only when requested (TASK_PROMPT), so this question asks for it alongside the weather.
  await page.getByLabel(N.CHAT_INPUT, { exact: true }).fill("Show current fire weather and official guidance.");
  await page.getByRole("button", { name: N.SEND_MESSAGE, exact: true }).click();
  // Mid-run: the supervisor has delegated twice and is waiting for the second specialist's result.
  await steps.getByText("Waiting for result", {exact:true}).waitFor();
  await processing.waitFor();
  assert.deepEqual(await activityRows(activity), [received,
    ["delegation", "done", "Detection agent: checking requested observations", "Response received"],
    ["delegation", "awaiting_result", "Guidance agent: finding published guidance", "Waiting for result"]]);
  assert.equal(await activity.getByRole("list", {name:"Supervisor delegation and synthesis", exact:true}).count(), 1);
  await checkContrast(activity, "li strong, li span, li small");
  await captureActivity(j, "running");
  b.heldTurn.release();
  await steps.getByText("Supervisor: combining results from 2 specialists", {exact:true}).waitFor();
  assert.deepEqual((await activityRows(activity))[3],
    ["synthesis", "running", "Supervisor: combining results from 2 specialists", "In progress"]);
  await checkContrast(activity, "li strong, li span, li small");
  const request = b.requests.find(call => call.method === "POST" && call.endpoint === "/investigation/turns").body;
  const endpoint = `/investigation/turns/${request.request_id}`;
  b.heldTurn.disconnect();
  await b.until(() => b.requests.some(call => call.method === "GET" && call.endpoint === endpoint),
    "An incomplete stream never read its retained owner receipt");
  assert.equal(b.investigationReceipts[request.request_id].receipt.status,"running",
    "A lost connection leaves accepted work running instead of inventing an answer");
  b.heldTurn.release();
  await page.getByText("Public investigation broker fixture.", { exact: false }).waitFor();
  await processing.waitFor({state: "hidden"});
  assert.equal(await answers.count(), answersBefore + 1, `One new "${N.AGENT_MESSAGE}" article holds the answer`);
  assert.match(await answers.last().innerText(), /Public investigation broker fixture\./);
  await steps.getByText("Completed", {exact:true}).waitFor();
  assert.deepEqual(await activityRows(activity), [received,
    ["delegation", "done", "Detection agent: checking requested observations", "Response received"],
    ["delegation", "done", "Guidance agent: finding published guidance", "Response received"],
    ["synthesis", "done", "Supervisor: combined results from 2 specialists into the answer", "Completed"]]);
  assert.equal(await page.getByText("tool-argument-marker", {exact:false}).count(), 0, "Tool arguments are never shown");
  await captureActivity(j, "completed");
  await j.walkthrough("1-investigate", page.locator(".investigation-assistant"));
  assert.equal(b.investigations, 1);
  const receipt = b.investigationReceipts[request.request_id].receipt;
  assert.equal(receipt.status,"completed");
  assert.equal(receipt.thread_id,request.thread_id);
  assert.deepEqual(receipt.snapshot,{type:"structuredResponse",content:b.investigationEvidence});
  assert.equal(b.requests.filter(call => call.method === "POST" && call.endpoint === "/investigation/turns").length,1,
    "Connection recovery must never invoke the question a second time");
  assert.equal(await page.evaluate(() => sessionStorage.getItem("fireguard_pending_investigation")),null);
}

/** Each answer's drawer keeps its own weather and sources, folded, and stays reachable in the viewport. */
async function answerDrawers(j) {
  const {page, activity, steps, received, answerDrawer} = j;
  await page.getByRole("button", {name:N.INSPECT_EVIDENCE,exact:true}).click();
  await answerDrawer.getByRole("heading", {name:"Weather observations",exact:true}).waitFor();
  await answerDrawer.getByText("72 °F",{exact:true}).waitFor();
  assert.equal(await answerDrawer.locator("details[open]").count(),0,"Raw records and source articles start folded");
  await answerDrawer.getByText("Raw provider observation",{exact:true}).click();
  await answerDrawer.getByText('"Clear fixture conditions"',{exact:false}).waitFor();
  await answerDrawer.getByText("1. Investigation source fixture",{exact:false}).click();
  await answerDrawer.getByText('"Retained source passage for the first answer."',{exact:true}).waitFor();
  await checkContrast(answerDrawer,".current-weather-section dd, .citation-excerpt, .citation-document a, .accordion-title-text");
  await page.keyboard.press("Escape");
  await page.getByLabel(N.CHAT_INPUT,{exact:true}).fill("Show another place.");
  await page.getByRole("button",{name:N.SEND_MESSAGE,exact:true}).click();
  await page.getByText("Second answer with separate evidence.",{exact:false}).waitFor();
  await steps.getByText("Supervisor: answered directly",{exact:true}).waitFor();
  assert.deepEqual(await activityRows(activity), [received,
    ["synthesis", "done", "Supervisor: answered directly", "Completed"]]);
  await page.getByRole("button",{name:N.INSPECT_EVIDENCE,exact:true}).first().click();
  await answerDrawer.getByText("72 °F",{exact:true}).waitFor();
  assert.equal(await answerDrawer.getByText("Second point",{exact:true}).count(),0,
    "The first answer's drawer must not acquire the second answer's location");
  await answerDrawer.evaluate(element=>Promise.allSettled(element.getAnimations().map(animation=>animation.finished)));
  const drawerPlacement = await answerDrawer.evaluate(element=>{
    const box=element.getBoundingClientRect();
    const close=element.querySelector('button[aria-label="Close panel"]');
    const button=close.getBoundingClientRect();
    return {visible:box.left>=0 && box.right<=innerWidth && box.top>=0 && box.bottom<=innerHeight,
      closeReachable:close.contains(document.elementFromPoint(button.x+button.width/2,button.y+button.height/2))};
  });
  assert.ok(drawerPlacement.visible && drawerPlacement.closeReachable,
    "The source drawer stays in the viewport above map loading and controls");
  await page.screenshot({path: j.shot("investigation-source-drawer.png"),fullPage:true});
  await page.keyboard.press("Escape");
}

/** A voice draft plays in the browser beside its review text; the failed guidance call reads Failed. */
async function voiceDraft(j) {
  const {page, b, activity, steps, received, answerDrawer} = j;
  // A small valid local audio fixture checks actual browser playback, without AWS media calls.
  const wave = Buffer.alloc(16044); wave.write("RIFF"); wave.writeUInt32LE(wave.length-8,4);
  wave.write("WAVEfmt ",8); wave.writeUInt32LE(16,16); wave.writeUInt16LE(1,20);
  wave.writeUInt16LE(1,22); wave.writeUInt32LE(8000,24); wave.writeUInt32LE(16000,28);
  wave.writeUInt16LE(2,32); wave.writeUInt16LE(16,34); wave.write("data",36); wave.writeUInt32LE(16000,40);
  await page.route(VOICE_FIXTURE_URL,route=>route.fulfill({contentType:"audio/wav",body:wave}));
  await page.getByLabel(N.CHAT_INPUT,{exact:true}).fill("Give official guidance and create a voice draft for this fire.");
  await page.getByRole("button",{name:N.SEND_MESSAGE,exact:true}).click();
  await answerDrawer.getByRole("heading",{name:"Voice drafts (1)",exact:true}).waitFor();
  await steps.getByText("Communication agent: preparing multilingual voice drafts",{exact:true}).waitFor();
  assert.deepEqual(await activityRows(activity), [received,
    ["delegation", "error", "Guidance agent: finding published guidance", "Failed"],
    ["delegation", "done", "Communication agent: preparing multilingual voice drafts", "Response received"],
    ["synthesis", "done", "Supervisor: combined results from 2 specialists into the answer", "Completed"]]);
  await answerDrawer.getByRole("button",{name:/English/}).click();
  const voice = answerDrawer.getByLabel("English draft recording",{exact:true});
  await voice.evaluate(async player=>{await player.play();});
  await page.waitForFunction(()=>document.querySelector('audio[aria-label="English draft recording"]')?.currentTime>0);
  await answerDrawer.getByText("Review message text",{exact:true}).click();
  await answerDrawer.getByText("Demonstration message for review.",{exact:true}).waitFor();
  await checkContrast(answerDrawer,".audio-language, .audio-voice, details p");
  await voice.evaluate(player=>player.pause());
  await page.keyboard.press("Escape");
  assert.equal(b.investigations,3);
}

/** A failed run: the open specialist call and the synthesis both read Failed, and nothing reads Completed. */
async function failedRun(j) {
  const {page, b, activity, steps, received} = j;
  const before = b.requests.length;
  await page.getByLabel(N.CHAT_INPUT,{exact:true}).fill("Check the fire weather again.");
  await page.getByRole("button",{name:N.SEND_MESSAGE,exact:true}).click();
  await page.getByText("Error: Investigation could not complete. Please retry.",{exact:true}).waitFor();
  await steps.getByText("Supervisor: answer not completed",{exact:true}).waitFor();
  assert.deepEqual(await activityRows(activity), [received,
    ["delegation", "error", "Detection agent: checking requested observations", "Failed"],
    ["synthesis", "error", "Supervisor: answer not completed", "Failed"]]);
  assert.equal(await activity.getByText("Completed",{exact:true}).count(), 0);
  await checkContrast(activity, "li strong, li span, li small");
  assert.equal(b.investigations,4);
  const calls = b.requests.slice(before).filter(request => request.endpoint.startsWith("/investigation/turns"));
  assert.equal(calls.filter(request => request.method === "POST").length,1,
    "The failed question submits exactly one request");
  assert.equal(calls.filter(request => request.method === "GET").length,0,
    "A definitive RUN_ERROR is shown directly without entering incomplete-stream recovery");
  const requestId = calls.find(request => request.method === "POST").body.request_id;
  assert.equal(b.investigationReceipts[requestId].receipt.status,"failed");
  assert.equal(await page.evaluate(() => sessionStorage.getItem("fireguard_pending_investigation")),null,
    "An authoritative failure clears only its completed intent");
}

/** The investigation map enters full screen and Escape restores it. */
async function investigationFullscreen(j) {
  const {page} = j, scope = page.locator(".investigation-workspace");
  const toggle = await enterMapFullscreen(page, page.getByRole("button", {name:"Enlarge investigation map",exact:true}),
    scope, j.t);
  await exitMapFullscreen(page, toggle, scope);
}

/** A place name that is not text offers no search area, and the page keeps running. */
async function nonTextPlaceName(j) {
  const {page, b, stage, STAGE_1} = j;
  await stage(STAGE_1).click();
  await page.getByLabel(N.CHAT_INPUT, {exact: true}).fill("Check this area once more.");
  await page.getByRole("button", {name: N.SEND_MESSAGE, exact: true}).click();
  await page.getByText("Answer with a place name that is not text.", {exact: false}).waitFor()
    .catch(error => { throw Error(`The answer did not render: ${b.errors.join("\n") || error.message}`); });
  assert.equal(b.investigations, 7);
  assert.deepEqual(b.errors, [], "An answer whose place name is not text must not break the page");
  assert.equal(await page.getByRole("region", {name: "Search area", exact: true}).count(), 0,
    "A place name that is not text offers no search area");
}

/** The journey ends with no exercise stop left over, at least one checked session update and no browser errors. */
async function noRuntimeErrors(j) {
  const {b} = j;
  assert.equal(b.firetruckStops, 2, "Leaving stage 3 with no active run sends no stop request");
  assert.ok(b.sessionUpdates > 0, "The session-update field check must have seen at least one update");
  assert.ok(!b.errors.length, b.errors.join("\n"));
}

module.exports = {opening, workflowKeyboard, liveActivity, answerDrawers, voiceDraft, failedRun,
  investigationFullscreen, nonTextPlaceName, noRuntimeErrors};
