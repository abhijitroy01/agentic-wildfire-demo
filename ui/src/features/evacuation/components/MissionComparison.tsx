import type { Result } from "../artifacts";
import { policyDescriptions,policyNames } from "./explanations";
import { crewRecords,missionMetrics,pairedCrewTiming } from "./mission";
import { clock,POLICY_COLORS } from "./replay";
import { formatNumber as number } from "./units";

// The arrival chart draws the phased policy as a thin dotted line, so it keeps a darker orange: 4.7:1 on white,
// where the shared POLICY_COLORS orange is 3.3:1 (3.0:1 on the workspace background).
const ARRIVAL_COLORS: readonly string[] = [POLICY_COLORS[0], POLICY_COLORS[1], "#a26616"];

const change = (value: number, reference: number, unit: string, moreIsBetter: boolean) : React.JSX.Element => {
  const delta = value - reference;
  const status = delta === 0 ? "unchanged" : (delta > 0) === moreIsBetter ? "improved" : "worse";
  return <span className={`mission-change ${status}`}>{delta === 0 ? "No change" :
    `${number(Math.abs(delta))} ${delta > 0 ? "more" : "fewer"} ${unit} · ${status}`}</span>;
};
const timingChange = (seconds: number | null) : string => seconds == null ? "Unavailable"
  : Math.abs(seconds) < 0.05 ? "No change" : `${number(Math.abs(seconds))} sec ${seconds < 0 ? "faster" : "slower"}`;
const timingValues = (current: number | null, baseline: number | null) : string =>
  current == null || baseline == null ? "Unavailable" : `${number(current)} vs ${number(baseline)} sec`;
const threshold = (m: NonNullable<ReturnType<typeof missionMetrics>>) : string =>
  m.t95Status === "not_reached" ? "Not reached" : m.t95 == null ? "Unavailable" : clock(m.t95);

/** Compact findings reused by the overview and the saved planning assessment. */
export function MissionFindings({ result, inspect }: {
  result: Result; inspect?: (policy: string, view: "overview" | "bottlenecks" | "crews") => void;
}): React.JSX.Element {
  const baseline = result.policies.find(p => p.policy === "baseline");
  const reference = baseline && missionMetrics(baseline, result.scenario);
  return <section className="mission-findings" aria-label="Simulation findings">
    <h3>People out. Crews in. Check both outcomes.</h3>
    <p>Changes against baseline at the same {result.scenario.duration_minutes}-minute cutoff.
      These full-run findings stay fixed while you replay.</p>
    {!reference && <p>No complete, valid baseline is available. Policy differences remain unavailable.</p>}
    {reference && <div className="mission-grid">{result.policies.filter(p => p !== baseline).map(p => {
      const current = missionMetrics(p, result.scenario), paired = pairedCrewTiming(p, baseline, result.scenario);
      return <article className="mission-policy" key={p.policy} data-finding-policy={p.policy}>
        <h4>{policyNames[p.policy]} vs Baseline</h4>
        {current ? <>
          <dl>
            {([
              ["Modeled resident exits", current.peopleExited, reference.peopleExited, "people out", true],
              ["Residents unresolved", current.peopleUnresolved, reference.peopleUnresolved, "unresolved", false],
              ["Residents exposed to assumed hazard", current.peopleExposed, reference.peopleExposed, "exposed", false],
              ["Crews reaching staging", current.staged, reference.staged, "crews staged", true],
            ] as const).map(([label, value, before, unit, better]) => <div key={label}>
              <dt>{label}</dt><dd>{number(value)} vs {number(before)}
                {change(value, before, unit, better)}</dd></div>)}
          </dl>
          <p><b className={paired?.response == null ? undefined : paired.response > 0.05 ? "worse"
            : paired.response < -0.05 ? "improved" : "unchanged"}>
            Paired crew response: {paired?.count ? timingChange(paired.response) : "Unavailable"}</b>
            {paired?.count ? ` · ${paired.count} crews reaching staging in both policies, out of ${paired.total} assigned.` :
              " · No valid matched arrival sample."}</p>
          <p>{current.notStaged} crews did not reach staging; baseline {reference.notStaged}.
            Nonarrivals are excluded from the timing average, and remain in these counts.</p>
          {inspect && <div className="finding-actions">
            <button className="secondary" onClick={() => inspect(p.policy, "overview")}>Review resident outcomes</button>
            <button className="secondary" onClick={() => inspect(p.policy, "crews")}>Review crew access</button>
            <button className="secondary" onClick={() => inspect(p.policy, "bottlenecks")}>Inspect congestion</button>
          </div>}
        </> : <p>Complete, valid results are unavailable. No improvement is inferred.</p>}
      </article>;
    })}</div>}
    {reference && result.policies.length === 1 && <p>Only baseline was recorded; no policy differences are available.</p>}
    <small>People use {result.scenario.people_per_vehicle} assumed occupants per vehicle. Read improvements and setbacks together;
      this run does not select an overall winner. Model exits are not verified safety, exposure is not injury, and staging is not suppression.</small>
  </section>;
}

