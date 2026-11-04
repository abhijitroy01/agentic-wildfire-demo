/* Boundary recovery regressions: transport ambiguity never creates replacement paid work or false evidence. */
const test = require("node:test");
const assert = require("node:assert/strict");
const React = require("react");
const { act, create } = require("react-test-renderer");
const { load } = require("../ts-module.cjs");
const { browser, json, hang, local, origin } = require("./support/browser.cjs");
const { streamOf, within } = require("./support/streams.cjs");
const quiet = { logger: { error() {}, warn() {}, log() {}, debug() {} },
  default: { error() {}, warn() {}, log() {}, debug() {} }, __esModule: true };
const settle = async () => { for (let i = 0; i < 12; i += 1) await new Promise(resolve => setImmediate(resolve)); };
const layers = () => Object.fromEntries(["riskZonesLayer", "locationsLayer", "firesLayer", "windLayer",
  "demographicLayer", "vegetationLayer"].map(name => [name, { held: [],
  removeAll() { this.held = []; }, addMany(values) { this.held.push(...values); } }]));
const uuid = "a5e9d994-442c-413f-9b42-1d1bf3372f98";
const snapshot = {type: "structuredResponse", content: {response: "Retained answer."}};

function pausedTimers(t, delay) {
  const scheduled = [], nativeSet = globalThis.setTimeout, nativeClear = globalThis.clearTimeout;
  t.mock.method(globalThis, "setTimeout", (callback, ms, ...args) => {
    if (ms !== delay) return nativeSet(callback, ms, ...args);
    const timer = {callback, cancelled: false}; scheduled.push(timer); return timer;
  });
  t.mock.method(globalThis, "clearTimeout", timer => {
    if (timer && typeof timer === "object" && "cancelled" in timer) timer.cancelled = true;
    else nativeClear(timer);
  });
  return scheduled;
}

test("a cancelled bootstrap waiter settles promptly while another shares and completes initialization", async () => {
  let release;
  const env = browser(`${origin}/`, (url) => url.endsWith("/workspace-config")
    ? new Promise(resolve => { release = () => resolve(json(local)); }) : json({owned: true}));
  const { brokerJson } = load("shared/services/workspaceAccess.ts", {stubs: {"./tabStorage": {
    tabStorage: env.storage, tabStorageIsPersistent: () => true,
  }}});
  const controller = new AbortController();
  const leaving = brokerJson("/first", {signal: controller.signal});
  const remaining = brokerJson("/second");
  controller.abort();
  await within(assert.rejects(leaving, {name: "AbortError"}), 100, "Cancelled bootstrap waiter");
  assert.equal(env.requests.length, 1);
  assert.ok(env.requests[0].init.signal instanceof AbortSignal);
  release();
  assert.deepEqual(await remaining, {owned: true});
  assert.deepEqual(env.requests.map(request => request.url),
    ["/simulation-api/v1/workspace-config", "/simulation-api/v1/second"]);
});

test("bootstrap has its own deadline, resets after failure and exposes a typed transient timeout", async t => {
  const timeout = AbortSignal.timeout;
  t.mock.method(AbortSignal, "timeout", () => timeout.call(AbortSignal, 15));
  const env = browser(`${origin}/`, hang);
  const {initialize, isTransientReadError} = load("shared/services/workspaceAccess.ts");
  await assert.rejects(initialize(), error => error.code === "WORKSPACE_STARTUP_TIMEOUT" && isTransientReadError(error));
  env.route = () => json(local);
  assert.deepEqual(await initialize(), local);
  assert.equal(env.requests.length, 2);
});

test("typed provider refusal overrides a transient HTTP status and ordinary invalid data stays permanent", async () => {
  const env = browser(`${origin}/`, url => url.endsWith("/workspace-config") ? json(local)
    : json({error: {code: "SOURCE_REFUSED", message: "Source refused", retryable: false, request_id: uuid}}, 503));
  const {brokerJson, isTransientReadError, BrokerError} = load("shared/services/workspaceAccess.ts");
  await assert.rejects(brokerJson("/refused"), error => !isTransientReadError(error)
    && error.retryable === false && error.requestId === uuid);
  env.route = () => json({error: {message: "Refused", request_id: "untrusted arbitrary text"}}, 403);
  await assert.rejects(brokerJson("/invalid-id"), error => error.requestId === null);
  assert.equal(isTransientReadError(new BrokerError("Temporary", 503, null, null)), true);
  assert.equal(isTransientReadError(new Error("Invalid retained artifact")), false);
});

test("denied browser storage preserves current-page state and session identity without throwing", t => {
  browser();
  const saved = Object.getOwnPropertyDescriptor(globalThis, "sessionStorage");
  Object.defineProperty(globalThis, "sessionStorage", {configurable: true,
    get() { throw new DOMException("Denied", "SecurityError"); }});
  t.after(() => Object.defineProperty(globalThis, "sessionStorage", saved));
  const {tabStorage} = load("shared/services/tabStorage.ts", {stubs: {"@/shared/utils/logger": quiet}});
  tabStorage.setItem("view", "response");
  assert.equal(tabStorage.getItem("view"), "response");
  tabStorage.removeItem("view");
  assert.equal(tabStorage.getItem("view"), null);
  const {SessionManager} = load("shared/utils/sessionManager.ts", {stubs: {"@/shared/utils/logger": quiet}});
  assert.equal(SessionManager.getOrCreateSessionId(), SessionManager.getOrCreateSessionId());
  const {restoredView, rememberView} = load("shared/services/viewPersistence.ts");
  assert.equal(restoredView({getItem() {throw new Error("Denied");}}, true), "chat");
  assert.doesNotThrow(() => rememberView({setItem() {throw new Error("Denied");}}, "response"));
});

