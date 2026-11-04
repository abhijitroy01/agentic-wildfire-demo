/* Camera through the broker (owner brief 2026-10-14), checked in Node without a browser: the broker transport,
 * the hidden popup action when no camera is configured, smoke and fire derived from detection claims, and the
 * record parsing. Run: npm --prefix ui run test:metrics */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { load } = require("../ts-module.cjs");
const { browser, brokerOr, hang, json, local, origin } = require("./support/browser.cjs");
const src = path.resolve(__dirname, "../../src");
const source = relative => fs.readFileSync(path.join(src, relative), "utf8");
const PNG = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0, 0, 0, 0]);
const configured = { ...local, camera_configured: true };
const unconfigured = { ...local, camera_configured: false };

const detection = (classes, confidences, boundingBoxes = []) =>
  ({ type: "detection", detection: { classes, confidences, boundingBoxes } });

/** The broker's latest records for the fixture device, as GET /camera/latest returns them. */
function view(imageId = "image-a0", claims = [detection(["smoke"], [0.5])]) {
  return { device_id: "fixture-device", camera_alias: "camera_a", groups: [
    { alias: "camera_a", records: [{ id: imageId, type: "image", timeRecorded: "2026-10-14T20:15:03Z", claims }] },
    { alias: "navigation", records: [{ type: "gps", gps: { latitude: 34.1235, longitude: -118.5679 } }] },
    { alias: "wind", records: [{ type: "anemometer", anemometer: { speed: 4.47, angle: 270 } }] },
  ] };
}

const image = (body = PNG, type = "image/png") => new Response(body, { headers: { "Content-Type": type } });

/** Count blob: URLs made and released, so a test can require that none is left unreleased. */
function blobUrls() {
  const { createObjectURL, revokeObjectURL } = URL;
  const urls = { created: [], revoked: [] };
  URL.createObjectURL = blob => { const url = createObjectURL(blob); urls.created.push(url); return url; };
  URL.revokeObjectURL = url => { urls.revoked.push(url); return revokeObjectURL(url); };
  urls.restore = () => { URL.createObjectURL = createObjectURL; URL.revokeObjectURL = revokeObjectURL; };
  return urls;
}

test("smoke and fire come from dedicated claims, else from the detection claims' classes and confidences", () => {
  const { detectionProbabilities } = load("features/camera/services/cameraRecords.ts");
  assert.deepEqual(detectionProbabilities([detection(["smoke", " Fire "], [0.82, 0.4]), detection(["SMOKE"], [0.9])]),
    { smokeProb: 0.9, fireProb: 0.4 }, "The highest confidence of each class, trimmed and in any case");
  assert.deepEqual(detectionProbabilities([{ type: "smoke", smoke: { smokeProb: 0.3 } },
    detection(["smoke", "fire"], [0.9, 0.8])]), { smokeProb: 0.3, fireProb: 0.8 }, "A dedicated claim wins");
  assert.deepEqual(detectionProbabilities([{ type: "fire", fire: { fireProb: 0.6 } },
    { type: "fire", fire: { fireProb: 0.7 } }]), { smokeProb: 0, fireProb: 0.7 }, "The last dedicated claim wins");
  assert.deepEqual(detectionProbabilities([]), { smokeProb: 0, fireProb: 0 });
  assert.deepEqual(detectionProbabilities(undefined), { smokeProb: 0, fireProb: 0 });
  assert.deepEqual(detectionProbabilities([detection(["smoke"], [1.5]), detection(["fire"], [Number.NaN]),
    detection(["smoke", "fire"], [0.9]), { type: "smoke", smoke: { smokeProb: -1 } }]),
  { smokeProb: 0, fireProb: 0 }, "Out-of-range, missing or unpaired confidences are ignored");
});

