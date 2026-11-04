/* How the UI handles data it does not control, checked in Node without a browser: AG-UI stream framing and
 * terminal events, what a malformed frame puts in the console, map observations and popup text from providers,
 * and which model-supplied links may open. Run: npm --prefix ui run test:metrics */
const test = require("node:test");
const assert = require("node:assert/strict");
const { load } = require("../ts-module.cjs");

test("weather query coordinates bind an observation to its place, including valid zero and aliases", () => {
  const { weatherQueriedAt } = load("shared/domain/incidentObservations.ts");
  assert.equal(weatherQueriedAt({queryLocation:{latitude:0,longitude:0}},0,0), true);
  assert.equal(weatherQueriedAt({queryLocation:{lat:0,lon:0}},0,0), true);
  assert.equal(weatherQueriedAt({queryLocation:{latitude:38,longitude:-120}},39,-121), false);
  assert.equal(weatherQueriedAt({queryLocation:{name:"city"}},39,-121), false);
  assert.equal(weatherQueriedAt({queryLocation:{latitude:NaN,longitude:0}},0,0), false);
  assert.equal(weatherQueriedAt({},39,-121), true);
});
const { encoder, streamOf, within } = require("./support/streams.cjs");

/** An https URL carrying fake user:password credentials, joined at run time so the source holds no
 * credentials-in-URL literal for credential scanners to flag. Link checks must refuse every such URL. */
const withCredentials = (user, password, rest) => "https://" + [user, password].join(":") + "@" + rest;

test("map observations preserve zero coordinates and reject invalid locations and invented missing measurements", () => {
  const { incidentObservations, observationValue, observationDetails } = load("shared/domain/incidentObservations.ts");
  const observations = incidentObservations({content: {mapData: {locations: [
    {latitude: 0, longitude: 0, currentWeather: {temperature: {value: 20, unit: "C"}}},
    {latitude: 91, longitude: 0, fire: {name: "Outside map"}},
    {latitude: 37, longitude: NaN, fire: {}},
    {latitude: 37, longitude: -121, fire: {name: "Reported fire", percentContained: 0}},
  ]}}});
  assert.equal(observations.length, 2);
  assert.equal(observationValue({value: 20, unit: "C"}), "68 °F");
  assert.equal(observationValue({value: 10, unit: "m/s"}), "22.4 mph");
  assert.equal(observationValue({value: 10, unit: "km/h"}), "6.2 mph");
  assert.equal(observationValue({value: NaN, unit: "%"}), "Unavailable");
  assert.equal(observationValue(null), "Unavailable");
  assert.equal(observationValue({value: 0, unit: "percent"}), "0 %");
  assert.equal(observationValue({value: 72, unit: "degF"}), "72 °F");
  assert.notEqual(observations[0].id, observations[1].id);
  assert.deepEqual(observationDetails(observations[1]).slice(0, 2),
    [["Reported area", "Unavailable"], ["Containment", "0%"]]);
});

test("AG-UI frames with CRLF line endings are dispatched as each frame ends, not when the stream closes", async () => {
  const { readAgUiStream } = load("shared/services/AgentEventStream.ts");
  let source;
  const stream = new ReadableStream({ start(controller) { source = controller; } });
  const seen = [];
  let started;
  const firstFrame = new Promise(resolve => { started = resolve; });
  const reading = readAgUiStream(stream, {
    onToolStart: (id, name) => { seen.push(["start", id, name]); started(); },
    onToolEnd: id => seen.push(["end", id]),
    onRunFinished: () => seen.push(["finished"]),
  });
  // The first frame's blank line arrives split across two chunks, between its "\r" and "\n".
  source.enqueue(encoder.encode('data: {"type":"TOOL_CALL_START","toolCallId":"a","toolCallName":"geocode"}\r\n\r'));
  source.enqueue(encoder.encode("\n"));
  await within(firstFrame, 1000, "Dispatch of the first CRLF frame while the stream is still open");
  // An event line plus one JSON value spread over two data lines, then a frame split after its first "\r".
  source.enqueue(encoder.encode('event: message\r\ndata: {"type":"TOOL_CALL_END",\r\ndata: "toolCallId":"a"}\r\n\r\n'));
  source.enqueue(encoder.encode('data: {"type":"RUN_FINISHED"}\r'));
  source.enqueue(encoder.encode("\n\r\n"));
  source.close();
  await reading;
  assert.deepEqual(seen, [["start", "a", "geocode"], ["end", "a"], ["finished"]]);
});

test("LF-delimited AG-UI frames still parse, including a final frame without a blank line", async () => {
  const { readAgUiStream } = load("shared/services/AgentEventStream.ts");
  const seen = [];
  await readAgUiStream(streamOf('data: {"type":"TOOL_CALL_START","toolCallId":"b","toolCallName":"weather"}\n\n',
    'data: {"type":"RUN_FINISHED"}'), {
    onToolStart: id => seen.push(["start", id]),
    onRunFinished: () => seen.push(["finished"]),
  });
  assert.deepEqual(seen, [["start", "b"], ["finished"]]);
});

