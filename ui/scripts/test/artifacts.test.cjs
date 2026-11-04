/* Saved simulation artifact validation. Run: npm --prefix ui run test:metrics */
const test = require("node:test");
const assert = require("node:assert/strict");
const { load } = require("../ts-module.cjs");
test("saved artifact envelopes support versions 3 and 4 and reject unsafe replay geometry", () => {
  const { validateArtifact } = load("features/evacuation/artifacts.ts");
  const result = { schema_version: 3, scenario: { lat: 39, lon: -121, vehicles: 1, people_per_vehicle: 2 },
    network: {}, sources: { sources: [] },
    traffic: { mode: "synthetic", status: "not_requested", matched_edges: 0 },
    assumptions: [], demand: [], policies: [{ policy: "baseline", summary: {},
      series: [{ t: 0, queue: 0 }], frames: [{ t: 0, hazard_radius_m: 0,
        closed_edges: [], vehicles: [["v1", -121, 39, 1, false]] }] }] };
  for (const schema_version of [3, 4]) {
    validateArtifact("/artifacts/result", { ...result, schema_version });
    const provider = { mode: "provider_speed_limits", status: "applied", matched_edges: 1,
      highway_edges: 2, retrieved_at: "2026-10-04T00:00:00Z", segments: [{}], matches: [{}],
      requests: [], excluded: [] };
    validateArtifact("/artifacts/result", { ...result, schema_version, traffic: provider });
    validateArtifact("/artifacts/result", { ...result, schema_version,
      traffic: { ...provider, status: "unavailable", matches: [], matched_edges: 0 } });
    for (const traffic of [{ ...provider, segments: undefined }, { ...provider, matches: undefined },
      { ...provider, matches: [] }, { ...provider, status: "unavailable" },
      { ...result.traffic, status: "applied" }, { status: "not_requested", matched_edges: 0 }]) {
      assert.throws(() => validateArtifact("/artifacts/result", { ...result, schema_version, traffic }),
        /incomplete|unsupported/);
    }
  }
  for (const broken of [{ ...result, schema_version: 99 }, { ...result, sources: null },
    { ...result, policies: [] }, { ...result, policies: [{ ...result.policies[0], frames: [] }] }]) {
    assert.throws(() => validateArtifact("/artifacts/result", broken), /incomplete|unsupported/);
  }
  const feature = { type: "Feature", properties: { id: "road" },
    geometry: { type: "LineString", coordinates: [[-121, 39], [-121, 40]] } };
  validateArtifact("/artifacts/network", { type: "FeatureCollection", features: [feature] });
  assert.throws(() => validateArtifact("/artifacts/network", { type: "FeatureCollection",
    features: [{ ...feature, geometry: { type: "LineString", coordinates: [[null, 39]] } }] }),
  /incomplete|unsupported/);
});
