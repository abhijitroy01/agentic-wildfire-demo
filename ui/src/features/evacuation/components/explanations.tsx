import { getUnitIcon,getUnitName } from "@/features/firetruck/utils/truckDisplayUtils";
import type { Frame,Policy,Result,Sample,Scenario,Sources } from "../artifacts";
import { crewDetails,crewPlan } from "./crews";
import { crewRole,exitThreshold,missionMetrics,stopIllustration,type StopIllustration } from "./mission";
import { POLICY_COLORS,clock as time,peakSample } from "./replay";
import { feet,formatDateTime } from "./units";


export const policyNames: Record<string, string> = {
  baseline: "Baseline", coordinated: "Coordinated", phased: "Phased departure",
};
export const policyDescriptions: Record<string, string> = {
  baseline: "Civilians use their initial routes and depart throughout the chosen window. Crews follow a fixed dispatch schedule. No periodic adaptive rerouting is requested.",
  coordinated: "Civilian departure times stay the same as baseline. Groups of drivers reconsider travel-time routes every 30 simulated seconds. Crew dispatch can wait up to two extra minutes when the first part of a route is congested.",
  phased: "Departure times shift toward earlier departures for origins nearer the fire coordinate. Drivers also reconsider routes, and crew dispatch can wait for congestion, as in the coordinated policy.",
};
const count = (n: number | undefined) : string => typeof n === "number" && Number.isFinite(n) && n >= 0 ? n.toLocaleString() : "Unavailable";

/** A replay metric card: label, value at the replay time (missing stays "Unavailable") and caption. */
export type MetricCard = readonly [label: string, value: number | undefined, caption: string];

/** The replay metric cards both the Evacuation workspace and the Traffic Lab show above their panels. */
export function MetricCards({ items }: { items: readonly MetricCard[] }) : React.JSX.Element {
  return <div className="metrics">{items.map(([label, value, caption]) =>
    <div key={label}><span>{label}</span><b>{value ?? "Unavailable"}</b><small>{caption}</small></div>)}</div>;
}

/** Run provenance panel: engine checks, road network, data snapshot, inputs and any reused result. */
export function RunEvidence({ result, job }: { result: Result | null; job?: { id?: string; status?: string; created?: number } | null }) : React.JSX.Element | null {
  if (!result) return null;
  const snapshot = result.sources;
  const available = snapshot?.sources?.filter((s) => s.status === "available") || [];
  const valid = result.policies.every((p) => p.summary.valid_engine_run && p.summary.valid_accounting);
  return <section className="run-evidence" aria-label="Run evidence">
    <div className="section-title"><b>Computed with {result.engine} {result.engine_version}</b>
      <span className={`badge ${  valid ? "available" : "unavailable"}`}>{valid ? "Engine checks passed" : "Review engine diagnostics"}</span></div>
    <div className="evidence-grid">
      <div><span>ROAD NETWORK</span><p>{count(result.network.edge_count)} directional road segments from OpenStreetMap.
        Downloaded {formatDateTime(result.network.retrieved_at)}.</p></div>
      <div><span>PUBLIC DATA SNAPSHOT</span><p>{available.length} sources responded when this run fetched observations
        {snapshot?.retrieved_at ? ` on ${formatDateTime(snapshot.retrieved_at)}` : ""}.</p></div>
      <div><span>SCENARIO INPUTS</span><p>{result.scenario.vehicles} synthetic civilian vehicles, {result.scenario.crews} synthetic crew assignments
        and an assumed expanding hazard. Seed {result.scenario.seed}.</p></div>
      {result.reused_result && <div><span>REUSED RESULT</span><p>{result.reused_result.basis}
        {Number.isFinite(result.reused_result.simulated_at) ? ` Simulated ${formatDateTime(result.reused_result.simulated_at)}.` : ""}</p></div>}
    </div>
    <p className="evidence-boundary">Road geometry is used to calculate movement.
      {result.traffic?.status === "applied" ? " The saved TomTom snapshot sets experimental speed limits on matched highway segments." :
        " Traffic observations are context only in this run."}
      {" "}Public observations do not currently set simulated demand, road capacity, hazard growth or crew dispatch.
      Closure, fire and weather observations remain context. This replay is a fixed recording, not a live traffic display.</p>
    <details><summary>Inspect source freshness and evidence for this run</summary>
      <p>Run {job?.id || "exported result"} · {result.policies.length} policy recordings.
        {" "}Engine checks count collisions, teleports and conserved demand; they do not validate this scenario for operational use.</p>
      <ul>{snapshot?.sources?.map((source) => {
        const records = Array.isArray(source.records) ? source.records : [];
        const timed = records.filter((r) => "fresh" in r);
        return <li key={source.name}><b>{source.name}:</b> {source.status.split("_").join(" ")}
          {source.retrieved_at ? ` · fetched ${formatDateTime(source.retrieved_at)}` : ""}
          {Array.isArray(source.records) ? ` · ${records.length} nearby records` : ""}
          {timed.length > 0 ? ` · ${timed.filter((r) => r.fresh).length} fresh at retrieval` : ""}
          {source.name.endsWith("LCS") ? ` · ${records.filter((r) => r.active).length} active under feed checks; not applied as simulation closures` : ""}
          {source.name.includes("CCTV") ? " · inventory only; image freshness unknown" : ""}</li>;
      })}</ul>
      <p>The displayed map can show a later context refresh, but the completed run retains the source snapshot listed here.
        Use <a href="#observations">Data coverage & freshness</a> to fetch current observations.</p>
      <p>Road download hash: <code>{result.network.osm_sha256}</code><br/>
        Converted network hash: <code>{result.network.net_sha256}</code>. Export run JSON to inspect inputs, frames and accounting.</p>
    </details>
  </section>;
}

