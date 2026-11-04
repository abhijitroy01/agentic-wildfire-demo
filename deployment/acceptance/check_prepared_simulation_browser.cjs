/** Real public workflow: explicit road review -> SUMO -> original Lab replay and evidence. */
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const zlib = require("node:zlib");
const assert = require("node:assert/strict");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
// The UI names the release browser harness also asserts, so a rename fails the deploy gate first.
const N = require("../../ui/scripts/browser/ui-names.cjs");
const {acceptanceOptions,verifyIncidentPoint,verifyCompletedRun} = require("./prepared_simulation_checks.cjs");
const options = acceptanceOptions(process.env);
const url = process.env.WFM_PUBLIC_URL;
if (!url || new URL(url).protocol !== "https:") throw Error("Set WFM_PUBLIC_URL to the deployment to test.");
const out = path.resolve(process.env.WFM_BROWSER_OUTPUT || ".runtime/verification/prepared-simulation-browser");
fs.mkdirSync(out, {recursive:true});
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));

(async () => {
  const browser = await chromium.launch({channel:"chrome",headless:true,
    args:["--use-angle=swiftshader","--enable-unsafe-swiftshader"]});
  const context = await browser.newContext({viewport:{width:1512,height:1100}});
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  const api = async endpoint => {
    const response = await context.request.get(new URL("/simulation-api/v1"+endpoint, url).href);
    assert.ok(response.ok(), `GET ${endpoint}: ${response.status()}`);
    return response.json();
  };
  const waitFor = async (read, done, label, maxMs=20*60*1000) => {
    const deadline = Date.now()+maxMs;
    let previous;
    while (Date.now() < deadline) {
      const value = await read();
      const state = JSON.stringify([value.status,value.phase,value.error]);
      if (state !== previous) console.log(label, value.status, value.phase || "", value.error || "");
      previous = state;
      if (["failed","cancelled","unavailable"].includes(value.status)) {
        throw Error(`${label}: ${value.error || value.road_coverage?.message || value.status}`);
      }
      if (done(value)) return value;
      await pause(3000);
    }
    throw Error(`${label} exceeded the acceptance deadline`);
  };
  try {
    await page.goto(url,{waitUntil:"domcontentloaded"});
    await page.getByRole("button",{name:N.STAGE_2,exact:true}).click();
    if (options.location.kind === "preset") {
      await page.getByRole("tab",{name:"Reference places",exact:true}).click();
      await page.getByLabel("Scenario reference",{exact:true}).selectOption(options.location.preset);
      await page.getByRole("button",{name:"Prepare with current data",exact:true}).click();
    } else {
      await page.getByRole("tab",{name:N.RADIO_TAB,exact:true}).click();
      if (options.location.kind === "coordinates") {
        await page.getByText("Enter a scenario coordinate",{exact:true}).click();
        await page.getByLabel("Latitude",{exact:true}).fill(String(options.location.coordinates.lat));
        await page.getByLabel("Longitude",{exact:true}).fill(String(options.location.coordinates.lon));
        await page.getByRole("button",{name:"Use coordinate & prepare",exact:true}).click();
      } else {
        await page.getByText(N.ADDRESS_ENTRY,{exact:true}).click();
        await page.getByLabel(N.ADDRESS_INPUT,{exact:true}).fill(options.location.address);
        await page.getByRole("button",{name:N.RESOLVE_AND_PREPARE,exact:true}).click();
      }
    }
    const preparingHeading = page.getByRole("heading",{name:N.PREPARING,exact:true});
    const reviewHeading = page.getByRole("heading",{name:N.REVIEW_DRAFT,exact:true});
    await preparingHeading.or(reviewHeading).waitFor({timeout:240000});
    const observedPreparing = await preparingHeading.isVisible();
    if (observedPreparing) {
      assert.ok(await page.getByRole("button",{name:N.RUN_DRAFT,exact:true}).isDisabled());
    }
    const sessionId = await page.evaluate(() => sessionStorage.getItem("fireguard_simulation_session"));
    const preparedSession = await api(`/sessions/${sessionId}`);
    assert.ok(preparedSession.latest_draft_id,"The accepted session must retain its direct preparation receipt");
    let initial = await api(`/scenario-drafts/${preparedSession.latest_draft_id}`);
    assert.ok(initial.draft_id);
    const incident = await api(`/incidents/${initial.incident_id}`);
    fs.writeFileSync(path.join(out,"preparation-request.json"),
      JSON.stringify({session_id:sessionId,options,draft:initial,incident},null,2));
    verifyIncidentPoint(incident,initial.scenario,options);
    let draft = await waitFor(() => api(`/scenario-drafts/${initial.draft_id}`),
      value => value.status === "ready","Preparation");
    if (options.trafficMode === "provider_speed_limits") {
      // This optional case explicitly requests provider traffic through the existing Simulation agent UI.
      await page.getByRole("button",{name:"Ask agent",exact:true}).click();
      const send = page.getByRole("button",{name:"Ask simulation agent",exact:true});
      await page.waitForFunction(() => !document.querySelector("#simulation-question")
        ?.closest("form").querySelector("button").disabled,{},{timeout:240000});
      await page.getByLabel("Question",{exact:true}).fill("Prepare a new current-data scenario at this verified point " +
        "with traffic_mode=provider_speed_limits. Do not submit it.");
      await send.click();
      const previousDraft = initial.draft_id;
      initial = await waitFor(async () => {
        const turns = await api(`/sessions/${sessionId}/turns`);
        const next = [...turns].reverse().find(turn => turn.draft?.draft_id !== previousDraft && turn.draft)?.draft;
        return next || {status:"waiting"};
      },value => !!value.draft_id,"Explicit provider preparation",240000);
      draft = await waitFor(() => api(`/scenario-drafts/${initial.draft_id}`),
        value => value.status === "ready","Provider road preparation");
      await page.getByRole("button",{name:"Scenario",exact:true}).click();
    }
    fs.writeFileSync(path.join(out,"validated-draft.json"),JSON.stringify(draft,null,2));
    verifyIncidentPoint(incident,draft.scenario,options);
    assert.equal(draft.scenario.traffic_mode,options.trafficMode);
    assert.ok(draft.network_validation.edge_count <= 14000 && draft.network_validation.gate_count > 0);
    if (options.location.kind !== "preset") {
      assert.equal(draft.network_validation.extent_mode,"auto");
    }
    assert.ok(draft.scenario.radius_km <= 15);
    assert.equal((await api("/runs")).length,0,"Preparation must not start a policy run");
    const foreign = await browser.newContext();
    await foreign.request.post(new URL("/simulation-api/v1/demo/session",url).href,
      {headers:{Origin:new URL(url).origin},data:{}});
    const denied = await foreign.request.get(new URL(`/simulation-api/v1/scenario-drafts/${draft.draft_id}/network`,url).href);
    assert.equal(denied.status(),404,"Another visitor must not read preparation geometry");
    await foreign.close();
    await page.getByRole("heading",{name:N.REVIEW_DRAFT,exact:true}).waitFor({timeout:15000});
    if (draft.network_validation.extent_mode !== "auto" &&
        draft.network_validation.effective_radius_km < draft.network_validation.requested_radius_km) {
      await page.getByText(N.SMALLER_AREA,{exact:true}).waitFor();
    }
    await page.locator('.evac-columns .simulation-map[data-ready="true"]').waitFor({timeout:30000});
    await page.screenshot({path:path.join(out,"validated-area.png"),fullPage:true,animations:"disabled"});
    // Reload before submission to verify that the prepared receipt and geometry survive.
    await page.reload({waitUntil:"domcontentloaded"});
    await page.getByRole("heading",{name:N.REVIEW_DRAFT,exact:true}).waitFor({timeout:30000});
    await page.getByRole("button",{name:N.RUN_DRAFT,exact:true}).click();
    const selected = await waitFor(async () => {
      const current = await api(`/sessions/${sessionId}`);
      return {status:current.selected_run_id ? "selected" : "waiting",...current};
    },value => !!value.selected_run_id,"Submission",240000);
    fs.writeFileSync(path.join(out,"submitted-session.json"),JSON.stringify(selected,null,2));
    const run = await waitFor(() => api(`/runs/${selected.selected_run_id}`),
      value => value.status === "completed","Simulation");
    const result = await api(`/runs/${run.run_id}/artifacts/result`);
    const network = await api(`/runs/${run.run_id}/artifacts/network`);
    verifyCompletedRun(result,draft,incident,options);
    fs.writeFileSync(path.join(out,"result.json.gz"),zlib.gzipSync(JSON.stringify(result)));
    fs.writeFileSync(path.join(out,"network.json.gz"),zlib.gzipSync(JSON.stringify(network)));
    await page.getByText(N.GUIDED_HEADLINE,{exact:true}).waitFor({timeout:180000});
    await page.waitForFunction(busy => !document.body.textContent.includes(busy),N.SIMULATOR_BUSY,{timeout:240000});
    await page.getByRole("slider").fill("600");
    await page.getByRole("button",{name:N.COORDINATED,exact:true}).click();
    await page.getByRole("button",{name:N.FIRE_CREWS,exact:true}).click();
    await page.getByLabel(N.CREW_ROUTES,{exact:true}).waitFor();
    await page.getByLabel(N.CREW_TRAILS,{exact:true}).check();
    await page.getByLabel(N.PLAYBACK_SPEED,{exact:true}).selectOption("60");
    await page.getByRole("button",{name:N.ENLARGE_MAP,exact:true}).click();
    await page.screenshot({path:path.join(out,"crew-replay-enlarged.png"),fullPage:true,animations:"disabled"});
    await page.getByRole("button",{name:N.RESTORE_MAP,exact:true}).click();
    await waitFor(async () => ({status:"saving",...await api(`/sessions/${sessionId}`)}),
      current => current.policy_id === "coordinated" && current.replay_time_s === 600,"Saved selection",30000);
    await page.reload({waitUntil:"domcontentloaded"});
    await page.getByText(N.GUIDED_HEADLINE,{exact:true}).waitFor({timeout:30000});
    assert.equal(await page.getByRole("slider").inputValue(),"600");
    assert.equal(await page.getByRole("button",{name:N.COORDINATED,exact:true}).getAttribute("aria-pressed"),"true");
    assert.equal((await api("/runs")).length,1,"Reload must not submit another simulation");
    await page.getByRole("button",{name:N.DATA_EVIDENCE,exact:true}).click();
    await page.getByRole("heading",{name:N.ASSUMPTIONS,exact:true}).waitFor();
    await page.screenshot({path:path.join(out,"source-evidence.png"),fullPage:true,animations:"disabled"});
    assert.ok(await page.getByTestId(N.GUIDED_TEST_ID).evaluate(element => element.scrollWidth<=element.clientWidth+1),
      "Workspace must not overflow horizontally");
    assert.deepEqual(errors,[]);
    const receipt={status:"passed",url,options,session_id:sessionId,run_id:run.run_id,draft,
      observed_preparing_in_browser:observedPreparing,
      result_sha256:crypto.createHash("sha256").update(JSON.stringify(result)).digest("hex"),
      traffic:{mode:result.traffic.mode,provider:result.traffic.provider,status:result.traffic.status,matched_edges:result.traffic.matched_edges,
        retrieved_at:result.traffic.retrieved_at},
      checks:["resolved U.S. scenario receipt and exact draft/run point","background bounded road validation","validated extent reviewed before submission",
        "prepared draft survives reload","foreign visitor denied preparation geometry","exact validated network reused",
        "three real SUMO policies; 400 vehicles and four crews",options.trafficMode === "provider_speed_limits"
          ? "real TomTom snapshot applied to matched roads" : "synthetic traffic is explicit; no provider speeds fabricated",
        "assumed simulation hazard remains separate from observed fires",
        "civilian accounting and engine integrity",
        "original Lab map and crew route/trail controls","enlarged replay","policy/time survive reload",
        "source evidence","no duplicate run","no browser runtime errors"]};
    fs.writeFileSync(path.join(out,"acceptance.json"),JSON.stringify(receipt,null,2));
    console.log("Live prepared simulation and UI acceptance passed",run.run_id);
  } catch(error) {
    await page.screenshot({path:path.join(out,"failure.png"),fullPage:true,animations:"disabled"}).catch(()=>{});
    fs.writeFileSync(path.join(out,"failure.txt"),await page.locator("body").innerText().catch(()=>"Page unavailable"));
    throw error;
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
