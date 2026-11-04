/* Native retained-job controls with explicitly synthetic contracts; no actual source/model work is created. */
const assert=require("node:assert/strict");
const {expectNoRequest,mapOverlayLayout,mapsRedrawn}=require("./helpers.cjs");
const {load}=require("../ts-module.cjs");
const {TERRAIN_SERVICE_URL}=load("shared/config.ts");
const N=require("./ui-names.cjs");

async function mapContextLifecycle(j,map,summary) {
  const {page,b}=j,service=new URL(TERRAIN_SERVICE_URL);
  const image=url=>url.origin===service.origin && url.pathname===`${service.pathname}/exportImage`;
  const context=map.locator(".map-context-controls");
  const ready=()=>map.locator('.map-context-controls[data-terrain-status="ready"]').waitFor();
  const failed=()=>map.locator('.map-context-controls[data-terrain-status="failed"]').waitFor();
  const received=[];
  const response=response=>{
    const url=new URL(response.url());
    if(image(url))received.push({status:response.status(),contentType:response.headers()["content-type"] || ""});
  };
  let blockedImages=0;
  const blockImage=async route=>{blockedImages++;await route.abort("failed");};
  const before=b.requests.length,counters=b.workCounters();
  page.on("response",response);
  try{
    await ready();
    await map.locator('.map-context-controls[data-streets-status="ready"]').waitFor();
    await page.route(image,blockImage);
    await map.getByRole("button",{name:"Zoom in",exact:true}).click();
    await failed();
    assert.ok(blockedImages>0,"A genuine new-viewport image request reached the forced provider outage");
    await context.getByText("Terrain shading unavailable.",{exact:true}).waitFor();
    await map.screenshot({path:j.shot("terrain-context-image-unavailable.png")});
    const beforeRetry=blockedImages;
    // The old failed panel may remain until React replaces its view. Observe the new native failure first.
    await Promise.all([
      page.waitForEvent("requestfailed",{predicate:request=>image(new URL(request.url()))}),
      context.getByRole("button",{name:"Retry map context",exact:true}).click(),
    ]);
    await failed();
    assert.ok(blockedImages>beforeRetry,"Retry attempted the same USGS export and its failure remained explicit");
    await context.getByText("Terrain shading unavailable.",{exact:true}).waitFor();
    await map.screenshot({path:j.shot("terrain-context-retry-unavailable.png")});
    await page.unroute(image,blockImage);
    const recovery=received.length;
    await context.getByRole("button",{name:"Retry map context",exact:true}).click();
    await ready();
    await map.locator('.map-context-controls[data-streets-status="ready"]').waitFor();
    assert.ok(received.slice(recovery).some(result=>result.status===200 && /^image\/png(?:;|$)/i.test(result.contentType)),
      "Recovery requires new real PNG pixels from the same USGS service, rather than cached old imagery");
    assert.equal(await context.getByText("Terrain shading unavailable.",{exact:true}).count(),0);
    await summary.getByText("Saved terrain assessment complete.",{exact:true}).waitFor();
    await summary.getByText("30 m model cells",{exact:true}).waitFor();
    await map.getByText("Modeled spread · 60 min",{exact:true}).waitFor();
    for(const minute of [30,60])await map.getByText(`Reached by ${minute} min`,{exact:true}).waitFor();
    assert.deepEqual(b.workCounters(),counters,"Map context reload preserves the owned scenario and creates no work");
    assert.ok(b.requests.slice(before).every(request=>request.method==="GET"),
      "Provider outage and same-provider retry cannot POST new source, model or simulation work");
    j.t?.diagnostic(`Real USGS context recovery: ${blockedImages} forced export failures, `+
      `${received.length-recovery} new recovery images.`);
  }finally{
    await page.unroute(image,blockImage);
    page.off("response",response);
  }
}

