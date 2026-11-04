/* Read a simulation result from stdin and evaluate the UI's metric functions.
 * Used by backend/evacuation/tests/test_integration_evidence.py for cross-language parity. */
const fs = require("node:fs");
const { load } = require("./ts-module.cjs");

const mission = load("features/evacuation/components/mission.ts");
const { congestion } = load("features/evacuation/components/congestion.ts");
const result = JSON.parse(fs.readFileSync(0, "utf8"));
const baseline = result.policies.find((p) => p.policy === "baseline");
console.log(JSON.stringify(Object.fromEntries(result.policies.map((p) => [
  p.policy, {
    mission: mission.missionMetrics(p, result.scenario),
    congestion: congestion(p, result.scenario.duration_minutes * 60),
    paired: baseline ? mission.pairedCrewTiming(p, baseline, result.scenario) : null,
  },
]))));
