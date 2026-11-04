/**
 * Map graphics: buildMapGraphics makes every graphic without touching the layers, and showMapGraphics swaps
 * them into the layers in one step, so useMapGraphics can drop a build that a newer request replaced.
 */
import type Graphic from "@arcgis/core/Graphic";
import type GraphicsLayer from "@arcgis/core/layers/GraphicsLayer";
import { SYMBOLS } from "@/shared/constants";
import { MPS_TO_MPH, weatherQueriedAt } from "@/shared/domain/incidentObservations";
import { answerAssessments, type ScenarioSelection } from "@/features/fire-exposure/services/assessment";
import { logger } from "@/shared/utils/logger";
import { densityColors, fetchDemographicAreas, fetchFederalLand } from "../services/overlayData";
import type { CenterLocation, FireLocation, MapData, MapLayers, WeatherData } from "../types/types";
import { fireExposureGraphics } from "./fireExposureGraphics";
import { cameraPopupActions, createCenterPopup, createFirePopup } from "./popupGenerators";
import { reportedPerimeterGraphics } from "./reportedPerimeterGraphics";

/** cameraAction: fire and weather popups offer the camera action (only while a camera is available). */
export interface GraphicsOptions {
  cameraAction?: boolean;
}

/** Each layer's graphics, built before any layer changes. */
export interface MapGraphics {
  riskZones: Graphic[];
  center: Graphic[];
  fires: Graphic[];
  wind: Graphic[];
  demographic: Graphic[];
  vegetation: Graphic[];
}
export type OverlayState = "loading" | "available" | "empty" | "unavailable";
export interface OptionalMapGraphics {
  demographic: Graphic[];
  vegetation: Graphic[];
  status: { demographic: OverlayState; vegetation: OverlayState };
}

/** The ArcGIS classes the graphics use, loaded on first use so they stay out of the entry chunk. */
async function arcgisModules(): Promise<{
  Graphic: typeof import("@arcgis/core/Graphic").default;
  Point: typeof import("@arcgis/core/geometry/Point").default;
  Polygon: typeof import("@arcgis/core/geometry/Polygon").default;
  SimpleFillSymbol: typeof import("@arcgis/core/symbols/SimpleFillSymbol").default;
  SimpleMarkerSymbol: typeof import("@arcgis/core/symbols/SimpleMarkerSymbol").default;
  PictureMarkerSymbol: typeof import("@arcgis/core/symbols/PictureMarkerSymbol").default;
}> {
  const [Graphic, Point, Polygon, SimpleFillSymbol, SimpleMarkerSymbol, PictureMarkerSymbol] = await Promise.all([
    import("@arcgis/core/Graphic"), import("@arcgis/core/geometry/Point"), import("@arcgis/core/geometry/Polygon"),
    import("@arcgis/core/symbols/SimpleFillSymbol"), import("@arcgis/core/symbols/SimpleMarkerSymbol"),
    import("@arcgis/core/symbols/PictureMarkerSymbol"),
  ]);
  return { Graphic: Graphic.default, Point: Point.default, Polygon: Polygon.default,
    SimpleFillSymbol: SimpleFillSymbol.default, SimpleMarkerSymbol: SimpleMarkerSymbol.default,
    PictureMarkerSymbol: PictureMarkerSymbol.default };
}
type Arcgis = Awaited<ReturnType<typeof arcgisModules>>;

/** A location as the assistant sends it: the wire FireLocation plus an optional display name. */
type MapLocation = FireLocation & { name?: string };

/** The wind icon and its size in pixels for a measured speed in mph. */
export function windIcon(speedMph: number): { url: string; size: number } {
  const url = speedMph < 6 ? SYMBOLS.wind.light : speedMph < 12 ? SYMBOLS.wind.moderate
    : speedMph < 19 ? SYMBOLS.wind.strong : speedMph < 31 ? SYMBOLS.wind.veryStrong : SYMBOLS.wind.extreme;
  return { url, size: Math.min(40, Math.max(30, 25 + speedMph / 2)) };
}

/** Fire and weather popup actions as ArcGIS autocasts them: the camera action, when offered, is a button. */
function popupActions(cameraAction: boolean): Array<ReturnType<typeof cameraPopupActions>[number] & { type: "button" }> {
  return cameraPopupActions(cameraAction).map(action => ({ ...action, type: "button" as const }));
}

/** A builder's graphics, or none (logged) when it fails, so one bad item never blanks the other layers. */
function orNone(name: string, build: () => Graphic[]): Graphic[] {
  try {
    return build();
  } catch (error) {
    logger.error(`GraphicsLoader: Failed to build ${name}:`, error);
    return [];
  }
}

