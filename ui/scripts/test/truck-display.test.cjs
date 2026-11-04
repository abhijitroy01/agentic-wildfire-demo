/* Firetruck display tables: the units legend, marker rings and risk badges render from them (UIWS-12); and the
 * exercise's broker calls. Run: npm --prefix ui run test:metrics */
const test = require("node:test");
const assert = require("node:assert/strict");
const { load } = require("../ts-module.cjs");
const { browser, brokerOr, json } = require("./support/browser.cjs");

const display = () => load("features/firetruck/utils/truckDisplayUtils.ts");

test("the firetruck exercise starts, reads, stops and checks its run only through the broker", async () => {
  const env = browser("https://lab.example.test/response", brokerOr((url, init) => {
    if (url === "/simulation-api/v1/firetruck/runs" && init.method === "POST") return json({ session_id: "run-7" });
    if (url === "/simulation-api/v1/firetruck/runs/run-7") {
      return json({ session_id: "run-7", status: init.method === "DELETE" ? "stopping" : "running" });
    }
    if (url === "/simulation-api/v1/firetruck/health") return json({ status: "ok" });
    return json({ error: { message: `Unexpected request: ${url}` } }, 404);
  }));
  // No Vite variable: every build uses the broker.
  const service = load("features/firetruck/services/firetruckService.ts");
  const config = { fire_location: { latitude: 34.1, longitude: -118.2, name: "Fixture Fire" },
    initial_truck_count: 4, max_truck_count: 12 };
  const started = await service.startSimulation(config);
  assert.equal(started.session_id, "run-7");
  assert.equal(started.websocket_url, "wss://lab.example.test/simulation-api/v1/firetruck/runs/run-7/stream");
  assert.equal((await service.getSimulationStatus("run-7")).active, true);
  assert.equal((await service.stopSimulation("run-7")).session_id, "run-7");
  assert.equal(await service.checkHealth(), true);
  const calls = env.requests.filter(request => !request.url.endsWith("/workspace-config"));
  assert.deepEqual(calls.map(request => [request.url, request.init.method]), [
    ["/simulation-api/v1/firetruck/runs", "POST"], ["/simulation-api/v1/firetruck/runs/run-7", "GET"],
    ["/simulation-api/v1/firetruck/runs/run-7", "DELETE"], ["/simulation-api/v1/firetruck/health", "GET"]]);
  const { request_id: requestId, ...body } = JSON.parse(calls[0].init.body);
  assert.match(requestId, /^[0-9a-f-]{36}$/, "Each start carries a new request id for the broker's idempotency");
  assert.deepEqual(body, config);
});

test("the unit, status and risk tables keep their legend order, labels and colors", () => {
  const { UNIT_DISPLAY, STATUS_DISPLAY, RISK_DISPLAY } = display();
  // Entries, not objects alone, so the legend order is pinned as well.
  assert.deepEqual(Object.entries(UNIT_DISPLAY), [
    ["ENGINE", { icon: "🚒", name: "Engine" }], ["TANKER", { icon: "🚛", name: "Tanker" }],
    ["RESCUE", { icon: "🚑", name: "Rescue" }], ["BRUSH", { icon: "🚙", name: "Brush" }],
    ["COMMAND", { icon: "🚗", name: "Command" }]]);
  assert.deepEqual(Object.entries(STATUS_DISPLAY), [
    ["EN_ROUTE", { color: "#3B82F6", label: "En Route" }], ["SUPPRESSION", { color: "#EF4444", label: "Suppression" }],
    ["RESUPPLY", { color: "#8B5CF6", label: "Resupply" }], ["HOLDING", { color: "#F59E0B", label: "Holding" }],
    ["STAGING", { color: "#6B7280", label: "Staging" }], ["EVACUATION", { color: "#DC2626", label: "Evacuation" }]]);
  assert.deepEqual(Object.entries(RISK_DISPLAY),
    [["LOW", "#10B981"], ["MEDIUM", "#F59E0B"], ["HIGH", "#EF4444"], ["CRITICAL", "#DC2626"]]);
});

test("lookups accept any letter case and give gray for a value outside the tables", () => {
  const { getUnitIcon, getUnitName, getStatusColor, getRiskColor } = display();
  assert.deepEqual([getUnitIcon("tanker"), getUnitIcon("Unknown"), getUnitIcon(undefined)], ["🚛", "🚒", "🚒"]);
  assert.deepEqual([getUnitName("rescue"), getUnitName("HOVERCRAFT")], ["Rescue", "HOVERCRAFT"]);
  assert.deepEqual([getStatusColor("evacuation"), getStatusColor("En_Route"), getStatusColor("parked"),
    getStatusColor(undefined)], ["#DC2626", "#3B82F6", "#6B7280", "#6B7280"]);
  assert.deepEqual([getRiskColor("critical"), getRiskColor("Low"), getRiskColor("extreme"), getRiskColor(undefined)],
    ["#DC2626", "#10B981", "#6B7280", "#6B7280"]);
});
