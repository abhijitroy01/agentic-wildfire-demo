/**
 * The map's two ArcGIS Living Atlas overlays around the search area: county population density and
 * federal land boundaries. Federal land is source context, not a measurement of fuels or fire behavior.
 */
import { POPULATION_SERVICE_URL, VEGETATION_SERVICE_URL } from "@/shared/config";
import { anySignal } from "@/shared/services/workspaceAccess";

export interface CenterPoint {
  latitude: number;
  longitude: number;
}

export type DensityCategory = "very_low" | "low" | "medium" | "high" | "very_high";

export interface DemographicFeature {
  geometry: { coordinates: number[][][] };
  attributes: {
    id: string;
    name: string;
    population: number;
    populationDensity: number;
    housingUnits: number;
    densityCategory: DensityCategory;
    state: string;
    county: string;
    type: "demographic";
    areaType: "county";
    source: string;
    objectId: number;
    sqMiles: number;
  };
}

export interface FederalLandFeature {
  geometry: { coordinates: number[][][] };
  attributes: { id: string; type: "federalLand"; name: string; agency: string; source: string };
}

interface ProviderFeature {
  attributes?: {
    OBJECTID?: number; NAME?: string; COUNTY_NAME?: string; POPULATION?: number; POP?: number; SQMI?: number;
    AREA_SQMI?: number; HOUSEHOLDS?: number; STATE_NAME?: string; STATE?: string;
    unit_name?: string; Agency?: string; AGENCY?: string;
  };
  geometry?: { rings?: number[][][]; coordinates?: number[][][] };
}

// A stalled Living Atlas answer must not keep a map update pending: every overlay query gives up after 15 seconds.
const OVERLAY_TIMEOUT_MS = 15_000;

/** Query an ArcGIS feature service for the features within radiusMiles of center; throws on an HTTP or provider error. */
async function queryWithinRadius(serviceUrl: string, center: CenterPoint, radiusMiles: number,
  limit: Record<string, string>, signal?: AbortSignal): Promise<ProviderFeature[]> {
  const parameters = new URLSearchParams({
    f: "json", where: "1=1", outFields: "*",
    geometry: JSON.stringify({ x: center.longitude, y: center.latitude, spatialReference: { wkid: 4326 } }),
    geometryType: "esriGeometryPoint", spatialRel: "esriSpatialRelIntersects",
    distance: String(radiusMiles * 1609.34), units: "esriSRUnit_Meter", outSR: "4326",
    returnGeometry: "true", ...limit,
  });
  const timeout = AbortSignal.timeout(OVERLAY_TIMEOUT_MS);
  const response = await fetch(`${serviceUrl}/query?${parameters}`,
    { signal: signal ? anySignal([signal, timeout]) : timeout });
  if (!response.ok) throw new Error(`HTTP ${response.status}: ${response.statusText}`);
  const data: { error?: unknown; features?: ProviderFeature[] } = await response.json();
  if (data.error) throw new Error("The provider rejected the query");
  if (!Array.isArray(data.features)) throw new Error("The overlay provider returned an unusable response.");
  return data.features;
}

/** Population density bands for the overlay colors. */
export function categorizeDensity(density: number): DensityCategory {
  if (density < 200) return "very_low"; // Rural
  if (density < 600) return "low"; // Suburban low
  if (density < 1200) return "medium"; // Suburban
  if (density < 2000) return "high"; // Urban
  return "very_high"; // Dense urban
}

const DENSITY_FILL: Record<DensityCategory, number[]> = {
  very_low: [200, 230, 255, 0.5], low: [150, 200, 255, 0.55], medium: [100, 150, 255, 0.6],
  high: [50, 100, 200, 0.65], very_high: [0, 50, 150, 0.7],
};
const DENSITY_OUTLINE: Record<DensityCategory, number[]> = {
  very_low: [100, 150, 200, 0.9], low: [70, 130, 180, 0.9], medium: [30, 100, 180, 0.9],
  high: [0, 70, 150, 1.0], very_high: [0, 40, 100, 1.0],
};
/** Fill and outline colors (a blue scheme) for one density band. */
export function densityColors(category: DensityCategory): { fill: number[]; outline: number[] } {
  return { fill: DENSITY_FILL[category] || [128, 128, 128, 0.5], outline: DENSITY_OUTLINE[category] || [128, 128, 128, 0.9] };
}

/** Population density; provider failure rejects rather than becoming a valid empty area. */
export async function fetchDemographicAreas(center: CenterPoint, radiusMiles = 100,
  signal?: AbortSignal): Promise<DemographicFeature[]> {
    const features = await queryWithinRadius(POPULATION_SERVICE_URL, center, radiusMiles,
      { maxRecordCount: "100" }, signal);
    return features.flatMap((feature, index) => {
      if (!feature.geometry?.rings) return [];
      const attributes = feature.attributes || {};
      const population = attributes.POPULATION || attributes.POP || 0;
      const sqMiles = attributes.SQMI || attributes.AREA_SQMI || 1;
      const populationDensity = Math.round(population / sqMiles);
      const county = attributes.NAME || attributes.COUNTY_NAME;
      return [{
        geometry: { coordinates: feature.geometry.rings },
        attributes: {
          id: `real_demo_${attributes.OBJECTID || index}_${Date.now()}`,
          name: county || `County ${index}`,
          population: Math.round(population),
          populationDensity,
          housingUnits: Math.round(attributes.HOUSEHOLDS || Math.floor(population * 0.4)),
          densityCategory: categorizeDensity(populationDensity),
          state: attributes.STATE_NAME || attributes.STATE || "Unknown",
          county: county || "Unknown",
          type: "demographic" as const,
          areaType: "county" as const,
          source: "ArcGIS Living Atlas - USA Counties",
          objectId: attributes.OBJECTID || 0,
          sqMiles: Math.round(sqMiles),
        },
      }];
    });
}

/** Federal boundaries; provider failure rejects rather than becoming a valid empty area. */
export async function fetchFederalLand(center: CenterPoint, radiusMiles = 100,
  signal?: AbortSignal): Promise<FederalLandFeature[]> {
    const features = await queryWithinRadius(VEGETATION_SERVICE_URL, center, radiusMiles,
      { resultRecordCount: "50" }, signal);
    return features.flatMap((feature, index) => {
      const coordinates = feature.geometry?.rings || feature.geometry?.coordinates;
      if (!coordinates?.length) return [];
      const attributes = feature.attributes || {};
      return [{
        geometry: { coordinates },
        attributes: {
          id: `federal-land-${attributes.OBJECTID ?? index}`, type: "federalLand" as const,
          name: attributes.unit_name || attributes.NAME || "Federal land",
          agency: attributes.Agency || attributes.AGENCY || "Agency unavailable",
          source: "ArcGIS Living Atlas — federal land boundaries",
        },
      }];
    });
}