test("write-only storage failure preserves in-memory handles despite successful native reads", () => {
  browser();
  global.sessionStorage = {getItem: key => key === "old" ? "stale disk value" : null,
    setItem() {throw new DOMException("Quota", "QuotaExceededError");},
    removeItem() {throw new DOMException("Denied", "SecurityError");}};
  const {tabStorage} = load("shared/services/tabStorage.ts", {stubs: {"@/shared/utils/logger": quiet}});
  tabStorage.setItem("request", uuid);
  assert.equal(tabStorage.getItem("request"), uuid);
  assert.equal(tabStorage.getItem("request"), uuid, "Readable empty native storage cannot erase the fallback handle");
  tabStorage.removeItem("old");
  assert.equal(tabStorage.getItem("old"), null, "Failed removal cannot resurrect retired disk state");
  const {SessionManager} = load("shared/utils/sessionManager.ts", {stubs: {"@/shared/utils/logger": quiet}});
  assert.equal(SessionManager.getOrCreateSessionId(), SessionManager.getOrCreateSessionId());
});

test("failed current-answer graphics clear prior evidence and retry rebuilds the rejected cache", async () => {
  const destination = layers(), mapView = {}, first = {locations: [], label: "first"}, next = {locations: [], label: "next"};
  let calls = 0, fail = true, state, renderer;
  const {useMapGraphics} = load("features/map/hooks/useMapGraphics.ts", {stubs: {
    "@/shared/utils/logger": quiet, "../utils/mapGraphicsLoader": {
      buildMapGraphics: async data => { calls += 1; if (data === next && fail) throw new Error("Chunk unavailable");
        return {fires: [data.label]}; },
      buildFireExposureGraphics: async () => [],
      showMapGraphics: async (where, data) => where.firesLayer.addMany(data.fires),
    },
  }});
  function Harness({data}) { state = useMapGraphics({layers: destination, mapView, mapData: data,
    centerLocation: null, cameraAvailable: false}); return null; }
  await act(async () => {renderer = create(React.createElement(Harness, {data: first})); await settle();});
  try {
    assert.deepEqual(destination.firesLayer.held, ["first"]);
    await act(async () => {renderer.update(React.createElement(Harness, {data: next})); await settle();});
    assert.deepEqual(destination.firesLayer.held, []);
    assert.match(state.error, /Chunk unavailable/);
    fail = false;
    await act(async () => {await state.updateGraphics(); await settle();});
    assert.equal(state.error, null);
    assert.deepEqual(destination.firesLayer.held, ["next"]);
    assert.equal(calls, 3);
  } finally {await act(async () => renderer.unmount());}
});

test("optional overlays do not delay primary evidence and their retry performs no primary/provider replay", async () => {
  const destination = layers(), mapView = {}, data = {locations: []}, center = {latitude: 39, longitude: -121};
  let resolveOverlay, primary = 0, optional = 0, state, renderer;
  const {useMapGraphics} = load("features/map/hooks/useMapGraphics.ts", {stubs: {
    "@/shared/utils/logger": quiet, "../utils/mapGraphicsLoader": {
      buildMapGraphics: async () => {primary += 1; return {fires: ["observed"]};},
      buildFireExposureGraphics: async () => [],
      showMapGraphics: async (where, graphics) => where.firesLayer.addMany(graphics.fires),
      buildMapOverlayGraphics: () => {optional += 1; return new Promise(resolve => {resolveOverlay = resolve;});},
      showMapOverlayGraphics: () => {},
    },
  }});
  function Harness() {state = useMapGraphics({layers: destination, mapView, mapData: data, centerLocation: center,
    cameraAvailable: false}); return null;}
  await act(async () => {renderer = create(React.createElement(Harness)); await settle();});
  try {
    assert.deepEqual(destination.firesLayer.held, ["observed"]);
    assert.equal(state.isLoading, false);
    assert.equal(state.overlayStatus.demographic, "loading");
    await act(async () => {resolveOverlay({status: {demographic: "unavailable", vegetation: "empty"}}); await settle();});
    assert.deepEqual(state.overlayStatus, {demographic: "unavailable", vegetation: "empty"});
    await act(async () => {state.retryOverlays(); await settle();});
    assert.equal(optional, 2);
    assert.equal(primary, 1);
  } finally {await act(async () => renderer.unmount());}
});

test("empty optional provider data is valid while refused or malformed data rejects", async () => {
  const env = browser(`${origin}/`, () => json({features: []}));
  const {fetchDemographicAreas} = load("features/map/services/overlayData.ts");
  assert.deepEqual(await fetchDemographicAreas({latitude: 39, longitude: -121}), []);
  env.route = () => json({error: {code: 400}});
  await assert.rejects(fetchDemographicAreas({latitude: 39, longitude: -121}), /provider rejected/);
  env.route = () => json({features: "invalid"});
  await assert.rejects(fetchDemographicAreas({latitude: 39, longitude: -121}), /unusable/);
});

test("rejected lazy map offers a real reload and does not pretend to restart the rejected import", async t => {
  const env = browser();
  window.location = location;
  const stubs = {"@/shared/utils/logger": quiet};
  Object.defineProperty(stubs, "./MapContainer", {get() {throw new Error("Chunk failed");}});
  t.mock.method(console, "error", () => {});
  const {default: Map} = load("features/map/components/LazyMapContainer.tsx", {stubs});
  let renderer;
  await act(async () => {renderer = create(React.createElement(Map, {mapData: null, centerLocation: null})); await settle();});
  try {
    const button = renderer.root.findByType("button");
    assert.deepEqual(button.children, ["Reload map"]);
    await act(async () => button.props.onClick());
    assert.equal(env.reloads, 1);
  } finally {await act(async () => renderer.unmount());}
});

