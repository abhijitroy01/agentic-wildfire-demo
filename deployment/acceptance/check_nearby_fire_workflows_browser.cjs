/** Published UI acceptance using actual saved Esri fire references and native road preparation. */
const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const N = require("../../ui/scripts/browser/ui-names.cjs");
const url = process.env.WFM_PUBLIC_URL;
assert.ok(url && new URL(url).protocol === "https:", "Set WFM_PUBLIC_URL to the published deployment.");
const out = path.resolve(process.env.WFM_BROWSER_OUTPUT || ".runtime/verification/nearby-fire-workflows-browser");
fs.mkdirSync(out, {recursive:true});
const point = {
  lat:Number(process.env.WFM_SIMULATION_LATITUDE || "46.94685564720169"),
  lon:Number(process.env.WFM_SIMULATION_LONGITUDE || "-121.71706464002244"),
};
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));

(async () => {
  const browser = await chromium.launch({channel:"chrome", headless:true,
    args:["--use-angle=swiftshader", "--enable-unsafe-swiftshader"]});
  const context = await browser.newContext({viewport:{width:1512,height:1100}});
  const page = await context.newPage(), errors = [], receipts = [];
  page.on("pageerror", error => errors.push(error.message));
  const api = async endpoint => {
    const response = await context.request.get(new URL("/simulation-api/v1"+endpoint,url).href);
    assert.ok(response.ok(), `GET ${endpoint}: ${response.status()}`);
    return response.json();
  };
  // These are acceptance observation windows, separate from application/provider timeouts.
  const wait = async (read, done, label) => {
    const deadline = Date.now()+20*60*1000;
    let last;
    while (Date.now()<deadline) {
      const value = await read();
      const state = JSON.stringify([value?.status,value?.phase,value?.error]);
      if (last !== state) console.log(label,state);
      last = state;
      if (done(value)) return value;
      await pause(3000);
    }
    throw Error(`${label}: no terminal receipt within the acceptance window`);
  };
  const session = async () => {
    const id = await page.evaluate(() => sessionStorage.getItem("fireguard_simulation_session"));
    return id ? api(`/sessions/${id}`) : null;
  };
  const prepared = async current => {
    const accepted = await wait(() => api(`/sessions/${current.session_id}`),
      value => !!value.latest_draft_id,"Saved direct preparation");
    const draft = await wait(() => api(`/scenario-drafts/${accepted.latest_draft_id}`),
      value => ["ready","unavailable","failed"].includes(value.status),"Road preparation");
    assert.notEqual(draft.status,"failed",draft.error || "Operational preparation failure");
    assert.equal(draft.status,"ready","This road-access acceptance requires a validated network");
    assert.equal(draft.network_validation.extent_mode,"auto");
    assert.ok(draft.scenario.radius_km<=15);
    const incident = await api(`/incidents/${accepted.incident_id}`);
    assert.equal(draft.scenario.lat,incident.location.lat);
    assert.equal(draft.scenario.lon,incident.location.lon);
    assert.equal((await api(`/sessions/${accepted.session_id}/turns`)).length,0,
      "Initial preparation must use REST directly without an agent turn");
    return {session:accepted,incident,draft};
  };
  try {
    await page.goto(url,{waitUntil:"domcontentloaded"});
    await page.getByRole("button",{name:N.STAGE_2,exact:true}).click();
    await page.getByRole("tab",{name:N.RADIO_TAB,exact:true}).click();
    await page.getByText("Enter a scenario coordinate",{exact:true}).click();
    await page.getByLabel("Latitude",{exact:true}).fill(String(point.lat));
    await page.getByLabel("Longitude",{exact:true}).fill(String(point.lon));
    await page.getByRole("button",{name:"Use coordinate & prepare",exact:true}).click();
    const initial = await wait(session,value => !!value?.incident_id,"Accepted coordinate");
    let current = await prepared(initial);
    assert.equal(current.incident.location.lat,point.lat);
    assert.equal(current.incident.location.lon,point.lon);
    receipts.push(current);
    const investigation = await wait(() => api(`/incidents/${initial.incident_id}/investigation`),
      value => value && ["completed","failed"].includes(value.status) && value.fire_observations?.length,
      "Saved Esri observations");
    const sources = investigation.fire_observations.filter(value => value.name &&
      (value.latitude!==point.lat || value.longitude!==point.lon)).slice(0,2);
    assert.equal(sources.length,2,"The selected live area must return two other fire source points");
    for (const original of sources) {
      await page.getByRole("button",{name:"Conditions",exact:true}).click();
      await page.waitForFunction(original => JSON.parse(
        sessionStorage.getItem("fireguard_reported_fire_catalog") || "[]").some(value =>
        value.source_id===original.source_id && value.latitude===original.latitude &&
        value.longitude===original.longitude && value.source_timestamp===original.source_timestamp),
      original,{timeout:30000});
      // The next investigation can retain a newer immutable receipt for the same source observation.
      const source = await page.evaluate(original => JSON.parse(
        sessionStorage.getItem("fireguard_reported_fire_catalog") || "[]").find(value =>
        value.source_id===original.source_id && value.latitude===original.latitude &&
        value.longitude===original.longitude && value.source_timestamp===original.source_timestamp),original);
      assert.ok(source,"The retained picker must preserve the exact source observation");
      const cards = page.getByRole("group",{name:"Observed locations"}).getByRole("button");
      await cards.filter({has:page.getByText(source.name,{exact:true})}).first().click();
      await page.getByRole("button",{name:`Start workflow for ${source.name}`,exact:true}).click();
      const next = await wait(session,value => value?.incident_id && value.session_id!==current.session.session_id,
        "Accepted reported-fire workflow");
      current = await prepared(next);
      const location = current.incident.location;
      assert.equal(location.origin,"reported_fire");
      assert.equal(location.source_id,source.source_id);
      assert.equal(location.source_observation_id,source.observation_id);
      assert.equal(location.lat,source.latitude);
      assert.equal(location.lon,source.longitude);
      assert.equal(location.source_timestamp,source.source_timestamp);
      const investigation = await wait(() => api(`/incidents/${next.incident_id}/investigation`),
        value => value && ["completed","failed"].includes(value.status),"New point investigation");
      const weather = investigation.snapshot?.content?.mapData?.locations?.filter(value => value.currentWeather) || [];
      assert.ok(weather.length,"This acceptance requires real returned point weather");
      assert.ok(weather.some(value => value.latitude===source.latitude && value.longitude===source.longitude),
        "New point weather must be returned at this scenario coordinate");
      receipts.push({...current,weather_points:weather.map(value => ({
        latitude:value.latitude,longitude:value.longitude,timestamp:value.currentWeather.timestamp,
      }))});
      await page.screenshot({path:path.join(out,`fire-${receipts.length-1}.png`),fullPage:true,animations:"disabled"});
      await page.reload({waitUntil:"domcontentloaded"});
      await page.getByRole("heading",{name:N.REVIEW_DRAFT,exact:true}).waitFor({timeout:30000});
      const restored = await session();
      assert.equal(restored.session_id,next.session_id);
      assert.equal(restored.latest_draft_id,current.draft.draft_id);
    }
    assert.equal((await api("/runs")).length,0,"Changing workflows and reloading must not submit a simulation");
    assert.deepEqual(errors,[]);
    fs.writeFileSync(path.join(out,"acceptance.json"),JSON.stringify({
      status:"passed",url,initial_point:point,workflows:receipts,
      checks:["real Esri immutable references","exact source coordinates and identity",
        "direct REST automatic road preparation","new point weather","reload retains latest draft",
        "two fire switches without duplicate run","no browser runtime errors"],
    },null,2));
    console.log("Published nearby-fire workflow acceptance passed");
  } catch (error) {
    await page.screenshot({path:path.join(out,"failure.png"),fullPage:true,animations:"disabled"}).catch(()=>{});
    fs.writeFileSync(path.join(out,"failure.txt"),await page.locator("body").innerText().catch(()=>"Page unavailable"));
    throw error;
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error);process.exitCode=1;});
