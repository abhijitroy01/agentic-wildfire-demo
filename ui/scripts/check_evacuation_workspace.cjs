/* Native FireGuard UI contract and recovery checks with explicitly mocked providers and agents, as sequential
 * node:test subtests on one page. Run against the built UI (release_checks.browser_check serves it with vite preview);
 * no AWS jobs are submitted. PLAYWRIGHT_MODULE may point to the workstation's installed Playwright module.
 * The stages live in ui/scripts/browser/; browser-checks.json lists the subtests that passed. */
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const fs = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const { fixtureProblems, loadFixture } = require("./browser/fixture.cjs");
const { createBroker, installBroker, REVIEW_ANSWER } = require("./browser/mock-broker.cjs");
const { CONDITION_TIMEOUT_MS, journey, recordFullscreenRequests } = require("./browser/helpers.cjs");
const investigate = require("./browser/stage-1-investigate.cjs");
const evacuation = require("./browser/stage-2-evacuation.cjs");
const { research } = require("./browser/research.cjs");
const response = require("./browser/stage-3-response.cjs");
const exercise = require("./browser/exercise.cjs");
const usScenario = require("./browser/us-scenario.cjs");
const backgroundJobs = require("./browser/background-jobs.cjs");

const root = path.resolve(__dirname, "../..");
const verification = process.env.WFM_BROWSER_OUTPUT || path.join(root, ".runtime/verification/final-integration");
const fabricUrl = process.env.FABRIC_TEST_URL || "http://127.0.0.1:3011";
// A completed local run, traffic-simulator/.runtime/runs/<SIMULATION_FIXTURE>/{result,network}.json.gz, or the tracked
// synthetic fixture when SIMULATION_FIXTURE is unset. The release gate always sets it to a real recording.
const fixture = loadFixture(root, process.env.SIMULATION_FIXTURE);
const { result, network } = fixture;
const problems = fixtureProblems(result, network);
if (problems.length) {
  console.error(`Browser fixture ${fixture.label} cannot run these checks. It needs:\n- ${problems.join("\n- ")}`);
  process.exit(1);
}
console.log(`Browser fixture: ${fixture.label}`);
const capabilities = JSON.parse(fs.readFileSync(path.join(root, "schemas/simulation/capabilities.json")));

// The journey is stateful (counters such as investigations === 7 build up across stages), so the subtests run in this
// order on one page, and a failure skips the stages after it.
const STAGES = [
  ["the public demo opens as an agentic planning demonstration with one workflow landmark; rapid stage changes " +
    "survive pending ArcGIS layer loads", investigate.opening],
  ["UI-05 keyboard Tab and Enter, no overflow and 4.5:1 contrast at 1440, 820 and 390 px; reduced motion",
    investigate.workflowKeyboard],
  ["live supervisor delegation and synthesis rows mid-run and on completion at 1440 and 390 px; tool arguments never " +
    "shown", investigate.liveActivity],
  ["per-answer weather and source drawers keep earlier evidence, start folded and stay in the viewport; the " +
    "supervisor answered directly", investigate.answerDrawers],
  ["voice draft browser playback and review text; a failed guidance call reads Failed", investigate.voiceDraft],
  ["a failed run's open specialist call and synthesis read Failed, never Completed", investigate.failedRun],
  ["the investigation map enters and leaves full screen", investigate.investigationFullscreen],
  ["no sign-in; a failed preparation survives reload without automatic retry; a new preparation preserves it and " +
    "requires review", evacuation.failedPreparation],
  ["guided replay: map expansion keeps the job; clock, policy, crews, bottlenecks and a typed question; the " +
    "Selected scenario strip", evacuation.guidedReplay],
  ["Research keeps its edits across modes and its map enters full screen; recorded results, folded export, " +
    "congestion, source evidence and crew phases; narrow findings fit", research],
  ["response plans: recorded policy deltas, folded sources with keyboard disclosure and peak-congestion navigation; " +
    "a mismatched input hash fails closed", evacuation.responsePlans],
  ["saved supervisor hierarchy in the incident Agent activity tab; the planning list delegates only to guidance",
    evacuation.savedActivity],
  ["refresh recovers the run and selection and reuses saved investigations, plans and analysis",
    evacuation.refreshRecovery],
  ["conditions keep each place's weather, unknown fire measurements stay unavailable, the forecast regroups by UTC " +
    "day; the evacuation map enters full screen and keeps the replay", evacuation.conditions],
  ["REPLAY-01 stage 3 replays the stage-2 run, policy and clock with no stage-2 panels; roster rows and illustrative " +
    "stop use come from the recording, labeled", response.replay],
  ["AGENT-02 one review turn with the crew_details question, sent directly to the Simulation agent, shown only under " +
    "its own policy", response.review],
  ["UI-03 the exercise opens and closes inside stage 3 with focus restored; navigation and reload restore stage 3 " +
    "and the saved review with no new work", response.navigationAndReload],
  ["a review whose facts cite every policy shows under no other policy, and after a reload stands in under none",
    response.everyPolicyReview],
  ["a Palisades 2025 preset selection submits one fresh run", evacuation.presetRun],
  ["radio playback through the broker starts exactly one run; 1366/1024/820/683 px geometry keeps the audio element",
    evacuation.radioRun],
  ["UI-04 stage 1's place starts stage 2 as a radio session with reported_place and no preset; a click during a " +
    "busy start waits for it; walkthrough stages 2 to 4", evacuation.searchAreaStart],
  ["a later answer keeps grouped fire exposure and returned scenario times with no new modeling work; its place " +
    "does not rebind the reviewed simulation", exercise.laterAnswer],
  ["UI-01 the exercise map fits the incident and stays put under unchanged telemetry", exercise.stableMap],
  ["UI-02 the selected unit's popup shows current telemetry beside its marker after an update and a pan",
    exercise.popup],
  ["UI-02 legends and undock control disjoint at 1440, 820 and 390 px; the units legend toggles by click and Enter with " +
    "aria-expanded, aria-controls, visible focus and 4.5:1 contrast", exercise.legends],
  ["leaving stage 3 sends one stop request and closes the exercise stream; no units on the Investigation map",
    exercise.leaving],
  ["returning to the exercise starts fresh, one status read shows the stopped run as stopped, and it still gets a " +
    "report", exercise.returning],
  ["only an explicit Dispatch starts the next run; the response map enters full screen; Stop and reload restore the " +
    "saved report", exercise.nextRun],
  ["an answer place name that is not text offers no search area and keeps the page running",
    investigate.nonTextPlaceName],
  ["a rejected U.S. location preserves the selected recording; reported-fire handoff uses the immutable assessment " +
    "ID and requires draft review",usScenario.explicitLocationHandoff],
  ["terrain preparation remains visibly pending after chat completion; saved mapped no-growth geometry, calm wind " +
    "and returned times draw without distance-circle substitution",backgroundJobs.terrainJob],
  ["Traffic Lab regional road discovery submits once, shows worker progress and loads its exact area result without " +
    "starting a simulation",backgroundJobs.roadDiscovery],
  ["no stop request without an active run; session updates seen; no browser runtime errors", investigate.noRuntimeErrors],
];

