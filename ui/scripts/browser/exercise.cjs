/* The standalone firetruck exercise in 3 · Response Tracker: the map under telemetry, the popup, the legends and the
 * stop, return and report lifecycle. */
const assert = require("node:assert/strict");
const {checkContrast, enterMapFullscreen, exitMapFullscreen, expectNoRequest, mapOverlayLayout, mapsRedrawn} =
  require("./helpers.cjs");
const N = require("./ui-names.cjs");

/** The exercise map's locators and measurements, created once per journey. */
function exerciseMap(j) {
  if (j.exerciseMap) return j.exerciseMap;
  const {page} = j;
  const responseMap = page.locator(".response-workspace .map-container");
  const markers = responseMap.locator(".truck-marker");
  const markerPositions = () => markers.evaluateAll(nodes => nodes.map(node => [node.style.left, node.style.top]));
  /** Marker positions once no user navigation is in progress and the markers have not moved for 1 s, longer than a
   * re-fit's 300 ms debounce, so a debounced fit or a zoom animation has finished. */
  async function settledMarkers() {
    await page.evaluate(() => { delete window.__markersSettling; });
    await page.waitForFunction(() => {
      const map = document.querySelector(".response-workspace .map-container"), now = performance.now();
      const key = JSON.stringify([...map.querySelectorAll(".truck-marker")].map(node => [node.style.left, node.style.top]));
      const last = window.__markersSettling;
      if (!last || last.key !== key || map.querySelector(".esri-view-surface")?.dataset.navigating === "true") {
        window.__markersSettling = {key, since: now};
        return false;
      }
      return now - last.since >= 1000;
    }, null, {polling: 100}).catch(error => { throw Error(`Unit markers did not settle: ${error.message}`); });
    return markerPositions();
  }
  /** A point on the map clear of markers, the popup, legends and map controls, for wheel and drag gestures. */
  const clearMapPoint = map => map.evaluate(node => {
    const box = node.getBoundingClientRect(), covered = ".truck-marker, .truck-popup, .map-legend, .fire-exposure-summary, " +
      ".response-units-legend, .map-undock-control, .basemap-switcher-topleft, .esri-ui-corner > *";
    for (const [fx, fy] of [[.15, .5], [.25, .4], [.35, .6], [.5, .3], [.65, .7]]) {
      const x = box.left + box.width * fx, y = box.top + box.height * fy, hit = document.elementFromPoint(x, y);
      if (hit && node.contains(hit) && !hit.closest(covered)) return {x, y};
    }
    return null;
  });
  // The units legend starts as one compact "Fire response units" toggle; its content shows only when expanded.
  const unitsLegend = responseMap.locator(".response-units-legend");
  const unitsToggle = unitsLegend.getByRole("button", {name: "Fire response units", exact: true});
  /** The toggle reads `expanded`, names the content it controls, and the unit types show only when expanded. */
  async function checkUnitsLegend(expanded) {
    assert.equal(await unitsToggle.getAttribute("aria-expanded"), String(expanded),
      `The units legend toggle must read ${expanded ? "expanded" : "collapsed"}`);
    const controls = await unitsToggle.getAttribute("aria-controls");
    assert.ok(controls, "The units legend toggle names the content it controls");
    assert.equal(await page.locator(`[id="${controls}"]`).getByText("Engine", {exact: true}).isVisible(), expanded,
      `Unit types must ${expanded ? "show when expanded" : "stay hidden while collapsed"}`);
    assert.equal(await unitsLegend.getByRole("heading", {name: "Unit Types", exact: true}).isVisible(), expanded);
  }
  j.exerciseMap = {responseMap, markers, settledMarkers, clearMapPoint, unitsLegend, unitsToggle, checkUnitsLegend};
  return j.exerciseMap;
}