test("incomplete radio EOF stops monitoring, logs the failure and exposes its alert", async () => {
  const {useLiveMonitoring} = load("features/radio-monitor/hooks/useLiveMonitoring.ts", {stubs: {
    "@/shared/services/workspaceAccess": {brokerFetch: async () => new Response(streamOf(
      'data: {"type":"connected","data":{}}\n\n'))}, "@/shared/utils/logger": quiet,
  }});
  let state, renderer;
  function Harness() {state = useLiveMonitoring({onDetected: async () => {}}); return null;}
  await act(async () => {renderer = create(React.createElement(Harness)); await settle();});
  try {
    await act(async () => {state.startMonitoring(); await settle();});
    assert.equal(state.isMonitoring, false);
    assert.match(state.errorMessage, /before a final result/);
    assert.equal(state.logs.at(-1).level, "ERROR");
  } finally {await act(async () => renderer.unmount());}
});

test("unlocated radio evidence stays absent, later address starts once and measured confidence zero survives", async () => {
  let controller, state, renderer;
  const calls = [], body = new ReadableStream({start(value) {controller = value;}});
  const send = data => controller.enqueue(new TextEncoder().encode(`data: ${JSON.stringify({type: "detection", data})}\n\n`));
  const {useLiveMonitoring} = load("features/radio-monitor/hooks/useLiveMonitoring.ts", {stubs: {
    "@/shared/services/workspaceAccess": {brokerFetch: async () => new Response(body)}, "@/shared/utils/logger": quiet,
  }});
  function Harness() {state = useLiveMonitoring({onDetected: async address => {calls.push(address);}}); return null;}
  await act(async () => {renderer = create(React.createElement(Harness)); await settle();});
  try {
    await act(async () => {state.startMonitoring(); await settle(); send({location: null}); await settle();});
    assert.deepEqual(calls, []);
    assert.equal(state.detection.location, null);
    assert.equal(state.detection.confidence, null);
    assert.deepEqual(state.detection.keywords, []);
    await act(async () => {send({location: "1234 Fixture Lane", confidence: 0, keywords: []}); await settle();});
    assert.deepEqual(calls, ["1234 Fixture Lane"]);
    assert.equal(state.detection.confidence, 0);
    assert.ok(state.logs.some(entry => entry.message === "Confidence: 0%"));
  } finally {await act(async () => renderer.unmount());}
});

test("radio heartbeat silence is bounded, while a user Stop remains quiet", async t => {
  const timers = pausedTimers(t, 30_000);
  const {useLiveMonitoring} = load("features/radio-monitor/hooks/useLiveMonitoring.ts", {stubs: {
    "@/shared/services/workspaceAccess": {brokerFetch: async () => new Response(new ReadableStream())},
    "@/shared/utils/logger": quiet,
  }});
  let state, renderer;
  function Harness() {state = useLiveMonitoring({onDetected: async () => {}}); return null;}
  await act(async () => {renderer = create(React.createElement(Harness)); await settle();});
  try {
    await act(async () => {state.startMonitoring(); await settle();});
    await act(async () => {timers.at(-1).callback(); await settle();});
    assert.equal(state.isMonitoring, false);
    assert.match(state.errorMessage, /stopped responding/);
    await act(async () => {state.startMonitoring(); await settle(); state.stopMonitoring(); await settle();});
    assert.equal(state.errorMessage, null);
  } finally {await act(async () => renderer.unmount());}
});

test("camera decode failure is visible and a fresh image recovers without retired image callbacks", async t => {
  const env = browser(), images = [], draws = [];
  const descriptor = Object.getOwnPropertyDescriptor(globalThis, "Image");
  globalThis.Image = class {constructor() {this.complete = true; this.naturalWidth = 800; this.naturalHeight = 600; images.push(this);}};
  t.after(() => {if (descriptor) Object.defineProperty(globalThis, "Image", descriptor); else delete globalThis.Image;});
  const {default: Viewer} = load("features/camera/components/CameraImageViewer.tsx", {stubs: {"@/shared/utils/logger": quiet}});
  const canvas = {parentElement: {clientWidth: 600}, getContext: () => ({drawImage: image => draws.push(image)})};
  let renderer;
  await act(async () => {renderer = create(React.createElement(Viewer, {imageUrl: "blob:broken", detection: null}),
    {createNodeMock: element => element.type === "canvas" ? canvas : null}); await settle();});
  try {
    await act(async () => {images[0].onerror(); await settle();});
    assert.equal(renderer.root.findAllByType("canvas").length, 0);
    assert.ok(JSON.stringify(renderer.toJSON()).includes("could not be decoded"));
    await act(async () => {renderer.update(React.createElement(Viewer, {imageUrl: "blob:fresh", detection: null})); await settle();});
    assert.equal(images[0].onload, null);
    assert.equal(images[0].onerror, null);
    await act(async () => {images.at(-1).onload(); await settle();});
    assert.equal(draws.length, 1);
    assert.equal(renderer.root.findAllByType("canvas").length, 1);
    assert.equal(env.requests.length, 0);
  } finally {await act(async () => renderer.unmount());}
});