/** Explain in plain words which phase a queued or running simulation job is in. */
export function ProgressExplanation({ job }: { job: { progress: number; status: string } }) : React.JSX.Element {
  const p = job.progress;
  const detail = job.status === "queued"
    ? "Your scenario is saved and waiting for the local or cloud worker to begin."
    : p < 0.04 ? "Finding the selected road corridor around your fire coordinate."
    : p < 0.09 ? "Downloading nearby roads. Public map services can take a few minutes to respond."
    : p < 0.12 ? "Preparing road directions, lanes and junctions for vehicle movement."
    : p < 0.15 ? "Saving available road, fire and weather observations as context for this run."
    : p < 0.2 ? "Assigning the same synthetic civilian demand and crew tasks to all selected policies."
    : p < 0.98 ? "Calculating vehicle movement and scenario closures for each policy. Minutes shown in the phase are simulated time, not a countdown of how long you must wait."
    : "Saving the comparison and vehicle recording. Playback will be available when this finishes.";
  return <p className="progress-explanation">{detail}</p>;
}

/** Replay status line: simulated clock, playing, paused or ended at the time limit. */
export function ReplayStatus({ result, frame, playing }: { result: Result | null; frame?: Frame | null; playing: boolean }) : React.JSX.Element | null {
  if (!result) return null;
  const t = frame?.t || 0;
  const end = t >= result.scenario.duration_minutes * 60;
  return <div className={`replay-status${  end ? " replay-ended" : ""}`}>
    <b>{end ? "End of recorded run" : playing ? "Playing the recorded simulation" : t === 0 ? "Replay ready" : "Replay paused"}
      {" · "}{time(t)} simulated</b>
    <span>{end
      ? `The ${result.scenario.duration_minutes}-minute time limit has been reached. Vehicles left on the map are unresolved at that cutoff. Press Play to restart the recording.`
      : "Press Play or drag the time slider to inspect vehicle movement. This is a recorded scenario, not live traffic."}</span>
  </div>;
}

/** Stage 2's radio status line: listening while a run plays, then how it ended. A run that ended in an error reads
 * "stopped", not "finished"; the stage 2 alert line shows the error. */
export function RadioStatus({ radio }: { radio: { isMonitoring: boolean; errorMessage: string | null } }) : React.JSX.Element {
  return <p>{radio.isMonitoring ? "Listening for a reported incident…"
    : radio.errorMessage ? "Radio processing stopped." : "Radio processing finished."}</p>;
}

