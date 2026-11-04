/* Traffic Lab and broker requests, demo sessions and research jobs, checked in Node without a browser, and the
 * broker as the only path to the agent, radio, firetruck and camera services. There is no user sign-in: deployed
 * visitors use the broker's demo-session cookie, local development serves one researcher.
 * Run: npm --prefix ui run test:metrics */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { load } = require("../ts-module.cjs");
const { browser, brokerOr, hang, json, origin, publicDemo } = require("./support/browser.cjs");
const React = require("react");
const { act, create } = require("react-test-renderer");

const sessionRequests = env => env.requests.filter(r => r.url === "/simulation-api/v1/demo/session");

const TIMEOUT_MESSAGE = "The simulation service did not respond within 1 second. Try again.";
const src = path.resolve(__dirname, "../../src");
const source = relative => fs.readFileSync(path.join(src, relative), "utf8");
// The Vite variables ui/src may read: two build flags and two ArcGIS Living Atlas data service URLs. Vite's
// built-ins (import.meta.env.DEV, PROD, MODE, BASE_URL, SSR) have no VITE_ prefix, so the scan does not see them.
const PUBLIC_VITE_NAMES = ["VITE_DEBUG", "VITE_EVACUATION_ENABLED", "VITE_POPULATION_SERVICE_URL",
  "VITE_VEGETATION_SERVICE_URL"];

test("ui/src reads no Vite variable other than the public build flags and ArcGIS data service URLs", () => {
  const files = fs.readdirSync(src, { recursive: true }).filter(file => /\.[cm]?[jt]sx?$/.test(file));
  assert.ok(files.includes("vite-env.d.ts"), "The scan covers the Vite variable declarations");
  const others = files.flatMap(file => (source(file).match(/\bVITE_\w+/g) ?? [])
    .filter(name => !PUBLIC_VITE_NAMES.includes(name)).map(name => `${file}: ${name}`));
  assert.deepEqual(others, [],
    "Radio, firetruck and camera data reach the browser only through /simulation-api/v1");
});

test("the camera broker modules read no Vite variable and no import.meta", () => {
  for (const file of ["features/camera/services/cameraBroker.ts", "features/camera/services/cameraRecords.ts"]) {
    assert.doesNotMatch(source(file), /VITE_|import\.meta/, file);
  }
});

test("research() sends the method and JSON body with the session cookie and no bearer token", async () => {
  const env = browser(`${origin}/`, brokerOr(() => json({ id: "job-1" }, 202)));
  const api = load("features/traffic-lab/services/api.ts");
  assert.deepEqual(await api.research("/jobs", { vehicles: 40 }), { id: "job-1" });
  const post = env.requests.at(-1);
  assert.equal(post.url, "/simulation-api/v1/research/jobs");
  assert.equal(post.init.method, "POST");
  assert.equal(post.init.body, JSON.stringify({ vehicles: 40 }));
  assert.equal(post.init.credentials, "same-origin");
  assert.equal("Authorization" in post.init.headers, false, "There is no user token to send");
  assert.ok(post.init.signal instanceof AbortSignal, "Every request is bounded by a signal");
  assert.equal(sessionRequests(env).length, 0, "Local development needs no demo session");
  await api.research("/config");
  const get = env.requests.at(-1);
  assert.equal(get.init.method, "GET");
  assert.equal(get.init.body, undefined);
});

test("research() reports the server's JSON error message, or the HTTP status without one", async () => {
  let reply = json({ error: { message: "Caltrans highway discovery is unavailable. Retry later." } }, 502);
  browser(`${origin}/`, brokerOr(() => reply));
  const api = load("features/traffic-lab/services/api.ts");
  await assert.rejects(api.research("/discover", { lat: 39.76, lon: -121.62, radius_km: 8 }),
    { message: "Caltrans highway discovery is unavailable. Retry later." });
  reply = new Response("upstream timeout", { status: 504 });
  await assert.rejects(api.research("/config"), { message: "Simulation request failed (504)." });
});