function dashboard(stubs, extraStubs = {}) {
  return load("features/reports/components/ResponseDashboardPanel.tsx", {stubs: {
    "@/features/firetruck/components/TruckCard": {__esModule: true, default: () => null},
    "@/features/reports/ResponseRegistryContext": {useResponseRegistryContext: () => ({getKnownFires: () =>
      [{fireId: "fixture-fire", fireName: "Fixture fire", location: {lat: 39, lng: -121}}]})},
    "@/shared/components/WorkspacePanel": {__esModule: true, default: ({children}) => React.createElement("div", {}, children)},
    "@/shared/utils/logger": quiet, "./ResponseDashboardPanel.css": {},
    "@/features/firetruck/services/firetruckService": {checkHealth: async () => true, ...stubs},
    ...extraStubs,
  }}).default;
}
const savedRun = {session_id: "owned-run", websocket_url: "", fireId: "fixture-fire", fireName: "Fixture fire"};

test("temporary restoration retains owned IDs, blocks another dispatch and explicit saved-read retry recovers", async () => {
  const env = browser();
  env.storage.setItem("fireguard_firetruck_session", JSON.stringify(savedRun));
  env.storage.setItem("fireguard_firetruck_report", "owned-report");
  // Cross-load Error class instances are distinct; use real transport so the component owns its BrokerError class.
  let failed = true;
  env.route = url => url.endsWith("/workspace-config") ? json(local) : url.endsWith("/health") ? json({healthy: true})
    : failed ? json({error: {message: "Temporary outage", retryable: true}}, 503)
    : url.includes("/reports/") ? json({report_id: "owned-report", status: "completed"})
    : json({session_id: "owned-run", status: "stopped", trucks: []});
  const Panel = load("features/reports/components/ResponseDashboardPanel.tsx", {stubs: {
    "@/features/firetruck/components/TruckCard": {__esModule: true, default: () => null},
    "@/features/reports/ResponseRegistryContext": {useResponseRegistryContext: () => ({getKnownFires: () => []})},
    "@/shared/components/WorkspacePanel": {__esModule: true, default: ({children}) => React.createElement("div", {}, children)},
    "@/shared/utils/logger": quiet, "./ResponseDashboardPanel.css": {},
  }}).default;
  let renderer;
  await act(async () => {renderer = create(React.createElement(Panel, {isOpen: true, inResponseTracker: true, onClose() {}})); await settle();});
  try {
    assert.equal(env.storage.getItem("fireguard_firetruck_session"), JSON.stringify(savedRun));
    assert.equal(env.storage.getItem("fireguard_firetruck_report"), "owned-report");
    assert.equal(renderer.root.findAllByType("select").length, 0);
    failed = false;
    const retry = renderer.root.findAllByType("button").find(button => button.children.includes("Retry saved reads"));
    await act(async () => {retry.props.onClick(); await settle();});
    assert.ok(renderer.root.findAllByType("a").some(link => link.children.includes("Open report")));
  } finally {await act(async () => renderer.unmount());}
});

test("report polling waits for its current read and stops at a retained terminal result", async t => {
  const timers = pausedTimers(t, 2500), env = browser();
  env.storage.setItem("fireguard_firetruck_session", JSON.stringify(savedRun));
  env.storage.setItem("fireguard_firetruck_report", "owned-report");
  let reads = 0, release, renderer;
  const Panel = dashboard({
    getSimulationStatus: async () => ({session_id: "owned-run", status: "stopped", active: false}),
    getFiretruckReport: async () => {reads += 1; if (reads === 1) return {report_id: "owned-report", status: "running"};
      return new Promise(resolve => {release = resolve;});},
  });
  await act(async () => {renderer = create(React.createElement(Panel, {isOpen: true, inResponseTracker: true, onClose() {}})); await settle();});
  try {
    const scheduled = timers.filter(timer => !timer.cancelled);
    assert.equal(scheduled.length, 1);
    await act(async () => {scheduled[0].callback(); await settle();});
    assert.equal(reads, 2);
    assert.equal(timers.length, 1, "The pending read schedules no overlapping poll");
    await act(async () => {release({report_id: "owned-report", status: "completed"}); await settle();});
    assert.equal(timers.length, 1, "A terminal result schedules no new read");
    assert.ok(renderer.root.findAllByType("a").some(link => link.children.includes("Open report")));
  } finally {await act(async () => renderer.unmount());}
});

test("duplicate dispatch clicks share one stable intent and lost acknowledgement reconciles by GET", async t => {
  const env = browser(), starts = [], reconciles = [];
  window.setTimeout = globalThis.setTimeout; window.clearTimeout = globalThis.clearTimeout;
  const descriptor = Object.getOwnPropertyDescriptor(globalThis, "WebSocket");
  globalThis.WebSocket = class {close() {}};
  t.after(() => {if (descriptor) Object.defineProperty(globalThis, "WebSocket", descriptor); else delete globalThis.WebSocket;});
  let rejectStart, renderer;
  const Panel = dashboard({
    startSimulation: (config, requestId) => {starts.push({config, requestId}); return new Promise((_resolve, reject) => {rejectStart = reject;});},
    getSimulationRequest: async requestId => {reconciles.push(requestId); return {request_id: requestId, session_id: "accepted-run", status: "starting"};},
  });
  await act(async () => {renderer = create(React.createElement(Panel, {isOpen: true, inResponseTracker: true, onClose() {}})); await settle();});
  try {
    await act(async () => renderer.root.findByType("select").props.onChange({target: {value: "fixture-fire"}}));
    const dispatch = renderer.root.findAllByType("button").find(button => button.props.className === "rdp-dispatch-btn");
    await act(async () => {void dispatch.props.onClick(); void dispatch.props.onClick(); await settle();});
    assert.equal(starts.length, 1);
    const retained = JSON.parse(env.storage.getItem("fireguard_firetruck_dispatch"));
    assert.equal(retained.requestId, starts[0].requestId);
    await act(async () => {rejectStart(new TypeError("Lost acknowledgement")); await settle();});
    assert.deepEqual(reconciles, [starts[0].requestId]);
    assert.equal(JSON.parse(env.storage.getItem("fireguard_firetruck_session")).session_id, "accepted-run");
    assert.equal(env.storage.getItem("fireguard_firetruck_dispatch"), null);
  } finally {await act(async () => renderer.unmount());}
});

