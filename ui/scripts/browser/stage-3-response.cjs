/* 3 · Response Tracker: the stage-2 replay, the crew roster against the recording and the AGENT-02 review turn. */
const assert = require("node:assert/strict");
const {activityRows, checkContrast} = require("./helpers.cjs");
const {clock, crewStates} = require("../traffic-lab/check_crew_ui.cjs");
const {REVIEW_ANSWER, REVIEW_EVERY_POLICY} = require("./mock-broker.cjs");
const N = require("./ui-names.cjs");

const ROLES = ["Engine", "Tanker", "Brush", "Rescue", "Command"];

/** The roster's expected stop line for crew `i` at `t`, computed from the recording alone, never with the app's
 * code: stops accrue between consecutive frames at or before t, capped at the recorded service time. */
function stopOracle(replayed) {
  const crewState = (frame, id) => frame.crews.find(c => c.id === id).state;
  const stoppedBy = (id, t) => replayed.frames.some(f => f.t <= t && crewState(f, id) === "staging_stop");
  const stopTime = (id, t) => replayed.frames.reduce((sum, f, i, all) => sum + (all[i + 1]?.t <= t &&
    crewState(f, id) === "staging_stop" ? all[i + 1].t - f.t : 0), 0);
  const expectedLine = (crew, i, t) => {
    if (i % 5 >= 3) return "Support role · no pumping";
    if (!stoppedBy(crew.id, t)) return "Stop not reached";
    const seconds = Math.min(stopTime(crew.id, t), crew.service_seconds);
    const use = `Illustrative · not simulated: about ${Math.round(450 * seconds / 60).toLocaleString("en-US")} gal at 450 gpm`;
    return seconds >= crew.service_seconds ? `${use} over the ${clock(crew.service_seconds)} recorded stop`
      : `${use}, ${clock(seconds)} into the ${clock(crew.service_seconds)} recorded stop`;
  };
  return {crewState, stoppedBy, expectedLine};
}

/** UI-03 and REPLAY-01: 3 · Response Tracker replays stage 2's run, policy and clock in the same workspace, with the
 * roster checked against the recording; the replay itself starts no work. */
async function replay(j) {
  const {page, b, result, guided, strip, stage, currentStage, STAGE_3} = j;
  // Moving between steps, opening the exercise or reloading starts no work.
  // A stage change with no exercise run active sends no stop request.
  j.workBefore = b.workCounters();
  const stage2Time = await page.getByRole("slider").inputValue();
  await stage(STAGE_3).click();
  await currentStage(STAGE_3);
  await page.getByRole("heading", {name: "Response Tracker", exact: true}).waitFor();
  await guided.getByText(j.replayedPolicy, {exact: true}).waitFor();
  assert.equal(await strip.innerText(), j.scenarioLine);
  assert.equal(await page.getByRole("slider").inputValue(), stage2Time);
  for (const [role, name] of [["button", N.COORDINATED], ["list", "Scenario progress"], ["navigation", "Incident details"],
    ["group", "Focus on a situation"], ["complementary", "Incident context"]]) {
    assert.equal(await guided.getByRole(role, {name, exact: true}).count(), 0, `Stage 3 shows no ${name} ${role}`);
  }
  const roster = guided.locator(".crew-explanation-panel");
  const rows = roster.locator("tbody tr");
  assert.equal(await rows.count(), result.crew_plan.length);
  const replayed = j.replayed = result.policies.find(p => p.policy === "coordinated");
  const {crewState, stoppedBy, expectedLine} = stopOracle(replayed);
  /** Seek stage 3 to `t` and check the roster, counters and labels against the recording. */
  j.checkReplayAt = async t => {
    await page.getByRole("slider").fill(String(t));
    const shown = replayed.frames.filter(f => f.t <= t).at(-1);
    await guided.getByText(`Recorded sample: ${clock(shown.t)} · no interpolation`, {exact: true}).waitFor();
    assert.equal(await guided.locator(".metrics > div", {hasText: "Crews on roads"}).locator("b").innerText(),
      String(shown.vehicles.filter(v => v[4]).length));
    const texts = await rows.allInnerTexts();
    result.crew_plan.forEach((crew, i) => {
      for (const part of [ROLES[i % 5], `${clock(crew.scheduled_departure_s)} scheduled`,
        crewStates[crewState(shown, crew.id)], expectedLine(crew, i, shown.t)]) {
        assert.ok(texts[i].includes(part), `Crew ${i + 1} at ${clock(shown.t)} lacks "${part}": ${texts[i]}`);
      }
      if (i % 5 >= 3 || !stoppedBy(crew.id, shown.t)) assert.ok(!texts[i].includes("gal"), texts[i]);
    });
    // Every resource number is labeled where it appears.
    assert.deepEqual(await guided.evaluate(root => {
      const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT), unlabeled = [];
      for (let node = walker.nextNode(); node; node = walker.nextNode()) {
        if (/\d[\d,]*\s*(gal|gpm)/.test(node.textContent) &&
            !node.parentElement.textContent.includes("Illustrative · not simulated")) unlabeled.push(node.textContent);
      }
      return unlabeled;
    }), []);
  };
  const pumpingIds = new Set(result.crew_plan.filter((_, i) => i % 5 < 3).map(c => c.id));
  j.stopFrame = replayed.frames.find(f => f.crews.some(c => pumpingIds.has(c.id) && c.state === "staging_stop"));
  assert.ok(j.stopFrame, "The fixture needs a pumping crew that makes its staging stop");
  await j.checkReplayAt(j.stopFrame.t + 60);
  await checkContrast(roster, "th, td, small, p");
  await j.checkReplayAt(result.scenario.duration_minutes * 60);
}