export async function buildFireExposureGraphics(mapData: MapData, selections: ScenarioSelection = {}): Promise<Graphic[]> {
  const arcgis = await arcgisModules();
  return orNone("fire exposure", () => fireExposureGraphics(arcgis, mapData.fireExposureData ?? [], selections));
}

/** Hydrate retained weather locally without repeating search-area overlay requests. */
export async function buildFireObservationGraphics(mapData: MapData,
  options: GraphicsOptions = {}): Promise<Pick<MapGraphics, "fires" | "wind">> {
  const arcgis = await arcgisModules();
  return {
    fires: orNone("fires", () => fireGraphics(arcgis, mapData, options.cameraAction ?? false)),
    wind: orNone("weather", () => weatherGraphics(arcgis, mapData, options.cameraAction ?? false)),
  };
}

function centerGraphics(arcgis: Arcgis, centerLocation: CenterLocation | null | undefined): Graphic[] {
  if (!centerLocation) return [];
  return [new arcgis.Graphic({
    geometry: new arcgis.Point({ longitude: centerLocation.longitude, latitude: centerLocation.latitude }),
    symbol: new arcgis.SimpleMarkerSymbol(SYMBOLS.location),
    attributes: { name: centerLocation.name || "Search Area", type: "center" },
  })];
}

function fireGraphics(arcgis: Arcgis, mapData: MapData, cameraAction: boolean): Graphic[] {
  return [...reportedPerimeterGraphics(arcgis, answerAssessments(mapData.fireExposureData ?? [], mapData.locations)),
    ...mapData.locations.flatMap((location: MapLocation) => {
    if (!location.fire || !Number.isFinite(location.longitude) || !Number.isFinite(location.latitude)
        || Math.abs(location.longitude) > 180 || Math.abs(location.latitude) > 90) return [];
    const currentWeather = locationWeather(mapData, location);
    const popup = createFirePopup(location.fire, currentWeather);
    return [new arcgis.Graphic({
      geometry: new arcgis.Point({ longitude: location.longitude, latitude: location.latitude }),
      symbol: new arcgis.PictureMarkerSymbol({ url: SYMBOLS.fire.url, width: "28px", height: "35px", xoffset: 0, yoffset: 0 }),
      attributes: { fire: location.fire, currentWeather, type: "fire" },
      popupTemplate: {
        title: popup.title,
        content: popup.content,
        actions: popupActions(cameraAction),
      },
    })];
  })];
}

/** A modeled fire uses its retained weather; unassessed locations keep observations queried at that place. */
function locationWeather(mapData: MapData, location: MapLocation): WeatherData | null | undefined {
  const { latitude, longitude } = location;
  const assessments = mapData.fireExposureData?.filter(item =>
    item.fire.latitude === latitude && item.fire.longitude === longitude);
  const weather = assessments?.find(item => item.weather?.availability === "available")?.weather;
  if (weather) {
    if (Math.abs(weather.query_latitude - latitude) >= 1e-7
        || Math.abs(weather.query_longitude - longitude) >= 1e-7) return null;
    return {
      timestamp: weather.observed_at ?? undefined,
      temperature: { value: weather.temperature_c, unit: "C" },
      relativeHumidity: { value: weather.relative_humidity_percent, unit: "%" },
      wind: { speed: { value: weather.wind_speed_mps, unit: "m/s" },
        direction: { value: weather.wind_from_degrees, unit: "deg" } },
    };
  }
  if (assessments?.length) return null;
  const original = location.currentWeather;
  return weatherQueriedAt(original, latitude, longitude) ? original : null;
}

/** Weather observations: a wind arrow when measured speed and direction exist, else a diamond. */
function weatherGraphics(arcgis: Arcgis, mapData: MapData, cameraAction: boolean): Graphic[] {
  return mapData.locations.flatMap((location: MapLocation) => {
    const { longitude, latitude } = location;
    const currentWeather = locationWeather(mapData, location);
    if (!currentWeather || !Number.isFinite(longitude) || !Number.isFinite(latitude)
      || Math.abs(longitude) > 180 || Math.abs(latitude) > 90) return [];
    const speed = currentWeather.wind?.speed?.value;
    const unit = currentWeather.wind?.speed?.unit || "";
    const direction = currentWeather.wind?.direction?.value;
    let symbol;
    if (typeof speed === "number" && Number.isFinite(speed) && speed > 0
      && typeof direction === "number" && Number.isFinite(direction)) {
      const lower = unit.toLowerCase();
      const mph = lower.includes("km") || lower.includes("kph") ? speed * 0.621371
        : unit === "m/s" ? speed * MPS_TO_MPH : speed;
      const { url, size } = windIcon(mph);
      symbol = new arcgis.PictureMarkerSymbol({ url, width: `${size}px`, height: `${size}px`,
        angle: (direction + 180) % 360, xoffset: location.fire ? 24 : 0, yoffset: location.fire ? 8 : 0 });
    } else {
      symbol = new arcgis.SimpleMarkerSymbol({ style: "diamond", color: "#0f766e", size: 16,
        outline: { color: "white", width: 2 }, xoffset: location.fire ? 24 : 0, yoffset: location.fire ? 8 : 0 });
    }
    const name = location.name || "Weather observation";
    const popup = location.fire ? createFirePopup(location.fire, currentWeather)
      : createCenterPopup({ name, currentWeather });
    return [new arcgis.Graphic({
      // Pixel offsets separate symbols without changing the observed geographic coordinate.
      geometry: new arcgis.Point({ longitude, latitude }),
      symbol,
      attributes: { fire: location.fire, name, currentWeather, type: "weather",
        originalLatitude: latitude, originalLongitude: longitude },
      popupTemplate: {
        title: popup.title,
        content: popup.content,
        actions: popupActions(cameraAction),
      },
    })];
  });
}