test("a broker view gives derived probabilities, merged detections, its image ID and wind in mph", () => {
  const records = load("features/camera/services/cameraRecords.ts");
  const latest = view("image-a0", [detection(["smoke"], [0.5]),
    detection(["smoke", "fire"], [0.82, 0.4], [[320, 180, 64, 48], [10, 20, 30, 40]]),
    detection(["fire"], [0.7, 0.2])]);
  const found = records.extractCameraDetection(latest.groups, latest.device_id, latest.camera_alias);
  assert.deepEqual([found.smokeProb, found.fireProb, found.evidenceId, found.timeRecorded],
    [0.82, 0.4, "image-a0", "2026-10-14T20:15:03Z"]);
  // Classes with boxes come first, so CameraImageViewer labels each box with its own class.
  assert.deepEqual(found.detection, { classes: ["smoke", "fire", "smoke"], confidences: [0.82, 0.4, 0.5],
    boundingBoxes: [{ x: 320, y: 180, width: 64, height: 48 }, { x: 10, y: 20, width: 30, height: 40 }] });
  const metrics = records.extractSensorsFromResponse(latest.groups);
  const wind = metrics.find(metric => metric.key === "wind");
  assert.equal(`${wind.value} ${wind.unit}`, "10.0 @ 270° mph", "Delphire's 4.47 m/s reads in mph");
  assert.equal(metrics.find(metric => metric.key === "gps").value, "34.1235, -118.5679");
});

test("the camera is available only when the workspace reports camera_configured", async () => {
  for (const [workspace, expected] of [[configured, true], [{ ...local, camera_configured: false }, false],
    [local, false], [{ access_mode: "closed" }, false]]) {
    browser(`${origin}/`, brokerOr(() => json({}), workspace));
    const { cameraAvailable } = load("features/camera/services/cameraBroker.ts");
    assert.equal(await cameraAvailable(), expected, JSON.stringify(workspace));
  }
});