/** A later stage-1 answer with a fire beside the incident offers its own place but does not rebind the simulation. */
async function laterAnswer(j) {
  const {page, b, stage, strip, searchArea, STAGE_1} = j;
  j.searchIncident = b.incident.incident_id;
  const searchStrip = await strip.innerText();
  const postsAfterSearch = [b.sessionPosts.length, b.incidentPosts.length];
  // A later answer with saved fire exposure beside the incident gives the shared map its own evidence.
  await stage(STAGE_1).click();
  await page.getByLabel(N.CHAT_INPUT,{exact:true}).fill("Check nearby fires again.");
  await page.getByRole("button",{name:N.SEND_MESSAGE,exact:true}).click();
  await page.getByText("Later answer with a nearby fixture fire.",{exact:false}).waitFor();
  assert.equal(b.investigations,6);
  // The later answer offers its own place but does not rebind the reviewed simulation.
  await searchArea.getByText("Search area: Later search place", {exact: true}).waitFor();
  assert.deepEqual([b.sessionPosts.length, b.incidentPosts.length], postsAfterSearch);
  assert.equal(b.incident.incident_id, j.searchIncident);
  assert.equal(await strip.innerText(), searchStrip);
  const map = page.locator(".investigation-workspace .map-container");
  const summary = map.getByRole("complementary", {name:"Fire exposure evidence",exact:true});
  await summary.getByRole("button", {name:/^Fire exposure evidence/}).click();
  await summary.getByText("DTN wind 8.9 mph from 270°", {exact:true}).waitFor();
  assert.equal(await summary.locator(".fire-exposure-item > strong").count(), 1,
    "Baseline and timed output for one fire belong in one evidence group");
  const details=summary.locator(".fire-exposure-details");
  assert.equal(await details.getAttribute("open"),null,"Model details start collapsed");
  await details.locator("summary").click();
  await summary.getByText("Synthetic retained UI contract fixture.", {exact:true}).first().waitFor();
  await checkContrast(summary, ".accordion-title-text, .fire-exposure-item > strong, p, dt, dd, label");
  const slider = summary.getByRole("slider", {name:"Scenario time for Later fixture fire",exact:true});
  const requestsBefore = b.requests.length;
  await slider.fill("0");
  await summary.getByText("Scenario time: 30 minutes", {exact:false}).waitFor();
  assert.equal(await slider.getAttribute("aria-valuetext"), "30 minutes after assumed initialization");
  const scenario = summary.getByRole("checkbox", {name:"Show surface-fire demo scenario",exact:true});
  await scenario.uncheck();
  assert.equal(await slider.isDisabled(), true);
  await scenario.check();
  assert.equal(await slider.isDisabled(), false);
  assert.equal(await slider.inputValue(), "2");
  await expectNoRequest(page, b, 500, r => r.endpoint.startsWith("/fire-exposure") || r.method === "POST",
    "Changing returned scenario geometry must not create an assessment or call a provider");
  assert.equal(b.requests.slice(requestsBefore).some(r => r.endpoint.startsWith("/fire-exposure")), false,
    "Saved scenario controls must not fetch a new assessment");
  await details.locator("summary").click();
  await map.screenshot({path:j.shot("fire-exposure-evidence-and-scenario-1440.png")});
  await summary.getByRole("button", {name:/^Fire exposure evidence/}).click();
}

/** UI-01: the map fits the selected incident and, after the user zooms out, unchanged telemetry neither reloads the
 * map nor re-fits it. */
