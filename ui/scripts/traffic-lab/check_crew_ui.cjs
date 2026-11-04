// Shared browser assertions against real, saved schema-v2 SUMO recordings.
const assert = require("node:assert/strict");
const path = require("node:path");
const N = require("../browser/ui-names.cjs");

/** The Traffic Lab root; scope every lab locator to it. The hidden Evacuation workspace stays mounted beside the lab
 * and renders the same map classes, so unscoped locators would match two elements. */
const trafficLab = page => page.getByTestId("traffic-lab");

const clock = t => `${Math.floor(t / 60)}:${String(Math.floor(t % 60)).padStart(2, "0")}`;
const names = { baseline: "Baseline", coordinated: N.COORDINATED, phased: "Phased departure" };
const states = {
  no_route: "No initial route", origin_closed: "Held at closed origin",
  waiting_to_depart: "Waiting to depart", to_staging: "Travelling toward staging",
  on_staging_segment: "On the staging segment", staging_stop: "Making the assigned staging stop",
  returning: "Travelling toward an exit gate", returned: "Returned to a gate",
};

module.exports = checkCrewUI;
// The recorded-time format and crew phase labels the UI shows, shared with the workspace harness.
module.exports.clock = clock;
module.exports.crewStates = states;

/** Check the lab's crew view against the recording; screenshots go to `target` as `<prefix>-*.png`. */
async function checkCrewUI(page, result, target, prefix) {
  assert([3, 4].includes(result.schema_version));
  assert.equal(result.crew_plan.length, result.scenario.crews);
  const lab = trafficLab(page);
  await lab.getByRole("button", { name: N.FIRE_CREWS, exact: true }).click();
  const panel = lab.getByRole("region", { name: "Crew dispatch explanation" });
  const slider = lab.getByRole("slider", { name: "Simulation time" });
  const routes = lab.getByLabel(N.CREW_ROUTES, { exact: true });
  const trails = lab.getByLabel(N.CREW_TRAILS, { exact: true });
  assert.equal(await panel.locator(".recording-gap").count(), 0);
  assert.match(await panel.innerText(), /planned paths, not observed movement/);
  for (const policy of result.policies) {
    await lab.getByRole("button", { name: names[policy.policy], exact: true }).click();
    await lab.getByRole("button", { name: "Start", exact: true }).click();
    assert.equal(await slider.inputValue(), "0");
    const seen = new Set();
    const samples = new Set([0, policy.frames.length - 1]);
    // Every phase actually saved in this policy must be rendered at least once.
    policy.frames.forEach((f, index) => {
      assert.equal(f.crews.length, result.scenario.crews);
      for (const crew of f.crews) {
        assert(states[crew.state], `Unknown crew phase: ${crew.state}`);
        if (!seen.has(crew.state)) { seen.add(crew.state); samples.add(index); }
        for (const field of ["departed_s", "reached_staging_s", "returned_s"]) {
          assert(crew[field] == null || crew[field] <= f.t, `${field} leaks a future event`);
        }
      }
    });
    for (const index of samples) {
      const frame = policy.frames[index];
      await slider.fill(String(index));
      const rows = panel.locator("tbody tr");
      assert.equal(await rows.count(), result.crew_plan.length);
      for (const [i, plan] of result.crew_plan.entries()) {
        const recorded = frame.crews.find(c => c.id === plan.id);
        const text = await rows.nth(i).innerText();
        assert(text.includes(`${clock(plan.scheduled_departure_s)} scheduled`));
        assert(text.includes(states[recorded.state]), `${policy.policy} ${frame.t} ${plan.id}: ${text}`);
        assert(text.includes(recorded.departed_s == null ? "Has not entered roads"
          : `Entered at ${clock(recorded.departed_s)}`));
        assert(text.includes(recorded.reached_staging_s == null ? "Staging not reached"
          : `Reached segment ${clock(recorded.reached_staging_s)}`));
        if (recorded.returned_s != null) assert(text.includes(`Returned ${clock(recorded.returned_s)}`));
        const onRoad = frame.vehicles.some(v => v[0] === plan.id);
        // A planned origin can be shown only when the run saved one; an unroutable crew may have none.
        const origin = !onRoad && !!plan.origin && ["waiting_to_depart", "origin_closed", "no_route"].includes(recorded.state);
        const action = rows.nth(i).getByRole("button");
        assert.equal(await action.isEnabled(), onRoad || origin, `${policy.policy} ${frame.t} ${plan.id} map action`);
        if (!onRoad && !origin) assert(text.includes("No position at this replay time"), text);
        assert.equal(await action.getAttribute("aria-label"), origin
          ? `Show planned origin for Crew ${i + 1}` : `Locate Crew ${i + 1}`);
      }
      // `has` locators are relative to each metric card, so they start from the page, not the lab.
      const returned = lab.locator(".metrics > div").filter({ has: page.getByText("Crews returned", { exact: true }) });
      assert.equal(await returned.locator("b").innerText(), String(frame.crews.filter(c => c.state === "returned").length));
    }
    const final = policy.frames.at(-1);
    assert.equal(final.crews.filter(c => c.returned_s != null).length, policy.summary.crews_returned);
    assert.equal(final.crews.filter(c => c.reached_staging_s != null).length, policy.summary.crews_staged);
  }
  await lab.getByRole("button", { name: "Baseline", exact: true }).click();
  await lab.getByRole("button", { name: "First crew on road", exact: true }).click();
  const first = result.policies[0].frames.findIndex(f => f.vehicles.some(v => v[4]));
  assert.equal(await slider.inputValue(), String(first));
  const vehicle = result.policies[0].frames[first].vehicles.find(v => v[4]);
  const crewNumber = result.crew_plan.findIndex(c => c.id === vehicle[0]) + 1;
  const recorded = result.policies[0].frames[first].crews.find(c => c.id === vehicle[0]);
  const row = (await panel.locator("tbody tr").nth(crewNumber - 1).innerText()).replace(/\s+/g, " ");
  const map = lab.locator(".map");
  const popup = lab.locator(".map-inspection");
  assert.equal(await popup.count(), 0, "No popup is open before Locate");
  // Locate alone opens the crew's popup: no map click.
  await lab.getByRole("button", { name: `Locate Crew ${crewNumber}`, exact: true }).click();
  await popup.waitFor();
  await map.scrollIntoViewIfNeeded();
  await page.waitForTimeout(650); // The documented camera transition lasts 500 ms.
  const located = (await popup.innerText()).replace(/\s+/g, " ");
  assert.match(located, new RegExp(`^Crew ${crewNumber} · fire crew`), located);
  const speed = vehicle[3] < 0.5 ? "stopped or very slow" : /\d+\.\d mph/.exec(row)?.[0];
  assert(speed, `The row shows a recorded speed: ${row}`);
  // The popup repeats the crew's table row: phase, speed, road entry and staging status.
  for (const part of [states[recorded.state], `Recorded speed: ${speed}`, `${clock(result.crew_plan[crewNumber - 1].scheduled_departure_s)} scheduled`,
    recorded.departed_s == null ? "Has not entered roads" : `Entered at ${clock(recorded.departed_s)}`,
    recorded.reached_staging_s == null ? "Staging not reached" : `Reached segment ${clock(recorded.reached_staging_s)}`]) {
    assert(located.includes(part), `Locate popup lacks "${part}": ${located}`);
    assert(row.includes(part.replace("Recorded speed: ", "")), `Row lacks "${part}": ${row}`);
  }
  await lab.locator(".map-wrap").screenshot({ path: path.join(target, `${prefix}-located.png`) });
  await lab.getByRole("button", { name: "Close popup", exact: true }).click();
  // The map's own click popup on the located crew names it. Locate places the crew 90 px below the map centre
  // (SimulationMap LOCATE_OFFSET_Y) so its popup clears the map caption.
  const box = await map.boundingBox();
  await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2 + 90);
  await popup.waitFor();
  const clicked = await popup.innerText();
  assert.match(clicked, new RegExp(`Fire crew · Crew ${crewNumber} · `), clicked);
  assert.match(clicked, /Recorded at/);
  await lab.locator(".map-wrap").screenshot({ path: path.join(target, `${prefix}-map-click.png`) });
  await lab.getByRole("button", { name: "Close popup", exact: true }).click();
  await routes.uncheck(); assert.equal(await routes.isChecked(), false);
  await trails.uncheck(); assert.equal(await trails.isChecked(), false);
  await lab.locator(".map-wrap").screenshot({ path: path.join(target, `${prefix}-positions-only.png`) });
  await routes.check(); await trails.check();
  await lab.getByRole("button", { name: "End of run", exact: true }).click();
  await lab.getByRole("button", { name: "Whole study area", exact: true }).click();
  await page.waitForTimeout(650);
  await page.screenshot({ path: path.join(target, `${prefix}-plans-and-trails.png`), fullPage: true });
}