test("a public demo starts its session before the first request, and a 401 renews it and reloads", async () => {
  let status = 200;
  const env = browser(`${origin}/`, brokerOr(url => (url.endsWith("/demo/session")
    ? json({ ready: true, access_mode: "public_demo", session_created: true })
    : json({ id: "job-1" }, status)), publicDemo));
  env.storage.setItem("fireguard_simulation_session", "earlier-session");
  const api = load("features/traffic-lab/services/api.ts");
  assert.deepEqual(await api.research("/jobs/job-1"), { id: "job-1" });
  assert.equal(sessionRequests(env).length, 1);
  assert.equal(sessionRequests(env)[0].init.method, "POST");
  assert.equal(sessionRequests(env)[0].init.credentials, "same-origin");
  assert.ok(env.requests.indexOf(sessionRequests(env)[0]) < env.requests.length - 1, "Session before the request");
  assert.equal(env.storage.getItem("fireguard_simulation_session"), null, "A new demo session forgets the old one");
  status = 401;
  env.storage.setItem("fireguard_simulation_session", "saved-session");
  await assert.rejects(api.research("/jobs/job-1"), { message: "Demo session refreshed. Reloading the workspace." });
  assert.equal(sessionRequests(env).length, 2);
  assert.equal(env.reloads, 1);
  assert.equal(env.storage.getItem("fireguard_simulation_session"), null);
});

test("a demo session the browser cannot keep renews once, then stops with an error instead of reloading again", async () => {
  let status = 401;
  const env = browser(`${origin}/`, brokerOr(url => (url.endsWith("/demo/session")
    ? json({ ready: true, access_mode: "public_demo", session_created: true })
    : json({ id: "job-1" }, status)), publicDemo));
  let api = load("features/traffic-lab/services/api.ts");
  const together = await Promise.allSettled([api.research("/jobs/job-1"), api.research("/config")]);
  assert.deepEqual(together.map(outcome => outcome.reason?.message),
    Array(2).fill("Demo session refreshed. Reloading the workspace."));
  assert.equal(env.reloads, 1, "Requests that fail together share one renewal and one reload");
  assert.equal(sessionRequests(env).length, 2, "The page's session start plus one renewal");
  api = load("features/traffic-lab/services/api.ts"); // The reloaded page: fresh modules, the same tab storage.
  await assert.rejects(api.research("/jobs/job-1"),
    { message: "The demo session could not be renewed. Allow cookies for this site, then reload the page." });
  assert.equal(env.reloads, 1, "A second 401 before any success does not reload again");
  assert.equal(sessionRequests(env).length, 3, "Only the reloaded page's own session start");
  status = 200;
  api = load("features/traffic-lab/services/api.ts"); // The visitor allows cookies and reloads by hand.
  assert.deepEqual(await api.research("/jobs/job-1"), { id: "job-1" });
  status = 401;
  await assert.rejects(api.research("/jobs/job-1"), { message: "Demo session refreshed. Reloading the workspace." });
  assert.equal(env.reloads, 2, "A successful request allows one renewal again");
});

test("a closed workspace stops with a readable message, creates no session and retries next time", async () => {
  const env = browser(`${origin}/`, brokerOr(() => json({ id: "job-1" }), { access_mode: "closed" }));
  const access = load("shared/services/workspaceAccess.ts");
  for (let attempt = 0; attempt < 2; attempt += 1) {
    await assert.rejects(access.initialize(), { message: "This workspace is not open to visitors yet." });
  }
  assert.equal(sessionRequests(env).length, 0);
  assert.equal(env.requests.filter(r => r.url.endsWith("/workspace-config")).length, 2, "A failed start is not cached");
});

test("a broker request that outlives its timeout is cancelled then and fails with a readable message", async () => {
  browser(`${origin}/`, brokerOr(hang));
  const { brokerJson } = load("shared/services/workspaceAccess.ts");
  const started = Date.now();
  await assert.rejects(brokerJson("/runs/run-1", { timeoutMs: 20 }), { message: TIMEOUT_MESSAGE });
  assert.ok(Date.now() - started < 2000, "The 20 ms timeout cancels the request itself");
});

