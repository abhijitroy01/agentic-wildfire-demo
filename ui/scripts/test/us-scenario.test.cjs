/* U.S. coordinate/corridor input and replay geometry; provider coverage remains a server receipt. */
const test = require("node:test");
const assert = require("node:assert/strict");
const {load} = require("../ts-module.cjs");
const {corridorId,corridorLabel,corridorFeatureMatches} = load("features/evacuation/artifacts.ts");
const {studyCameraBounds,studyAreaGeometry,scenarioHazardGeometry} = load("features/evacuation/components/mapGeometry.ts");
const area = ring => ring.slice(1).reduce((sum,point,index) =>
  sum+ring[index][0]*point[1]-point[0]*ring[index][1],0)/2;

test("OSM corridor identity and labels stay separate; retained Caltrans recordings still select their exact geometry", () => {
  const road = {corridor_id:"osm:name:01234567890123456789",label:"4th Avenue",name:"4th Avenue",source:"OpenStreetMap"};
  assert.equal(corridorId(road),road.corridor_id);
  assert.equal(corridorLabel(road.corridor_id,road),"4th Avenue");
  assert.equal(corridorFeatureMatches({corridor_id:road.corridor_id},road.corridor_id),true);
  assert.equal(corridorFeatureMatches({corridor_id:"osm:way:2"},road.corridor_id),false);
  const legacy = {route_type:"State",route_number:191};
  assert.equal(corridorId(legacy),"State 191");
  assert.equal(corridorLabel("State 191",legacy),"State 191");
  assert.equal(corridorFeatureMatches({Route:191,RouteType:"State"},"State 191"),true);
  assert.equal(corridorFeatureMatches({Route:191,RouteType:"US"},"State 191"),false);
  assert.equal(corridorLabel("auto"),"Road discovery pending");
  assert.equal(corridorLabel(road.corridor_id,{...road,corridor_id:"other",label:"Another road"}),"Selected road corridor");
});

test("explicit coordinates preserve zero, Seattle, Alaska and Hawaii and reject incomplete/nonfinite values", () => {
  const {scenarioCoordinates} = load("features/evacuation/components/CoordinateEntry.tsx");
  assert.deepEqual(scenarioCoordinates("0","0"),{lat:0,lon:0});
  for (const [lat,lon] of [[47.603,-122.33],[64.2,-149.49],[19.7,-155.1],[51.5,179.99]]) {
    assert.deepEqual(scenarioCoordinates(String(lat),String(lon)),{lat,lon});
  }
  for (const pair of [["","0"],["0"," "],["NaN","0"],["91","0"],["0","181"]]) {
    assert.equal(scenarioCoordinates(...pair),null);
  }
});

test("failed preparation retries its saved source and never substitutes a fire name for an unavailable assessment", () => {
  const {incidentLocationInput} = load("features/evacuation/services/locationInput.ts");
  const location = {reported_address:"Reported fixture fire",matched_address:"Reported fixture fire",lat:0,lon:0};
  const saved = (origin,extra = {}) => ({preset_id:null,location:{...location,origin,...extra}});
  assert.deepEqual(incidentLocationInput(saved("reported_fire",{source_assessment_id:"immutable-assessment"})),
    {fire_assessment_id:"immutable-assessment"});
  assert.throws(() => incidentLocationInput(saved("reported_fire")),/reference is unavailable/);
  assert.deepEqual(incidentLocationInput(saved("user_coordinates")),
    {coordinates:{lat:0,lon:0,label:"Reported fixture fire"}});
  assert.deepEqual(incidentLocationInput(saved("geocoded_place")),{reported_place:"Reported fixture fire"});
  assert.deepEqual(incidentLocationInput(saved("geocoded_address")),{reported_address:"Reported fixture fire"});
  assert.deepEqual(incidentLocationInput({...saved("geocoded_address"),preset_id:"palisades-2025"}),
    {preset_id:"palisades-2025"});
});

test("split Alaska study bounds fit around the retained point without changing WGS84 source boxes", () => {
  const boxes = [[179.9,51.4,180,51.6],[-180,51.4,-179.9,51.6]], before = JSON.stringify(boxes);
  const bounds = studyCameraBounds(51.5,179.99,4000,boxes);
  assert.ok(Math.abs(bounds[0][0]-179.9)<1e-9 && Math.abs(bounds[1][0]-180.1)<1e-9,bounds);
  assert.equal(JSON.stringify(boxes),before);
  const geometry = studyAreaGeometry(51.5,179.99,4000,boxes).geometry;
  assert.equal(geometry.type,"MultiPolygon");
  assert.equal(geometry.coordinates.length,2);
  for (const [ring] of geometry.coordinates) {
    assert.deepEqual(ring[0],ring.at(-1));
    assert.ok(ring.every(([lon,lat]) => Math.abs(lon)<=180 && Math.abs(lat)<=90));
    assert.ok(ring.slice(1).every((point,index) => Math.abs(point[0]-ring[index][0])<=180));
  }
});

test("scenario hazard display uses geodesic distance and splits date-line crossings without implying observed closures", () => {
  const haversine = (a,b) => {
    const r=Math.PI/180,lat1=a[1]*r,lat2=b[1]*r;
    return 6371008.8*2*Math.asin(Math.sqrt(Math.sin((lat2-lat1)/2)**2+
      Math.cos(lat1)*Math.cos(lat2)*Math.sin((b[0]-a[0])*r/2)**2));
  };
  for (const center of [[-122.33,47.6],[-149.49,64.2],[-155.1,19.7]]) {
    const feature = scenarioHazardGeometry(...center,2000);
    assert.equal(feature.geometry.type,"Polygon");
    assert.ok(area(feature.geometry.coordinates[0])>0,"GeoJSON exterior follows the right-hand rule");
    for (const point of feature.geometry.coordinates[0]) {
      assert.ok(Math.abs(haversine(center,point)-2000)<0.001);
    }
  }
  const wrapped = scenarioHazardGeometry(179.99,51.5,4000);
  assert.equal(wrapped.geometry.type,"MultiPolygon");
  assert.deepEqual(wrapped.properties,{radius_m:4000},"No closure or observed-fire claims in display geometry");
  for (const [ring] of wrapped.geometry.coordinates) {
    assert.ok(area(ring)>0,"Split GeoJSON exteriors remain counterclockwise");
    assert.ok(ring.every(([lon]) => Math.abs(lon)<=180));
    assert.ok(ring.slice(1).every((point,index) => Math.abs(point[0]-ring[index][0])<=180));
  }
});

test("location observation and forecast default timezone is explicitly UTC", () => {
  const {INCIDENT_TIME_ZONE,observationTime} = load("shared/domain/incidentObservations.ts");
  assert.equal(INCIDENT_TIME_ZONE,"UTC");
  assert.match(observationTime("2026-10-26T16:00:00Z"),/4:00 PM UTC/);
});
