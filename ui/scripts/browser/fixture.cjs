/* The simulation recording the browser checks replay, and what a recording must contain for them. */
const fs = require("node:fs");
const path = require("node:path");
const zlib = require("node:zlib");

// Tracked, labeled synthetic fixture for worktrees and fresh clones; the release gate always names a real recording.
const SYNTHETIC = "ui/test-fixtures/browser-synthetic-grid";
const POLICIES = ["baseline", "coordinated", "phased"];

/** Load run `id` from traffic-simulator/.runtime/runs/<id>/, or the tracked synthetic fixture when `id` is unset. */
function loadFixture(root, id) {
  const folder = id ? path.join(root, "traffic-simulator/.runtime/runs", id) : path.join(root, SYNTHETIC);
  if (!fs.existsSync(folder)) {
    throw Error(`No browser fixture at ${folder}. Set SIMULATION_FIXTURE to a completed local run ID under ` +
      `traffic-simulator/.runtime/runs, or restore the tracked ${SYNTHETIC} fixture.`);
  }
  const read = name => JSON.parse(zlib.gunzipSync(fs.readFileSync(path.join(folder, `${name}.json.gz`))));
  const about = id ? {synthetic: false, label: `recorded run ${id}`}
    : JSON.parse(fs.readFileSync(path.join(folder, "fixture.json")));
  return {id: id || path.basename(SYNTHETIC), synthetic: about.synthetic === true, label: about.label,
    result: read("result"), network: read("network")};
}

/** The browser checks' requirements that `result` and `network` do not meet, in words; empty when they can run. */
function fixtureProblems(result, network) {
  const problems = [];
  if (![3, 4].includes(result?.schema_version)) problems.push("result schema_version 3 or 4");
  const plan = Array.isArray(result?.crew_plan) && result.crew_plan.length ? result.crew_plan : null;
  if (!plan) problems.push("a non-empty crew_plan");
  const policies = Array.isArray(result?.policies) ? result.policies : [];
  if (POLICIES.some(policy => !policies.some(p => p.policy === policy)))
    problems.push(`three policies: ${POLICIES.join(", ")}`);
  // Crew positions 1-3 of every 5 are the pumping demo roles (Engine, Tanker, Brush); the replay checks their stop.
  const pumping = new Set((plan || []).filter((_, i) => i % 5 < 3).map(crew => crew.id));
  const coordinated = policies.find(p => p.policy === "coordinated");
  if (!coordinated?.frames?.some(frame => frame.crews?.some(c => pumping.has(c.id) && c.state === "staging_stop")))
    problems.push("a pumping crew (an Engine, Tanker or Brush position) that makes its staging stop under coordinated");
  if (typeof result?.input_bundle_id !== "string" || !result.input_bundle_id) problems.push("an input_bundle_id");
  if (!/^[0-9a-f]{64}$/.test(result?.input_bundle_sha256 ?? "")) problems.push("a SHA-256 input_bundle_sha256");
  if (network?.type !== "FeatureCollection" || !Array.isArray(network.features))
    problems.push("a GeoJSON FeatureCollection network");
  return problems;
}

module.exports = {fixtureProblems, loadFixture};
