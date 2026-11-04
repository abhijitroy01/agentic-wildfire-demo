import type { Sources } from "@/features/evacuation/artifacts";
import { formatDateTime,miles,mph,OfficialSourceLink,SourceDetails,temperatureF } from "@/features/evacuation/presentation";
import type React from "react";


/** Public observation cards for the selected area; a saved run snapshot is labeled separately. */
export function SourceCoverage({ data, refresh, busy, saved, restore }: {
  data: Sources | null; refresh: () => void; busy: boolean; saved?: Sources; restore?: () => void;
}): React.ReactElement {
  return <section className="sources" id="observations"><div className="section-title"><div><span className="eyebrow">OBSERVATIONS</span>
    <h2>Data coverage &amp; freshness</h2></div><button className="secondary" onClick={refresh} disabled={busy}>
      {busy ? "Checking…" : "Refresh feeds"}</button></div>
    <p>These source cards provide context. Applying traffic speeds requires the separate traffic-speed scenario option;
      demand, closures and hazard growth remain explicit assumptions.</p>
    {saved && <p className="source-snapshot-label"><b>{data === saved ? "Run's saved public-source snapshot" : "Refreshed public context · not used in this completed run"}</b>
      {data !== saved && <button className="secondary" onClick={restore}>Show run&apos;s saved sources</button>}</p>}
    {data?.point && <small>Snapshot area: {data.point.lat}, {data.point.lon} · {miles(data.radius_km)} radius.
      Refreshing observations does not alter a completed simulation.</small>}
    {!data && <div className="empty">Check available sources for this coordinate, or start a run to save an observation snapshot.</div>}
    <div className="source-grid">{data?.sources?.map((s) => {
      const records = Array.isArray(s.records) ? s.records : [];
      const timed = records.filter((r) => "fresh" in r);
      const fresh = timed.filter((r) => r.fresh).length;
      return <article className="source-card" key={s.name}>
        <div><b>{s.name}</b><span className={`badge ${  s.status}`}>{s.status.replace("_", " ")}</span></div>
        <p>{s.use || s.error || "Public observation source"}</p>
        {s.values?.currentSpeed !== undefined && <p><b>{mph(s.values.currentSpeed)}</b>
          {" "}reported at retrieval · {mph(s.values.freeFlowSpeed)} free flow · confidence {s.values.confidence}
          {s.values.roadClosure ? " · provider reports closure" : ""}</p>}
        {s.properties?.periods?.[0] && <p>{s.properties.periods[0].shortForecast} ·{" "}
          {temperatureF(s.properties.periods[0].temperature, s.properties.periods[0].temperatureUnit)} · wind{" "}
          {s.properties.periods[0].windSpeed} {s.properties.periods[0].windDirection}</p>}
        <small>{Array.isArray(s.records) ? `${records.length} nearby records` : typeof s.records === "number" ? `${s.records} features` : ""}
          {timed.length > 0 ? ` · ${fresh} fresh at retrieval / ${timed.length - fresh} stale or unknown` : ""}
          {` · fetched ${formatDateTime(s.retrieved_at)}`}</small>
        {s.incomplete && <strong>Provider response may be truncated.</strong>}
        {s.geojson && s.geojson.features.length > 0 && <small>
          {s.geojson.features.filter((f) => f.properties?.capture_fresh_6h).length} perimeters captured within 6 hours;
          {" "}older or unknown captures need review.
        </small>}
        {records.some((r) => r.image_freshness) && <small>Image capture freshness is unknown.</small>}
        <SourceDetails source={s}/>
        <OfficialSourceLink source={s}/>
      </article>;
    })}</div>
  </section>;
}