/** Map key legend: vehicle and crew dots, exit gates, highway, closures, hazard and fire perimeter. */
export function MapGuide({ sources, scenario }: { sources?: Sources | null; scenario: Pick<Scenario, "lat" | "lon"> }) : React.JSX.Element {
  const matching = sources?.point?.lat === scenario.lat && sources?.point?.lon === scenario.lon;
  const perimeter = matching ? sources?.sources?.find((s) => s.name === "WFIGS fire perimeters") : undefined;
  return <section className="map-guide" aria-label="Map key">
    <h3>How to read this map</h3>
    <div className="map-key-grid">
      {[
        ["civilian", "Blue dots · evacuation vehicles", "Only vehicles already on the roads are drawn. A cluster may be a queue; inspect a dot to see its recorded speed."],
        ["crew", "Orange dots · fire crews", "Larger vehicles with synthetic staging tasks. They obey the same road closures and signals as civilian traffic."],
        ["gate", "Green circles · assumed exit gates", "Reaching a gate counts as leaving the modeled network. A gate is not a verified safe refuge."],
        ["highway", "Blue line · selected road corridor", "The corridor you chose. Thin gray-blue lines are other roads in the model."],
        ["closure", "Dark red lines · scenario closures", "New entry is prohibited along each entire directional road segment if any part intersects the assumed hazard. Red can extend beyond the circle."],
        ["hazard", "Dashed red circle · assumed hazard", "The shaded area grows using your radius, start time and growth settings. It is not an observed fire boundary or a spread forecast."],
      ].map(([kind, label, detail]) => <div className="map-key-item" key={kind}>
        <i className={`map-swatch ${kind}`} aria-hidden="true"/><div><b>{label}</b><p>{detail}</p></div>
      </div>)}
    </div>
    <p className="map-context"><span className="fire-key" aria-hidden="true">⊙</span> The red target marks your fire coordinate.
      {" "}{perimeter?.status === "available"
        ? perimeter.geojson?.features?.length
          ? "Dashed brown outlines are observed WFIGS perimeters from the displayed snapshot; their capture freshness is listed under Data coverage."
          : "No observed WFIGS perimeter was returned for this snapshot area. The red circle is still your scenario assumption."
        : "Observed fire-perimeter context is not available for this displayed area. The red circle remains a scenario assumption."}</p>
  </section>;
}

/** Narrate the replay frame: hazard growth, vehicles exited, waiting and exposed. */
export function ReplayExplanation({ result, policy, frame, metrics }: { result: Result | null; policy?: Policy; frame?: Frame | null; metrics?: Sample | null }) : React.JSX.Element | null {
  if (!result || !policy || !frame || !metrics) return null;
  const s = result.scenario;
  const started = frame.t >= s.hazard_start_minutes * 60;
  const ended = frame.t >= s.duration_minutes * 60;
  return <section className="replay-explanation" aria-label="Replay explanation">
    <div className="section-title"><h3>What is happening at {time(frame.t)}?</h3>
      <span className="badge">{policyNames[policy.policy]}</span></div>
    <p className="policy-explanation">{policyDescriptions[policy.policy]}</p>
    <div className="explain-grid">
      <article><h4>People and traffic</h4>
        <p><b>{count(metrics.exited)} of {count(s.vehicles)} civilian vehicles have reached a model exit.</b>
          {" "}{count(metrics.active)} are still on roads and {count(metrics.not_departed)} have not departed.</p>
        <p>{metrics.queue > 0
          ? `${count(metrics.queue)} vehicles, including crews, are moving below about 1.1 mph at this instant. They may be queued, waiting at signals or closures, or making a scheduled crew stop. The recording does not identify a single cause for each stop.`
          : "No on-road vehicle is moving below about 1.1 mph at this instant."}</p>
        {frame.t === 0 && <p>At the start, vehicles are still waiting at their assigned origins. Press Play to see them enter the roads.</p>}
      </article>
      <article><h4>Hazard and access</h4>
        <p>{!started
          ? `The assumed hazard begins at ${time(s.hazard_start_minutes * 60)}, with an initial radius of ${feet(s.hazard_radius_m)} and growth of ${feet(s.hazard_growth_m_min)} per minute.`
          : `The assumed hazard radius is about ${feet(frame.hazard_radius_m)}. ${count(metrics.closed_edges)} directional road segments are closed to new entry at this recorded time.`}</p>
        <p>Closing a segment does not delete vehicles already on it. They remain visible and may attempt to leave. Future departures from closed origins stay in the “Not departed” count.</p>
        <p>{count(metrics.exposed)} civilian vehicles have intersected the assumed hazard by this time, including any waiting at affected origins. This cumulative exposure count is not an injury or fatality estimate.</p>
      </article>
    </div>
    <p className="crew-explanation"><b>Crews:</b> {metrics.crews_staged} of {s.crews} have reached an assigned staging segment by this time.
      {" "}A staging arrival is not completion of firefighting. The model assigns a two-minute stop before the crew attempts to return to an exit gate.
      {ended && <> By the end of this run, {policy.summary.crews_returned} had returned to a gate.</>}</p>
  </section>;
}