async function stableMap(j) {
  const {page, b, stage, STAGE_3} = j;
  const {responseMap, markers, settledMarkers, clearMapPoint} = exerciseMap(j);
  await stage(STAGE_3).click();
  await j.openExercise();
  await page.locator(".response-workspace .map-legend").waitFor();
  // Opening the exercise starts no run by itself. The window also lands the selection below inside the map's fit to the
  // later answer (300 ms debounce, then a 1 s animation, once the view exists), which the page does not expose as
  // state: the map must still move to the incident, or E-101 never enters the view.
  await expectNoRequest(page, b, 600, r => r.method === "POST" && r.endpoint === "/firetruck/runs",
    "Opening the exercise must not start a run before Dispatch");
  await page.getByLabel("Select fire incident").selectOption(b.incident.incident_id);
  await page.getByRole("button", {name: "Dispatch 4 trucks", exact: true}).click();
  await page.getByText("E-101", {exact: true}).waitFor();
  await markers.first().waitFor();
  const fitted = await settledMarkers();
  assert.equal(await responseMap.evaluate(map => {
    const view = map.querySelector(".esri-view-root").getBoundingClientRect();
    return [...map.querySelectorAll(".truck-marker")].every(node => {
      const box = node.getBoundingClientRect(), x = box.left + box.width / 2, y = box.top + box.height / 2;
      return x >= view.left && x <= view.right && y >= view.top && y <= view.bottom;
    });
  }), true, `The map must fit the selected incident, with every unit marker in view: ${JSON.stringify(fitted)}`);
  const zoomOut = responseMap.getByRole("button", {name: "Zoom out", exact: true});
  const zoomPath = await zoomOut.count() ? "button" : "wheel";
  if (zoomPath === "button") await zoomOut.click();
  else {
    const point = await clearMapPoint(responseMap);
    await page.mouse.move(point.x, point.y);
    await page.mouse.wheel(0, 400);
  }
  const zoomedOut = await settledMarkers();
  assert.notDeepEqual(zoomedOut, fitted, "Zooming out must move the unit marker, or the check below proves nothing");
  await responseMap.evaluate(map => {
    window.ui01MapReloads = 0;
    new MutationObserver(records => {
      for (const node of records.flatMap(record => [...record.addedNodes]))
        if (node instanceof Element && (node.matches(".map-loading-overlay") || node.querySelector(".map-loading-overlay")))
          window.ui01MapReloads++;
    }).observe(map, {childList: true, subtree: true});
  });
  for (let frame = 0; frame < 5; frame++) {
    b.truckSocket.send(JSON.stringify({...b.truckEvidence(), event_type: "TRUCK_UPDATE"}));
    // Each window outlasts a re-fit (300 ms debounce, then a 1 s animation), so a reload or re-fit would show below.
    await expectNoRequest(page, b, 2000, r => r.method === "WEBSOCKET" ||
      (r.endpoint.startsWith("/firetruck/runs") && r.method !== "GET"),
    "Unchanged telemetry must not start, stop or reconnect the exercise run");
  }
  const mapReloads = await page.evaluate(() => window.ui01MapReloads);
  const afterTelemetry = await settledMarkers();
  console.log(`UI-01 (zoom ${zoomPath}): ${mapReloads} map loading overlays; markers ${JSON.stringify(zoomedOut)} -> ${
    JSON.stringify(afterTelemetry)}`);
  assert.equal(mapReloads, 0, "Unchanged telemetry must not reload the map");
  assert.deepEqual(afterTelemetry, zoomedOut, "Unchanged telemetry must leave every unit marker in place");
}

