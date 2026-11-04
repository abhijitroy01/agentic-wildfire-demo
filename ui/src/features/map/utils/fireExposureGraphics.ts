import type Graphic from "@arcgis/core/Graphic";
import type Polygon from "@arcgis/core/geometry/Polygon";
import type SimpleFillSymbol from "@arcgis/core/symbols/SimpleFillSymbol";
import type { AssessmentResult } from "@/features/fire-exposure/generated/contracts";
import {
  isExposureGeometry, visibleExposureFeatures,
  scenarioHorizons,
  type ExposureGeometry, type ScenarioSelection,
} from "@/features/fire-exposure/services/assessment";
import { escapeHtml, type PopupResult } from "./popupGenerators";

interface ExposureArcgis {
  Graphic: typeof Graphic;
  Polygon: typeof Polygon;
  SimpleFillSymbol: typeof SimpleFillSymbol;
}

const STYLES = {
  proximity: { fill: [245, 158, 11, 0.12], outline: [180, 110, 20, 0.6], width: 1 },
  downwind: { fill: [234, 88, 12, 0.25], outline: [194, 65, 12, 0.9], width: 2 },
  surface_fire: { fill: [220, 38, 38, 0.18], outline: [185, 28, 28, 1], width: 2 },
  landscape: { fill: [220, 38, 38, 0.24], outline: [185, 28, 28, 1], width: 2 },
};

/** Sequential color follows the real returned time steps; a slider change never recolors a retained contour. */
export function scenarioContourStyle(minutes: number, horizons: readonly number[]): {
  fill: number[]; outline: number[]; width: number;
} {
  const index = horizons.indexOf(minutes);
  const fraction = Math.max(0, index) / Math.max(1, horizons.length - 1);
  const palette = [[153, 39, 25], [205, 84, 20], [157, 115, 0]];
  const position = fraction * (palette.length - 1), step = Math.min(Math.floor(position), palette.length - 2);
  const first = palette[step], second = palette[step + 1], blend = position - step;
  const outline = first.map((channel, offset) => Math.round(channel + (second[offset] - channel) * blend));
  return { fill: [...outline, 0.12], outline: [...outline, 1], width: 1.75 };
}

function signedArea(ring: number[][]): number {
  return ring.slice(1).reduce((area, point, index) =>
    area + ring[index][0] * point[1] - point[0] * ring[index][1], 0) / 2;
}

/** ArcGIS outer rings are clockwise and holes counterclockwise; copy rather than mutate saved GeoJSON. */
export function arcgisExposureRings(geometry: ExposureGeometry): number[][][][] {
  if (!isExposureGeometry(geometry)) return [];
  let polygons: number[][][][];
  if (geometry.type === "Polygon") polygons = [geometry.coordinates];
  else if (geometry.type === "MultiPolygon") polygons = geometry.coordinates;
  else return [];
  return polygons.map(polygon => polygon.map((ring, index) => {
    const copied = ring.map(point => [...point]);
    const area = signedArea(copied);
    return (index === 0 ? area > 0 : area < 0) ? copied.reverse() : copied;
  }));
}

export function createFireExposurePopup(attributes: Record<string, unknown>): PopupResult {
  const landscape = attributes.kind === "landscape";
  const scenario = landscape || attributes.kind === "surface_fire";
  const title = landscape ? "Modeled spread scenario" : scenario ? "Surface-fire demo scenario"
    : "Illustrative fire exposure";
  const time = typeof attributes.elapsed_minutes === "number"
    ? `<p>Scenario time: ${escapeHtml(attributes.elapsed_minutes)} minutes after assumed initialization.</p>` : "";
  const assumptions = Array.isArray(attributes.assumptions)
    ? attributes.assumptions.filter((item): item is string => typeof item === "string") : [];
  return { title, content: `<div><strong>${escapeHtml(attributes.fire_name as string || "Reported fire")}</strong>
    <p>${escapeHtml(attributes.label as string)}</p>${time}
    <p>Fire record updated: ${escapeHtml(attributes.source_timestamp as string || "Time unavailable")}</p>
    <p>Weather observation: ${escapeHtml(attributes.weather_timestamp as string || "Unavailable")}</p>
    <p>${escapeHtml(assumptions.join(" "))}</p>
    <p>${landscape ? "Mapped LANDFIRE fuel and elevation with assumed moisture and static DTN wind; not an operational forecast."
      : scenario ? "Assumed fuels, moisture and wind; not an operational spread forecast."
      : "Spatial context, not a predicted fire perimeter or burn probability."} Outside these shapes does not mean safe.</p></div>` };
}

/** Construct graphics from retained evidence only. No data fetch or model execution occurs in the renderer. */
export function fireExposureGraphics(arcgis: ExposureArcgis, assessments: readonly AssessmentResult[],
  selections: ScenarioSelection = {}): Graphic[] {
  return assessments.flatMap(assessment => visibleExposureFeatures(assessment, selections).flatMap(feature => {
    const { elapsed_minutes: minutes, kind } = feature.properties;
    const { fill, outline, width } = ["surface_fire", "landscape"].includes(kind) && typeof minutes === "number"
      ? scenarioContourStyle(minutes, scenarioHorizons(assessment)) : STYLES[kind];
    const attributes = { ...feature.properties, type: "fireExposure", assessment_id: assessment.assessment_id,
      fire_ref: assessment.fire_ref, fire_name: assessment.fire.name, model_version: assessment.model_version,
      source_timestamp: assessment.fire.source_timestamp, weather_timestamp: assessment.weather?.observed_at,
      assumptions: assessment.assumptions };
    const popup = createFireExposurePopup(attributes);
    return arcgisExposureRings(feature.geometry).map(rings => new arcgis.Graphic({
      geometry: new arcgis.Polygon({ rings, spatialReference: { wkid: 4326 } }),
      symbol: new arcgis.SimpleFillSymbol({ color: fill, outline: { color: outline, width } }),
      attributes,
      popupTemplate: popup,
    }));
  }));
}