test("a truncated investigation stream rejoins its retained snapshot without a second POST", async () => {
  let intent;
  const env = browser(`${origin}/`, (url, init) => {
    if (url.endsWith("/workspace-config")) return json(local);
    if (init.method === "POST") {intent = JSON.parse(init.body); return new Response(streamOf('data: {"type":"RUN_STARTED"}\n\n'));}
    assert.equal(JSON.parse(env.storage.getItem("fireguard_pending_investigation")).acknowledged,true,
      "Successful POST headers are persisted before stream recovery reads");
    return json({request_id: intent.request_id, thread_id: intent.thread_id, status: "completed", snapshot, error: null});
  });
  const {invokeAgentStreaming} = load("shared/services/AgentService.ts", {stubs: {"@/shared/utils/logger": quiet}});
  const outcome = await within(new Promise(resolve => invokeAgentStreaming("Fixture question", {
    onComplete: answer => resolve({answer}), onError: error => resolve({error}),
  })), 1000, "Retained investigation answer");
  assert.equal(outcome.error, undefined);
  assert.equal(outcome.answer.response, "Retained answer.");
  assert.equal(env.requests.filter(request => request.init.method === "POST").length, 1);
  assert.equal(env.requests.filter(request => request.url.endsWith(intent.request_id)).length, 1);
  assert.equal(env.storage.getItem("fireguard_pending_investigation"), null);
});

test("a definitive investigation RUN_ERROR remains visible and never enters receipt recovery", async () => {
  const message = "Investigation could not complete. Please retry.";
  const code = "INVESTIGATION_FAILED";
  let cancelled = false;
  const env = browser(`${origin}/`, (url, init) => {
    if (url.endsWith("/workspace-config")) return json(local);
    if (init.method === "POST") return new Response(new ReadableStream({
      start(controller) {controller.enqueue(new TextEncoder().encode(`data: {"type":"RUN_STARTED"}\n\n`
        + `data: ${JSON.stringify({type: "RUN_ERROR", message, code})}\n\n`));},
      cancel() {cancelled = true;},
    }));
    return json({error: {code: "NOT_FOUND", message: "Unknown retained turn"}}, 404);
  });
  const {invokeAgentStreaming} = load("shared/services/AgentService.ts", {stubs: {"@/shared/utils/logger": quiet}});
  const outcome = await within(new Promise(resolve => invokeAgentStreaming("Fixture question", {
    onComplete: answer => resolve({answer}), onError: error => resolve({error}),
  })), 1000, "Definitive investigation failure");
  assert.equal(outcome.answer, undefined);
  assert.equal(outcome.error.message, message);
  assert.equal(outcome.error.code, code);
  assert.equal(cancelled,true,"A definitive terminal failure settles and cancels a connection that remains open");
  assert.equal(env.requests.filter(request => request.init.method === "POST").length, 1);
  assert.equal(env.requests.filter(request => request.url.includes("/investigation/turns/")).length, 0);
  assert.equal(env.storage.getItem("fireguard_pending_investigation"), null);
});

test("reload recovery reads the same accepted investigation and never submits the saved question again", async () => {
  const intent = {requestId: uuid, threadId: "fixture-thread", question: "Previously accepted question"};
  const env = browser(`${origin}/`, url => url.endsWith("/workspace-config") ? json(local)
    : json({request_id: uuid, thread_id: intent.threadId, status: "completed", snapshot, error: null}));
  env.storage.setItem("fireguard_pending_investigation", JSON.stringify(intent));
  const {resumeAgentInvestigation} = load("shared/services/AgentService.ts", {stubs: {"@/shared/utils/logger": quiet}});
  const outcome = await within(new Promise(resolve => resumeAgentInvestigation(intent, {
    onComplete: answer => resolve({answer}), onError: error => resolve({error}),
  })), 1000, "Reloaded investigation answer");
  assert.equal(outcome.error, undefined);
  assert.equal(outcome.answer.response, "Retained answer.");
  assert.equal(env.requests.some(request => request.init.method === "POST"), false);
  assert.equal(env.storage.getItem("fireguard_pending_investigation"), null);
});

test("a restored chat reopens its read after StrictMode effect replay without duplicating the saved question", async () => {
  const intent = {requestId: uuid, threadId: "fixture-thread", question: "Previously accepted question"};
  const env = browser(), reads = [];
  env.storage.setItem("fireguard_pending_investigation", JSON.stringify(intent));
  const {useAgentSubmission} = load("features/layout/hooks/useAgentSubmission.ts", {stubs: {
    react: {...React, useEffect: (setup, deps) => React.useEffect(() => {
      const cleanup = setup(); cleanup?.(); return setup();
    }, deps)},
    "@/shared/services/AgentService": {
      invokeAgentStreaming() {throw new Error("A restored question must never POST");},
      resumeAgentInvestigation: (accepted, callbacks) => {reads.push({accepted, callbacks});
        return {cancel: () => queueMicrotask(() => callbacks.onError(new Error("Retired read cancelled")))};},
    },
    "@/features/reports/ResponseRegistryContext": {useResponseRegistryContext: () => ({registerFires() {}})},
    "@/shared/utils/logger": quiet,
  }});
  let state, renderer;
  function Harness() {state = useAgentSubmission({onEvidence() {}}); return null;}
  await act(async () => {renderer = create(React.createElement(Harness)); await settle();});
  try {
    assert.equal(reads.length, 2);
    assert.deepEqual(reads.map(read => read.accepted.requestId), [uuid, uuid]);
    assert.equal(state.messages.filter(message => message.sender === "user").length, 1);
    await act(async () => {reads[1].callbacks.onComplete({response: "Retained answer.", locations: []}); await settle();});
    assert.equal(state.isLoading, false);
    assert.equal(state.messages.some(message => message.sender === "system"), false);
  } finally {await act(async () => renderer.unmount());}
});