/** UI-02: the selected unit's popup shows current telemetry and stays beside its marker after an update and a pan. */
async function popup(j) {
  const {page, b} = j;
  const {responseMap, markers, settledMarkers, clearMapPoint} = exerciseMap(j);
  const unitPopup = responseMap.locator(".truck-popup");
  /** The popup's left edge minus its marker's; the overlay places the popup 30 px right of the marker. */
  const popupOffset = async () => await unitPopup.evaluate(node => parseFloat(node.style.left)) -
    await markers.first().evaluate(node => parseFloat(node.style.left));
  await markers.first().click();
  await unitPopup.getByText("5 mph", {exact: true}).waitFor();
  Object.assign(b.truckChanges, {speed_mph: 9, latitude: b.fixtureLocation.lat + .005});
  b.truckSocket.send(JSON.stringify({...b.truckEvidence(), event_type: "TRUCK_UPDATE"}));
  await unitPopup.getByText("9 mph", {exact: true}).waitFor();
  const updated = await settledMarkers();
  assert.ok(Math.abs(await popupOffset() - 30) <= .5, "The popup must stay beside its marker after an update");
  const panFrom = await clearMapPoint(responseMap);
  assert.ok(panFrom, "The exercise map needs a point clear of markers, popup and legends to drag");
  // Drag toward the map center so the marker, and with it the popup, stays in view.
  const mapBox = await responseMap.boundingBox(), [markerLeft, markerTop] = updated[0].map(parseFloat);
  const panBy = [Math.sign(mapBox.width / 2 - markerLeft) * 120 || 120, Math.sign(mapBox.height / 2 - markerTop) * 60 || 60];
  await page.mouse.move(panFrom.x, panFrom.y);
  await page.mouse.down();
  await page.mouse.move(panFrom.x + panBy[0], panFrom.y + panBy[1], {steps: 12});
  await page.mouse.up();
  assert.notDeepEqual(await settledMarkers(), updated, "Dragging must pan the exercise map");
  assert.ok(Math.abs(await popupOffset() - 30) <= .5, "The popup must stay beside its marker after a pan");
  await responseMap.screenshot({path: j.shot("exercise-popup-after-pan.png")});
  // Position the real telemetry marker so its popup Close overlaps Undock. Panning can reach this same geometry,
  // but the exact gesture result varies with native map frames; this keeps the stacking regression deterministic.
  await mapsRedrawn(page);
  const overlapPoint = await responseMap.evaluate(map => {
    const view = window.__wfmBrowserNativeViews.flatMap(collection => collection.toArray())
      .find(view => view.container && map.contains(view.container) && !view.destroyed);
    const marker = map.querySelector(".truck-marker"), close = map.querySelector(".popup-close").getBoundingClientRect();
    const undock = map.querySelector(".map-undock-control").getBoundingClientRect();
    const point = view.toMap({
      x: parseFloat(marker.style.left) + undock.left + undock.width / 2 - close.left - close.width / 2,
      y: parseFloat(marker.style.top) + undock.top + undock.height / 2 - close.top - close.height / 2,
    });
    const bounds = view.container.getBoundingClientRect(), underControl = view.toMap({
      x:undock.left + undock.width / 2 - bounds.left, y:undock.top + undock.height / 2 - bounds.top,
    });
    return {popup:{latitude:point.latitude, longitude:point.longitude},
      marker:{latitude:underControl.latitude, longitude:underControl.longitude}};
  });
  const previousChanges = {...b.truckChanges};
  Object.assign(b.truckChanges, overlapPoint.popup);
  b.truckSocket.send(JSON.stringify({...b.truckEvidence(), event_type:"TRUCK_UPDATE"}));
  await settledMarkers();
  assert.ok(Math.abs(await popupOffset() - 30) <= .5, "The overlapping popup must still follow its telemetry marker");
  await responseMap.screenshot({path:j.shot("exercise-popup-over-undock.png")});
  // Finish the popup interaction before independent viewport/legend checks; a selected popup follows its marker.
  const closePopup = unitPopup.getByRole("button", {name: "✕", exact: true});
  assert.equal(await closePopup.evaluate(button => {
    const close = button.getBoundingClientRect(), undock = button.closest(".map-container")
      .querySelector(".map-undock-control").getBoundingClientRect();
    const x = close.left + close.width / 2, y = close.top + close.height / 2;
    return x > undock.left && x < undock.right && y > undock.top && y < undock.bottom;
  }), true, "The regression must place popup Close underneath the undock rectangle");
  assert.equal(await closePopup.evaluate(button => {
    const box = button.getBoundingClientRect(), hit = document.elementFromPoint(
      box.left + box.width / 2, box.top + box.height / 2);
    return hit === button || button.contains(hit);
  }), true, "The selected popup Close must receive a normal click even beside the map undock control");
  await closePopup.click();
  await unitPopup.waitFor({state: "hidden"});
  Object.assign(b.truckChanges, overlapPoint.marker);
  b.truckSocket.send(JSON.stringify({...b.truckEvidence(), event_type:"TRUCK_UPDATE"}));
  await settledMarkers();
  assert.equal(await markers.first().evaluate(marker => {
    const box = marker.getBoundingClientRect(), undock = marker.closest(".map-container")
      .querySelector(".map-undock-control").getBoundingClientRect();
    const x = box.left + box.width / 2, y = box.top + box.height / 2;
    return x > undock.left && x < undock.right && y > undock.top && y < undock.bottom;
  }), true, "The regression must also place the marker underneath the undock rectangle");
  assert.equal(await responseMap.locator(".map-undock-control").evaluate(button => {
    const box = button.getBoundingClientRect(), hit = document.elementFromPoint(
      box.left + box.width / 2, box.top + box.height / 2);
    return hit === button || button.contains(hit);
  }), true, "A truck marker underneath Undock must leave the map control accessible");
  b.truckChanges = previousChanges;
  b.truckSocket.send(JSON.stringify({...b.truckEvidence(), event_type:"TRUCK_UPDATE"}));
  await settledMarkers();
  await j.walkthrough("5-standalone-exercise", responseMap);
}

