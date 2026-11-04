import type { Result } from "../artifacts";
import { OutcomeExplanation,policyNames } from "./explanations";
import { exitThreshold,missionMetrics } from "./mission";
import { clock,POLICY_COLORS as colors } from "./replay";
/** Save a value as a pretty-printed JSON file download. */
export function download(value: unknown, name: string): void {
  const url = URL.createObjectURL(new Blob([JSON.stringify(value, null, 2)], { type: "application/json" }));
  const a = document.createElement("a"); a.href = url; a.download = name; a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** Policy comparison table: exits, unresolved and exposed per policy, replay picker and JSON export. */
export function Comparison({ result, index, setIndex }: { result: Result; index: number; setIndex: (index: number) => void }) : React.JSX.Element | null {
  if (!result) return null;
  const maxT = result.scenario.duration_minutes * 60;
  return <section className="comparison"><div className="section-title">
    <div><span className="eyebrow">MATCHED DEMAND · SAME SEED</span><h2>Compare the experiments</h2></div>
    <button className="secondary" onClick={() => download(result, "wildfire-comparison.json")}>Export run JSON</button>
  </div><p>Exited means reaching an assumed model gate. Exposed means intersecting the scenario exclusion; it is not a fatality estimate.</p>
    <div className="comparison-grid"><div className="table-scroll"><table><thead><tr><th>Policy / replay</th><th>Exited</th><th>Unresolved</th><th>Exposed</th><th>Crews staged</th><th>95% exit time</th></tr></thead>
      <tbody>{result.policies.map((p, i: number) => {
        const t95 = exitThreshold(p, result.scenario);
        const valid = missionMetrics(p, result.scenario);
        return <tr key={p.policy} className={i === index ? "selected" : ""}>
        <th><button aria-label={`Replay ${policyNames[p.policy]}`} onClick={() => setIndex(i)}><i style={{ background: colors[i] }}/>{policyNames[p.policy]}</button></th>
        <td>{valid ? p.summary.exited_vehicles : "Unavailable"}</td>
        <td>{valid ? p.summary.unresolved_vehicles : "Unavailable"}</td>
        <td>{valid ? p.summary.exposed_vehicles : "Unavailable"}</td>
        <td>{valid ? `${p.summary.crews_staged}/${result.scenario.crews}` : "Unavailable"}</td>
        <td>{t95.status === "not_reached" ? "Not reached" : t95.seconds == null ? "Unavailable" : clock(t95.seconds)}</td>
      </tr>; })}</tbody></table></div>
      <div className="chart"><b>Vehicles reaching the model gate</b>
        <svg viewBox="0 0 500 160" role="img" aria-label="Cumulative model exits by policy over time">
          {[0, 0.5, 1].map(x => <g key={x}><line x1="40" x2="488" y1={130 - x * 110} y2={130 - x * 110} stroke="#dce4e3"/>
            <text x="2" y={134 - x * 110}>{Math.round(result.scenario.vehicles * x)}</text></g>)}
          {result.policies.map((p, i: number) => <polyline key={p.policy} fill="none" stroke={colors[i]} strokeWidth="3"
            points={p.series.map((s) => `${40 + s.t / maxT * 448},${130 - s.exited / result.scenario.vehicles * 110}`).join(" ")}/>)}
          <text x="40" y="154">0 min</text><text x="439" y="154">{result.scenario.duration_minutes} min</text>
        </svg></div></div>
    {result.policies.some((p) => !p.summary.valid_engine_run) &&
      <div className="error">Engine validation detected collisions or teleports. Treat this comparison as invalid and inspect the exported diagnostics.</div>}
    <OutcomeExplanation result={result} policy={result.policies[index]}/>
    <details><summary>Model assumptions, provenance, and accounting</summary>
      <ul>{[...result.assumptions, ...result.network.limitations].map((s: string) => <li key={s}>{s}</li>)}</ul>
      <p>Engine: {result.engine} {result.engine_version}. Network: {result.network.edge_count} directed edges.
        Source retrieved {new Date(result.network.retrieved_at * 1000).toLocaleString()}.</p>
      <p>OSM SHA-256: <code>{result.network.osm_sha256}</code></p>
      {result.policies.map((p) => <p key={p.policy}><b>{policyNames[p.policy]}:</b> {p.summary.not_departed_vehicles} not departed,
        {p.summary.origin_blocked_vehicles != null &&
          <> including {p.summary.origin_blocked_vehicles} at closed origins,</>}
        {" "}{p.summary.active_vehicles} still in network, {p.summary.unroutable_vehicles} without an initial route,
        {" "}{p.summary.crews_returned} crews returned, {p.summary.exposed_crews} crews exposed,
        {" "}{p.summary.crews_not_departed} crews not departed, {p.summary.crews_without_route} without a route,
        {p.summary.crews_origin_blocked != null &&
          <> {p.summary.crews_origin_blocked} crews held at closed origins,</>}
        {" "}{p.summary.teleports} teleports, {p.summary.collision_events} collision events.</p>)}
    </details>
  </section>;
}

