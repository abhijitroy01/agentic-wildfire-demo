/* Radio Investigation: stage 2's broker radio stream, its radio status and its error line.
 * Run: npm --prefix ui run test:metrics */
const test = require("node:test");
const assert = require("node:assert/strict");
const { load } = require("../ts-module.cjs");
const { streamOf } = require("./support/streams.cjs");

/** A radio stream as the broker sends it (radio_broker.py): one SSE data frame per event. */
const radioStream = (...events) => streamOf(...events.map(event => `data: ${JSON.stringify(event)}\n\n`));

test("stage 2 reads 'stopped' and shows the broker's radio error in its alert line until a new run starts", async t => {
  const React = require("react");
  const { renderToStaticMarkup } = require("react-dom/server");
  const { act, create } = require("react-test-renderer");
  const failure = "Radio investigation could not complete.";
  const runs = [
    radioStream({ type: "connected", data: { session_id: "mon-fixture", status: "starting" } },
      { type: "step_start", data: { step: 1, name: "Processing fixture-clip.mp3", description: "" } },
      { type: "error", data: { message: failure } }),
    radioStream({ type: "connected", data: { session_id: "mon-fixture-2", status: "starting" } },
      { type: "complete", data: { success: true, total_detections: 0, sources_processed: 1, first_detection: null } }),
  ];
  const posts = [];
  t.mock.method(globalThis, "fetch", async (url, init = {}) => {
    if (String(url).endsWith("/workspace-config")) {
      return new Response(JSON.stringify({ access_mode: "local" }), { headers: { "Content-Type": "application/json" } });
    }
    posts.push([String(url), init.method, JSON.parse(init.body)]);
    return new Response(runs.shift(), { headers: { "Content-Type": "text/event-stream" } });
  });
  // Every build reads the broker's stream: no Vite variable selects it.
  const { useLiveMonitoring } = load("features/radio-monitor/hooks/useLiveMonitoring.ts");
  const { RadioStatus } = load("features/evacuation/components/explanations.tsx");
  let radio, renderer;
  // Stage 2 (EvacuationWorkspace) passes a strategy and renders radio.errorMessage as its role="alert" line.
  function Harness() { radio = useLiveMonitoring({ onDetected: async () => {} }); return null; }
  /** Stage 2's status line above that alert, for the hook's current state. */
  const status = () => renderToStaticMarkup(React.createElement(RadioStatus, { radio }));
  /** Start a radio run and let its stream play out. */
  const play = async () => {
    await act(async () => { radio.startMonitoring(); });
    for (let turn = 0; turn < 50 && radio.isMonitoring; turn += 1) {
      await act(() => new Promise(resolve => setImmediate(resolve)));
    }
    assert.equal(radio.isMonitoring, false, "The run ended");
  };
  await act(async () => { renderer = create(React.createElement(Harness)); });
  try {
    await play();
    assert.equal(radio.errorMessage, failure);
    assert.equal(status(), "<p>Radio processing stopped.</p>", "A failed run does not read finished");
    assert.deepEqual(radio.logs.slice(-2).map(entry => [entry.level, entry.message]),
      [["INFO", "Step: Processing fixture-clip.mp3"], ["ERROR", failure]]);
    await play();
    assert.equal(radio.errorMessage, null, "A new run clears the earlier failure");
    assert.equal(status(), "<p>Radio processing finished.</p>");
    assert.deepEqual(posts, Array(2).fill(["/simulation-api/v1/radio/stream", "POST", { playback: true }]));
  } finally {
    await act(async () => renderer.unmount());
  }
});
