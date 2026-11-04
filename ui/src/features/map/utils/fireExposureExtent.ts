import { isExposureGeometry } from "@/features/fire-exposure/services/assessment";
import type { CenterLocation, MapData } from "../types/types";

export const BASEMAP_LATITUDE_LIMIT = 85.0511287798066;
const MAX_INITIAL_ZOOM = 12;
const MAX_TERRAIN_FOCUS_ZOOM = 15;
const MAX_CAMERA_POINTS = 65536;

interface ExposureExtent {
  xmin: number;
  ymin: number;
  xmax: number;
  ymax: number;
  zoom: number;
}

function mercatorLatitude(latitude: number): number {
  const bounded = Math.max(-BASEMAP_LATITUDE_LIMIT, Math.min(BASEMAP_LATITUDE_LIMIT, latitude));
  return Math.log(Math.tan(Math.PI / 4 + bounded * Math.PI / 360));
}

function geographicLatitude(value: number): number {
  return (2 * Math.atan(Math.exp(value)) - Math.PI / 2) * 180 / Math.PI;
}

/** Fit this answer's saved geometry in one world copy. Only the camera bounds respect the basemap's latitude limit;
 * source coordinates, fire markers and GeoJSON remain unchanged. No scenario selection or data retrieval occurs. */
export function fireExposureInitialExtent(mapData: MapData, centerLocation: CenterLocation | null,
  viewport: { width: number; height: number; terrainFocus?: boolean }): ExposureExtent | null {
  const coordinates: number[][] = [];
  for (const assessment of mapData.fireExposureData ?? []) {
    if (assessment.availability !== "available") continue;
    for (const feature of assessment.geojson.features ?? []) {
      const geometry = feature.geometry;
      if (!isExposureGeometry(geometry)) continue;
      const polygons = geometry.type === "Polygon" ? [geometry.coordinates]
        : geometry.type === "MultiPolygon" ? geometry.coordinates : [];
      for (const polygon of polygons) for (const ring of polygon) for (const point of ring) {
        if (coordinates.length === MAX_CAMERA_POINTS) return null;
        coordinates.push(point);
      }
    }
  }
  if (!coordinates.length) return null;
  const locations = [...mapData.locations, ...(centerLocation ? [centerLocation] : [])];
  for (const location of locations) {
    if (Number.isFinite(location.longitude) && Number.isFinite(location.latitude)
        && Math.abs(location.longitude) <= 180 && Math.abs(location.latitude) <= 90) {
      coordinates.push([location.longitude, location.latitude]);
    }
  }

  // The gap outside the shortest longitude arc is excluded, so ±180° neighbors stay beside each other.
  const longitudes = [...new Set(coordinates.map(point => ((point[0] % 360) + 360) % 360))].sort((a, b) => a - b);
  let largestGap = -1, start = longitudes[0];
  for (let index = 0; index < longitudes.length; index += 1) {
    const next = longitudes[(index + 1) % longitudes.length] + (index === longitudes.length - 1 ? 360 : 0);
    const gap = next - longitudes[index];
    if (gap > largestGap) { largestGap = gap; start = next % 360; }
  }
  const span = 360 - largestGap;
  const anchor = mapData.locations.find(location => location.fire
    && Number.isFinite(location.longitude) && Number.isFinite(location.latitude)
    && Math.abs(location.longitude) <= 180 && Math.abs(location.latitude) <= 90)?.longitude ?? coordinates[0][0];
  const longitudeCenter = start + span / 2;
  const center = longitudeCenter + 360 * Math.round((anchor - longitudeCenter) / 360);
  const halfWidth = Math.min(180, Math.max(span * 1.25 / 2, 1e-6));
  const latitudes = coordinates.map(point => mercatorLatitude(point[1]));
  const low = Math.min(...latitudes), high = Math.max(...latitudes);
  const middle = (low + high) / 2, halfHeight = Math.max((high - low) * 1.25 / 2, 1e-6);
  const bottom = Math.max(-Math.PI, middle - halfHeight), top = Math.min(Math.PI, middle + halfHeight);
  const width = Number.isFinite(viewport.width) && viewport.width > 0 ? viewport.width : 1000;
  const height = Number.isFinite(viewport.height) && viewport.height > 0 ? viewport.height : 800;
  const fitZoom = Math.floor(Math.log2(Math.min(width / (256 * halfWidth / 180),
    height / (256 * (top - bottom) / (2 * Math.PI)))));
  return {
    xmin: center - halfWidth, xmax: center + halfWidth,
    ymin: geographicLatitude(bottom), ymax: geographicLatitude(top),
    zoom: Math.max(0, Math.min(viewport.terrainFocus ? MAX_TERRAIN_FOCUS_ZOOM : MAX_INITIAL_ZOOM, fitZoom)),
  };
}