test("manual camera refresh aborts on deactivation and a late snapshot is revoked", async () => {
  browser();
  const requests = [], revoked = [];
  let state, renderer;
  const {useCameraData} = load("features/camera/hooks/useCameraData.ts", {stubs: {
    "@/shared/utils/logger": quiet, "../services/cameraBroker": {
      brokerCameraSnapshot: signal => new Promise(resolve => requests.push({signal, resolve})),
      revokeImageUrl: url => revoked.push(url),
    },
  }});
  function Harness({active}) {state = useCameraData(active); return null;}
  await act(async () => {renderer = create(React.createElement(Harness, {active: true})); await settle();});
  try {
    await act(async () => {requests[0].resolve({imageUrl: "blob:first", deviceId: "camera", sensorAlias: "Camera", detection: {}});
      await settle();});
    const refresh = state.refresh();
    assert.equal(requests.length, 2);
    await act(async () => {renderer.update(React.createElement(Harness, {active: false})); await settle();});
    assert.equal(requests[1].signal.aborted, true);
    await act(async () => {requests[1].resolve({imageUrl: "blob:late", detection: {}}); await refresh; await settle();});
    assert.equal(state.snapshot, null);
    assert.equal(state.isLoading, false);
    assert.deepEqual(revoked, ["blob:first", "blob:late"]);
  } finally {await act(async () => renderer.unmount());}
});

test("health can be rechecked and a retired health response cannot overwrite the newer result", async () => {
  browser();
  let calls = 0, resolveOld, renderer;
  const Panel = dashboard({checkHealth: () => {calls += 1;
    return calls === 1 ? new Promise(resolve => {resolveOld = resolve;}) : Promise.resolve(calls > 2);}});
  const props = {isOpen: true, inResponseTracker: true, onClose() {}};
  await act(async () => {renderer = create(React.createElement(Panel, props)); await settle();});
  try {
    await act(async () => {renderer.update(React.createElement(Panel, {...props, isOpen: false})); await settle();});
    const recheck = renderer.root.findAllByType("button").find(button => button.children.includes("Recheck firetruck service"));
    await act(async () => {recheck.props.onClick(); await settle();});
    assert.equal(calls, 3);
    await act(async () => {resolveOld(false); await settle();});
    assert.ok(JSON.stringify(renderer.toJSON()).includes("Connected"));
    assert.equal(renderer.root.findAllByType("button").some(button => button.children.includes("Recheck firetruck service")), false);
  } finally {await act(async () => renderer.unmount());}
});

test("an invalid saved dispatch does not discard valid independent report evidence", async () => {
  const env = browser();
  env.storage.setItem("fireguard_firetruck_dispatch", "{bad json");
  env.storage.setItem("fireguard_firetruck_session", JSON.stringify(savedRun));
  env.storage.setItem("fireguard_firetruck_report", "owned-report");
  const reads = [];
  const Panel = dashboard({
    getSimulationStatus: async () => ({session_id: "owned-run", status: "stopped", active: false}),
    getFiretruckReport: async id => {reads.push(id); return {report_id: id, status: "completed"};},
  });
  let renderer;
  await act(async () => {renderer = create(React.createElement(Panel,
    {isOpen: true, inResponseTracker: true, onClose() {}})); await settle();});
  try {
    assert.equal(env.storage.getItem("fireguard_firetruck_dispatch"), null);
    assert.deepEqual(reads, ["owned-report"]);
    assert.ok(renderer.root.findAllByType("a").some(link => link.children.includes("Open report")));
  } finally {await act(async () => renderer.unmount());}
});

test("report duplicate clicks retain one request and a lost acknowledgement reconciles without another POST", async () => {
  const env = browser(), starts = [], receipts = [];
  env.storage.setItem("fireguard_firetruck_session", JSON.stringify(savedRun));
  let rejectStart, renderer;
  const Panel = dashboard({
    getSimulationStatus: async () => ({session_id: "owned-run", status: "stopped", active: false}),
    generateFiretruckReport: (runId, question, requestId) => {starts.push({runId, question, requestId});
      return new Promise((_resolve, reject) => {rejectStart = reject;});},
    getFiretruckReportRequest: async (runId, requestId) => {receipts.push({runId, requestId});
      return {report_id: "accepted-report", status: "completed"};},
  });
  await act(async () => {renderer = create(React.createElement(Panel,
    {isOpen: true, inResponseTracker: true, onClose() {}})); await settle();});
  try {
    const button = renderer.root.findAllByType("button").find(button => button.children.includes("Generate report"));
    await act(async () => {void button.props.onClick(); void button.props.onClick(); await settle();});
    assert.equal(starts.length, 1);
    assert.equal(JSON.parse(env.storage.getItem("fireguard_firetruck_report_request")).requestId, starts[0].requestId);
    await act(async () => {rejectStart(new TypeError("Lost report response")); await settle();});
    assert.deepEqual(receipts, [{runId: "owned-run", requestId: starts[0].requestId}]);
    assert.equal(env.storage.getItem("fireguard_firetruck_report"), "accepted-report");
    assert.equal(env.storage.getItem("fireguard_firetruck_report_request"), null);
    assert.equal(starts.length, 1);
  } finally {await act(async () => renderer.unmount());}
});