/** One polygon overlay around the search area, or none (logged) when its provider fails. */
async function overlayGraphics<T extends { geometry: { coordinates: number[][][] }; attributes: object }>(
  arcgis: Arcgis, centerLocation: CenterLocation | null | undefined, name: string,
  fetchAround: (center: CenterLocation) => Promise<T[]>,
  colors: (feature: T) => { fill: number[]; outline: number[] }): Promise<{graphics: Graphic[]; state: OverlayState}> {
  if (!centerLocation) return {graphics: [], state: "empty"};
  try {
    const graphics = (await fetchAround(centerLocation)).map(feature => {
      const { fill, outline } = colors(feature);
      return new arcgis.Graphic({
        geometry: new arcgis.Polygon({ rings: feature.geometry.coordinates, spatialReference: { wkid: 4326 } }),
        symbol: new arcgis.SimpleFillSymbol({ color: fill, outline: { color: outline, width: 1.5 } }),
        attributes: { ...feature.attributes },
      });
    });
    return {graphics, state: graphics.length ? "available" : "empty"};
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    logger.error(`GraphicsLoader: Failed to load ${name} data:`, error);
    return {graphics: [], state: "unavailable"};
  }
}

/** Optional context is independent of primary observed evidence. */
export async function buildMapOverlayGraphics(centerLocation: CenterLocation, signal?: AbortSignal)
  : Promise<OptionalMapGraphics> {
  const arcgis = await arcgisModules();
  const [demographic, vegetation] = await Promise.all([
    overlayGraphics(arcgis, centerLocation, "population", center => fetchDemographicAreas(center, 100, signal),
      feature => densityColors(feature.attributes.densityCategory)),
    overlayGraphics(arcgis, centerLocation, "federal land", center => fetchFederalLand(center, 100, signal),
      () => ({ fill: [65, 125, 105, 0.12], outline: [65, 125, 105, 0.6] })),
  ]);
  return { demographic: demographic.graphics, vegetation: vegetation.graphics,
    status: { demographic: demographic.state, vegetation: vegetation.state } };
}

/** Build primary evidence without provider overlay requests or layer mutation. */
export async function buildMapGraphics(mapData: MapData, centerLocation: CenterLocation | null | undefined,
  options: GraphicsOptions = {}): Promise<MapGraphics> {
  const cameraAction = options.cameraAction ?? false;
  const arcgis = await arcgisModules();
  return {
    riskZones: orNone("fire exposure", () => fireExposureGraphics(arcgis, mapData.fireExposureData ?? [])),
    center: orNone("the search area", () => centerGraphics(arcgis, centerLocation)),
    fires: orNone("fires", () => fireGraphics(arcgis, mapData, cameraAction)),
    wind: orNone("weather", () => weatherGraphics(arcgis, mapData, cameraAction)),
    demographic: [],
    vegetation: [],
  };
}

/** Replace each layer's graphics with the built ones in one synchronous step. */
export async function showMapGraphics(layers: MapLayers, graphics: MapGraphics,
  options: {optional?: boolean} = {}): Promise<void> {
  const swaps: Array<[GraphicsLayer, Graphic[]]> = [
    [layers.riskZonesLayer, graphics.riskZones], [layers.locationsLayer, graphics.center],
    [layers.firesLayer, graphics.fires], [layers.windLayer, graphics.wind],
  ];
  if (options.optional !== false) swaps.push([layers.demographicLayer, graphics.demographic],
    [layers.vegetationLayer, graphics.vegetation]);
  for (const [layer, items] of swaps) {
    layer.removeAll();
    layer.addMany(items);
  }
}

export function showMapOverlayGraphics(layers: MapLayers, graphics: OptionalMapGraphics): void {
  layers.demographicLayer.removeAll(); layers.demographicLayer.addMany(graphics.demographic);
  layers.vegetationLayer.removeAll(); layers.vegetationLayer.addMany(graphics.vegetation);
}
