/** Shared observation projection for cards and maps. Missing measurements stay unavailable. */
export interface IncidentObservation {
  id: string; latitude: number; longitude: number; name: string;
  /** An immutable broker-owned Esri observation receipt, distinct from this display selection. */
  sourceObservationId?: string;
  fire?: Record<string, unknown>; weather?: Record<string, unknown>;
}
export function incidentObservations(snapshot: unknown): IncidentObservation[] {
  if (!snapshot || typeof snapshot !== "object") return [];
  const content = (snapshot as {content?: {mapData?: {locations?: unknown[]}}}).content;
  return (content?.mapData?.locations || []).flatMap((entry, index) => {
    if (!entry || typeof entry !== "object") return [];
    const item = entry as Record<string, unknown>;
    if (typeof item.latitude !== "number" || !Number.isFinite(item.latitude) ||
      typeof item.longitude !== "number" || !Number.isFinite(item.longitude) ||
      Math.abs(item.latitude) > 90 || Math.abs(item.longitude) > 180) return [];
    const fire = item.fire && typeof item.fire === "object" ? item.fire as Record<string, unknown> : undefined;
    const weather = item.currentWeather && typeof item.currentWeather === "object" ?
      item.currentWeather as Record<string, unknown> : undefined;
    if (!fire && !weather) return [];
    return [{ id: `observation-${item.latitude}-${item.longitude}-${String(fire?.name || item.name || index)}`, latitude:item.latitude, longitude:item.longitude,
      name:String(fire?.name || item.name || "Weather observation"), fire, weather }];
  });
}
/** Miles per hour in one meter per second: UI values use U.S. units. */
export const MPS_TO_MPH = 2.236936;
/** Legacy observations may omit a query coordinate; when retained, it must name this observed place. */
export function weatherQueriedAt(weather: {
  queryLocation?: { latitude?: number; longitude?: number; lat?: number; lon?: number };
} | null | undefined, latitude: number, longitude: number): boolean {
  const query = weather?.queryLocation;
  if (!query) return true;
  const queryLatitude = query.latitude ?? query.lat, queryLongitude = query.longitude ?? query.lon;
  return typeof queryLatitude === "number" && Number.isFinite(queryLatitude)
    && typeof queryLongitude === "number" && Number.isFinite(queryLongitude)
    && Math.abs(queryLatitude - latitude) < 1e-7 && Math.abs(queryLongitude - longitude) < 1e-7;
}

export function observationValue(value: unknown): string {
  if (value == null) return "Unavailable";
  if (typeof value === "number" && !Number.isFinite(value)) return "Unavailable";
  if (typeof value === "object") {
    const item = value as Record<string, unknown>;
    if ("value" in item) {
      const unit = String(item.unit || item.unitCode || "");
      const conversions: Record<string, [number, number, string]> = {
        C:[1.8,32,"°F"], celsius:[1.8,32,"°F"], "wmoUnit:degC":[1.8,32,"°F"],
        F:[1,0,"°F"], degF:[1,0,"°F"], percent:[1,0,"%"], degrees:[1,0,"°"],
        "m/s":[MPS_TO_MPH,0,"mph"], "km/h":[0.621371,0,"mph"],
      };
      if (typeof item.value === "number" && Number.isFinite(item.value)) {
        const [scale, offset, label] = conversions[unit] || [1,0,unit];
        return `${(item.value*scale+offset).toLocaleString("en-US",{maximumFractionDigits:1})} ${label}`.trim();
      }
      return item.value == null || (typeof item.value === "number" && !Number.isFinite(item.value))
        ? "Unavailable" : `${item.value} ${unit}`.trim();
    }
    return Object.entries(item).map(([key, nested]) => `${key}: ${observationValue(nested)}`).join(" · ");
  }
  return String(value);
}
/** Source instants remain UTC unless a verified incident timezone is supplied. */
export const INCIDENT_TIME_ZONE = "UTC";
export function observationTime(value: unknown, timeZone = INCIDENT_TIME_ZONE): string {
  if (typeof value !== "string" && typeof value !== "number") return "Time unavailable";
  const date = new Date(value);
  return Number.isFinite(date.getTime()) ? date.toLocaleString("en-US", {
    month:"short", day:"numeric", hour:"numeric", minute:"2-digit", timeZone, timeZoneName:"short",
  }) : "Time unavailable";
}
export function weatherMetrics(weather?: Record<string, unknown>): Array<[string, string]> {
  const wind = weather?.wind as Record<string, unknown> | undefined;
  const precipitation = weather?.precipitation as Record<string, unknown> | undefined;
  return [["Temperature", observationValue(weather?.temperature)], ["Wind", observationValue(wind?.speed)],
    ["Gusts", observationValue(wind?.gust)], ["Humidity", observationValue(weather?.relativeHumidity)],
    ["Precipitation", observationValue(precipitation?.amount)],
    ["Rain probability", observationValue(precipitation?.probability)]];
}
export function observationDetails(observation: IncidentObservation): Array<[string, string]> {
  const entries: Array<[string,string]> = [];
  if (observation.fire) {
    const fire = observation.fire;
    entries.push(["Reported area", fire.acresBurned == null ? "Unavailable" : `${Number(fire.acresBurned).toLocaleString()} acres`],
      ["Containment", fire.percentContained == null ? "Unavailable" : `${fire.percentContained}%`],
      ["Distance", fire.distanceMiles == null ? "Unavailable" : `${fire.distanceMiles} miles`],
      ["Fire updated", observationTime(fire.lastUpdated)]);
  }
  if (observation.weather) {
    const weather = observation.weather;
    const wind = weather.wind as Record<string, unknown> | undefined;
    entries.push(...weatherMetrics(weather), ["Wind direction", observationValue(wind?.direction)],
      ["Dew point", observationValue(weather.dewpoint)], ["Heat index", observationValue(weather.heatIndex)],
      ["Conditions", observationValue(weather.weatherDescription)], ["Weather time", observationTime(weather.timestamp)]);
  }
  return entries;
}