/** The Coordinated review shows only while Coordinated is replayed; another policy offers a fresh review instead. */
async function checkReviewFollowsPolicy(j, liveActivity) {
  const {findings, direct} = j;
  await j.replayPolicy("Phased departure");
  assert.equal(await findings.getByText(REVIEW_ANSWER, {exact: true}).count(), 0,
    "Stage 3 must not show the Coordinated review while it replays Phased departure");
  assert.equal(await direct.count(), 0, "Stage 3 must not show the Coordinated review's activity under Phased departure");
  assert.equal(await findings.getByRole("button", {name: "Review response findings", exact: true}).isEnabled(), true);
  await j.replayPolicy(N.COORDINATED);
  await findings.getByText(REVIEW_ANSWER, {exact: true}).waitFor();
  assert.equal(await direct.count(), liveActivity ? 1 : 0);
}

/** AGENT-02, first half: one review turn, sent straight to the Simulation agent, with no illustrative values. */
async function review(j) {
  const {b, findings, workBefore} = j;
  assert.deepEqual(b.workCounters(), workBefore, "The replay itself starts no work");
  await findings.getByRole("button", {name: "Review response findings", exact: true}).click();
  await findings.getByText(REVIEW_ANSWER, {exact: true}).waitFor();
  j.direct = findings.locator('.agent-activity[aria-label="Agent activity · sent directly to the Simulation agent"]');
  assert.deepEqual(await activityRows(j.direct),
    [[null, "result_received", "Reading retained simulation evidence", "Response received"]]);
  assert.equal(await findings.getByText("Supervisor", {exact: false}).count(), 0, "No supervisor routes this turn");
  await findings.locator("summary", {hasText: "Evidence: crew"}).click();
  await findings.getByText(`${N.COORDINATED} · crews staged: ${j.replayed.summary.crews_staged}`, {exact: false}).waitFor();
  assert.equal(b.reviewTurns, 1);
  await checkContrast(findings, "p, b, summary, details li, li strong, li span, li small");
  j.workAfterReview = {...workBefore, agentTurns: workBefore.agentTurns + 1};
  assert.deepEqual(b.workCounters(), j.workAfterReview, "The review sends exactly one turn");
  await checkReviewFollowsPolicy(j, true);
  assert.deepEqual(b.workCounters(), j.workAfterReview, "Choosing another policy starts no work");
}

