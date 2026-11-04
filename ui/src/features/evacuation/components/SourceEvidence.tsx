import { useState } from "react";
import type { Result,Source,SourceRecord,Sources,Traffic,TrafficSegment } from "../artifacts";
import { formatDateTime,mph,temperatureF } from "./units";

const text = (value: unknown) : string => typeof value === "string" && value.trim() ? value : "Unavailable";
const percent = (value: unknown) : string => typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 1
  ? `${Math.round(value * 100)}%` : "Unavailable";
const minutes = (value: unknown) : string => typeof value === "number" && Number.isFinite(value) && value >= 0 ? `${(value / 60).toFixed(1)} min` : "Unavailable";
const yesNo = (value: unknown) : "Unavailable" | "Yes" | "No" => value === true ? "Yes" : value === false ? "No" : "Unavailable";
const speedVsFreeFlow = (speed: number, freeFlow: number): string => {
  if (!Number.isFinite(speed) || !Number.isFinite(freeFlow) || speed < 0 || freeFlow <= 0) return "Unavailable";
  const slower = Math.max(0, Math.round((1 - speed / freeFlow) * 100));
  return slower ? `${slower}% below free flow` : "At or above free flow";
};

export function SourcePreview({ result, sources, inspect, busy }: { result?: Result | null; sources?: Sources | null; inspect: () => void; busy: boolean }) : React.JSX.Element {
  const traffic = result?.traffic, snapshot = result?.sources || sources;
  const segments = traffic?.segments || [];
  const speeds = segments.map(s => s.speed_kmh).filter(s => Number.isFinite(s) && s >= 0);
  const feeds = snapshot?.sources || [];
  const pointReading = feeds.find(s => s.name === "TomTom traffic flow" && s.status === "available");
  return <section className="source-preview" aria-label="Real data behind this scenario">
    <div className="section-title"><div><span className="eyebrow">SOURCE EVIDENCE</span>
      <h3>Real data behind this scenario</h3></div>
      <button className="secondary" disabled={busy} onClick={inspect}>
        {!result && !sources ? "Fetch public source data" : "Inspect source data"}</button></div>
    <div className="source-preview-grid">
      <div><b>TomTom</b><p>{traffic?.status === "applied"
        ? <>{speeds.length ? `${mph(Math.min(...speeds))} – ${mph(Math.max(...speeds))}` : "Speed readings unavailable"}
          {" · "}{segments.length} saved provider segments.<br/>{traffic.matched_edges} of {traffic.highway_edges} highway segments matched.
          <br/>Fetched {formatDateTime(traffic.retrieved_at)}.</>
        : <>No TomTom speeds applied{result ? " in this recording" : " yet"}.
          {pointReading ? <><br/>Point reading: {mph(pointReading.values?.currentSpeed)} · fetched {formatDateTime(pointReading.retrieved_at)}.
            {" "}Context only.</> : " Inspect source data for access and coverage."}</>}</p></div>
      <div><b>Roads and public feeds</b><p>{result?.network
        ? <>OpenStreetMap road geometry · downloaded {formatDateTime(result.network.retrieved_at)}.<br/></> : null}
        {snapshot ? <>{feeds.filter(s => s.status === "available").length} of {feeds.length} feeds responded in this snapshot.
          <br/>Fetched {formatDateTime(snapshot.retrieved_at)}.</> : "No public-source readings fetched for this area yet."}</p></div>
    </div>
    <small>{result ? "Saved inputs for this run. " : ""}Provider readings and forecasts have their own dates.
      They do not make the vehicle replay live or validate evacuation outcomes.</small>
  </section>;
}