/** Explain a policy's final exits and exposure relative to the baseline policy. */
export function OutcomeExplanation({ result, policy }: { result: Result | null; policy: Policy }) : React.JSX.Element | null {
  if (!result) return null;
  const s = policy.summary;
  const requested = result.scenario.vehicles;
  const baseline = result.policies.find((p) => p.policy === "baseline");
  const delta = baseline ? s.exited_vehicles - baseline.summary.exited_vehicles : null;
  const comparable = baseline && missionMetrics(baseline, result.scenario) && missionMetrics(policy, result.scenario);
  const t95 = exitThreshold(policy, result.scenario);
  return <section className="outcome-explanation" aria-label="Result explanation">
    <h3>{policyNames[policy.policy]} · how to interpret the result</h3>
    <p><b>{count(s.exited_vehicles)} of {count(requested)} vehicles ({(100 * s.exited_vehicles / requested).toFixed(1)}%) reached an assumed exit by {result.scenario.duration_minutes} minutes.</b>
      {" "}That represents {count(s.exited_people)} people at the entered occupancy. It does not establish that they reached safety outside the model.</p>
    <p><b>{count(s.unresolved_vehicles)} vehicles remain unresolved: {count(s.active_vehicles)} still on roads + {count(s.not_departed_vehicles)} not departed.</b>
      {" "}{s.unroutable_vehicles} had no initial route to a gate.
      {s.origin_blocked_vehicles != null && <> Another {s.origin_blocked_vehicles} routed {s.origin_blocked_vehicles === 1 ? "vehicle was" : "vehicles were"} held at closed origins.</>}
      {" "}Other departures may wait for space or timing; people are not removed from the accounting.</p>
    <p><b>“95% exit time”</b> requires at least {Math.ceil(requested * 0.95)} of the originally requested {requested} vehicles to reach a gate.
      {" "}{t95.status === "not_reached" ? "“Not reached” means that threshold was not met within this run."
        : t95.seconds == null ? "The saved threshold timing is missing or invalid; it remains unavailable."
          : `This policy reached that threshold at ${time(t95.seconds)}.`}</p>
    <p><b>Exposure and crews:</b> {s.exposed_vehicles} civilian vehicles intersected the assumed hazard at some point;
      {" "}{s.crews_staged} of {s.crews_requested} crews reached staging and {s.crews_returned} returned to an exit.
      {" "}Staging, returning, and exposure are separate outcomes.</p>
    {policy.policy !== "baseline" && delta !== null && comparable && <p className="comparison-delta">
      Compared with baseline, this policy produced {Math.abs(delta)} {delta < 0 ? "fewer" : delta > 0 ? "more" : "additional"} exits
      {" "}in this one scenario. Review exposure, crew outcomes and unresolved demand as well; this does not establish a generally better policy.</p>}
    <small>All result totals describe the full run, even when the map is paused at an earlier time. The map counters describe the selected replay time.</small>
  </section>;
}