test("definitively missing saved report receipt permits an explicit same-ID retry", async () => {
  const env = browser(), starts = [];
  const intent = {runId: "owned-run", requestId: uuid, question: "Retained question"};
  env.storage.setItem("fireguard_firetruck_session", JSON.stringify(savedRun));
  env.storage.setItem("fireguard_firetruck_report_request", JSON.stringify(intent));
  class Missing extends Error {httpStatus = 404;}
  const Panel = dashboard({
    getSimulationStatus: async () => ({session_id: "owned-run", status: "stopped", active: false}),
    getFiretruckReportRequest: async () => {throw new Missing("No accepted receipt");},
    generateFiretruckReport: async (runId, question, requestId) => {starts.push({runId, question, requestId});
      return {report_id: "accepted-report", status: "completed"};},
  }, {"@/shared/services/workspaceAccess": {BrokerError: Missing, isTransientReadError: () => false}});
  let renderer;
  await act(async () => {renderer = create(React.createElement(Panel,
    {isOpen: true, inResponseTracker: true, onClose() {}})); await settle();});
  try {
    assert.deepEqual(starts, []);
    const button = renderer.root.findAllByType("button").find(button => button.children.includes("Retry same report request"));
    assert.equal(button.props.disabled, false);
    await act(async () => {await button.props.onClick(); await settle();});
    assert.deepEqual(starts, [intent]);
  } finally {await act(async () => renderer.unmount());}
});

test("investigation accepted before lost response headers recovers its receipt using GET only", async () => {
  let intent;
  const env = browser(`${origin}/`, (url, init) => {
    if (url.endsWith("/workspace-config")) return json(local);
    if (init.method === "POST") {intent = JSON.parse(init.body); throw new TypeError("Connection lost before headers");}
    return json({request_id: intent.request_id, thread_id: intent.thread_id, status: "completed", snapshot, error: null});
  });
  const {invokeAgentStreaming} = load("shared/services/AgentService.ts", {stubs: {"@/shared/utils/logger": quiet}});
  const outcome = await within(new Promise(resolve => invokeAgentStreaming("Fixture question", {
    onComplete: answer => resolve({answer}), onError: error => resolve({error}),
  })), 1000, "Accepted investigation after lost headers");
  assert.equal(outcome.error, undefined);
  assert.equal(outcome.answer.response, "Retained answer.");
  assert.equal(env.requests.filter(request => request.init.method === "POST").length, 1);
  assert.equal(env.requests.filter(request => request.url.endsWith(intent.request_id)).length, 1);
});

test("unavailable investigation reads stop after a bounded budget and preserve the accepted intent for explicit recovery", async t => {
  const timers = pausedTimers(t, 2500), intent = {requestId: uuid, threadId: "fixture-thread", question: "Retained question"};
  const env = browser(`${origin}/`, url => url.endsWith("/workspace-config") ? json(local)
    : json({error: {message: "Temporary read outage", retryable: true}}, 503));
  env.storage.setItem("fireguard_pending_investigation", JSON.stringify(intent));
  const {resumeAgentInvestigation} = load("shared/services/AgentService.ts", {stubs: {"@/shared/utils/logger": quiet}});
  let failure;
  const handle = resumeAgentInvestigation(intent, {onError: error => {failure = error;}});
  try {
    await settle();
    for (let index = 0; index < 4; index += 1) {
      assert.equal(timers.length, index + 1);
      timers[index].callback(); await settle();
    }
    assert.match(failure.message, /Temporary read outage/);
    assert.equal(env.requests.filter(request => request.url.endsWith(uuid)).length, 5);
    assert.equal(timers.length, 4);
    assert.equal(env.storage.getItem("fireguard_pending_investigation"), JSON.stringify(intent));
    assert.equal(env.requests.some(request => request.init.method === "POST"), false);
  } finally {handle.cancel();}
});

test("lost investigation headers tolerate an initial missing receipt without losing the paid request UUID", async t => {
  const timers = pausedTimers(t,2500);
  let posted, reads = 0;
  const env = browser(`${origin}/`, (url,init) => {
    if (url.endsWith("/workspace-config")) return json(local);
    if (init.method === "POST") {
      posted = JSON.parse(init.body);
      throw new TypeError("Connection lost while acceptance was committing");
    }
    if (++reads === 1) return json({error:{code:"NOT_FOUND",message:"Turn not found",retryable:false}},404);
    return json({request_id:posted.request_id,thread_id:posted.thread_id,status:"completed",snapshot,error:null});
  });
  const {invokeAgentStreaming} = load("shared/services/AgentService.ts",{stubs:{"@/shared/utils/logger":quiet}});
  const result = new Promise(resolve => invokeAgentStreaming("Fixture question",{
    onComplete:answer => resolve({answer}),onError:error => resolve({error}),
  }));
  await settle();
  const retained = JSON.parse(env.storage.getItem("fireguard_pending_investigation"));
  assert.equal(retained.requestId,posted.request_id);
  assert.equal(retained.threadId,posted.thread_id);
  assert.notEqual(retained.acknowledged,true);
  assert.equal(timers.length,1);
  timers[0].callback();
  const outcome = await within(result,1000,"Late investigation acceptance");
  assert.equal(outcome.error,undefined);
  assert.equal(outcome.answer.response,"Retained answer.");
  assert.equal(env.requests.filter(request => request.init.method === "POST").length,1);
  assert.deepEqual(env.requests.filter(request => request.url.includes("/investigation/turns/"))
    .map(request => request.url),Array(2).fill(`/simulation-api/v1/investigation/turns/${posted.request_id}`));
  assert.equal(env.storage.getItem("fireguard_pending_investigation"),null);
});

