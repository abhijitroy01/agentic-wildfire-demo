/* One explicitly authorized real UI Investigation, from the actual source coordinate in a successful CLI receipt.
 * Set WFM_TERRAIN_RECEIPT, WFM_TERRAIN_SUBMIT=true, WFM_PUBLIC_URL and optionally WFM_BROWSER_OUTPUT.
 * Native Chrome/Playwright; no routed/mock results, provider credentials, SUMO work or saved cookies. */
const fs=require("node:fs");
const path=require("node:path");
const crypto=require("node:crypto");
const assert=require("node:assert/strict");
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || "playwright");
const N=require("../../ui/scripts/browser/ui-names.cjs");
const helpers=require("../../ui/scripts/browser/helpers.cjs");
const checks=require("./terrain_browser_checks.cjs");
assert.equal(typeof chromium.launch,"function","Playwright must export callable chromium.launch");
checks.assertBrowserIntegration(helpers,N);
const {CONDITION_TIMEOUT_MS,mapsRedrawn}=helpers;
const input=process.env.WFM_TERRAIN_RECEIPT;
assert.ok(input,"Set WFM_TERRAIN_RECEIPT to the successful source-bound terrain CLI receipt");
const options=checks.options(process.env,JSON.parse(fs.readFileSync(input,"utf8")));
const out=path.resolve(process.env.WFM_BROWSER_OUTPUT || ".runtime/verification/terrain-public-browser");
fs.mkdirSync(out,{recursive:true});
const digest=value=>crypto.createHash("sha256").update(JSON.stringify(value)).digest("hex");
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const report={status:"running",origin:new URL(options.url).origin,started_at:new Date().toISOString(),
  input_receipt:path.basename(input),requested_fire:options.fire,
  investigation_network_failures:[],
  network_classifications:checks.requestClassifications([]),
  scope:"One additional real UI Investigation and owner-scoped saved reads; no road, SUMO or source retry."};
const save=()=>fs.writeFileSync(path.join(out,"terrain-browser-acceptance.json"),JSON.stringify(report,null,2)+"\n");
save();