/** UI-02 overlays: all cards, native scale and undock control are disjoint at 1440, 820 and 390 px with the units and wind
 * legends collapsed and expanded; the units legend toggles by click and Enter with visible focus. */
async function legends(j) {
  const {page} = j;
  const {responseMap, unitsLegend, unitsToggle, checkUnitsLegend} = exerciseMap(j);
  /** Legends and undock control at 1440, 820 and 390 px, with screenshots named for the units legend's `state`. */
  async function checkLegendLayout(state) {
    for (const [width, height] of [[1440, 1000], [820, 1180], [390, 844]]) {
      await page.setViewportSize({width, height});
      await mapsRedrawn(page);
      const layout = await mapOverlayLayout(responseMap, [".response-units-legend", ".wind-legend"]);
      console.log(`UI-02 exercise legends, units legend ${state}, at ${width}px: ${JSON.stringify(layout)}`);
      assert.deepEqual(layout, {missing: [], overlaps: [], outside: []}, `Exercise overlays (${state}) at ${width}px`);
      await responseMap.screenshot({path: j.shot(`exercise-legends-${state}-${width}.png`)});
    }
    await page.setViewportSize({width: 1440, height: 1000});
    await mapsRedrawn(page);
  }
  await checkUnitsLegend(false);
  await checkContrast(unitsLegend, ".accordion-title-text");
  await checkLegendLayout("collapsed");
  await unitsToggle.click();
  await checkUnitsLegend(true);
  await checkContrast(unitsLegend, ".accordion-title-text, h5, .legend-item span");
  const windToggle = responseMap.locator(".wind-legend").getByRole("button", {name: /Wind speed/});
  await windToggle.click();
  assert.equal(await windToggle.getAttribute("aria-expanded"), "true");
  await checkLegendLayout("expanded");
  await windToggle.click();
  await unitsToggle.click();
  await checkUnitsLegend(false);
  // Keyboard: Enter expands it and collapses it again, with a visible focus ring.
  await unitsToggle.focus();
  await page.keyboard.press("Enter");
  await checkUnitsLegend(true);
  assert.equal(await unitsToggle.evaluate(node => node.matches(":focus-visible") &&
    getComputedStyle(node).outlineStyle !== "none" && parseFloat(getComputedStyle(node).outlineWidth) > 0), true,
  "The units legend toggle must show keyboard focus");
  await page.keyboard.press("Enter");
  await checkUnitsLegend(false);
}

/** Leaving stage 3 ends the exercise: one stop request, its stream closed, and no units on the Investigation map. */
async function leaving(j) {
  const {page, b, stage, STAGE_1} = j;
  assert.deepEqual([b.firetruckStops, b.truckStreamCloses], [0, 0]);
  await stage(STAGE_1).click();
  await b.until(() => b.firetruckStops > 0 && b.truckStreamCloses > 0,
    `Leaving stage 3 must stop the run and close its stream: ${b.firetruckStops} stops, ${b.truckStreamCloses} closes`);
  const investigationMap = page.locator(".investigation-workspace .map-container");
  await investigationMap.locator(".fire-exposure-summary").waitFor();
  // Past the stream's 3 s reconnect delay, so a reconnect or second stop would show.
  await expectNoRequest(page, b, 3500, r => r.method === "WEBSOCKET" ||
    (r.method === "DELETE" && r.endpoint.startsWith("/firetruck/runs/")),
  "Leaving stage 3 must neither reconnect the exercise stream nor send a second stop");
  assert.deepEqual([b.firetruckStops, b.truckStreamCloses, b.truckSocketOpen], [1, 1, false],
    "Leaving stage 3 sends exactly one stop request and closes the stream without reconnecting");
  assert.equal(await investigationMap.locator(".truck-marker").count(), 0, "No exercise units on the Investigation map");
  assert.equal(await investigationMap.locator(".response-units-legend").count(), 0,
    "No units legend on the Investigation map");
  await investigationMap.screenshot({path: j.shot("investigation-after-leaving-exercise-1440.png")});
}