test("aborting the caller's signal cancels the request with an AbortError, not a timeout message", async () => {
  browser(`${origin}/`, brokerOr(hang));
  const { brokerJson } = load("shared/services/workspaceAccess.ts");
  const controller = new AbortController();
  const pending = brokerJson("/runs/run-1", { signal: controller.signal });
  controller.abort();
  await assert.rejects(pending, { name: "AbortError" });
});

test("api() and research() pass their method, caller signal and timeout to the shared broker helper", async () => {
  const env = browser(`${origin}/`, brokerOr((url, init) =>
    (url.includes("/sessions/") ? json({ session_id: "session-1" }) : hang(url, init))));
  const evacuation = load("features/evacuation/services/api.ts");
  const lab = load("features/traffic-lab/services/api.ts");
  assert.deepEqual(await evacuation.api("/sessions/session-1", { replay_time_s: 30 }, { method: "PATCH" }),
    { session_id: "session-1" });
  assert.equal(env.requests.at(-1).init.method, "PATCH");
  assert.equal(env.requests.at(-1).init.body, JSON.stringify({ replay_time_s: 30 }));
  const controller = new AbortController();
  const pending = evacuation.api("/runs/run-1", undefined, { signal: controller.signal });
  controller.abort();
  await assert.rejects(pending, { name: "AbortError" });
  await assert.rejects(evacuation.api("/runs/run-1", undefined, { timeoutMs: 20 }), { message: TIMEOUT_MESSAGE });
  await assert.rejects(lab.research("/jobs/run-1", undefined, { timeoutMs: 20 }), { message: TIMEOUT_MESSAGE });
  assert.deepEqual(env.requests.map(r => r.url).filter(url => !url.endsWith("/workspace-config")),
    ["/simulation-api/v1/sessions/session-1", "/simulation-api/v1/runs/run-1",
      "/simulation-api/v1/research/jobs/run-1"]);
});

test("the AbortSignal.any fallback aborts with whichever input aborts first", () => {
  browser();
  const access = load("shared/services/workspaceAccess.ts");
  const native = AbortSignal.any;
  Object.defineProperty(AbortSignal, "any", { value: undefined, configurable: true, writable: true });
  try {
    const first = new AbortController(), second = new AbortController();
    const combined = access.anySignal([first.signal, second.signal]);
    assert.equal(combined.aborted, false);
    second.abort(new Error("second"));
    assert.equal(combined.aborted, true);
    assert.equal(combined.reason.message, "second");
    const early = new AbortController();
    early.abort(new Error("early"));
    assert.equal(access.anySignal([early.signal, first.signal]).reason.message, "early");
  } finally {
    Object.defineProperty(AbortSignal, "any", { value: native, configurable: true, writable: true });
  }
});

test("a late 'running' poll cannot undo 'cancelling', and only active runs are polled", () => {
  browser();
  const { newerJob } = load("features/traffic-lab/hooks/useResearchJob.ts");
  const { clock, isActiveStatus } = load("features/evacuation/components/replay.ts");
  const job = (status, id = "run-1") => ({ id, status, phase: status, progress: 0.5, created: 1, scenario: {} });
  assert.equal(newerJob(job("cancelling"), job("running")).status, "cancelling");
  assert.equal(newerJob(job("cancelling"), job("queued")).status, "cancelling");
  assert.equal(newerJob(job("running"), job("cancelling")).status, "cancelling");
  assert.equal(newerJob(job("cancelling"), job("cancelled")).status, "cancelled");
  assert.equal(newerJob(job("running"), job("completed")).status, "completed");
  assert.equal(newerJob(null, job("running")).status, "running");
  assert.equal(newerJob(job("cancelling", "run-2"), job("running")).status, "running", "Different runs are not compared");
  assert.deepEqual(["queued", "running", "cancelling", "cancelled", "completed", "failed", null, undefined]
    .map(status => isActiveStatus(status)), [true, true, true, false, false, false, false, false]);
  assert.deepEqual([0, 59.9, 61.9, 600].map(seconds => clock(seconds)), ["0:00", "0:59", "1:01", "10:00"]);
});

