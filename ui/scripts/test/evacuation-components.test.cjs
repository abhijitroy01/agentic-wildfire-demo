/* Evacuation workspace components rendered in Node: Response findings and the stage-1 search area.
 * Run: npm --prefix ui run test:metrics */
const test = require("node:test");
const assert = require("node:assert/strict");
const { load } = require("../ts-module.cjs");
const { DIRECT_TITLE, loadActivity, notice } = require("./support/agent-activity.cjs");

test("Response findings asks the Simulation agent and shows its cited answer without illustrative numbers", () => {
  const { createElement } = require("react");
  const { renderToStaticMarkup } = require("react-dom/server");
  const ResponseFindings = load("features/evacuation/components/ResponseFindings.tsx").default;
  const { receiveSimulationActivity } = loadActivity();
  const question = "Which crews did not reach staging, and how did waiting and travel times change?";
  const answer = { session_id: "s", incident_id: "i", location_revision: 1, turn_id: "t", status: "answer", question,
    answer: "Two crews did not reach staging.", ui_actions: [], evidence: [{ evidence_id: "evidence-1", view: "crew",
      availability: "available", artifact_refs: [{ run_id: "run-1", sha256: "x" }], comparisons: [], missing_reasons: [],
      pagination: { has_more: false }, facts: [{ fact_id: "f1", run_id: "run-1", metric: "crews_staged", value: 2,
        availability: "available", policy_id: "coordinated" }] }] };
  const render = props => renderToStaticMarkup(createElement(ResponseFindings, { question,
    disabled: false, review: null, answer: null, onReview: () => {}, ...props })).replace(/<!-- -->/g, "");
  const button = html => html.match(/<button[^>]*>Review response findings<\/button>/)?.[0];
  /** The text of the panel's one status line, which stays mounted so assistive technology announces each change. */
  const status = html => {
    const lines = [...html.matchAll(/<p[^>]*role="status"[^>]*>([^<]*)<\/p>/g)];
    assert.equal(lines.length, 1, html);
    return lines[0][1];
  };
  assert.ok(!button(render({})).includes("disabled"), render({}));
  assert.equal(status(render({})), "");
  const events = receiveSimulationActivity([], notice("TOOL_CALL_START"), 1000);
  // The status line names the policy the review was asked under, from the review's own record.
  const running = render({ disabled: true, review: { policyId: "phased", status: "running", events } });
  assert.ok(button(running).includes("disabled"), running);
  assert.equal(status(running), "The Simulation agent is reviewing the crew evidence for Phased departure.");
  assert.ok(running.includes(`aria-label="${DIRECT_TITLE}"`) && running.includes("Calling tool"), running);
  const done = render({ review: { policyId: "coordinated", status: "completed", events }, answer });
  assert.ok(done.includes("Two crews did not reach staging.") && done.includes("Coordinated · crews staged: 2"), done);
  assert.equal(status(done), "");
  assert.equal(status(render({ review: { policyId: "coordinated", status: "failed", events } })),
    "The review did not complete. Saved evidence remains available; try again.");
  for (const html of [running, done]) assert.ok(!/\d[\d,]*\s*(gal|gpm)|Illustrative · not simulated/.test(html), html);
  const unavailable = render({ question: null });
  assert.ok(button(unavailable).includes("disabled") && unavailable.includes("Crew evidence is not available for this run."),
    unavailable);
  assert.ok(!render({}).includes("Crew evidence is not available"));
});

test("the stage-1 search area shows the answer's place as text and accepts city names through the place contract", () => {
  const fs = require("node:fs");
  const path = require("node:path");
  const { createElement } = require("react");
  const { renderToStaticMarkup } = require("react-dom/server");
  const SearchAreaEntry = load("features/evacuation/components/SearchAreaEntry.tsx").default;
  const { REPORTED_PLACE_MIN_LENGTH } = load("features/evacuation/services/api.ts");
  // The constant mirrors the published IncidentRequest contract, which the server enforces.
  const contract = JSON.parse(fs.readFileSync(path.resolve(__dirname, "../../../schemas/simulation/openapi.json"), "utf8"));
  const place = contract.components.schemas.IncidentRequest.properties.reported_place.anyOf
    .find(schema => schema.type === "string");
  assert.equal(REPORTED_PLACE_MIN_LENGTH, place.minLength);
  const render = place => renderToStaticMarkup(createElement(SearchAreaEntry, { place, onSimulate: () => {} }))
    .replace(/<!-- -->/g, "");
  const button = html => html.match(/<button[^>]*>Simulate this search area<\/button>/)?.[0];
  const ready = render("Paradise, California");
  assert.ok(ready.includes('aria-label="Search area"') && ready.includes("Search area: Paradise, California"), ready);
  assert.ok(!button(ready).includes("disabled") && !ready.includes("Enter a fuller place name"), ready);
  assert.ok(!button(render("Seattle")).includes("disabled"), "A search locality is distinct from a radio address");
  const hostile = render("<img src=x onerror=alert(1)>");
  assert.ok(hostile.includes("Search area: &lt;img") && !hostile.includes("<img"), hostile);
  const short = render("x".repeat(REPORTED_PLACE_MIN_LENGTH - 1));
  assert.ok(button(short).includes("disabled"), short);
  assert.ok(short.includes("Enter a fuller place name in 2 · Evacuation &amp; Crew Access."), short);
  assert.ok(!button(render("x".repeat(REPORTED_PLACE_MIN_LENGTH))).includes("disabled"));
});
