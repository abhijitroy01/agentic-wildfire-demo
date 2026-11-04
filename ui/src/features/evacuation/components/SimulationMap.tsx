import type { Feature,FeatureCollection,Point } from "geojson";
import * as maplibregl from "maplibre-gl";
import mapWorkerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url";
import "maplibre-gl/dist/maplibre-gl.css";
import { useEffect,useMemo,useRef,useState } from "react";
import { corridorFeatureMatches } from "../artifacts";
import type { Discovery,FocusRequest,Frame,Network,Policy,Result,Road,Scenario,Sources,StudyBounds } from "../artifacts";
import { scenarioHazardGeometry,studyAreaGeometry,studyCameraBounds } from "./mapGeometry";
import { crewDetailLines,crewDetails } from "./crews";
import { clock } from "./replay";
import { feet,mph,speedMph } from "./units";
import { observationDetails, type IncidentObservation } from "@/shared/domain/incidentObservations";
maplibregl.setWorkerUrl(mapWorkerUrl);
export type { Frame } from "../artifacts";
/** Default map element id. A second mounted map (the Traffic Lab) must pass its own `id`. */
export const REPLAY_MAP_ID = "simulation-replay-map";
const EMPTY: FeatureCollection = { type: "FeatureCollection", features: [] };
function point(coordinates: number[], properties = {}): Feature<Point> {
  return { type: "Feature", geometry: { type: "Point", coordinates }, properties };
}
/** The least zoom Locate eases to, so the located crew stands apart from nearby traffic. */
const LOCATE_ZOOM = 16;
/** Locate places the crew this many pixels below the map centre, so its popup clears the map caption above it. */
const LOCATE_OFFSET_Y = 90;
/** Draw the Locate outline at the crew's position in this frame, or clear it (no crew, or no position). */
function outlineCrew(m: maplibregl.Map, frame: Frame | null | undefined, id: string | null): void {
  const v = id ? frame?.vehicles.find(vehicle => vehicle[0] === id) : undefined;
  (m.getSource("crew-focus") as maplibregl.GeoJSONSource).setData(
    { type: "FeatureCollection", features: v ? [point([v[1], v[2]], { id: v[0] })] : [] });
}
/** Locate popup content: the same crew details as the crew's table row. */
function crewPopupContent(heading: string, lines: [string, string][], note: string): HTMLElement {
  const content = document.createElement("section"), title = document.createElement("strong");
  content.className = "map-inspection"; title.textContent = heading;
  content.append(title);
  for (const [label, value] of lines) {
    const line = document.createElement("p"), name = document.createElement("b");
    name.textContent = `${label}: `; line.append(name, document.createTextNode(value)); content.append(line);
  }
  const small = document.createElement("small"); small.textContent = note; content.append(small);
  return content;
}
/** MapLibre replay map: roads, hazard, vehicles, crews, traffic and congestion layers. */
export function SimulationMap({ id = REPLAY_MAP_ID, scenario, discovery, network, result, policy, frame, sources, onInspect,
  focus, focusRequest, observations = [], observationFocus = 0, selectedObservation, onSelectObservation, preparedGates = [], studyBounds,
  showStudyArea = true, showRoutes, showTrails, showTraffic, showCongestion }: { id?: string; scenario: Pick<Scenario, "lat" | "lon" | "radius_km" | "highway"> & { hazard_radius_m?: number }; discovery?: Discovery | null; network?: Network | null;
  result?: Result | null; policy?: Policy; frame?: Frame | null; sources?: Sources | null; onInspect?: () => void;
  focus?: string; focusRequest?: FocusRequest | null; preparedGates?: { coordinate: number[] }[]; showRoutes?: boolean; showTrails?: boolean;
  showTraffic?: boolean; showCongestion?: boolean; observations?: IncidentObservation[]; observationFocus?: number;
  selectedObservation?: string | null; onSelectObservation?: (id: string) => void;
  studyBounds?: StudyBounds; showStudyArea?: boolean }) : React.JSX.Element {
  const savedBounds = result?.network.study_bounds || studyBounds || discovery?.study_bounds;
  const observationMarkers = useRef(new Map<string, maplibregl.Marker>());
  const selectObservation = useRef(onSelectObservation); selectObservation.current = onSelectObservation;
  const initialCenter = useRef<[number, number]>([scenario.lon, scenario.lat]);
  const focusContext = useRef({ scenario, result, frame, network, savedBounds });
  focusContext.current = { scenario, result, frame, network, savedBounds };
  const element = useRef<HTMLDivElement>(null);
  const map = useRef<maplibregl.Map | null>(null);
  const popup = useRef<maplibregl.Popup | null>(null);
  /** The crew Locate outlined; the outline follows it through replay until another map focus is requested. */
  const located = useRef<string | null>(null);
  const inspect = useRef(onInspect);
  inspect.current = onInspect;
  const crewTracks = useMemo(() => {
    const tracks: Record<string, { t: number; coordinate: number[] }[]> = {};
    for (const f of policy?.frames || []) {
      for (const v of f.vehicles) if (v[4]) (tracks[v[0]] ||= []).push({ t: f.t, coordinate: [v[1], v[2]] });
    }
    return tracks;
  }, [policy]);
  const [ready, setReady] = useState(false);
  const [mapError, setMapError] = useState(false);
  useEffect(() => {
    if (!element.current) return;
    const m = new maplibregl.Map({
      container: element.current, center: initialCenter.current, zoom: 11,
      style: { version: 8, sources: {
        osm: { type: "raster", tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"], tileSize: 256,
          attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap contributors</a>' },
      }, layers: [{ id: "osm", type: "raster", source: "osm", paint: { "raster-saturation": -0.8, "raster-opacity": 0.64 } }] },
    });
    map.current = m;
    // Hidden evidence views and responsive sidebar changes can resize the
    // container after the window event. Follow its actual visible dimensions.
    const resizeObserver = new ResizeObserver(() => {
      if (element.current?.clientWidth && element.current?.clientHeight) m.resize();
    });
    resizeObserver.observe(element.current);
    m.addControl(new maplibregl.NavigationControl(), "top-right");
    m.on("error", () => setMapError(true));
    m.on("load", () => {
      ["roads", "highways", "fire", "hazard", "perimeters", "vehicles", "gates", "closures",
        "crew-routes", "crew-trails", "crew-bases", "crew-stages", "crew-focus", "traffic-speeds", "traffic-focus", "study-area"].forEach(
        id => m.addSource(id, { type: "geojson", data: EMPTY }));
      m.addLayer({ id: "study-area", type: "line", source: "study-area",
        paint: { "line-color": "#365d5b", "line-width": 2, "line-dasharray": [4, 3] } });
      m.addLayer({ id: "perimeters", type: "fill", source: "perimeters",
        paint: { "fill-color": "#c65835", "fill-opacity": 0.14 } });
      m.addLayer({ id: "perimeter-lines", type: "line", source: "perimeters",
        paint: { "line-color": "#a04d25", "line-width": 1.5, "line-dasharray": [3, 2] } });
      m.addLayer({ id: "roads", type: "line", source: "roads",
        paint: { "line-color": "#526f78", "line-width": 1.7, "line-opacity": 0.82 } });
      m.addLayer({ id: "highways", type: "line", source: "highways",
        paint: { "line-color": "#176894", "line-width": 4.5 } });
      m.addLayer({ id: "traffic-focus", type: "line", source: "traffic-focus",
        paint: { "line-color": "#f6c344", "line-width": 10, "line-opacity": 0.92 } });
      m.addLayer({ id: "traffic-speeds", type: "line", source: "traffic-speeds",
        paint: { "line-width": 5, "line-dasharray": [2, 1], "line-color": [
          "step", ["/", ["get", "provider_speed_kmh"], ["get", "free_flow_kmh"]],
          "#994080", 0.4, "#c67e20", 0.75, "#198269",
        ] } });
      m.addLayer({ id: "hazard", type: "fill", source: "hazard",
        paint: { "fill-color": "#dd553e", "fill-opacity": 0.2 } });
      m.addLayer({ id: "hazard-line", type: "line", source: "hazard",
        paint: { "line-color": "#c74836", "line-width": 2, "line-dasharray": [2, 2] } });
      m.addLayer({ id: "closures", type: "line", source: "closures",
        paint: { "line-color": "#a92c2c", "line-width": 5 } });
      m.addLayer({ id: "crew-routes", type: "line", source: "crew-routes",
        paint: { "line-color": "#7954a0", "line-width": 2.5, "line-dasharray": [3, 2], "line-opacity": 0.7 } });
      m.addLayer({ id: "crew-trails", type: "line", source: "crew-trails",
        paint: { "line-color": "#ed7a2b", "line-width": 3, "line-opacity": 0.85 } });
      m.addLayer({ id: "crew-bases", type: "circle", source: "crew-bases",
        paint: { "circle-color": "#7954a0", "circle-radius": 7, "circle-stroke-width": 2, "circle-stroke-color": "#fff" } });
      m.addLayer({ id: "crew-stages", type: "circle", source: "crew-stages",
        paint: { "circle-color": "#fff", "circle-radius": 8, "circle-stroke-width": 3, "circle-stroke-color": "#ed7a2b" } });
      m.addLayer({ id: "gates", type: "circle", source: "gates",
        paint: { "circle-color": "#176c59", "circle-radius": 9, "circle-stroke-width": 3, "circle-stroke-color": "#fff" } });
      m.addLayer({ id: "fire", type: "circle", source: "fire",
        paint: { "circle-color": "#fff", "circle-radius": 9, "circle-stroke-width": 3, "circle-stroke-color": "#b73927" } });
      m.addLayer({ id: "fire-core", type: "circle", source: "fire",
        paint: { "circle-color": "#b73927", "circle-radius": 3 } });
      m.addLayer({ id: "slow-vehicles", type: "circle", source: "vehicles", layout: { visibility: "none" },
        paint: { "circle-radius": ["case", ["<", ["get", "speed"], 0.5], 10, 0],
          "circle-color": "#291f68", "circle-opacity": 0.42 } });
      m.addLayer({ id: "vehicles", type: "circle", source: "vehicles", paint: {
        "circle-color": ["case", ["==", ["get", "crew"], 1], "#ef5436", "#1575ba"],
        "circle-radius": ["case", ["==", ["get", "crew"], 1], 7, 4],
        "circle-stroke-width": 1.2, "circle-stroke-color": "#fff",
      } });
      // Outline ring around the crew Locate selected; drawn above the vehicle dots and not clickable.
      m.addLayer({ id: "crew-focus", type: "circle", source: "crew-focus", paint: {
        "circle-radius": 13, "circle-opacity": 0, "circle-stroke-width": 3, "circle-stroke-color": "#1f2a44",
      } });
      const clickable = ["vehicles", "crew-stages", "crew-bases", "fire", "gates", "closures",
        "crew-trails", "crew-routes", "traffic-speeds", "highways", "roads", "hazard", "perimeters"];
      m.on("click", event => {
        const hits = m.queryRenderedFeatures(event.point, { layers: clickable });
        const feature = clickable.map(id => hits.find(f => f.layer.id === id)).find(Boolean);
        if (!feature) return;
        inspect.current?.();
        const props = feature.properties;
        let title = "", explanation = "";
        switch (feature.layer.id) {
          case "traffic-speeds":
            title = `TomTom snapshot · ${mph(Number(props.provider_speed_kmh))}`;
            explanation = `Free flow ${mph(Number(props.free_flow_kmh))}; applied SUMO limit ${speedMph(Number(props.speed_limit_m_s))}. Fetched ${new Date(Number(props.retrieved_at) * 1000).toLocaleString()}. Approximate directional match; individual observation time is unknown. This is a frozen speed input, not a tracked vehicle or live replay.`;
            break;
          case "vehicles": {
            const speed = Number(props.speed);
            const { result, frame } = focusContext.current;
            const crew = Number(props.crew) && result && frame ? crewDetails(result, frame, String(props.id)) : null;
            title = `${Number(props.crew) ? "Fire crew" : "Evacuation vehicle"}${crew ? ` · ${crew.name}` : ""} · ${speedMph(speed)}`;
            explanation = `Recorded at ${clock(Number(props.t))}. ${speed < 0.5 ? "Stopped or very slow. This could be a queue, signal, closure, or scheduled crew stop; the recording does not identify the exact cause." : "Moving through the simulated road network."}`;
            break;
          }
          case "closures":
            title = "Road segment closed by the scenario";
            explanation = "Any intersection with the assumed hazard closes this entire directional segment to new entry, even beyond the circle. Vehicles already on it are retained and can attempt to leave. This is not an observed road closure.";
            break;
          case "crew-bases":
            title = "Synthetic crew origin";
            explanation = "An assigned starting point near the outside of the modeled area, not an actual fire station or live dispatch location.";
            break;
          case "crew-stages":
            title = "Assigned crew staging segment";
            explanation = "A synthetic destination outside the initial hazard buffer. Crews have an assigned two-minute stop here, then attempt to return to an exit. The expanding hazard can later affect access.";
            break;
          case "crew-routes":
            title = "Planned crew route";
            explanation = "The route assigned at the start of the simulation, including travel to staging and the attempted return. The dashed line does not prove that a crew completed the route.";
            break;
          case "crew-trails":
            title = "Recorded crew movement";
            explanation = "This orange trail connects saved crew positions up to the selected replay time. It shows simulated movement already recorded, rather than a future route or live vehicle feed.";
            break;
          case "fire":
            title = "Your fire coordinate";
            explanation = "The center of the assumed expanding hazard. This point does not establish an observed ignition location or fire perimeter.";
            break;
          case "gates":
            title = `Assumed exit gate ${props.number}`;
            explanation = "A vehicle reaching this gate counts as exiting the model. The location is not a verified safe refuge and downstream traffic is outside this simulation.";
            break;
          case "hazard":
            title = `Assumed hazard · ${feet(Number(props.radius_m))} radius`;
            explanation = "This area comes from the scenario's start time, radius and growth rate. It is not a live fire boundary or a forecast. Entering it contributes to cumulative exposure.";
            break;
          case "perimeters":
            title = "Observed WFIGS perimeter";
            explanation = "Context from the displayed source snapshot. Check capture freshness under Data coverage. This outline does not control the scenario's simulated closures.";
            break;
          default:
            title = feature.layer.id === "highways" ? "Selected road corridor" : "Modeled road segment";
            explanation = "This segment is not marked closed by the scenario at this replay time. That is not a statement about real-world road safety or access.";
        }
        const content = document.createElement("div");
        content.className = "map-inspection";
        const heading = document.createElement("strong"), body = document.createElement("p");
        heading.textContent = title; body.textContent = explanation;
        content.append(heading, body);
        popup.current?.remove();
        popup.current = new maplibregl.Popup({ maxWidth: "310px" })
          .setLngLat(event.lngLat).setDOMContent(content).addTo(m);
      });
      m.on("mousemove", event => {
        m.getCanvas().style.cursor = m.queryRenderedFeatures(event.point, { layers: clickable }).length ? "pointer" : "";
      });
      setReady(true);
    });
    return () => { resizeObserver.disconnect(); m.remove(); map.current = null; };
  }, []);
  useEffect(() => {
    if (!ready || !map.current) return;
    const m = map.current;
    const markers = observations.map(observation => {
      const button = document.createElement("button");
      button.type = "button"; button.className = "incident-observation-marker";
      button.textContent = observation.fire ? "F" : "W";
      button.setAttribute("aria-label", `${observation.fire ? "Reported fire" : "Weather"}: ${observation.name}`);
      button.addEventListener("click", () => selectObservation.current?.(observation.id));
      const details = document.createElement("section"), heading = document.createElement("h3");
      details.className = "map-observation-details"; heading.textContent = observation.name;
      details.append(heading);
      for (const [label, value] of observationDetails(observation)) {
        const line = document.createElement("p"), name = document.createElement("strong");
        name.textContent = `${label}: `; line.append(name, document.createTextNode(value)); details.append(line);
      }
      const note = document.createElement("small");
      note.textContent = "Reported fire/weather context. This marker does not drive simulated hazard growth.";
      details.append(note);
      const detailPopup = new maplibregl.Popup({maxWidth:"360px"}).setDOMContent(details);
      const marker = new maplibregl.Marker({element:button}).setLngLat([observation.longitude,observation.latitude])
         .setPopup(detailPopup).addTo(m);
      observationMarkers.current.set(observation.id, marker);
      return marker;
    });
    const registry = observationMarkers.current;
    return () => { for (const marker of markers) marker.remove(); registry.clear(); };
  }, [ready, observations]);
  useEffect(() => {
    for (const [id, marker] of observationMarkers.current) {
      marker.getElement().setAttribute("aria-pressed", String(id === selectedObservation));
    }
  }, [ready, observations, selectedObservation]);
  useEffect(() => {
    if (!ready || !map.current || !observations.length || !observationFocus) return;
    const bounds = new maplibregl.LngLatBounds([scenario.lon,scenario.lat],[scenario.lon,scenario.lat]);
    for (const observation of observations) bounds.extend([observation.longitude,observation.latitude]);
    map.current.fitBounds(bounds,{padding:80,maxZoom:12,duration:0});
  }, [ready, observationFocus, observations, scenario.lat, scenario.lon]);
  useEffect(() => {
    if (!ready || !map.current) return;
    const m = map.current;
    const set = (id: string, data: FeatureCollection) : Promise<void> => (m.getSource(id) as maplibregl.GeoJSONSource).setData(data);
    set("study-area", { type: "FeatureCollection",
      features: showStudyArea ? [studyAreaGeometry(scenario.lat,scenario.lon,scenario.radius_km*1000,savedBounds)] : [] });
    set("fire", { type: "FeatureCollection", features: [point([scenario.lon, scenario.lat])] });
    set("roads", network || EMPTY);
    const selected = network ? network.features.filter((f) => f.properties.highway) :
      discovery?.geometry?.features?.filter(f => corridorFeatureMatches(f.properties,scenario.highway)) || [];
    set("highways", { type: "FeatureCollection", features: selected });
    set("gates", { type: "FeatureCollection",
      features: (result?.network?.gates || preparedGates).map((g, i: number) => point(g.coordinate, { number: i + 1 })) });
    const matchesArea = sources?.point?.lat === scenario.lat && sources?.point?.lon === scenario.lon;
    const observed = matchesArea &&
      sources?.sources?.find((s) => s.name === "WFIGS fire perimeters")?.geojson;
    set("perimeters", observed || EMPTY);
  }, [ready, scenario, discovery, network, result, sources, preparedGates, savedBounds, showStudyArea]);
  useEffect(() => {
    if (!ready || !map.current) return;
    if (!showStudyArea) {
      map.current.easeTo({center:[scenario.lon,scenario.lat],zoom:11,duration:0}); return;
    }
    map.current.fitBounds(studyCameraBounds(scenario.lat,scenario.lon,scenario.radius_km*1000,savedBounds),
      {padding:40,duration:0});
  }, [ready, scenario.lat, scenario.lon, scenario.radius_km, savedBounds, showStudyArea]);
  useEffect(() => {
    if (!ready || !map.current) return;
    popup.current?.remove();
    const set = (id: string, data: FeatureCollection) : Promise<void> => (map.current!.getSource(id) as maplibregl.GeoJSONSource).setData(data);
    set("vehicles", { type: "FeatureCollection", features: (frame?.vehicles || [])
      .map((v) => point([v[1], v[2]], { id: v[0], crew: v[4], speed: v[3], t: frame?.t })) });
    set("hazard", { type: "FeatureCollection", features: frame && frame.hazard_radius_m > 0 ?
      [scenarioHazardGeometry(scenario.lon, scenario.lat, frame.hazard_radius_m)] : [] });
    const closed = new Set(frame?.closed_edges || []);
    set("closures", { type: "FeatureCollection",
      features: network?.features?.filter((f) => closed.has(f.properties.id)) || [] });
    outlineCrew(map.current, frame, located.current);
  }, [ready, frame, network, scenario.lat, scenario.lon]);
  useEffect(() => {
    if (!ready || !map.current) return;
    const m = map.current;
    m.resize();
    m.setLayoutProperty("slow-vehicles", "visibility", showCongestion ? "visible" : "none");
    const set = (id: string, features: Feature[]) : Promise<void> =>
      (m.getSource(id) as maplibregl.GeoJSONSource).setData({ type: "FeatureCollection", features });
    const byId = new Map<string, Road>((network?.features || []).map((f) => [f.properties.id, f]));
    const routes: Feature[] = [], bases: Feature[] = [], stages: Feature[] = [];
    if (showRoutes) for (const plan of result?.crew_plan || []) {
      const lines = (plan.route || []).map((id: string) => byId.get(id)?.geometry.coordinates).filter((coordinates): coordinates is number[][] => !!coordinates);
      if (lines.length) routes.push({ type: "Feature", properties: { id: plan.id },
        geometry: { type: "MultiLineString", coordinates: lines } });
      const origin = byId.get(plan.origin)?.geometry.coordinates;
      const stage = byId.get(plan.stage)?.geometry.coordinates;
      if (origin) bases.push(point(origin[0], { id: plan.id }));
      if (stage?.length) stages.push(point(stage[stage.length - 1], { id: plan.id }));
    }
    set("crew-routes", routes); set("crew-bases", bases); set("crew-stages", stages);
    const trails: Feature[] = showTrails ? Object.entries(crewTracks).flatMap(([id, positions]) => {
      const coordinates = positions.filter(p => p.t <= (frame?.t || 0)).map(p => p.coordinate);
      return coordinates.length > 1 ? [{ type: "Feature", properties: { id },
        geometry: { type: "LineString", coordinates } }] : [];
    }) : [];
    set("crew-trails", trails);
  }, [ready, focus, showRoutes, showTrails, showCongestion, result, network, crewTracks, frame]);
  useEffect(() => {
    if (!ready || !map.current) return;
    (map.current.getSource("traffic-speeds") as maplibregl.GeoJSONSource).setData(
      showTraffic ? result?.traffic?.geojson || EMPTY : EMPTY);
  }, [ready, result, showTraffic]);
  useEffect(() => {
    if (!ready || !map.current || !focusRequest) return;
    const m = map.current;
    const { scenario, result, frame, network, savedBounds } = focusContext.current;
    (m.getSource("traffic-focus") as maplibregl.GeoJSONSource).setData(EMPTY);
    located.current = focusRequest.kind === "crew" ? focusRequest.id || null : null;
    outlineCrew(m, frame, located.current);
    if (focusRequest.kind === "observation") {
      const marker = observationMarkers.current.get(focusRequest.id || "");
      if (marker) {
        for (const item of observationMarkers.current.values()) item.getPopup()?.remove();
        m.easeTo({center:marker.getLngLat(), zoom:12, duration:500});
        marker.getPopup()?.addTo(m);
      }
    } else if (focusRequest.kind === "traffic-segment") {
      const segment = result?.traffic?.segments?.find((item) => item.id === focusRequest.id);
      if (!segment?.geometry) return;
      (m.getSource("traffic-focus") as maplibregl.GeoJSONSource).setData({
        type: "FeatureCollection", features: [{type: "Feature", properties: {}, geometry: segment.geometry}],
      });
      const bounds = new maplibregl.LngLatBounds();
      const addCoordinates = (value: unknown): void => {
        if (Array.isArray(value) && value.length >= 2 &&
            typeof value[0] === "number" && typeof value[1] === "number") {
          bounds.extend([value[0], value[1]]);
        } else if (Array.isArray(value)) value.forEach(addCoordinates);
      };
      addCoordinates(segment.geometry.coordinates);
      if (!bounds.isEmpty()) {
        m.fitBounds(bounds, {padding: 90, maxZoom: 15, duration: 500});
        const matched = result?.traffic.matches?.filter((item) => item.segment_id === segment.id).length || 0;
        const content = document.createElement("div");
        content.className = "map-inspection";
        const heading = document.createElement("strong"), body = document.createElement("p");
        heading.textContent = `TomTom segment ${focusRequest.number} · ${focusRequest.highway}`;
        body.textContent = `${mph(Number(segment.speed_kmh))} reported versus ${mph(Number(segment.free_flow_kmh))} free flow. ${matched} modeled directional highway ${matched === 1 ? "segment uses" : "segments use"} this saved reading. Gold shows the TomTom geometry; dashed color shows matched modeled roads.`;
        content.append(heading, body);
        popup.current?.remove();
        popup.current = new maplibregl.Popup({maxWidth: "330px"})
          .setLngLat(bounds.getCenter()).setDOMContent(content).addTo(m);
      }
    } else if (focusRequest.kind === "crew") {
      const v = frame?.vehicles.find((vehicle) => vehicle[0] === focusRequest.id);
      if (v && frame) {
        m.easeTo({ center: [v[1], v[2]], zoom: Math.max(LOCATE_ZOOM, m.getZoom()), duration: 500,
          offset: [0, LOCATE_OFFSET_Y] });
        popup.current?.remove();
        const crew = result ? crewDetails(result, frame, v[0]) : null;
        if (crew) popup.current = new maplibregl.Popup({ maxWidth: "310px" }).setLngLat([v[1], v[2]])
          .setDOMContent(crewPopupContent(`${crew.name} · fire crew`, crewDetailLines(crew),
            `Recorded at ${clock(frame.t)}. The dark ring marks this crew; it follows the crew through replay.`)).addTo(m);
      }
    } else if (focusRequest.kind === "crew-origin") {
      const crew = result?.crew_plan?.find(item => item.id === focusRequest.id);
      const road = network?.features.find(item => item.properties.id === crew?.origin);
      const coordinate = road?.geometry.coordinates[0];
      if (coordinate) {
        m.easeTo({center:[coordinate[0],coordinate[1]], zoom:13, duration:500});
        const details = document.createElement("section"), heading = document.createElement("strong"), note = document.createElement("p");
        details.className = "map-inspection"; heading.textContent = `${crew?.id} · planned origin`;
        note.textContent = "Assumed dispatch origin from the saved plan, not a recorded crew position.";
        details.append(heading, note); popup.current?.remove();
        popup.current = new maplibregl.Popup().setLngLat([coordinate[0],coordinate[1]]).setDOMContent(details).addTo(m);
      }
    } else if (focusRequest.kind === "hazard") {
      const radius = Math.max((frame?.hazard_radius_m || scenario.hazard_radius_m || 0) * 3, 1200);
      m.fitBounds(studyCameraBounds(scenario.lat,scenario.lon,radius),
        { padding: 35, maxZoom: 14, duration: 500 });
    } else {
      m.fitBounds(studyCameraBounds(scenario.lat,scenario.lon,scenario.radius_km*1000,savedBounds),
        {padding:40,duration:500});
    }
  }, [ready, focusRequest]);
  return <div className="map-wrap" id={id}>
    <div ref={element} className="map simulation-map" data-ready={ready} aria-label="Simulation replay map"/>
    <div className="map-caption"><span className="dot red"/> Scenario coordinate <b>{scenario.lat.toFixed(4)}, {scenario.lon.toFixed(4)}</b>
      {!!observations.length && <span> · F: reported fire · W: weather point · select for linked details</span>}</div>
    {mapError && <div className="map-error">Some map resources are unavailable. Simulation geometry remains visible.</div>}
    <div className="map-hint">Blue dots: evacuation vehicles · Orange dots/trails: crews · Purple dashes: crew routes
      {showCongestion ? " · Dark halos: slow/stopped vehicles" : ""} · Dark red roads: scenario closures
      <br/>{showStudyArea ? "Dashed boundary: study extent, not a safety boundary. " : "Road study extent is not yet validated. "}
      Click a vehicle, road or shaded area for an explanation. Clicking pauses playback.</div>
    {showTraffic && result?.traffic?.status === "applied" && <div className="traffic-map-key">
      Dashed traffic speeds: green ≥75% of free flow · amber 40–75% · purple &lt;40%. Saved snapshot; gaps are unknown.
    </div>}
  </div>;
}