export function ProviderReadings({ traffic, highway, locate }: {
  traffic: Traffic;
  highway?: string;
  locate?: (segment: TrafficSegment, number: number) => void;
}) : React.JSX.Element | null {
  if (traffic?.status !== "applied") return null;
  const segments = traffic.segments || [], matches = traffic.matches || [];
  return <section className="provider-readings" aria-label="TomTom provider readings">
    <span className="eyebrow">SAVED PROVIDER RESPONSE · TRAFFIC INPUT</span>
    <h3>What TomTom reported{highway ? ` for ${highway}` : ""}</h3>
    <p>Actual values saved from the corridor requests, shown once per distinct provider segment.
      A provider segment can cover several modeled road segments. Speed limits were applied only where a road matched.</p>
    <p><b>“Segment 1”, “Segment 2”, and so on are display row numbers—not road names, priorities, or simulation phases.</b>
      {" "}Use “Matched roads” to see how many modeled directional road edges used that provider reading.</p>
    <p><b>Fetched {formatDateTime(traffic.retrieved_at)}.</b> Individual measurement time was not supplied.
      “Reported speed” describes that saved response, not traffic now. Confidence is the provider's value, not model accuracy.</p>
    {!segments.length ? <p>Individual provider readings are unavailable in this recording.</p> :
      <div className="table-scroll source-records"><table aria-label="Saved TomTom readings"><thead><tr>
        <th>Provider segment</th><th>Reported speed</th><th>Free-flow speed</th><th>Speed vs free flow</th><th>Travel time</th>
        <th>Free-flow time</th><th>Confidence</th><th>Closure reported</th><th>Matched roads</th>
        {locate && <th>Map</th>}<th>Fetched</th>
      </tr></thead><tbody>{segments.map((s, i) => <tr key={s.id || i} data-segment={s.id}>
        <th>Segment {i + 1}<small>{s.id}</small></th><td>{mph(s.speed_kmh)}</td><td>{mph(s.free_flow_kmh)}</td>
        <td>{speedVsFreeFlow(s.speed_kmh, s.free_flow_kmh)}</td>
        <td>{minutes(s.travel_time_s)}</td><td>{minutes(s.free_flow_time_s)}</td><td>{percent(s.confidence)}</td>
        <td>{yesNo(s.road_closure)}</td><td>{matches.filter(m => m.segment_id === s.id).length}</td>
        {locate && <td><button type="button" onClick={() => locate(s, i + 1)}
          aria-label={`Show segment ${i + 1}${highway ? ` on ${highway}` : ""} map`}>Show on map</button></td>}
        <td>{formatDateTime(s.retrieved_at)}</td>
      </tr>)}</tbody></table></div>}
    <p>Free flow is the provider's uncongested reference. “No” closure report does not establish that a road is safe or open.
      Unmatched roads keep OSM speed limits. The detailed road matches below show the limits used by SUMO.</p>
    <a href="https://docs.tomtom.com/traffic-api/documentation/tomtom-maps/v1/traffic-flow/flow-segment-data"
      target="_blank" rel="noreferrer">TomTom's field definitions ↗</a>
  </section>;
}

