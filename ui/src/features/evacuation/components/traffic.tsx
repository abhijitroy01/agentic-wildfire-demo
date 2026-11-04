import type { Traffic } from "../artifacts";
import { formatDateTime,mph,speedMph } from "./units";

export function TrafficEvidence({ traffic }: { traffic: Traffic }) : React.JSX.Element {
  const applied = traffic?.status === "applied";
  return <section className="run-evidence" aria-label="Traffic snapshot evidence">
    <b>{applied ? "Traffic-informed speeds · saved TomTom snapshot" : "Assumed traffic · no provider speeds applied"}</b>
    {applied ? <>
      <p>{traffic.matched_edges} of {traffic.highway_edges} selected-highway directional segments use provider speeds
        as experimental speed limits, bounded by their OSM road limits. The same limits stay fixed in all three policies.</p>
      <small>Snapshot fetched {formatDateTime(traffic.retrieved_at)}. Vehicles and departure demand remain synthetic;
        the network starts empty. This is not a live traffic replay.</small>
        <p>Matching uses nearby geometry and its coordinate order to infer travel direction. Ambiguous matches,
          low-confidence readings, old responses and provider-reported closures are excluded. Unmatched roads retain OSM limits.</p>
        <p>The provider does not supply individual observation timestamps. A recent response is not proof of a recent sensor measurement.
          These speeds do not reconstruct initial queues or establish a calibrated traffic model.
          SUMO driver speed-factor variation and junction limits still apply; the displayed limit is not a hard clamp on every vehicle.</p>
        <details><summary>Inspect matched speeds and source quality</summary>
          <p>{traffic.requests.length} request receipts · {traffic.segments.length} distinct provider segments
            {" · "}{traffic.excluded.length} excluded segments. Road matches are approximate, not an authoritative lane crosswalk.</p>
          <div className="table-scroll"><table><thead><tr><th>OSM segment</th><th>Provider speed</th>
            <th>Free flow</th><th>Applied limit</th><th>Confidence</th></tr></thead>
            <tbody>{traffic.matches.map((m) => <tr key={m.edge_id}><th>{m.edge_id}</th>
              <td>{mph(m.provider_speed_kmh)}</td><td>{mph(m.free_flow_kmh)}</td>
              <td>{speedMph(m.speed_limit_m_s)}</td><td>{Math.round(m.confidence * 100)}%</td></tr>)}</tbody></table></div>
          <p>Matched responses retrieved from {formatDateTime(Math.min(...traffic.matches.map((m) => m.retrieved_at)))}
            {" "}to {formatDateTime(Math.max(...traffic.matches.map((m) => m.retrieved_at)))}.
            Export run JSON to inspect the frozen snapshot and exclusion reasons.</p>
        </details>
    </> : traffic?.mode === "provider_speed_limits" && traffic.reason === "daily_budget_reached"
      ? <p role="status">Provider speeds were requested, but today&apos;s shared TomTom request budget was exhausted.
        No provider speed was applied. Every road retains its OSM speed limit; the saved result records this reason.</p>
      : <p>Movement uses OSM road limits and the manual speed factor. Public source cards provide context.
        To apply provider speeds to a new run, choose “Use current TomTom speeds” in the scenario editor.</p>}
  </section>;
}
