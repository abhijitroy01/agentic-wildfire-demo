import type { Feature,MultiPolygon,Polygon } from "geojson";
import type { StudyBounds } from "../artifacts";

const EARTH_RADIUS_M = 6371008.8;
const RADIANS = Math.PI / 180;
const MAP_LATITUDE_LIMIT = 85.0511287798066;
type Coordinate = number[];
export type CameraBounds = [[number,number],[number,number]];
const clampCameraLatitude = (latitude: number): number =>
  Math.max(-MAP_LATITUDE_LIMIT,Math.min(MAP_LATITUDE_LIMIT,latitude));
const wrapAround = (longitude: number, center: number): number =>
  center + ((longitude-center+540)%360)-180;

/** Bounds are saved WGS84 source geometry; camera longitude may unwrap beside the incident at the date line. */
export function studyCameraBounds(latitude: number, longitude: number, radiusM: number,
  saved?: StudyBounds): CameraBounds {
  const boxes = saved?.filter(box => box.length === 4 && box.every(Number.isFinite)
    && box[0] >= -180 && box[2] <= 180 && box[0] <= box[2]
    && box[1] >= -90 && box[3] <= 90 && box[1] <= box[3]);
  if (boxes?.length && boxes.length === saved?.length) {
    const xs = boxes.flatMap(box => [wrapAround(box[0],longitude),wrapAround(box[2],longitude)]);
    return [[Math.min(...xs),clampCameraLatitude(Math.min(...boxes.map(box => box[1])))],
      [Math.max(...xs),clampCameraLatitude(Math.max(...boxes.map(box => box[3])))]];
  }
  const angle = Math.max(0,radiusM) / EARTH_RADIUS_M, deltaLat = angle / RADIANS;
  const ratio = Math.sin(angle) / Math.cos(latitude*RADIANS);
  const deltaLon = Math.abs(ratio) >= 1 ? 180 : Math.asin(Math.abs(ratio)) / RADIANS;
  return [[longitude-deltaLon,clampCameraLatitude(latitude-deltaLat)],
    [longitude+deltaLon,clampCameraLatitude(latitude+deltaLat)]];
}

function clipLongitude(ring: Coordinate[], boundary: number, keepGreater: boolean): Coordinate[] {
  const result: Coordinate[] = [];
  let previous = ring[ring.length-1];
  for (const current of ring) {
    const inside = (point: Coordinate): boolean => keepGreater ? point[0] >= boundary : point[0] <= boundary;
    if (inside(current) !== inside(previous)) {
      const fraction = (boundary-previous[0]) / (current[0]-previous[0]);
      result.push([boundary,previous[1]+fraction*(current[1]-previous[1])]);
    }
    if (inside(current)) result.push([...current]);
    previous = current;
  }
  return result;
}

function splitPolygon(ring: Coordinate[]): Polygon | MultiPolygon {
  const parts: number[][][][] = [];
  for (const shift of [-360,0,360]) {
    const clipped = clipLongitude(clipLongitude(ring,-180+shift,true),180+shift,false)
      .map(point => [point[0]-shift,point[1]]);
    if (clipped.length >= 3) {
      clipped.push([...clipped[0]]);
      parts.push([clipped]);
    }
  }
  return parts.length === 1 ? {type:"Polygon",coordinates:parts[0]} : {type:"MultiPolygon",coordinates:parts};
}

/** Display-only geodesic circle from the recording's assumed radius; never an observed fire perimeter. */
export function scenarioHazardGeometry(longitude: number, latitude: number, radiusM: number): Feature<Polygon | MultiPolygon> {
  const angle = radiusM / EARTH_RADIUS_M, lat = latitude*RADIANS, lon = longitude*RADIANS;
  const ring = Array.from({length:64},(_,index) => {
    const bearing = -index*2*Math.PI/64;
    const nextLat = Math.asin(Math.sin(lat)*Math.cos(angle)+Math.cos(lat)*Math.sin(angle)*Math.cos(bearing));
    const nextLon = lon+Math.atan2(Math.sin(bearing)*Math.sin(angle)*Math.cos(lat),
      Math.cos(angle)-Math.sin(lat)*Math.sin(nextLat));
    return [wrapAround(nextLon/RADIANS,longitude),nextLat/RADIANS];
  });
  return {type:"Feature",properties:{radius_m:radiusM},geometry:splitPolygon(ring)};
}

export function studyAreaGeometry(latitude: number, longitude: number, radiusM: number,
  saved?: StudyBounds): Feature<Polygon | MultiPolygon> {
  const [[west,south],[east,north]] = studyCameraBounds(latitude,longitude,radiusM,saved);
  return {type:"Feature",properties:{},geometry:splitPolygon([
    [west,south],[east,south],[east,north],[west,north],
  ])};
}