export function SourceDetails({ source }: { source: Source }) : React.JSX.Element {
  const [all, setAll] = useState(false);
  if (source.status !== "available") return <p>No readings available in this snapshot.</p>;
  const periods = source.properties?.periods || [];
  const records: SourceRecord[] = Array.isArray(source.records) ? source.records : (source.geojson?.features || []).map(f => ({ properties: f.properties }));
  let headers: string[] = [], rows: (string | number)[][] = [], note = "";
  if (source.name === "TomTom traffic flow") {
    const v = source.values || {};
    headers = ["Reported speed", "Free flow", "Travel time", "Confidence", "Closure reported"];
    rows = [[mph(v.currentSpeed), mph(v.freeFlowSpeed), minutes(v.currentTravelTime), percent(v.confidence), yesNo(v.roadClosure)]];
    note = "A separate point reading for context; it is not the corridor snapshot applied to the simulation. Individual measurement time is unknown.";
  } else if (periods.length) {
    headers = ["Forecast starts", "Temperature", "Wind", "Forecast"];
    rows = periods.map(p => [formatDateTime(p.startTime), temperatureF(p.temperature, p.temperatureUnit),
      `${text(p.windSpeed)} ${text(p.windDirection)}`, text(p.shortForecast)]);
    note = "Forecast periods from this saved response, not weather observations. They do not drive simulated fire growth.";
  } else if (source.name.endsWith("LCS")) {
    headers = ["Road / direction", "From → to", "Closure / work", "Record updated", "Fresh at retrieval", "Qualified active at retrieval"];
    rows = records.map(r => [`${text(r.location?.begin?.beginRoute)  } · ${  text(r.location?.travelFlowDirection)}`,
      `${text(r.location?.begin?.beginLocationName)} → ${text(r.location?.end?.endLocationName)}`,
      `${text(r.closure?.typeOfClosure)} · ${text(r.closure?.typeOfWork)}`, formatDateTime(r.record_epoch),
      yesNo(r.fresh), yesNo(r.active)]);
    note = "Feed checks at the time of retrieval; not current road access. These records do not automatically close simulation roads.";
  } else if (source.name.includes("CCTV")) {
    headers = ["Camera location", "Direction", "Inventory updated", "In service"];
    rows = records.map(r => [text(r.location?.locationName), text(r.location?.direction), formatDateTime(r.record_epoch), yesNo(r.in_service)]);
    note = "Camera inventory only. No camera images or observed vehicle counts are used; image capture freshness is unknown.";
  } else if (source.name === "WFIGS fire perimeters") {
    headers = ["Incident", "Polygon captured", "Capture fresh at retrieval"];
    rows = records.map(r => [text(r.properties?.attr_IncidentName || r.properties?.poly_IncidentName),
      formatDateTime(typeof r.properties?.poly_PolygonDateTime === "number"
        ? r.properties.poly_PolygonDateTime / 1000 : r.properties?.poly_PolygonDateTime),
      yesNo(r.properties?.capture_fresh_6h)]);
    note = "Observed perimeter context. No returned features does not establish that there is no fire.";
  } else {
    headers = ["Location", "Record updated", "Observation time", "Fresh at retrieval"];
    rows = records.map(r => [text(r.location?.locationName || r.location?.begin?.beginLocationName),
      formatDateTime(r.record_epoch), formatDateTime(r.observed_at), yesNo(r.fresh)]);
    note = "Available record metadata. Missing measurements remain unavailable.";
  }
  return <details className="source-detail"><summary>Inspect saved readings · {rows.length} {periods.length ? "forecast periods" : "records"}</summary>
    <p>{note}</p>
    {!rows.length ? <p>No nearby records returned in this snapshot. This is a coverage result, not proof of clear conditions.</p> : <>
      <p>Showing {all ? rows.length : Math.min(rows.length, 12)} of {rows.length} records.</p>
      <div className="table-scroll source-records"><table aria-label={`${source.name} saved readings`}>
        <thead><tr>{headers.map(h => <th key={h}>{h}</th>)}</tr></thead>
        <tbody>{(all ? rows : rows.slice(0, 12)).map((row, i) => <tr key={i}>{row.map((v, j) => <td key={j}>{v}</td>)}</tr>)}</tbody>
      </table></div>
      {rows.length > 12 && <button className="secondary" onClick={() => setAll(!all)}>{all ? "Show first 12" : `Show all ${rows.length} records`}</button>}
    </>}
  </details>;
}

export function OfficialSourceLink({ source }: { source: Source }) : React.JSX.Element | null {
  try {
    const url = new URL(source.url || "");
    if (url.protocol !== "https:" || !["cwwp2.dot.ca.gov", "api.weather.gov", "services3.arcgis.com"].includes(url.hostname)) return null;
    url.search = ""; url.hash = ""; url.username = ""; url.password = "";
    return <a href={url.href} target="_blank" rel="noreferrer">Official source endpoint ↗</a>;
  } catch { return null; }
}