export function MissionComparison({ result, crewOnly = false }: { result: Result; crewOnly?: boolean }) : React.JSX.Element {
  const scenario = result.scenario, horizon = scenario.duration_minutes * 60;
  const baseline = result.policies.find((p) => p.policy === "baseline");
  const reference = baseline && missionMetrics(baseline, scenario);
  const arrivalTimes: (number[] | null)[] = result.policies.map((p): number[] | null => {
    const records = crewRecords(p, horizon);
    if (!records || !missionMetrics(p, scenario) || records.length !== scenario.crews) return null;
    const times = records.filter(c => c.reached_staging_s != null).map(c => c.reached_staging_s as number).sort((a, b) => a - b);
    return times.length === p.summary.crews_staged ? times : null;
  });
  return <section className="mission-comparison" aria-label={crewOnly ? "Crew policy comparison" : "Evacuation and crew outcomes"}>
    <h3>{crewOnly ? "Crew access across policies" : "Detailed resident and crew outcomes"}</h3>
    <p>Compare each policy with baseline at the same {scenario.duration_minutes}-minute cutoff.
      Values show the alternative first, then baseline. {crewOnly
        ? "Check nonarrival counts alongside timing; a faster arrival sample can exclude crews that never arrived."
        : "A shorter queue is useful only when the evacuation and crew-access outcomes support it."}</p>
    {!reference && <p className="recording-gap">A complete baseline with passing engine and accounting checks is needed.
      No improvement is inferred from missing or invalid results.</p>}
    {reference && result.policies.length === 1 && <p className="recording-gap">
      Only baseline was recorded. No alternative policy comparison is available.</p>}
    {reference && <div className="mission-grid">{result.policies.filter((p) => p !== baseline).map((p) => {
      const m = missionMetrics(p, scenario), paired = pairedCrewTiming(p, baseline, scenario);
      return <article className="mission-policy" key={p.policy} data-mission-policy={p.policy}>
        <h4>{policyNames[p.policy]} vs Baseline</h4>
        {!m ? <p>Complete, valid results are unavailable. No benefit can be assessed.</p> : <>
          {!crewOnly && <div className="mission-objective"><b>Residents leaving the area</b>
            <dl>
              <dt>People reaching model exits</dt><dd>{number(m.peopleExited)} vs {number(reference.peopleExited)}
                {change(m.peopleExited, reference.peopleExited, "people out", true)}</dd>
              <dt>People still unresolved</dt><dd>{number(m.peopleUnresolved)} vs {number(reference.peopleUnresolved)}
                {change(m.peopleUnresolved, reference.peopleUnresolved, "unresolved", false)}</dd>
              <dt>People not yet departed</dt><dd>{number(m.peopleWaiting)} vs {number(reference.peopleWaiting)}
                {change(m.peopleWaiting, reference.peopleWaiting, "waiting", false)}</dd>
              <dt>People in vehicles exposed to the assumed hazard</dt><dd>{number(m.peopleExposed)} vs {number(reference.peopleExposed)}
                {change(m.peopleExposed, reference.peopleExposed, "exposed", false)}</dd>
            </dl>
            <p>95% exit time: {threshold(m)}; baseline {threshold(reference)}.
              {m.t95 != null && reference.t95 != null && <> {timingChange(m.t95 - reference.t95)}.</>}</p>
          </div>}
          <div className="mission-objective"><b>Crews getting into position</b>
            <dl>
              <dt>Crews reaching staging</dt><dd>{m.staged} of {scenario.crews} vs {reference.staged} of {scenario.crews}
                {change(m.staged, reference.staged, "crews staged", true)}</dd>
              <dt>Crews not reaching staging</dt><dd>{m.notStaged} vs {reference.notStaged}
                {change(m.notStaged, reference.notStaged, "crews missing staging", false)}</dd>
              <dt>Crews exposed to the assumed hazard</dt><dd>{m.crewExposed} vs {reference.crewExposed}
                {change(m.crewExposed, reference.crewExposed, "crews exposed", false)}</dd>
              <dt>Crews not yet on roads</dt><dd>{m.crewWaiting} vs {reference.crewWaiting}
                {change(m.crewWaiting, reference.crewWaiting, "crews waiting", false)}</dd>
            </dl>
            {scenario.crews === 0 ? <p>No crews requested; crew efficiency cannot be assessed.</p>
              : paired?.count ? <div className="paired-timing">
                <p><b>Response time: {timingChange(paired.response)}</b> on average for the same {paired.count} crews
                  reaching staging in both runs, out of {paired.total} assigned.</p>
                <p>{paired.earlier} arrived earlier · {paired.later} later · {paired.unchanged} unchanged.
                  Response runs from scheduled dispatch to staging, including time waiting to enter a road.</p>
                <p>Paired mean response: {timingValues(paired.current.response, paired.baseline.response)}.
                  {" "}Wait: {timingValues(paired.current.departureWait, paired.baseline.departureWait)}.
                  {" "}Road travel: {timingValues(paired.current.travel, paired.baseline.travel)}.</p>
                <p>Wait to enter roads: {timingChange(paired.departureWait)}. Road travel: {timingChange(paired.travel)}.
                  This average excludes unarrived crews; review the missing-staging count above.</p>
              </div> : <p>{paired ? "No crew reached staging in both runs; no paired response time is available."
                : "Individual crew timing was not saved or is incomplete. Arrival totals do not establish faster response."}</p>}
          </div>
          {!crewOnly && <p className="mission-verdict"><b>Decision:</b> Read improvements and setbacks together. More resident exits with slower crew access
            is a tradeoff; more staged crews with more exposed residents is also a tradeoff. This run does not select an overall winner.</p>}
        </>}
      </article>;
    })}</div>}
    <p className="mission-boundary">{!crewOnly && <>People counts use the entered {scenario.people_per_vehicle} people per vehicle.
      Unresolved, waiting and exposed counts overlap; do not add them together. </>}
      Model exits are not verified safety; exposure is not an injury estimate. Staging measures access, not fire suppression,
      containment or lives saved.</p>
    <details><summary>When crews reach staging · all policies</summary>
      <div className="crew-arrival-chart">
        <svg viewBox="0 0 520 180" role="img" aria-label="Cumulative crews reaching staging over simulated time">
          {[0, 0.5, 1].map(f => <g key={f}><line x1="38" x2="496" y1={140 - f * 115} y2={140 - f * 115} stroke="#dce4e3"/>
            <text x="4" y={144 - f * 115}>{number(scenario.crews * f)}</text></g>)}
          {result.policies.map((p, i: number) => {
            const times = arrivalTimes[i];
            if (!times) return null;
            const points = ["38,140"];
            times.forEach((t, index) => {
              const x = 38 + t / horizon * 458, height = 115 / Math.max(1, scenario.crews);
              points.push(`${x},${140 - index * height}`, `${x},${140 - (index + 1) * height}`);
            });
            points.push(`496,${140 - times.length / Math.max(1, scenario.crews) * 115}`);
            return <polyline key={p.policy} data-policy={p.policy} fill="none" stroke={ARRIVAL_COLORS[i % 3]}
              strokeWidth="3" strokeDasharray={i ? i === 1 ? "7 3" : "2 3" : undefined} points={points.join(" ")}/>;
          })}
          <text x="38" y="170">0 min</text><text x="426" y="170">{scenario.duration_minutes} min</text>
        </svg>
        <div className="chart-key">{result.policies.map((p, i: number) => <span key={p.policy}>
          <i style={{ background: ARRIVAL_COLORS[i % 3] }}/>{policyNames[p.policy]}{" "}
          {arrivalTimes[i] ? `${["(solid)", "(dashed)", "(dotted)"][i % 3]} · ${arrivalTimes[i].length} of ${scenario.crews} staged`
            : "· timing unavailable"}</span>)}</div>
      </div>
      <p>Higher and earlier steps mean more crews reached staging sooner. Lines can overlap. Missing or invalid individual
        timing produces no line, not zero arrivals. This chart uses full-run event times, regardless of the map’s replay time.</p>
    </details>
    <details><summary>What changes in each policy?</summary><div className="policy-methods">
      {Object.entries(policyDescriptions).map(([key, description]) => <article key={key}>
        <b>{policyNames[key]}{key === "baseline" ? " · reference" : ""}</b><p>{description}</p>
      </article>)}
    </div></details>
  </section>;
}
