/* Evacuation simulation turns over the AG-UI stream. Run: npm --prefix ui run test:metrics */
const test = require("node:test");
const assert = require("node:assert/strict");
const { load } = require("../ts-module.cjs");
const { browser, brokerOr, origin } = require("./support/browser.cjs");

test("simulation turns require completed streams, preserve activity, and reject stale context", async () => {
  const request = { session_id: "session-1", incident_id: "incident-1", location_revision: 1 };
  const output = { ...request, turn_id: "turn-1", question: "Compare", answer: "Saved answer", status: "answer" };
  const frame = value => `data: ${JSON.stringify(value)}\r\n\r\n`;
  let chunks = [];
  browser(`${origin}/`, brokerOr(() => new Response(new ReadableStream({
    start(controller) {
      chunks.forEach(chunk => controller.enqueue(new TextEncoder().encode(chunk)));
      controller.close();
    },
  }))));
  const { turn } = load("features/evacuation/services/api.ts");
  const snapshot = frame({ type: "STATE_SNAPSHOT", snapshot: { content: { simulationData: output } } });
  chunks = [snapshot];
  await assert.rejects(turn(request, () => {}), /without a completed/);
  chunks = [snapshot, frame({ type: "RUN_ERROR", message: "Fixture failure" })];
  await assert.rejects(turn(request, () => {}), /Fixture failure/);
  chunks = [frame({ type: "CUSTOM", value: { tool_name: "get_simulation_status" } }),
    snapshot.slice(0, 37), snapshot.slice(37), 'data: {"type":"RUN_FINISHED"}'];
  const seen = [];
  assert.deepEqual(await turn(request, text => seen.push(["activity", text]),
    { onCustom: event => seen.push(["custom", event]) }), output);
  // The CUSTOM notice reaches onCustom whole, before the existing activity text.
  assert.deepEqual(seen, [["custom", { type: "CUSTOM", value: { tool_name: "get_simulation_status" } }],
    ["activity", "get_simulation_status"]]);
  chunks = [snapshot, frame({ type: "RUN_FINISHED" })];
  await assert.rejects(turn({ ...request, location_revision: 2 }, () => {}), /Invalid simulation answer/);
});
