import type { Result } from "../artifacts";
import { comparison,congestion } from "./congestion";
import { policyNames } from "./explanations";
import { missionMetrics } from "./mission";
import { clock as time } from "./replay";
import { formatNumber as number } from "./units";

const delta = (n: number, noun: string) : string => n === 0 ? `the same number of ${noun}`
  : `${number(Math.abs(n))} ${n > 0 ? "more" : "fewer"} ${noun}`;

/** Congestion evidence; resident and crew findings have their own views. */
export function PolicyInsights({ result, inspect }: {
  result: Result; inspect: (index: number, seconds: number) => void;
}) : React.JSX.Element {
  const horizon = result.scenario.duration_minutes * 60;
  const baselineIndex = result.policies.findIndex((p) => p.policy === "baseline");
  const baseline = result.policies[baselineIndex];
  const reference = baseline && missionMetrics(baseline, result.scenario) && congestion(baseline, horizon);
  const alternatives = result.policies.filter((p) => p.policy !== "baseline");
  return <section className="policy-insights" aria-label="Policy takeaways">
    <span className="eyebrow">RESULTS FOR THIS SCENARIO</span>
    <h2>Congestion tradeoffs across policies</h2>
    <p>Full {result.scenario.duration_minutes}-minute recordings, with the same civilian demand, road network and hazard.
      These totals stay fixed while you replay.</p>
    {!baseline && <p className="recording-gap">This recording has no baseline. A baseline is needed to calculate changes.</p>}
    {baseline && alternatives.length === 0 && <p className="recording-gap">Only baseline was recorded. Run all three policies to compare their effects.</p>}
    <details open><summary>Congestion tradeoffs against baseline</summary>
    {baseline && <div className="takeaway-grid">{alternatives.map((p) => {
      const change = missionMetrics(p, result.scenario) && baseline && missionMetrics(baseline, result.scenario)
        ? comparison(p, baseline, horizon) : null;
      return <article className="takeaway" key={p.policy} data-policy={p.policy}>
        <span className="eyebrow">{policyNames[p.policy]} vs baseline</span>
        <h3>{change?.title || "Comparison unavailable"}</h3>
        {change ? <>
          <p><b>{change.slowDelta === 0 ? "No change in" : `${number(Math.abs(change.slowDelta))} ${change.slowDelta < 0 ? "fewer" : "more"}`} slow vehicle-minutes</b>
            {change.percent !== null && change.slowDelta !== 0 ? ` (${change.percent.toFixed(1)}%)` : ""}.
            {" "}There were {delta(change.exitDelta, "exits")} and {delta(change.waitingDelta, "vehicles not departed")} at the cutoff.</p>
          <p>Peak slow/stopped count: <b>{change.current.peak}</b> vs {change.reference.peak} in baseline.
            {" "}At the cutoff: {change.current.endSlow} vs {change.reference.endSlow} slow/stopped.</p>
          <p>{change.exitDelta < 0 ? "Fewer exits means more unresolved demand, even if road queues look smaller."
            : change.waitingDelta > 0 ? "More vehicles are still waiting at origins. Account for that waiting before calling the lower road queue a benefit."
            : change.slowDelta < 0 && change.exitDelta >= 0 ? "This run spent less time in very slow traffic without sacrificing exits. Check exposure and crew staging before choosing a policy."
            : "Routing and departure changes did not improve every measure. Compare road queues together with exits, exposure and crew staging."}</p>
          <small>Exposed civilian vehicles: {p.summary.exposed_vehicles} vs {baseline.summary.exposed_vehicles}.
            {" "}Crews staged: {p.summary.crews_staged} vs {baseline.summary.crews_staged}.</small>
          <button className="secondary" onClick={() => inspect(result.policies.indexOf(p), change.reference.peakTime)}>
            Inspect {policyNames[p.policy]} at {time(change.reference.peakTime)}
          </button>
        </> : <p>A complete time series and passing engine/accounting checks are required. No benefit is inferred from missing or invalid data.</p>}
      </article>;
    })}</div>}
    {reference && <p className="comparison-inspect">Inspect each policy at baseline’s peak ({time(reference.peakTime)}) to compare the same moment.
      {" "}<button className="secondary" onClick={() => inspect(baselineIndex, reference.peakTime)}>Inspect Baseline at {time(reference.peakTime)}</button></p>}
    </details>
    <details open><summary>Congestion measures and how to read them</summary>
      <div className="table-scroll"><table aria-label="Congestion comparison"><thead><tr>
        <th>Policy</th><th>Slow vehicle-minutes</th><th>Average slow/stopped</th><th>Peak / time</th>
        <th>Slow at cutoff</th><th>Exited</th><th>Still on roads</th><th>Not departed</th>
      </tr></thead><tbody>{result.policies.map((p) => {
        const valid = missionMetrics(p, result.scenario);
        const c = valid && congestion(p, horizon);
        return <tr key={p.policy} data-policy={p.policy}><th>{policyNames[p.policy]}</th>
          <td>{c ? number(c.slowMinutes) : "Unavailable"}</td><td>{c ? number(c.averageSlow) : "Unavailable"}</td>
          <td>{c ? `${c.peak} / ${time(c.peakTime)}` : "Unavailable"}</td><td>{c?.endSlow ?? "Unavailable"}</td>
          <td>{valid ? p.summary.exited_vehicles : "Unavailable"}</td><td>{valid ? p.summary.active_vehicles : "Unavailable"}</td>
          <td>{valid ? p.summary.not_departed_vehicles : "Unavailable"}</td></tr>;
      })}</tbody></table></div>
      <p><b>Slow vehicle-minutes</b> estimates the area under the slow/stopped count curve. Ten vehicles slow for one minute equals
        ten vehicle-minutes; lower values mean fewer vehicles, shorter slow periods, or both. Average slow/stopped is that area divided by run length.
        Peak is the largest recorded count, which can occur at different times for different policies.</p>
      <p>Slow means below about 1.1 mph. Counts include crews and their scheduled stops. Estimates interpolate between saved samples;
        they omit waiting before road entry and do not measure delay relative to a free-flow trip. A small change is descriptive, not evidence of statistical significance.
        Phased vs baseline changes both routing/dispatch and departure timing, so this comparison cannot isolate either cause.</p>
    </details>
  </section>;
}