/** The standalone exercise is a sub-view of stage 3; navigation and reload restore stage 3 and start no work. */
async function navigationAndReload(j) {
  const {page, b, guided, exercise, workflow, findings, stage, currentStage, STAGE_1, STAGE_3} = j;
  // The standalone exercise is a sub-view of stage 3: it replaces the replay, and closing it returns there.
  await j.openExercise();
  await exercise.getByText("Synthetic fire suppression exercise. Crew staging in the evacuation simulation is a separate outcome.",
    {exact: true}).waitFor();
  assert.equal(await guided.isVisible(), false, "The exercise replaces the replay");
  assert.deepEqual(await workflow.locator('[aria-current="step"]').allTextContents(), [STAGE_3]);
  await page.waitForFunction(() => document.activeElement?.textContent === "Return to Response Tracker");
  await exercise.getByRole("button", {name: "Return to Response Tracker", exact: true}).click();
  await guided.waitFor();
  await page.waitForFunction(() => document.activeElement?.textContent === "Standalone firetruck exercise");
  await j.openExercise();
  await page.keyboard.press("Escape");
  await exercise.waitFor({state: "detached"});
  await guided.getByText(j.replayedPolicy, {exact: true}).waitFor();
  const replayTime = await page.getByRole("slider").inputValue();
  await b.selectionSaved("coordinated", Number(replayTime));
  for (const name of [STAGE_1, STAGE_3]) {
    await stage(name).click();
    await currentStage(name);
  }
  await page.reload();
  await currentStage(STAGE_3);
  await guided.getByText(j.replayedPolicy, {exact: true}).waitFor();
  await page.waitForFunction(line => document.querySelector('[aria-label="Selected scenario"]')?.innerText === line,
    j.scenarioLine);
  await page.waitForFunction(({testId, value}) => document.querySelector(`[data-testid="${testId}"] input[type="range"]`)
    ?.value === value, {testId: N.GUIDED_TEST_ID, value: replayTime});
  await findings.getByText(REVIEW_ANSWER, {exact: true}).waitFor();
  assert.deepEqual(b.workCounters(), j.workAfterReview,
    "Navigation, the exercise and reload must not start jobs, agents or reports; the saved review answer is reused");
  assert.equal(b.reviewTurns, 1);
  // After the reload the saved answer stands in for the review, still only under the policy its cited facts name.
  await checkReviewFollowsPolicy(j, false);
  assert.deepEqual(b.workCounters(), j.workAfterReview, "Choosing another policy after the reload starts no work");
}

/** An answer whose facts cite every policy never shows under a policy it was not asked for. A saved turn keeps no
 * policy of its own, so after a reload it stands in under none, and the Coordinated-only answer stands in instead. */
async function everyPolicyReview(j) {
  const {page, b, guided, findings, currentStage, STAGE_3} = j;
  /** No review answer shows while stage 3 replays Phased departure or Baseline; ends replaying Coordinated. */
  async function checkNoReviewElsewhere(when) {
    for (const policy of ["Phased departure", "Baseline"]) {
      await j.replayPolicy(policy);
      assert.equal(await findings.getByText(/^Fixture crew review/).count(), 0, `A review answer shows under ${policy} ${when}`);
    }
    await j.replayPolicy(N.COORDINATED);
  }
  b.reviewCitesEveryPolicy = true;
  await findings.getByRole("button", {name: "Review response findings", exact: true}).click();
  await findings.getByText(REVIEW_EVERY_POLICY, {exact: true}).waitFor();
  b.reviewCitesEveryPolicy = false;
  await checkNoReviewElsewhere("while the every-policy review is live");
  await findings.getByText(REVIEW_EVERY_POLICY, {exact: true}).waitFor();
  await b.selectionSaved("coordinated", Number(await page.getByRole("slider").inputValue()));
  await page.reload();
  await currentStage(STAGE_3);
  await guided.getByText(j.replayedPolicy, {exact: true}).waitFor();
  await findings.getByText(REVIEW_ANSWER, {exact: true}).waitFor();
  assert.equal(await findings.getByText(REVIEW_EVERY_POLICY, {exact: true}).count(), 0,
    "After a reload an answer citing every policy must not stand in under Coordinated");
  await checkNoReviewElsewhere("after a reload");
  assert.equal(b.reviewTurns, 2);
}

/** Stage 3's line naming the policy it replays (ResponseTracker.tsx writes the stage name out, so it stays here). */
const replayingLine = policy => `Replaying ${policy} · selected in 2 · Evacuation & Crew Access`;

/** Choose `policy` in stage 2, then return to stage 3, which replays it. */
function replayPolicy(j) {
  return async policy => {
    await j.stage(j.STAGE_2).click();
    await j.page.getByRole("button", {name: policy, exact: true}).click();
    await j.stage(j.STAGE_3).click();
    await j.guided.getByText(replayingLine(policy), {exact: true}).waitFor();
  };
}

module.exports = {replay, review, navigationAndReload, everyPolicyReview, replayPolicy,
  REPLAYED_POLICY: replayingLine(N.COORDINATED)};