test("map popups build the camera action only through cameraPopupActions and the workspace's availability", () => {
  const { cameraPopupActions, CAMERA_FEED_ACTION } = load("features/map/utils/popupGenerators.ts");
  assert.deepEqual(cameraPopupActions(false), []);
  assert.deepEqual(cameraPopupActions(true), [CAMERA_FEED_ACTION]);
  const count = (text, pattern) => (text.match(pattern) || []).length;
  const loader = source("features/map/utils/mapGraphicsLoader.ts");
  assert.equal(count(loader, /actions: popupActions\(cameraAction\),/g), 2, "Fire and weather templates");
  assert.equal(count(loader, /cameraPopupActions\(cameraAction\)/g), 1, "One place builds the popup actions");
  assert.equal(count(loader, /const cameraAction = options\.cameraAction \?\? false;/g), 1, "Off unless asked");
  assert.match(loader, /fireGraphics\(arcgis, mapData, cameraAction\)/);
  assert.match(loader, /weatherGraphics\(arcgis, mapData, cameraAction\)/);
  assert.doesNotMatch(loader, /CAMERA_FEED_ACTION|cameraPopupActions\(true\)/);
  const popups = source("features/map/hooks/useMapPopups.ts");
  assert.equal(count(popups, /cameraPopupActions\(/g), 1);
  assert.match(popups, /cameraPopupActions\(includesCameraAction && cameraAvailable\)/);
  assert.doesNotMatch(popups, /CAMERA_FEED_ACTION\.(title|className)/, "No hand-built camera button");
  assert.match(source("features/map/hooks/useMapGraphics.ts"),
    /buildMapGraphics\(mapData, centerLocation, \{cameraAction: cameraAvailable\}\)/);
  const container = source("features/map/components/MapContainer.tsx");
  assert.match(container, /const cameraAvailable = useCameraAvailable\(\);/);
  assert.equal(count(container, /^ {4}cameraAvailable,$/gm), 2, "Both map hooks follow the workspace");
  for (const file of fs.readdirSync(path.join(src, "features/map"), { recursive: true })) {
    if (!/\.(ts|tsx|js)$/.test(file) || file.endsWith("popupGenerators.ts")) continue;
    assert.doesNotMatch(source(path.join("features/map", file)), /\[CAMERA_FEED_ACTION\]/, file);
  }
});

test("a camera snapshot reads the broker's latest records, then the one image they name", async () => {
  const env = browser(`${origin}/`, brokerOr(url => (url.endsWith("/camera/latest") ? json(view()) : image()),
    configured));
  const { brokerCameraSnapshot } = load("features/camera/services/cameraBroker.ts");
  const urls = blobUrls();
  try {
    const snapshot = await brokerCameraSnapshot();
    assert.deepEqual([snapshot.deviceId, snapshot.sensorAlias, snapshot.detection.evidenceId],
      ["fixture-device", "camera_a", "image-a0"]);
    assert.deepEqual(urls.created, [snapshot.imageUrl]);
  } finally {
    urls.restore();
  }
  assert.deepEqual(env.requests.map(request => request.url), ["/simulation-api/v1/workspace-config",
    "/simulation-api/v1/camera/latest", "/simulation-api/v1/camera/images/image-a0"]);
});

test("the popup availability hook starts off, then follows camera_configured", async () => {
  const React = require("react");
  const { act, create } = require("react-test-renderer");
  const settle = async () => {
    for (let turn = 0; turn < 10; turn += 1) await new Promise(resolve => setImmediate(resolve));
  };
  // No Vite variable: an unconfigured workspace never offers the camera.
  for (const [workspace, expected] of [[configured, true], [unconfigured, false]]) {
    browser(`${origin}/`, brokerOr(() => json({}), workspace));
    const { useCameraAvailable } = load("features/camera/hooks/useCameraAvailable.ts");
    const seen = [];
    function Harness() { seen.push(useCameraAvailable()); return null; }
    let renderer;
    await act(async () => { renderer = create(React.createElement(Harness)); await settle(); });
    try {
      const label = JSON.stringify(workspace);
      assert.equal(seen[0], false, `No camera action before the answer: ${label}`);
      assert.equal(seen.at(-1), expected, label);
    } finally {
      await act(async () => renderer.unmount());
    }
  }
});

test("the broker transport sends the demo session cookie, a bounded signal and no Authorization", async () => {
  const env = browser(`${origin}/`, brokerOr(url => (url.endsWith("/camera/latest") ? json(view()) : image()),
    configured));
  const broker = load("features/camera/services/cameraBroker.ts");
  const latest = await broker.fetchBrokerLatest();
  assert.equal(latest.device_id, "fixture-device");
  const request = env.requests.at(-1);
  assert.equal(request.url, "/simulation-api/v1/camera/latest");
  assert.equal(request.init.method, "GET");
  assert.equal(request.init.credentials, "same-origin");
  assert.ok(request.init.signal instanceof AbortSignal, "Every request is bounded by a signal");
  assert.equal("Authorization" in request.init.headers, false, "The broker holds the camera key");
  const urls = blobUrls();
  try {
    const shown = await broker.fetchBrokerImage("image a/0");
    assert.match(shown, /^blob:/);
    assert.deepEqual(urls.created, [shown]);
    assert.equal(env.requests.at(-1).url, "/simulation-api/v1/camera/images/image%20a%2F0");
    assert.equal(env.requests.at(-1).init.headers.Accept, "image/webp, image/png");
  } finally {
    urls.restore();
  }
});

test("an image the broker does not serve gives no image, and a refused latest answer shows its message", async () => {
  let latest = () => json({ error: { code: "CAMERA_NOT_CONFIGURED",
    message: "The camera is not configured for this demonstration." } }, 503);
  let shown = () => image(new TextEncoder().encode("<html></html>"), "text/html");
  browser(`${origin}/`, brokerOr(url => (url.endsWith("/camera/latest") ? latest() : shown()), configured));
  const broker = load("features/camera/services/cameraBroker.ts");
  await assert.rejects(broker.fetchBrokerLatest(), { message: "The camera is not configured for this demonstration." });
  await assert.rejects(broker.brokerCameraSnapshot(), { message: "The camera is not configured for this demonstration." });
  assert.equal(await broker.fetchBrokerImage("image-a0"), null, "Not WebP or PNG");
  shown = () => json({ error: { code: "CAMERA_IMAGE_NOT_FOUND", message: "Not in the latest records." } }, 404);
  assert.equal(await broker.fetchBrokerImage("image-a0"), null);
  latest = () => json({ device_id: "fixture-device", groups: "not groups" });
  await assert.rejects(broker.fetchBrokerLatest(), { message: "The camera returned an unusable response." });
});

test("a later round finds the image another API process held, with that round's detections", async () => {
  const answers = [view("image-a0", [detection(["smoke"], [0.5])]), view("image-a1", [detection(["smoke"], [0.9])])];
  const env = browser(`${origin}/`, brokerOr(url => {
    if (url.endsWith("/camera/latest")) return json(answers.length > 1 ? answers.shift() : answers[0]);
    return url.endsWith("/image-a1") ? image() : json({ error: { message: "Not in the latest records." } }, 404);
  }, configured));
  const broker = load("features/camera/services/cameraBroker.ts");
  const urls = blobUrls();
  try {
    const snapshot = await broker.brokerCameraSnapshot();
    assert.match(snapshot.imageUrl, /^blob:/);
    assert.deepEqual([snapshot.detection.evidenceId, snapshot.detection.smokeProb], ["image-a1", 0.9]);
    assert.deepEqual([snapshot.deviceId, snapshot.sensorAlias], ["fixture-device", "camera_a"]);
    assert.deepEqual(urls.created, [snapshot.imageUrl]);
    assert.deepEqual(env.requests.map(request => request.url).filter(url => url.includes("/camera/")), [
      "/simulation-api/v1/camera/latest", "/simulation-api/v1/camera/images/image-a0",
      "/simulation-api/v1/camera/latest", "/simulation-api/v1/camera/images/image-a1"]);
  } finally {
    urls.restore();
  }
});

test("three rounds without an image give a snapshot without one and leave no blob: URL", async () => {
  const env = browser(`${origin}/`, brokerOr(url => (url.endsWith("/camera/latest") ? json(view())
    : json({ error: { message: "Not in the latest records." } }, 404)), configured));
  const broker = load("features/camera/services/cameraBroker.ts");
  const urls = blobUrls();
  try {
    const snapshot = await broker.brokerCameraSnapshot();
    assert.equal(snapshot.imageUrl, null);
    assert.equal(snapshot.detection.smokeProb, 0.5, "The details of the last answer are still shown");
    assert.equal(env.requests.filter(request => request.url.endsWith("/camera/latest")).length, broker.CAMERA_ROUNDS);
    assert.deepEqual(urls.created, []);
  } finally {
    urls.restore();
  }
});

test("a camera request that outlives its timeout reads as one, and the caller's abort propagates", async () => {
  const timeout = AbortSignal.timeout;
  AbortSignal.timeout = () => timeout.call(AbortSignal, 20);
  try {
    browser(`${origin}/`, brokerOr(hang, configured));
    const broker = load("features/camera/services/cameraBroker.ts");
    await assert.rejects(broker.fetchBrokerLatest(), { message: "The camera service did not answer in time." });
    assert.equal(await broker.fetchBrokerImage("image-a0"), null);
  } finally {
    AbortSignal.timeout = timeout;
  }
  browser(`${origin}/`, brokerOr(hang, configured));
  const broker = load("features/camera/services/cameraBroker.ts");
  for (const pending of [signal => broker.fetchBrokerLatest(signal), signal => broker.fetchBrokerImage("image-a0", signal)]) {
    const controller = new AbortController();
    const request = pending(controller.signal);
    controller.abort();
    await assert.rejects(request, { name: "AbortError" });
  }
});
