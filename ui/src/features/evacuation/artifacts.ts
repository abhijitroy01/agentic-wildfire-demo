/** Saved SUMO artifacts shared by the workspace and Traffic Lab. SI units remain unchanged. */
import type { Feature,FeatureCollection,Geometry,LineString,MultiLineString } from "geojson";

export interface Scenario {
  lat: number; lon: number; radius_km: number; highway: string; vehicles: number;
  people_per_vehicle: number; crews: number; duration_minutes: number; departure_minutes: number;
  seed: number; exit_direction: string; hazard_radius_m: number; hazard_growth_m_min: number;
  hazard_start_minutes: number; background_speed_factor: number; traffic_mode: string; policies: string[];
}
export type Vehicle = [string, number, number, number, boolean | number];
export interface CrewRecord {
  id: string; scheduled_departure_s: number; departed_s: number | null;
  reached_staging_s: number | null; returned_s: number | null; state: string;
}
export interface Frame {
  t: number; hazard_radius_m: number; closed_edges: string[]; vehicles: Vehicle[]; crews?: CrewRecord[];
}
export interface Sample {
  t: number; queue: number; exited: number; active: number; not_departed: number;
  // No crews_returned: the simulator does not write it per sample; crewsReturned() derives it from frame crews.
  exposed: number; closed_edges: number; crews_staged: number;
}
export interface Summary {
  valid_engine_run: boolean; valid_accounting: boolean; simulated_seconds: number;
  exited_vehicles: number; active_vehicles: number; not_departed_vehicles: number;
  unresolved_vehicles?: number; exposed_vehicles: number; crews_staged: number; crews_returned: number;
  exited_people: number; crews_requested: number; exposed_crews: number; crews_not_departed: number; t95_seconds: number | null;
  origin_blocked_vehicles?: number; unroutable_vehicles: number; crews_without_route: number;
  crews_origin_blocked?: number; teleports: number; collision_events: number;
}
export interface Policy { policy: string; summary: Summary; series: Sample[]; frames: Frame[] }
/** A crew's saved plan: its staging stop lasts `service_seconds`; `routable` appears only in older recordings. */
export interface CrewPlan {
  id: string; origin: string; stage: string; route: string[]; stage_index: number | null;
  scheduled_departure_s: number; service_seconds: number; routable?: boolean;
}
export interface Demand { id: string; origin: string; depart: number; routable: boolean; people: number }
export type Road = Feature<LineString, { id: string; highway: boolean; Route?: number; RouteType?: string;
  corridor_id?: string }>;
export type Network = FeatureCollection<LineString, Road["properties"]>;
/** New U.S. corridors keep OSM identity; historical recordings retain their Caltrans route fields. */
export interface Highway {
  corridor_id?: string; label?: string; source?: string; route_ref?: string | null; name?: string | null;
  road_class?: string; source_way_ids?: number[]; route_type?: string; route_number?: string | number;
  approx_distance_km?: number;
}
export interface CorridorProperties {
  corridor_id?: string; Route?: string | number; RouteType?: string; source_way_id?: number;
  source?: string; route_ref?: string | null; name?: string | null; road_class?: string;
}
export type StudyBounds = number[][];
export interface Discovery {
  geometry: FeatureCollection<Geometry, CorridorProperties>; highways: Highway[]; source_mode?: string;
  study_bounds?: StudyBounds;
  area?: Pick<Scenario, "lat" | "lon" | "radius_km">;
}
export function corridorId(value: Highway): string {
  return value.corridor_id || (value.route_type && value.route_number != null
    ? `${value.route_type} ${value.route_number}` : "");
}
export function corridorLabel(id: string, value?: Highway | null): string {
  if (id === "auto") return "Road discovery pending";
  return value && corridorId(value) === id && value.label ? value.label
    : /^(State|US|Interstate) \d{1,3}$/.test(id) ? id : "Selected road corridor";
}
export function corridorFeatureMatches(properties: CorridorProperties | null, id: string): boolean {
  return !!properties && (properties.corridor_id === id || (
    properties.Route != null && properties.RouteType != null &&
    `${properties.RouteType} ${properties.Route}` === id));
}
export interface TrafficSegment {
  id: string; geometry: LineString | MultiLineString; speed_kmh: number; free_flow_kmh: number;
  travel_time_s: number; free_flow_time_s: number; confidence: number; road_closure: boolean; retrieved_at: number;
}
export interface TrafficMatch {
  edge_id: string; segment_id: string; provider_speed_kmh: number; free_flow_kmh: number;
  speed_limit_m_s: number; confidence: number; retrieved_at: number;
}
export interface ProviderTraffic {
  mode: "provider_speed_limits"; status: "applied" | "unavailable";
  matched_edges: number; highway_edges: number; retrieved_at: string | number;
  segments: TrafficSegment[]; matches: TrafficMatch[]; requests: unknown[]; excluded: unknown[];
  geojson?: FeatureCollection;
  reason?: string;
  message?: string;
}
export interface SyntheticTraffic {
  mode: "synthetic"; status: "not_requested"; matched_edges: 0;
  segments?: never; matches?: never; geojson?: never;
}
export type Traffic = ProviderTraffic | SyntheticTraffic;
export interface SourceRecord {
  record_epoch?: number; observed_at?: number | string; fresh?: boolean; active?: boolean;
  in_service?: boolean; image_freshness?: string;
  location?: { locationName?: string; direction?: string; travelFlowDirection?: string;
    begin?: { beginRoute?: string; beginLocationName?: string }; end?: { endLocationName?: string } };
  closure?: { typeOfClosure?: string; typeOfWork?: string };
  properties?: { attr_IncidentName?: string; poly_IncidentName?: string;
    poly_PolygonDateTime?: number | string; capture_fresh_6h?: boolean };
}
export interface Source {
  name: string; status: string; retrieved_at?: number; url?: string; use?: string; incomplete?: boolean; note?: string; error?: string;
  records?: SourceRecord[]; geojson?: FeatureCollection<Geometry, NonNullable<SourceRecord["properties"]>>;
  values?: { currentSpeed?: number; freeFlowSpeed?: number; currentTravelTime?: number;
    confidence?: number; roadClosure?: boolean };
  properties?: { periods?: { startTime: string; temperature: number; temperatureUnit: string;
    windSpeed: string; windDirection: string; shortForecast: string }[] };
}
export interface Sources {
  point: { lat: number; lon: number }; radius_km: number; retrieved_at: number; sources: Source[];
}
export interface Result {
  input_bundle_id?: string; input_bundle_sha256?: string; schema_version: 3 | 4; scenario: Scenario; policies: Policy[]; sources: Sources; traffic: Traffic;
  engine: string; engine_version: string; assumptions: string[]; crew_plan?: CrewPlan[]; demand: Demand[];
  network: { edge_count: number; retrieved_at: number; limitations: string[]; osm_sha256: string;
    net_sha256: string; selected_route: Highway; corridor_label?: string; study_bounds?: StudyBounds;
    gates: { coordinate: number[] }[] };
  reused_result?: { basis: string; simulated_at: number };
}
export interface FocusRequest {
  kind: string; id?: string; number?: number; highway?: string; token?: number; changed?: number;
}