test("a final answer is the saved snapshot followed by RUN_FINISHED; streamed text is never the answer", async () => {
  const { readFinalAgentSnapshot } = load("shared/services/AgentEventStream.ts");
  const framed = (...events) => streamOf(...events.map(event => `data: ${JSON.stringify(event)}\n\n`));
  const snapshot = { type: "universal_response", content: { response: "Saved evidence." } };
  assert.deepEqual(await readFinalAgentSnapshot(framed(
    { type: "TEXT_MESSAGE_CONTENT", delta: '{"content":{"response":"Unverified prose"}}' },
    { type: "STATE_SNAPSHOT", snapshot }, { type: "RUN_FINISHED" }), {}), snapshot);
  // Streamed text shaped like a saved response is still progress text, not a result.
  const prose = framed({ type: "TEXT_MESSAGE_CONTENT", delta: JSON.stringify(snapshot) }, { type: "RUN_FINISHED" });
  await assert.rejects(readFinalAgentSnapshot(prose, {}), /without a completed saved response/);
});

test("a non-JSON AG-UI frame is skipped and logged by size, never by content", async t => {
  const { readAgUiStream } = load("shared/services/AgentEventStream.ts");
  const warn = t.mock.method(console, "warn", () => {});
  const finished = [];
  await readAgUiStream(streamOf("data: evacuation question about 12 Example Road\n\n",
    'data: {"type":"RUN_FINISHED"}\n\n'), { onRunFinished: () => finished.push(true) });
  assert.deepEqual(finished, [true], "Frames after the malformed one still dispatch");
  assert.equal(warn.mock.callCount(), 1);
  const logged = warn.mock.calls[0].arguments.join(" ");
  assert.ok(!logged.includes("Example Road"), "The frame text never reaches the console");
  assert.match(logged, /\b\d+ characters\b/);
});

test("model-supplied report links open only as https Amazon S3 addresses", () => {
  const { allowedHttpsUrl, isAmazonS3Host } = load("shared/utils/urlAllowlist.ts");
  const reportLink = url => allowedHttpsUrl(url, isAmazonS3Host);
  for (const url of [
    "https://fireguard-reports.s3.us-west-2.amazonaws.com/reports/s/RPT-1.html?X-Amz-Signature=abc",
    "https://fireguard-reports.s3.amazonaws.com/reports/s/RPT-1.html",
    "https://s3.us-gov-west-1.amazonaws.com/dotted.bucket.name/RPT-1.html",
  ]) assert.equal(reportLink(url), url);
  for (const url of [
    "javascript://fireguard-reports.s3.amazonaws.com/%0Aalert(1)",
    "http://fireguard-reports.s3.amazonaws.com/RPT-1.html",
    "https://evil.example/RPT-1.html",
    "https://fireguard-reports.s3.amazonaws.com.evil.example/RPT-1.html",
    withCredentials("user", "secret", "fireguard-reports.s3.amazonaws.com/RPT-1.html"),
    "https://fireguard-reports.s3.amazonaws.com:8443/RPT-1.html",
    "https://s3-reports-1234.us-west-2.elb.amazonaws.com/RPT-1.html",
    "https://abc123.execute-api.us-east-1.amazonaws.com/RPT-1.html",
    "data:text/html,<script>alert(1)</script>",
    "not a url", "", undefined, null, 42,
  ]) assert.equal(reportLink(url), undefined, String(url));
});

test("an allowlisted link needs https on a listed host, with no credentials or port", () => {
  const { allowedHttpsUrl } = load("shared/utils/urlAllowlist.ts");
  const official = url => allowedHttpsUrl(url, host => ["www.ready.gov"].includes(host));
  assert.equal(official("https://www.ready.gov/wildfires"), "https://www.ready.gov/wildfires");
  for (const url of ["http://www.ready.gov/wildfires", "https://ready.gov/wildfires", "https://www.ready.gov:444/x",
    withCredentials("name", "pass", "www.ready.gov/x"), "javascript:alert(1)"]) assert.equal(official(url), undefined, url);
});

test("federal land popups escape provider text and do not invent fuel measurements", () => {
  const { createVegetationPopup } = load("features/map/utils/popupGenerators.ts");
  const popup = createVegetationPopup({name:"<script>land</script>", agency:"Forest & Parks",
    source:"ArcGIS Living Atlas — federal land boundaries"});
  assert.ok(popup.title.includes("&lt;script&gt;"));
  assert.ok(popup.content.includes("Forest &amp; Parks"));
  assert.ok(popup.content.includes("Land ownership does not establish"));
  assert.ok(!/Fuel Model|Spread Rate|Risk Score/.test(popup.content));
});