(async()=>{
  let browser,page;
  try{
    browser=await chromium.launch({channel:process.env.PLAYWRIGHT_CHROMIUM_CHANNEL || "chrome",headless:true,
      args:["--use-angle=swiftshader","--enable-unsafe-swiftshader"]});
    const context=await browser.newContext({viewport:{width:1512,height:1100}});
    page=await context.newPage();
    page.setDefaultTimeout(CONDITION_TIMEOUT_MS);
    const requests=[],classified=[],errors=[];
    let resolveTurn,rejectTurn;
    const turn=new Promise((resolve,reject)=>{resolveTurn=resolve;rejectTurn=reject;});
    void turn.catch(()=>{}); // A fast refusal can arrive before the click promise settles; await still reports it below.
    const binding="recordTerrainInvestigationCapture";
    await page.exposeBinding(binding,({frame},value)=>{
      if(frame!==page.mainFrame())return;
      try{
        report.stream_capture=checks.streamStatus(value);
        save();
        if(value.status==="started")return;
        const answer=checks.capturedSnapshot(value,options);
        report.stream_capture.validated_final_snapshot=true;
        save();
        resolveTurn(answer);
      }catch(error){
        rejectTurn(Error(error.code==="ERR_ASSERTION" ? error.message.split("\n")[0].slice(0,500)
          : "UI Investigation did not return a valid source-bound final terrain snapshot"));
      }
    });
    await page.addInitScript(checks.installInvestigationCapture,binding);
    // Attach before submission; the only source turn completes before slow background terrain work.
    page.on("request",request=>{
      const target=new URL(request.url());
      if(target.origin===new URL(options.url).origin && target.pathname.startsWith("/simulation-api/"))
        requests.push({method:request.method(),path:target.pathname});
      const classification=checks.classifyBrowserRequest(request.url(),request.method());
      if(classification){
        classified.push(classification);
        report.network_classifications=checks.requestClassifications(classified);
        save();
      }
    });
    page.on("requestfailed",request=>{
      if(new URL(request.url()).pathname==="/simulation-api/v1/investigation/turns"){
        // CDP transport diagnostics do not replace the same response's authoritative cloned SSE frames.
        report.investigation_network_failures.push({
          method:request.method(),error_code:checks.networkFailureCode(request.failure()?.errorText),
        });
        save();
      }
    });
    page.on("pageerror",error=>errors.push(error.name));
    await page.goto(options.url,{waitUntil:"domcontentloaded"});
    await page.getByRole("button",{name:N.STAGE_1,exact:true}).click();
    await page.getByLabel(N.CHAT_INPUT,{exact:true}).fill(options.question);
    await page.getByRole("button",{name:N.SEND_MESSAGE,exact:true}).click();
    const answer=await turn;
    report.source_observations=answer.locations.filter(location=>location.fire).map(location=>({
      latitude:location.latitude,longitude:location.longitude,
      fire:{name:location.fire.name,sourceId:location.fire.sourceId,lastUpdated:location.fire.lastUpdated},
    }));
    report.job_references=answer.jobs;
    save();
    const map=page.locator(".investigation-workspace .map-container");
    const summary=map.getByRole("complementary",{name:"Fire exposure evidence",exact:true});
    await summary.waitFor();
    const api=async endpoint=>{
      const response=await context.request.get(new URL("/simulation-api/v1/"+endpoint,options.url).href,
        {timeout:65000});
      assert.ok(response.ok(),`GET ${endpoint}: HTTP ${response.status()}`);
      return response.json();
    };
    const receipts=[];
    for(const reference of answer.jobs){
      let current;
      while(true){
        current=checks.jobReceipt(await api(`fire-exposure/jobs/${reference.job_id}`),reference,answer.locations);
        report.jobs=[...receipts.map(job=>({job_id:job.job_id,status:job.status,phase:job.phase,progress:job.progress})),
          {job_id:current.job_id,status:current.status,phase:current.phase,progress:current.progress}];
        save();
        if(!checks.active.has(current.status))break;
        await pause(2500);
      }
      assert.equal(current.status,"completed","Actual terrain job must complete; no success geometry substitutes a failure");
      for(const assessment of current.assessments)
        assert.deepEqual(await api(`fire-exposure/assessments/${assessment.assessment_id}`),assessment,
          "Completed job and saved owner assessment must match exactly");
      receipts.push(current);
    }
    const selected=checks.selection(receipts,answer.locations,options);
    const assessment=selected.assessment;
    report.selected={assessment_id:assessment.assessment_id,fire_ref:assessment.fire_ref,
      job_id:selected.jobId,choice:selected.choice,fire:assessment.fire,input_hash:assessment.input_hash,
      model_version:assessment.model_version,outcome:selected.outcome,horizons:selected.horizons,
      landscape:assessment.landscape,weather:assessment.weather,
      geometry_sha256:assessment.geojson.features.map(feature=>digest(feature.geometry))};
    save();
    await summary.getByText("Saved terrain assessment complete.",{exact:true}).first().waitFor();
    await summary.getByRole("combobox",{name:"Focus reported fire",exact:true}).selectOption(selected.choice);
    await summary.getByRole("checkbox",{name:"Show terrain and fuel informed scenario",exact:true}).waitFor();
    assert.equal((await summary.locator(".fire-exposure-item > strong").innerText()).trim(),
      assessment.fire.name || "Reported fire");
    if(selected.outcome==="no_growth")
      await summary.getByText("No modeled growth beyond the ignition cell.",{exact:true}).waitFor();
    if(assessment.weather.wind_speed_mps===0)
      await summary.getByText("DTN measured calm wind",{exact:true}).waitFor();
    assert.equal(await summary.getByRole("checkbox",{name:"Show historical distance context",exact:true}).count(),0);
    assert.equal(await map.getByText("Illustrative exposure",{exact:true}).count(),0);
    assert.equal(await map.getByText("Uniform-fuel demo",{exact:true}).count(),0);
    const toggle=summary.getByRole("checkbox",{name:"Show terrain and fuel informed scenario",exact:true});
    const slider=summary.getByRole("slider",{
      name:`Scenario time for ${assessment.fire.name || "reported fire"}`,exact:true});
    assert.equal(await toggle.isChecked(),true,"The new answer shows its selected actual terrain mode by default");
    const maximum=selected.horizons.at(-1);
    await slider.fill(String(selected.horizons.length-1));
    await map.getByText(`Modeled spread · ${maximum} min`,{exact:true}).waitFor();
    for(const minute of selected.horizons)
      await map.getByText(`Reached by ${minute} min`,{exact:true}).waitFor();
    await summary.getByText(`${assessment.landscape.manifest.resolution_m} m model cells`,{exact:true}).waitFor();
    const details=summary.locator(".fire-exposure-details");
    assert.equal(await details.getAttribute("open"),null,"Technical model details start collapsed");
    await map.locator(".esri-scale-bar").waitFor();
    const contextControls=map.locator(".map-context-controls");
    await contextControls.getByRole("checkbox",{name:"Terrain shading",exact:true}).waitFor();
    await contextControls.getByText("OpenStreetMap · USGS terrain",{exact:true}).waitFor();
    await map.locator(".map-loading-overlay").waitFor({state:"hidden"});
    assert.equal(await map.locator(".map-error-overlay").count(),0);
    assert.ok(await map.locator("canvas").count(),"Actual ArcGIS canvas must exist");
    await map.getByRole("button",{name:"Enlarge investigation map",exact:true}).click();
    await page.locator(".map-undocked").waitFor();
    // Let this selected fire's existing debounce/fit animation finish; this is paint settling, never a job deadline.
    await page.waitForTimeout(checks.mapFitMilliseconds);
    await map.locator('.map-context-controls[data-streets-status="ready"][data-terrain-status="ready"]').waitFor();
    await mapsRedrawn(page);
    report.map_context={streets:await contextControls.getAttribute("data-streets-status"),
      terrain:await contextControls.getAttribute("data-terrain-status"),
      provider:"OpenStreetMap · USGS terrain",model_resolution_m:assessment.landscape.manifest.resolution_m};
    save();
    await map.screenshot({path:path.join(out,`terrain-live-map-${maximum}-min.png`),animations:"disabled"});
    const from=requests.length;
    if(selected.horizons.length>1){
      const minimum=selected.horizons[0];
      await slider.fill("0");
      await map.getByText(`Modeled spread · ${minimum} min`,{exact:true}).waitFor();
      await map.getByText(`Reached by ${minimum} min`,{exact:true}).waitFor();
      for(const minute of selected.horizons.slice(1))
        assert.equal(await map.getByText(`Reached by ${minute} min`,{exact:true}).count(),0,
          "An earlier scenario time must hide later stored contours");
      await map.locator(".map-loading-overlay").waitFor({state:"hidden"});
      await mapsRedrawn(page);
      await map.screenshot({path:path.join(out,`terrain-live-map-${minimum}-min.png`),animations:"disabled"});
    }
    await toggle.uncheck();
    assert.equal(await map.getByText(/^Modeled spread ·/).count(),0);
    await toggle.check();
    await map.getByText(`Modeled spread · ${maximum} min`,{exact:true}).waitFor();
    await page.waitForTimeout(500); // A bounded quiet window proves controls do not create another workload.
    report.network_classifications=checks.requestClassifications(classified);
    report.control_requests={total:requests.slice(from).length,
      saved_gets:requests.slice(from).filter(request=>request.method==="GET").length};
    report.browser_runtime_errors=errors;
    save();
    checks.noExtraWork(requests);
    assert.ok(requests.slice(from).every(request=>request.method==="GET"),"Returned-geometry controls are read-only");
    assert.equal(report.network_classifications.forbidden_total,0,
      "Browser may read only exact population/federal-land/USGS terrain context; no fire/weather provider, AgentCore or private modeler");
    assert.equal(errors.length,0,"Public terrain UI must have no browser runtime errors");
    const cookie=(await context.cookies()).find(cookie=>cookie.name==="__Host-wildfire-demo");
    assert.ok(cookie?.secure && cookie.httpOnly,"Fresh visitor session remains browser-owned and HttpOnly");
    report.status="passed";
    report.checks=["one explicitly requested real UI Investigation","exact source point and original job references",
      "completed job equals retained owner assessment GET","actual available terrain or declared no-growth result",
      "selected saved fire and actual horizon contours","real map canvas with scale and model resolution",
      "compact collapsed model details and actual ready street/terrain context","distance circles remain off",
      "geometry controls create no source/model/SUMO work","only exact configured map context reads allowed",
      "no direct browser fire/weather provider, AgentCore or private modeler calls",
      "no browser runtime errors"];
    report.finished_at=new Date().toISOString();
    save();
    console.log(JSON.stringify({status:report.status,outcome:selected.outcome,
      assessment_id:assessment.assessment_id,output:out}));
  }catch(error){
    report.status="failed";
    report.error=String(error.message || error.name || "Terrain browser acceptance failed").slice(0,2000);
    report.finished_at=new Date().toISOString();
    save();
    if(page)await page.screenshot({path:path.join(out,"terrain-live-failure.png"),fullPage:true,
      animations:"disabled"}).catch(()=>{});
    console.error(JSON.stringify({status:"failed",output:out,error:report.error}));
    process.exitCode=1;
  }finally{await browser?.close();}
})();