test("Traffic Lab retries failed initialization and shows saved failures when history is selected again", async () => {
  let unavailable = true, state, renderer, starts = 0;
  const failed = { id: "failed-run", status: "failed", progress: 0, phase: "Failed",
    error: "Saved provider failure", created: 1, scenario: { highway: "State 1" } };
  const env = browser(`${origin}/`, brokerOr(url => {
    if (url.endsWith("/config")) {
      if (unavailable) throw new Error("Configuration unavailable");
      return json({ traffic_key_configured: true, deployment: "fixture", purpose: "test" });
    }
    return json(url.endsWith("/jobs") ? [failed] : failed);
  }));
  Object.assign(env.events, { clearTimeout, setTimeout });
  const { useResearchJob } = load("features/traffic-lab/hooks/useResearchJob.ts");
  function Harness() {
    state = useResearchJob({ visible: false, loadStart: async () => { starts += 1; }, onLoaded: () => {} });
    return null;
  }
  const settle = () => new Promise(resolve => setImmediate(resolve));
  try {
    await act(async () => { renderer = create(React.createElement(Harness)); await settle(); });
    assert.equal(state.access, "failed");
    assert.equal(state.error, "Configuration unavailable");
    unavailable = false;
    await act(async () => { state.reload(); await settle(); });
    assert.equal(state.access, "ready");
    assert.equal(starts, 1);
    for (let selection = 0; selection < 2; selection += 1) {
      await act(async () => { await state.select(failed.id); await settle(); });
      assert.equal(state.error, "Saved provider failure");
      assert.equal(state.running, false);
    }
  } finally {
    await act(async () => renderer?.unmount());
  }
});

test("another research scenario and history selection leave active server jobs running", async () => {
  const jobs = [], cancellations = [];
  let state, renderer;
  const env = browser(`${origin}/`, brokerOr((url, init) => {
    if (url.endsWith("/config")) return json({ deployment: "AWS", traffic_key_configured: true });
    if (url.endsWith("/jobs") && init.method === "POST") {
      const job = { id: `run-${jobs.length + 1}`, status: "running", phase: "Simulating",
        scenario: JSON.parse(init.body), progress: 0.2 };
      jobs.unshift(job);
      return json(job, 202);
    }
    if (url.endsWith("/jobs")) return json(jobs);
    if (url.endsWith("/cancel")) cancellations.push(url);
    return json(jobs.find(job => url.endsWith(`/${job.id}`)));
  }));
  Object.assign(env.events, { clearTimeout, setTimeout });
  const { useResearchJob } = load("features/traffic-lab/hooks/useResearchJob.ts");
  function Harness() {
    state = useResearchJob({ visible: false, loadStart: async () => {}, onLoaded: () => {} });
    return null;
  }
  const settle = () => new Promise(resolve => setImmediate(resolve));
  try {
    await act(async () => { renderer = create(React.createElement(Harness)); await settle(); });
    await act(async () => { await state.start({ vehicles: 40 }); await settle(); });
    const first = state.job.id;
    await act(async () => { state.clear(); await settle(); });
    assert.equal(state.job, null);
    assert.equal(jobs[0].status, "running");
    await act(async () => { await state.start({ vehicles: 80 }); await settle(); });
    assert.equal(state.history.length, 2);
    await act(async () => { await state.select(first); await settle(); });
    assert.equal(state.job.id, first);
    assert.equal(state.running, true);
    assert.deepEqual(jobs.map(job => job.status), ["running", "running"]);
    assert.deepEqual(cancellations, []);
  } finally {
    await act(async () => renderer?.unmount());
  }
});