test("reload of a legacy unacknowledged investigation reconciles missing-then-completed status using GET only", async t => {
  const timers = pausedTimers(t,2500);
  const intent = {requestId:uuid,threadId:"fixture-thread",question:"Unconfirmed saved question"};
  let reads = 0;
  const env = browser(`${origin}/`,url => url.endsWith("/workspace-config") ? json(local)
    : ++reads === 1 ? json({error:{code:"NOT_FOUND",message:"Turn not found",retryable:false}},404)
      : json({request_id:uuid,thread_id:intent.threadId,status:"completed",snapshot,error:null}));
  env.storage.setItem("fireguard_pending_investigation",JSON.stringify(intent));
  const {resumeAgentInvestigation} = load("shared/services/AgentService.ts",{stubs:{"@/shared/utils/logger":quiet}});
  const result = new Promise(resolve => resumeAgentInvestigation(intent,{
    onComplete:answer => resolve({answer}),onError:error => resolve({error}),
  }));
  await settle();
  assert.equal(env.storage.getItem("fireguard_pending_investigation"),JSON.stringify(intent));
  assert.equal(timers.length,1);
  timers[0].callback();
  const outcome = await within(result,1000,"Reloaded unknown acceptance");
  assert.equal(outcome.error,undefined);
  assert.equal(outcome.answer.response,"Retained answer.");
  assert.equal(env.requests.some(request => request.init.method === "POST"),false);
  assert.deepEqual(env.requests.filter(request => request.url.endsWith(uuid)).map(request => request.url),
    Array(2).fill(`/simulation-api/v1/investigation/turns/${uuid}`));
  assert.equal(env.storage.getItem("fireguard_pending_investigation"),null);
});

test("five missing unacknowledged receipt reads stop while manual same-ID recovery can find a late commit", async t => {
  const timers = pausedTimers(t,2500);
  const intent = {requestId:uuid,threadId:"fixture-thread",question:"Unconfirmed saved question"};
  let committed = false, failure;
  const env = browser(`${origin}/`,url => url.endsWith("/workspace-config") ? json(local)
    : committed ? json({request_id:uuid,thread_id:intent.threadId,status:"completed",snapshot,error:null})
      : json({error:{code:"NOT_FOUND",message:"Turn not found",retryable:false}},404));
  env.storage.setItem("fireguard_pending_investigation",JSON.stringify(intent));
  const {resumeAgentInvestigation} = load("shared/services/AgentService.ts",{stubs:{"@/shared/utils/logger":quiet}});
  const handle = resumeAgentInvestigation(intent,{onError:error => {failure = error;}});
  try {
    await settle();
    for (let index = 0; index < 4; index += 1) {
      assert.equal(timers.length,index+1);
      timers[index].callback(); await settle();
    }
    assert.match(failure.message,/acceptance is still unconfirmed.*same saved request/i);
    assert.equal(env.requests.filter(request => request.url.endsWith(uuid)).length,5);
    assert.equal(timers.length,4);
    assert.equal(env.storage.getItem("fireguard_pending_investigation"),JSON.stringify(intent));
    committed = true;
    const outcome = await within(new Promise(resolve => resumeAgentInvestigation(
      JSON.parse(env.storage.getItem("fireguard_pending_investigation")),{
        onComplete:answer => resolve({answer}),onError:error => resolve({error}),
      })),1000,"Manual same-request recovery after late commit");
    assert.equal(outcome.error,undefined);
    assert.equal(outcome.answer.response,"Retained answer.");
    assert.equal(env.requests.filter(request => request.url.endsWith(uuid)).length,6);
    assert.equal(env.requests.some(request => request.init.method === "POST"),false);
    assert.equal(env.storage.getItem("fireguard_pending_investigation"),null);
  } finally {handle.cancel();}
});

test("a definitively missing or expired acknowledged investigation clears its retained handle", async () => {
  for (const status of [404,410]) {
    const intent = {requestId:uuid,threadId:"fixture-thread",question:"Known accepted question",acknowledged:true};
    const env = browser(`${origin}/`,url => url.endsWith("/workspace-config") ? json(local)
      : json({error:{code:status === 404 ? "NOT_FOUND" : "EXPIRED",message:"Accepted turn expired",retryable:false}},status));
    env.storage.setItem("fireguard_pending_investigation",JSON.stringify(intent));
    const {resumeAgentInvestigation} = load("shared/services/AgentService.ts",{stubs:{"@/shared/utils/logger":quiet}});
    const outcome = await within(new Promise(resolve => resumeAgentInvestigation(intent,{
      onComplete:answer => resolve({answer}),onError:error => resolve({error}),
    })),1000,"Definitively expired accepted turn");
    assert.equal(outcome.answer,undefined);
    assert.equal(outcome.error.message,"Accepted turn expired");
    assert.equal(env.requests.filter(request => request.url.endsWith(uuid)).length,1);
    assert.equal(env.requests.some(request => request.init.method === "POST"),false);
    assert.equal(env.storage.getItem("fireguard_pending_investigation"),null);
  }
});

test("a late investigation acknowledgement cannot replace a newer retained request", () => {
  const env = browser();
  const {acknowledgeInvestigation,retainInvestigation} = load("shared/services/investigationRecovery.ts");
  const older = {requestId:uuid,threadId:"old-thread",question:"Old question"};
  const newer = {requestId:crypto.randomUUID(),threadId:"new-thread",question:"New question"};
  retainInvestigation(newer);
  acknowledgeInvestigation(older);
  assert.equal(older.acknowledged,true);
  assert.equal(env.storage.getItem("fireguard_pending_investigation"),JSON.stringify(newer));
});