/** Explain where and when the queue peaked for a policy, scaled across all policies. */
export function BottleneckExplanation({ result, policy, frame }: { result: Result | null; policy?: Policy; frame?: Frame | null }) : React.JSX.Element | null {
  if (!result || !policy) return null;
  const peak = peakSample(policy.series);
  const maximum = Math.max(1, ...result.policies.flatMap((p) => p.series.map((s) => s.queue)));
  const horizon = result.scenario.duration_minutes * 60;
  const colors = POLICY_COLORS;
  return <section className="bottleneck-explanation" aria-label="Bottleneck explanation">
    <h3>Where the simulation helps you investigate bottlenecks</h3>
    <p>Closely packed, slow vehicles show where to investigate restricted movement. Replay the buildup,
      inspect vehicle speeds and scenario closures, then compare whether another policy changes the queue.
      A stopped vehicle alone does not identify the cause: signals, traffic, closures and crew stops can all contribute.</p>
    <div className="explain-grid">
      <div><b>Slow or stopped vehicles over time · all policies</b>
        <svg viewBox="0 0 500 180" role="img" aria-label="Slow and stopped vehicles over time by policy">
          {[0, 0.5, 1].map(f => <g key={f}><line x1="40" x2="480" y1={140 - f * 115} y2={140 - f * 115} stroke="#dce4e3"/>
            <text x="0" y={144 - f * 115}>{Math.round(maximum * f)}</text></g>)}
          {result.policies.map((p, i: number) => <polyline key={p.policy} fill="none" stroke={colors[i]}
            strokeWidth={p.policy === policy.policy ? 3.5 : 2} points={p.series.map((s) =>
              `${40 + s.t / horizon * 440},${140 - s.queue / maximum * 115}`).join(" ")}/>)}
          <line x1={40 + (frame?.t || 0) / horizon * 440} x2={40 + (frame?.t || 0) / horizon * 440}
            y1="18" y2="144" stroke="#263e3d" strokeDasharray="3 3"/>
          <text x="40" y="166">0 min</text><text x="430" y="166">{result.scenario.duration_minutes} min</text>
        </svg>
        <div className="chart-key">{result.policies.map((p, i: number) =>
          <span key={p.policy}><i style={{ background: colors[i] }}/>{policyNames[p.policy]}</span>)}</div>
        <small>Below about 1.1 mph, including crews and scheduled stops. The vertical line marks the map's replay time.</small>
      </div>
      <div><p><b>{policyNames[policy.policy]}:</b> the highest recorded slow/stopped count was <b>{peak.queue}</b> at {time(peak.t)}.
        At the final cutoff, {policy.series.at(-1)!.queue} vehicles were slow or stopped.</p>
        <p>Coordinated routing gives groups of civilian drivers a chance to choose travel-time routes every 30 seconds.
          Phased departures change when drivers enter the network. Crews may wait up to two extra minutes for congestion on their initial route.</p>
        <p>These policies cannot reopen hazard closures, create missing roads or guarantee a detour. Compare final exits, exposure and crew staging together.
          A lower queue count alone is not proof that more people exited.</p></div>
    </div>
  </section>;
}

/** One line for a crew's illustrative stop use; every number it shows carries the illustrative label. */
function illustrationLine(illustration: StopIllustration): string {
  switch (illustration.kind) {
    case "support": return "Support role · no pumping";
    case "not_reached": return "Stop not reached";
    case "unavailable": return "Illustration unavailable for this recording";
    case "stopped": {
      const use = `Illustrative · not simulated: about ${illustration.gallons.toLocaleString("en-US")} gal at ${illustration.gpm} gpm`;
      return illustration.seconds >= illustration.stopSeconds
        ? `${use} over the ${time(illustration.stopSeconds)} recorded stop`
        : `${use}, ${time(illustration.seconds)} into the ${time(illustration.stopSeconds)} recorded stop`;
    }
    default: {
      const unexpected: never = illustration;
      return unexpected;
    }
  }
}

/** Explain fire crew dispatch and staging, with each crew's phase and route or trail toggles. With `replayPolicy`
 * (3 · Response Tracker), each crew also shows its demo role and one illustrative line from that policy's recording. */