/** Returning starts fresh: nothing resumes and no units show; the stopped run still gets a report. */
async function returning(j) {
  const {page, b, exercise, stage, STAGE_3} = j;
  const {responseMap, markers, unitsLegend} = exerciseMap(j);
  const statusReadsBeforeReturn = b.firetruckStatusReads;
  await page.setViewportSize({width: 1512, height: 1100});
  await stage(STAGE_3).click();
  await j.openExercise();
  await page.getByLabel("Select fire incident").waitFor();
  await responseMap.locator(".map-legend").waitFor();
  // The closed stream never brings the final snapshot, so the run's card must still come to read stopped.
  const runSummary = exercise.getByRole("region", {name: "Simulation report", exact: true}).locator("p").first();
  await runSummary.filter({hasText: / · stopped\b/}).waitFor().catch(async () =>
    assert.fail(`After returning, the run stopped by leaving must read stopped, not "${await runSummary.innerText()}"`));
  assert.equal(await exercise.getByRole("button", {name: "Stop simulation", exact: true}).count(), 0);
  assert.equal(await exercise.getByText("E-101", {exact: true}).count(), 0, "Returning shows no units of the stopped run");
  assert.equal(await markers.count(), 0, "No exercise units on the exercise map after returning");
  assert.equal(await unitsLegend.count(), 0, "No units legend on the exercise map without units");
  assert.deepEqual([b.firetruckStarts, b.firetruckStops], [1, 1], "Returning to the exercise starts and stops nothing");
  await page.getByLabel("Ask about this simulation").fill("Which suppression stages were reached?");
  await page.getByRole("button", {name: "Generate report", exact: true}).click();
  await page.getByRole("link", {name: "Open report", exact: true}).waitFor();
  assert.equal(b.reportRequests, 1);
  assert.equal(b.firetruckStatusReads - statusReadsBeforeReturn, 1, "Returning reads the stopped run's status once");
}

/** Only an explicit Dispatch starts the next run, whose legend starts collapsed and whose Stop is its own request; the
 * response map enters full screen; reload restores the saved report without regenerating it. */
async function nextRun(j) {
  const {page, b, currentStage, STAGE_3} = j;
  const {markers, checkUnitsLegend} = exerciseMap(j);
  await page.getByLabel("Select fire incident").selectOption(b.incident.incident_id);
  await page.getByRole("button", {name: "Dispatch 4 trucks", exact: true}).click();
  await markers.first().waitFor();
  assert.equal(b.firetruckStarts, 2);
  await checkUnitsLegend(false);
  const scope = page.locator(".response-workspace");
  const toggle = await enterMapFullscreen(page, page.getByRole("button", {name: "Enlarge response map", exact: true}),
    scope, j.t);
  await exitMapFullscreen(page, toggle, scope);
  await page.getByRole("button", {name: "Stop simulation", exact: true}).click();
  await page.getByRole("button", {name: "Stop simulation", exact: true}).waitFor({state: "hidden"});
  assert.equal(b.firetruckStops, 2, "The Stop button sends the next run's one stop request");
  await page.getByLabel("Ask about this simulation").fill("Which suppression stages were reached?");
  await page.getByRole("button", {name: "Generate report", exact: true}).click();
  await page.getByRole("link", {name: "Open report", exact: true}).waitFor();
  await page.screenshot({path: j.shot("firetruck-report.png"), fullPage: true});
  assert.equal(b.firetruckStarts, 2); assert.equal(b.reportRequests, 2);
  await page.reload();
  await currentStage(STAGE_3);
  await j.openExercise();
  await page.getByRole("link", {name: "Open report", exact: true}).waitFor();
  await page.getByText("E-101", {exact: true}).waitFor();
  assert.equal(b.firetruckStarts, 2, "Reload must restore saved firetruck evidence without restarting");
  assert.equal(b.reportRequests, 2, "Reload must restore the saved report without regenerating");
  await page.setViewportSize({ width: 820, height: 1180 });
  await page.screenshot({ path: j.shot("workspace-tablet.png"), fullPage: true });
  await page.setViewportSize({width: 1512, height: 1100});
}

module.exports = {laterAnswer, stableMap, popup, legends, leaving, returning, nextRun};