const object = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);
const finite = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value);
const trafficEnvelope = (value: unknown): boolean => {
  if (!object(value)) return false;
  // Synthetic runs deliberately have no provider payload. Absence is not a measured zero speed.
  if (value.mode === "synthetic") {
    return value.status === "not_requested" && value.matched_edges === 0
      && value.segments === undefined && value.matches === undefined;
  }
  return value.mode === "provider_speed_limits"
    && (value.status === "applied" || value.status === "unavailable")
    && finite(value.matched_edges) && finite(value.highway_edges)
    && (typeof value.retrieved_at === "string" || finite(value.retrieved_at))
    && Array.isArray(value.segments) && Array.isArray(value.matches)
    && Array.isArray(value.requests) && Array.isArray(value.excluded)
    && value.matched_edges === value.matches.length
    && (value.status === "applied" ? value.matches.length > 0 : value.matches.length === 0);
};

/** Preparation gates use an extensible JSON contract; only verified coordinates reach the map. */
export function gateCoordinates(value: unknown): { coordinate: number[] }[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap(gate => object(gate) && Array.isArray(gate.coordinate)
    && gate.coordinate.length === 2 && gate.coordinate.every(finite) ? [{ coordinate: gate.coordinate }] : []);
}

/** Validate the saved artifact envelope once. Outcome completeness remains an explicit availability check.
 * Version 3 recordings lack some crew detail; version 4 adds frozen-input provenance. Both retain SI units.
 */
export function validateArtifact(path: string, value: unknown): void {
  if (path.endsWith("/artifacts/result")) {
    if (!object(value) || (value.schema_version !== 3 && value.schema_version !== 4)
        || !object(value.scenario) || !finite(value.scenario.lat) || !finite(value.scenario.lon)
        || !finite(value.scenario.vehicles) || !finite(value.scenario.people_per_vehicle)
        || !object(value.network) || !object(value.sources) || !Array.isArray(value.sources.sources)
        || !trafficEnvelope(value.traffic)
        || (value.crew_plan !== undefined && !Array.isArray(value.crew_plan))
        || !Array.isArray(value.policies)
        || !value.policies.length || !Array.isArray(value.assumptions) || !Array.isArray(value.demand)
        || !value.assumptions.every(item => typeof item === "string")
        || !value.policies.every(policy => object(policy) && typeof policy.policy === "string"
          && object(policy.summary) && Array.isArray(policy.series) && policy.series.length > 0
          && policy.series.every(sample => object(sample) && finite(sample.t) && finite(sample.queue))
          && Array.isArray(policy.frames) && policy.frames.length > 0
          && policy.frames.every(frame => object(frame) && finite(frame.t) && finite(frame.hazard_radius_m)
            && Array.isArray(frame.closed_edges) && frame.closed_edges.every(edge => typeof edge === "string")
            && Array.isArray(frame.vehicles)
            && frame.vehicles.every(vehicle => Array.isArray(vehicle) && vehicle.length === 5
              && typeof vehicle[0] === "string" && vehicle.slice(1, 4).every(finite)
              && (typeof vehicle[4] === "boolean" || finite(vehicle[4])))))) {
      throw new Error("This saved simulation artifact is incomplete or uses an unsupported version.");
    }
  } else if (path.endsWith("/artifacts/network") || path.endsWith("/network")) {
    if (!object(value) || value.type !== "FeatureCollection" || !Array.isArray(value.features)
        || !value.features.every(feature => object(feature) && feature.type === "Feature"
          && object(feature.geometry) && feature.geometry.type === "LineString"
          && Array.isArray(feature.geometry.coordinates) && feature.geometry.coordinates.length >= 2
          && feature.geometry.coordinates.every(point => Array.isArray(point) && point.length >= 2
            && point.every(finite)) && object(feature.properties)
          && typeof feature.properties.id === "string")) {
      throw new Error("The saved road network is incomplete or unsupported.");
    }
  }
}
