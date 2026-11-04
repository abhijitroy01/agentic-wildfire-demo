/* Investigation chat: each completed answer keeps its own evidence. Run: npm --prefix ui run test:metrics */
const test = require("node:test");
const assert = require("node:assert/strict");
const { load } = require("../ts-module.cjs");
test("completed chat answers retain their own evidence when a later question has different sources", () => {
  const React = require("react");
  const { act, create } = require("react-test-renderer");
  const useChat = load("features/chat/hooks/useChat.ts").default;
  const { useDetailPanelState } = load("features/layout/hooks/useDetailPanelState.ts");
  let chat, details;
  function Harness() { chat = useChat(); details = useDetailPanelState(); return null; }
  let renderer;
  act(() => { renderer = create(React.createElement(Harness)); });
  const first = {response:"Weather", location:{latitude:38.5, longitude:-121.5, name:"Sacramento"},
    locations:[], fireExposureData:[], forecastData:{days:[{date:"2026-10-07"}]}, audioData:null,
    knowledgeBaseCitations:[{documentName:"First source"}]};
  const second = {...first, response:"Seattle weather", location:{name:"Seattle", latitude:47.6, longitude:-122.3},
    forecastData:null, knowledgeBaseCitations:null};
  act(() => { chat.startStreamingMessage("first"); });
  act(() => { chat.finalizeStreamingMessage("first", first); details.receiveEvidence(first); });
  act(() => { chat.startStreamingMessage("second"); });
  act(() => { chat.finalizeStreamingMessage("second", second); details.receiveEvidence(second); });
  assert.equal(details.evidence.location.name, "Seattle");
  const earlier = chat.messages.find(item => item.id === "first").evidence;
  act(() => { details.inspectEvidence(earlier); });
  assert.equal(details.evidence.location.name, "Sacramento");
  assert.equal(details.evidence.knowledgeBaseCitations[0].documentName, "First source");
  assert.equal(details.evidence.forecastData.days.length, 1);
  assert.equal(details.isDetailPanelOpen, true);
  act(() => renderer.unmount());
});
