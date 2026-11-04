/* Research scenarios (Traffic Lab) inside stage 2: kept edits, the full-screen map and recorded research results. */
const assert = require("node:assert/strict");
const checkCrewUI = require("../traffic-lab/check_crew_ui.cjs");
const {enterMapFullscreen, exitMapFullscreen} = require("./helpers.cjs");
const N = require("./ui-names.cjs");

/** Both workflows stay mounted; switching modes preserves research edits and the guided replay. */
async function research(j) {
  const {page, result} = j;
  await page.getByRole("button", { name: "Research scenarios", exact: true }).click();
  await page.getByRole("heading", { name: "Free-form experiments", exact: true }).waitFor();
  await page.getByLabel("Evacuation vehicles", { exact: true }).fill("80");
  await page.getByRole("button", { name: "Guided scenario", exact: true }).click();
  assert.equal(await page.getByRole("slider").inputValue(), "600");
  await page.getByRole("button", { name: "Research scenarios", exact: true }).click();
  assert.equal(await page.getByLabel("Evacuation vehicles", { exact: true }).inputValue(), "80");
  const lab = page.getByTestId("traffic-lab");
  const toggle = await enterMapFullscreen(page, page.getByRole("button", {name: "Enlarge research map", exact: true}),
    lab, j.t);
  await page.screenshot({path: j.shot("research-fullscreen.png")});
  await exitMapFullscreen(page, toggle, lab);
  await page.waitForFunction(() => document.activeElement?.textContent === "Enlarge research map");
  assert.equal(await page.getByLabel("Evacuation vehicles", { exact: true }).inputValue(), "80");
  await lab.getByLabel(/^Recent research runs/).selectOption(j.b.researchJob.id);
  await lab.getByLabel("Simulation findings", {exact:true}).waitFor();
  assert.equal(await lab.getByRole("region",{name:"Real data behind this scenario"}).count(),0);
  await lab.getByText("Recorded outcomes and export", {exact:true}).click();
  await lab.getByRole("heading", {name:"Compare the experiments",exact:true}).waitFor();
  await lab.getByRole("button", {name:"Bottlenecks",exact:true}).click();
  await lab.getByRole("heading", {name:"Congestion tradeoffs across policies"}).waitFor();
  assert.equal(await lab.getByLabel("Simulation findings",{exact:true}).count(),0);
  await checkCrewUI(page,result,j.verification,"research-crew");
  assert.equal(await lab.getByLabel("Evacuation and crew outcomes",{exact:true}).count(),0);
  await lab.getByLabel("Crew policy comparison",{exact:true}).waitFor();
  await lab.getByRole("button",{name:N.DATA_EVIDENCE,exact:true}).click();
  await lab.getByRole("button",{name:"Download source evidence JSON",exact:true}).waitFor();
  await lab.getByRole("region",{name:"Real data behind this scenario",exact:true}).waitFor();
  await lab.getByRole("button",{name:"Overview",exact:true}).click();
  await page.setViewportSize({width:683,height:900});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,"Research findings must fit a narrow viewport");
  await page.screenshot({path: j.shot("research-findings-narrow.png"),fullPage:true});
  await page.setViewportSize({width:1512,height:1100});
}

module.exports = {research};