export function CrewExplanation({ result, frame, showRoutes, showTrails, setRoutes, setTrails, locate, replayPolicy }: { result: Result | null; frame?: Frame | null; showRoutes: boolean; showTrails: boolean; setRoutes: (value: boolean) => void; setTrails: (value: boolean) => void; locate: (id: string, origin?: boolean) => void; replayPolicy?: Policy }) : React.JSX.Element | null {
  if (!result || !frame) return null;
  const plan = crewPlan(result);
  return <section className="crew-explanation-panel" aria-label="Crew dispatch explanation">
    <h3>How the fire crews are dispatched and arrive</h3>
    <p>Each crew starts at a synthetic origin near the outside of the study area and follows a planned route toward a staging segment
      outside the initial hazard buffer. The planned task is a two-minute stop, then an attempted return to a model exit gate.
      These are assumed locations and tasks, not real fire stations or live dispatch orders.</p>
    <div className="crew-steps"><span>1 · Wait at assigned origin</span><span>2 · Drive to staging</span>
      <span>3 · Stop for two minutes</span><span>4 · Attempt return to a gate</span></div>
    <p>Schedules are staggered to avoid dispatching every crew at once. Baseline uses the times below; coordinated and phased policies
      can delay submission by up to 120 seconds when an initial route segment has more than eight stopped vehicles.
      Actual road entry can wait longer for space. Crews obey road access rules and signals; no emergency signal priority is modeled.</p>
    <div className="route-controls"><label><input type="checkbox" checked={showTrails} onChange={e => setTrails(e.target.checked)}/>
      Show recorded crew trails in orange</label>
      {result.crew_plan && <label><input type="checkbox" checked={showRoutes} onChange={e => setRoutes(e.target.checked)}/>
        Show planned crew routes, origins and staging points</label>}</div>
    {!result.crew_plan && <p className="recording-gap">This older recording contains crew positions and totals but not the planned staging locations or individual dispatch phases.
      Orange trails show recorded movement only. New runs save the route plan and crew timeline.</p>}
    {replayPolicy && <p className="demo-role-note">Demo roles are display labels assigned by crew position; they do not
      change the simulated vehicles. Resource numbers are illustrative, accrue only during recorded staging stops, and are
      never sent to agents or saved.</p>}
    {plan.length ? <div className="table-scroll"><table><thead><tr><th>Crew</th>{replayPolicy && <th>Demo role · illustrative use</th>}<th>Scheduled / actual road entry</th><th>At {time(frame.t)}</th><th>Staging / return</th><th>Map action</th></tr></thead>
      <tbody>{plan.map((c, i: number) => {
        // Non-null: every id comes from crewPlan(result) itself.
        const d = crewDetails(result, frame, c.id)!;
        const role = crewRole(i);
        return <tr key={c.id}><th scope="row">{d.name}</th>
          {replayPolicy && <td><span aria-hidden="true">{getUnitIcon(role)}</span> <b>{getUnitName(role)}</b><br/>
            <small>{illustrationLine(stopIllustration(replayPolicy, c.id, role, result.crew_plan?.[i]?.service_seconds,
              frame.t))}</small></td>}
          <td>{d.scheduled}<br/><small>{d.entry}</small></td>
          <td>{d.phase}{d.speed && <><br/><small>Recorded speed: {d.speed}</small></>}</td>
          <td>{d.staging}{d.returned && <><br/>{d.returned}</>}</td>
          <td><button className="locate-crew" disabled={!d.onRoad && !d.plannedOrigin}
            aria-label={d.plannedOrigin ? `Show planned origin for ${d.name}` : `Locate ${d.name}`}
            onClick={() => locate(c.id, d.plannedOrigin)}>{d.plannedOrigin ? "Show planned origin" : "Locate"}</button>
            {!d.onRoad && !d.plannedOrigin && <small>No position at this replay time</small>}</td></tr>;
      })}</tbody></table></div> : <p>No crew vehicles were requested in this scenario.</p>}
    <small>Orange trails connect recorded positions up to the selected time. Dashed purple lines, when enabled, are planned paths, not observed movement.
      Reaching a staging segment is distinct from completing its stop or returning to a gate.</small>
  </section>;
}