async function terrainJob(j) {
  const {page,b,stage,STAGE_1}=j;
  await stage(STAGE_1).click();
  await page.getByLabel(N.CHAT_INPUT,{exact:true}).fill("Show the synthetic pending terrain contract.");
  await page.getByRole("button",{name:N.SEND_MESSAGE,exact:true}).click();
  await page.getByText("Synthetic pending terrain job contract fixture.",{exact:false}).waitFor();
  const map=page.locator(".investigation-workspace .map-container");
  const summary=map.getByRole("complementary",{name:"Fire exposure evidence",exact:true});
  await summary.getByText("Terrain assessment running.",{exact:false}).waitFor();
  await summary.getByRole("progressbar",{name:"Terrain assessment progress",exact:true}).waitFor();
  await summary.getByRole("button",{name:"Cancel terrain assessment",exact:true}).waitFor();
  assert.equal(await map.getByText(/^Modeled spread ·/).count(),0,"Pending status has no success footprint legend");
  await map.screenshot({path:j.shot("terrain-job-pending.png")});
  b.completeFireRiskJob=true;
  await summary.getByText("Saved terrain assessment complete.",{exact:true}).waitFor();
  await summary.getByText("DTN measured calm wind",{exact:true}).waitFor();
  await summary.getByText("No modeled growth beyond the ignition cell.",{exact:true}).waitFor();
  await map.getByText("Modeled spread · 60 min",{exact:true}).waitFor();
  for(const minute of [30,60])await map.getByText(`Reached by ${minute} min`,{exact:true}).waitFor();
  await summary.getByText("30 m model cells",{exact:true}).waitFor();
  const details=summary.locator(".fire-exposure-details");
  assert.equal(await details.getAttribute("open"),null,"Detailed model assumptions start collapsed");
  await map.locator(".esri-scale-bar").waitFor();
  await map.locator(".map-context-controls").getByRole("checkbox",{name:"Terrain shading",exact:true}).waitFor();
  await map.locator(".map-loading-overlay").waitFor({state:"hidden"});
  assert.equal(await map.getByText("Illustrative exposure",{exact:true}).count(),0,"Distance circles remain off");
  const before=b.requests.length;
  const slider=summary.getByRole("slider",{name:"Scenario time for Fixture fire",exact:true});
  await slider.fill("0");
  await map.getByText("Modeled spread · 30 min",{exact:true}).waitFor();
  assert.equal(await map.getByText("Reached by 60 min",{exact:true}).count(),0,
    "The earlier time hides later returned contours");
  const toggle=summary.getByRole("checkbox",{name:"Show terrain and fuel informed scenario",exact:true});
  await toggle.uncheck();
  assert.equal(await map.getByText(/^Modeled spread ·/).count(),0,"The legend follows actual layer visibility");
  await toggle.check();
  await map.getByText("Modeled spread · 60 min",{exact:true}).waitFor();
  await expectNoRequest(page,b,500,r=>r.endpoint.startsWith("/fire-exposure") || r.method==="POST",
    "Geometry controls use only retained shapes");
  assert.equal(b.requests.slice(before).some(r=>r.endpoint.startsWith("/fire-exposure")),false);
  assert.ok(b.fireRiskJobReads>0);
  await mapContextLifecycle(j,map,summary);
  const viewport=page.viewportSize();
  try{
    await page.setViewportSize({width:390,height:844});
    await mapsRedrawn(page);
    assert.deepEqual(await mapOverlayLayout(map),{missing:[],overlaps:[],outside:[]},
      "Phone map evidence, context, legends, native scale and undock control stay visible and disjoint");
    await map.screenshot({path:j.shot("terrain-context-390.png")});
  }finally{
    if(viewport)await page.setViewportSize(viewport);
    await mapsRedrawn(page);
  }
  await map.locator('.map-context-controls[data-terrain-status="ready"]').waitFor();
  await map.locator('.map-context-controls[data-streets-status="ready"]').waitFor();
  await map.screenshot({path:j.shot("terrain-job-completed-no-growth.png")});
}

async function roadDiscovery(j) {
  const {page,b,stage,STAGE_2}=j;
  await stage(STAGE_2).click();
  const scenarioTab=page.getByRole("button",{name:"Scenario",exact:true});
  if(await scenarioTab.isVisible())await scenarioTab.click();
  await page.getByRole("button",{name:"Research scenarios",exact:true}).click();
  const lab=page.getByTestId("traffic-lab");
  await lab.getByRole("heading",{name:"Free-form experiments",exact:true}).waitFor();
  const edit=lab.getByRole("button",{name:"Edit scenario / start another run",exact:true});
  // This journey restores a completed research run after reload; wait for its artifacts before opening the editor.
  await edit.waitFor();
  await edit.click();
  await lab.getByLabel("Fire latitude",{exact:true}).fill("47.60395");
  await lab.getByLabel("Fire longitude",{exact:true}).fill("-122.3305");
  const before=b.workCounters(),requests=b.requests.length;
  await lab.getByRole("button",{name:"Discover nearby road corridors",exact:true}).click();
  await lab.getByText("Synthetic regional road extraction pending",{exact:true}).waitFor();
  await lab.getByRole("progressbar",{name:"Road discovery progress",exact:true}).waitFor();
  assert.equal(await lab.getByRole("button",{name:"Cancel road discovery",exact:true}).isEnabled(),true);
  await lab.screenshot({path:j.shot("road-discovery-pending.png")});
  b.completeRoadDiscovery=true;
  await lab.getByRole("button",{name:"Discover nearby road corridors",exact:true}).waitFor();
  await lab.getByText("Synthetic UI road contract fixture; no provider extraction.",{exact:true}).waitFor();
  assert.equal(await lab.getByRole("combobox",{name:/^Road corridor/}).inputValue(),"osm:way:fixture");
  assert.deepEqual(b.workCounters(),before,"Road discovery does not create a SUMO run or Investigation");
  const submitted=b.requests.slice(requests).filter(r=>r.endpoint==="/research/discover" && r.method==="POST");
  assert.equal(submitted.length,1,"Discovery submits one area and then reads its retained worker receipt");
  assert.deepEqual(Object.keys(submitted[0].body).sort(),["lat","lon","radius_km","request_id"]);
  assert.match(submitted[0].body.request_id,/^[a-f0-9-]{36}$/i);
  await lab.screenshot({path:j.shot("road-discovery-completed.png")});
}

module.exports={terrainJob,roadDiscovery};
