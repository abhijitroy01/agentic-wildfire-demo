import type {
  AssessmentResult, FireRiskJobReference, FireRiskJobResponse, FireRiskPreparationError,
} from "../generated/contracts";

export type ExposureFeature = NonNullable<AssessmentResult["geojson"]["features"]>[number];
export type ExposureGeometry = ExposureFeature["geometry"];
export type ScenarioSelection = Readonly<Record<string, number | null>>;

export class FireRiskReceiptError extends Error {
  override name = "FireRiskReceiptError";
}

function object(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

function finite(value: unknown, minimum: number, maximum: number): value is number {
  return typeof value === "number" && Number.isFinite(value) && value >= minimum && value <= maximum;
}

function strings(value: unknown): value is string[] {
  return Array.isArray(value) && value.length <= 64
    && value.every(item => typeof item === "string" && item.length <= 10000);
}

function nullableString(value: unknown): boolean {
  return value == null || typeof value === "string";
}

function coordinate(value: unknown): value is [number, number] {
  return Array.isArray(value) && value.length === 2 && finite(value[0], -180, 180) && finite(value[1], -90, 90);
}

function ring(value: unknown): boolean {
  if (!Array.isArray(value) || value.length < 4 || value.length > 8192 || !value.every(coordinate)) return false;
  const first = value[0], last = value[value.length - 1];
  if (first[0] !== last[0] || first[1] !== last[1]) return false;
  // The modeler splits antimeridian crossings. Polar caps close through a same-pole longitude edge.
  return value.slice(1).every((point, index) => Math.abs(point[0] - value[index][0]) <= 180
    || (Math.abs(point[1]) === 90 && point[1] === value[index][1]));
}

function polygon(value: unknown): boolean {
  return Array.isArray(value) && value.length > 0 && value.length <= 1024 && value.every(ring);
}

export function isExposureGeometry(value: unknown): value is ExposureGeometry {
  if (!object(value)) return false;
  const valid = value.type === "Polygon" ? polygon(value.coordinates)
    : value.type === "MultiPolygon" && Array.isArray(value.coordinates) && value.coordinates.length > 0
      && value.coordinates.length <= 1024 && value.coordinates.every(polygon);
  if (!valid) return false;
  const polygons = value.type === "Polygon" ? [value.coordinates] : value.coordinates;
  return (polygons as number[][][][]).reduce((total, item) =>
    total + item.reduce((count, points) => count + points.length, 0), 0) <= 16384;
}

function feature(value: unknown): boolean {
  if (!object(value) || value.type !== "Feature" || !isExposureGeometry(value.geometry)
      || !object(value.properties)) return false;
  const properties = value.properties;
  return ["proximity", "downwind", "surface_fire", "landscape"].includes(String(properties.kind))
    && typeof properties.label === "string"
    && (properties.elapsed_minutes == null || finite(properties.elapsed_minutes, 0, 1440))
    && (properties.inner_km == null || finite(properties.inner_km, 0, 500))
    && (properties.outer_km == null || finite(properties.outer_km, 0, 500));
}

/** Check retained model output before it becomes map geometry; malformed exposure never removes fire markers. */
export function isAssessmentResult(value: unknown): value is AssessmentResult {
  if (!object(value) || !["1", "2"].includes(String(value.schema_version)) || !uuid(value.assessment_id)
      || typeof value.fire_ref !== "string" || typeof value.input_hash !== "string"
      || !/^[a-f0-9]{64}$/.test(value.input_hash) || typeof value.model_version !== "string"
      || typeof value.calculated_at !== "string" || !Number.isFinite(Date.parse(value.calculated_at))
      || !["available", "unavailable"].includes(String(value.availability))
      || !["proximity", "wind_informed", "surface_fire", "landscape"].includes(String(value.mode))
      || !object(value.fire) || !finite(value.fire.latitude, -90, 90) || !finite(value.fire.longitude, -180, 180)
      || !nullableString(value.fire.name) || !nullableString(value.fire.source_id)
      || !nullableString(value.fire.source_timestamp)
      || (value.fire.perimeter != null && !isExposureGeometry(value.fire.perimeter))
      || !strings(value.assumptions) || !strings(value.warnings) || !object(value.geojson)
      || value.geojson.type !== "FeatureCollection" || !Array.isArray(value.geojson.features)
      || value.geojson.features.length > 32 || !value.geojson.features.every(feature)) return false;
  if (value.weather != null) {
    const weather = value.weather;
    if (!object(weather) || weather.source !== "DTN" || !finite(weather.query_latitude, -90, 90)
        || !finite(weather.query_longitude, -180, 180)
        || !["available", "unavailable", "stale"].includes(String(weather.availability))
        || !nullableString(weather.observed_at)
        || (weather.wind_speed_mps != null && !finite(weather.wind_speed_mps, 0, 150))
        || (weather.wind_from_degrees != null && !finite(weather.wind_from_degrees, 0, 360))) return false;
  }
  if (value.scenario != null) {
    const scenario = value.scenario;
    if (!object(scenario) || !["Pyrothermel/Behave", "Pyrothermel/Behave directional arrival grid"]
      .includes(String(scenario.engine)) || typeof scenario.moisture_profile !== "string"
        || typeof scenario.wind_height_assumption !== "string"
        || typeof scenario.initialization !== "string" || !Array.isArray(scenario.horizons_minutes)
        || scenario.horizons_minutes.length > 24
        || !scenario.horizons_minutes.every(item => finite(item, 0, 1440))) return false;
    if (scenario.engine === "Pyrothermel/Behave"
        && (!finite(scenario.fuel_model, 1, 256) || !finite(scenario.slope_degrees, 0, 90))) return false;
    if (scenario.engine === "Pyrothermel/Behave directional arrival grid"
        && !finite(scenario.directional_radius, 1, 5)) return false;
  }
  if (value.failure != null && (!object(value.failure) || typeof value.failure.code !== "string"
      || typeof value.failure.message !== "string")) return false;
  if (value.mode === "landscape") {
    if (value.schema_version !== "2" || !object(value.scenario)
        || value.scenario.engine !== "Pyrothermel/Behave directional arrival grid"
        || value.geojson.features.some(item => item.properties.kind !== "landscape")) return false;
    if (value.availability === "available" && !landscapeSummary(value.landscape)) return false;
    if (value.availability === "unavailable" && (value.failure == null || value.geojson.features.length)) return false;
  } else if (value.geojson.features.some(item => item.properties.kind === "landscape")) return false;
  return true;
}

function landscapeSummary(value: unknown): boolean {
  if (!object(value) || !object(value.manifest) || value.solver !== "directed-cell-arrival-1") return false;
  const manifest = value.manifest;
  return ["CONUS", "AK", "HI"].includes(String(manifest.region))
    && typeof manifest.fuel_product === "string" && typeof manifest.elevation_product === "string"
    && typeof manifest.captured_at === "string" && Number.isFinite(Date.parse(manifest.captured_at))
    && typeof manifest.fuel_source === "string" && typeof manifest.elevation_source === "string"
    && finite(manifest.resolution_m, 30, 30) && finite(manifest.width, 5, 1000)
    && finite(manifest.height, 5, 1000)
    && ["raster_sha256", "fuel_sha256", "elevation_sha256"].every(key =>
      typeof manifest[key] === "string" && /^[a-f0-9]{64}$/.test(manifest[key]))
    && finite(value.ignition_slope_degrees, 0, 90) && finite(value.ignition_aspect_degrees, 0, 360)
    && finite(value.reached_cells, 0, 250000) && finite(value.modeled_cells, 0, 250000)
    && (value.spread_cells == null || finite(value.spread_cells, 0, 250000));
}

export function uuid(value: unknown): value is string {
  return typeof value === "string" && /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i.test(value);
}

/** Only the broker's fixed public handoff failure can become answer evidence; never render upstream error text. */
export function preparationError(value: unknown): FireRiskPreparationError | undefined {
  return object(value) && value.code === "RISK_PREPARATION_FAILED"
    && value.message === "Terrain assessment preparation could not be queued."
    ? { code: value.code, message: value.message } : undefined;
}

export function answerJobs(value: unknown): FireRiskJobReference[] {
  if (!Array.isArray(value)) return [];
  const seen = new Set<string>();
  return value.slice(0, 4).filter((item): item is FireRiskJobReference => {
    if (!object(item) || !uuid(item.job_id) || seen.has(item.job_id) || !Array.isArray(item.fire_refs)
        || !item.fire_refs.length || item.fire_refs.length > 4
        || !item.fire_refs.every(ref => typeof ref === "string" && ref.length > 0 && ref.length <= 512)
        || new Set(item.fire_refs).size !== item.fire_refs.length) return false;
    seen.add(item.job_id);
    return true;
  });
}

/** A job receipt must match the saved intent and contain only its own declared fire results. */
export function isFireRiskJobResponse(value: unknown, reference: FireRiskJobReference): value is FireRiskJobResponse {
  if (!object(value) || value.job_id !== reference.job_id || !Array.isArray(value.fire_refs)
      || value.fire_refs.length !== reference.fire_refs.length
      || !value.fire_refs.every((ref, index) => ref === reference.fire_refs[index])
      || !["queued", "running", "cancelling", "completed", "failed", "cancelled"].includes(String(value.status))
      || typeof value.phase !== "string" || !value.phase.length || !finite(value.progress, 0, 1)
      || !nullableString(value.error) || !Array.isArray(value.assessments) || value.assessments.length > 4
      || !value.assessments.every(item => isAssessmentResult(item) && reference.fire_refs.includes(item.fire_ref))) return false;
  const ids = value.assessments.map(item => item.assessment_id);
  const refs = value.assessments.map(item => item.fire_ref);
  if (new Set(ids).size !== ids.length || new Set(refs).size !== refs.length) return false;
  return value.status === "completed" ? refs.length === reference.fire_refs.length
    : value.assessments.length === 0;
}

/** Geometry must belong to a reported fire in this answer, rather than the search area or a previous answer. */
export function answerAssessments(value: unknown,
  locations: readonly { latitude: number; longitude: number; fire?: unknown }[]): AssessmentResult[] {
  if (!Array.isArray(value)) return [];
  const seen = new Set<string>();
  return value.slice(0, 16).filter((item): item is AssessmentResult => {
    if (!isAssessmentResult(item) || seen.has(item.assessment_id)) return false;
    const matched = locations.some(location => {
      if (!object(location.fire) || !finite(location.latitude, -90, 90) || !finite(location.longitude, -180, 180)
          || Math.abs(location.latitude - item.fire.latitude) >= 1e-7
          || Math.abs(location.longitude - item.fire.longitude) >= 1e-7) return false;
      if (typeof location.fire.sourceId === "string" && location.fire.sourceId !== item.fire.source_id) return false;
      const observed = typeof location.fire.lastUpdated === "string" ? Date.parse(location.fire.lastUpdated) : NaN;
      return !Number.isFinite(observed)
        || (typeof item.fire.source_timestamp === "string" && Date.parse(item.fire.source_timestamp) === observed);
    });
    if (!matched) return false;
    seen.add(item.assessment_id);
    return true;
  });
}

export function assessmentGroups(assessments: readonly AssessmentResult[]): AssessmentResult[][] {
  const groups = new Map<string, AssessmentResult[]>();
  for (const assessment of assessments) {
    const key = assessment.fire_ref;
    const group = groups.get(key) ?? [];
    group.push(assessment);
    groups.set(key, group);
  }
  return [...groups.values()];
}

/** Only genuine returned scenario outlines supply time controls. */
export function scenarioHorizons(assessment: AssessmentResult): number[] {
  if (assessment.availability !== "available" || !["surface_fire", "landscape"].includes(assessment.mode)
      || !assessment.scenario) return [];
  return [...new Set((assessment.geojson.features ?? []).flatMap(item =>
    ["surface_fire", "landscape"].includes(item.properties.kind)
    && typeof item.properties.elapsed_minutes === "number" ? [item.properties.elapsed_minutes] : []))]
    .sort((first, second) => first - second);
}

export function selectedScenarioMinute(assessment: AssessmentResult, selections: ScenarioSelection): number | null {
  const horizons = scenarioHorizons(assessment);
  if (!horizons.length || selections[assessment.assessment_id] === null) return null;
  const selected = selections[assessment.assessment_id];
  return typeof selected === "number" && horizons.includes(selected) ? selected : horizons[horizons.length - 1];
}

export function visibleExposureFeatures(assessment: AssessmentResult, selections: ScenarioSelection): ExposureFeature[] {
  if (assessment.availability !== "available") return [];
  const selected = selectedScenarioMinute(assessment, selections);
  return (assessment.geojson.features ?? []).filter(item =>
    ["surface_fire", "landscape"].includes(item.properties.kind)
      ? selected !== null && typeof item.properties.elapsed_minutes === "number"
        && item.properties.elapsed_minutes <= selected
      : typeof selections[assessment.assessment_id] === "number")
    // Draw later cumulative footprints first so earlier contours remain visible above them.
    .sort((first, second) => (second.properties.elapsed_minutes ?? 0) - (first.properties.elapsed_minutes ?? 0));
}

/** Coincident retained outlines have no additional modeled growth; never manufacture separated time bands. */
export function noAdditionalScenarioGrowth(assessment: AssessmentResult, selections: ScenarioSelection): boolean {
  const selected = selectedScenarioMinute(assessment, selections);
  if (selected === null) return false;
  const previous = scenarioHorizons(assessment).filter(minutes => minutes < selected).at(-1);
  if (previous === undefined) return false;
  const geometryAt = (minutes: number): string => JSON.stringify((assessment.geojson.features ?? [])
    .filter(item => ["surface_fire", "landscape"].includes(item.properties.kind)
      && item.properties.elapsed_minutes === minutes)
    .map(item => JSON.stringify(item.geometry)).sort());
  return geometryAt(selected) === geometryAt(previous);
}