test("Native workspace browser checks", async t => {
  fs.mkdirSync(verification, { recursive: true });
  const b = createBroker({result, network, capabilities});
  const browser = await chromium.launch({ channel: "chrome", headless: true,
    args: ["--use-angle=swiftshader", "--enable-unsafe-swiftshader"] });
  const passed = [];
  let failed = null;
  try {
    const context = await browser.newContext({ viewport: { width: 1512, height: 1100 } });
    context.setDefaultTimeout(CONDITION_TIMEOUT_MS);
    await context.addInitScript(recordFullscreenRequests);
    const page = await context.newPage();
    const j = {...journey(page, verification), b, result, network, fabricUrl, REVIEW_ANSWER,
      replayedPolicy: response.REPLAYED_POLICY};
    j.replayPolicy = response.replayPolicy(j);
    page.on("pageerror", error => b.errors.push(error.stack || error.message));
    page.on("console", message => {
      if (message.type() !== "error") return;
      console.log("Browser console:", message.text().slice(0, 300));
      // A fit must stop quietly when its map unmounts, for example on a quick change of workflow stage.
      if (message.text().startsWith("Map centering")) b.errors.push(message.text().slice(0, 300));
    });
    await installBroker(page, b, fabricUrl);
    for (const [name, check] of STAGES) {
      if (failed) {
        await t.test(name, {skip: `an earlier stage failed: ${failed}`}, () => {});
        continue;
      }
      let ok = false;
      await t.test(name, async subtest => { j.t = subtest; await check(j); ok = true; });
      if (ok) passed.push(name); else failed = name;
    }
  } finally {
    fs.writeFileSync(path.join(verification, "browser-checks.json"), JSON.stringify({
      passed: !failed && passed.length === STAGES.length,
      fixture: {id: fixture.id, synthetic: fixture.synthetic, label: fixture.label},
      scope: "Native UI using mocked broker/providers and an unchanged SUMO fixture; no AWS jobs submitted.",
      submitted: b.submitted, prepared: b.prepared, analysis_requests: b.analyzed, investigations: b.investigations,
      radioRequests: b.radioRequests, firetruckStarts: b.firetruckStarts, firetruckStops: b.firetruckStops,
      firetruckStatusReads: b.firetruckStatusReads, truckStreamCloses: b.truckStreamCloses,
      reportRequests: b.reportRequests, incidentStarts: b.incidentStarts, updatedPlans: b.updatedPlans,
      agentTurns: b.agentTurns, reviewTurns: b.reviewTurns, sessionUpdates: b.sessionUpdates,
      delayedLayerLoads: b.delayedLayerLoads, checks: passed, failed,
    }, null, 2));
    await browser.close();
  }
  console.log(failed ? `Native workspace browser checks failed at: ${failed}` : "Native workspace browser checks passed.");
});
