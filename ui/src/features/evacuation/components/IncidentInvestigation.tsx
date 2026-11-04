import CitationsSection from "@/features/chat/components/CitationsSection";
import ForecastSection from "@/features/weather/components/ForecastSection";
import AgentActivity from "@/shared/components/AgentActivity";
import type { KnowledgeBaseCitation } from "@/shared/services/agentResponseParser";
import { labelForTool } from "@/shared/services/agentToolLabels";
import { savedSupervisorRun, type ToolEvent } from "@/shared/services/toolActivity";
import type { IncidentInvestigationResponse } from "../generated/contracts";
import type { ForecastData } from "@/features/weather";
import { INCIDENT_TIME_ZONE, observationDetails, observationTime, weatherMetrics } from "@/shared/domain/incidentObservations";
import type { IncidentObservation } from "@/shared/domain/incidentObservations";
import type { Result } from "../artifacts";
import { MissionFindings } from "./MissionComparison";

export function investigationActivity(task: IncidentInvestigationResponse | null): ToolEvent[] {
  return (task?.activity || []).map(event => ({
    id: `${task?.task_id}:${event.tool_call_id}`, name: event.name, label: labelForTool(event.name),
    startedAt: event.started_at*1000, endedAt: event.ended_at == null ? undefined : event.ended_at*1000,
    status: event.status,
  }));
}
function content(task: IncidentInvestigationResponse | null): Record<string, unknown> {
  return task?.snapshot?.content && typeof task.snapshot.content === "object"
    ? task.snapshot.content as Record<string, unknown> : {};
}

/** A location owns its fire and point weather; forecasts keep their own source location. */
export function IncidentConditions({ task, observations, selectedId, select, point, startFire, busy = false }: {
  task: IncidentInvestigationResponse | null; observations: IncidentObservation[]; selectedId: string | null;
  select: (id: string) => void; point?: {lat: number; lon: number};
  startFire?: (observation: IncidentObservation) => void; busy?: boolean;
}): React.JSX.Element {
  const first = content(task), selected = observations.find(value => value.id === selectedId) || observations[0];
  const forecastLocation = first.location as {name?: string; latitude?: number; longitude?: number} | undefined;
  const label = (observation: IncidentObservation): string => !observation.fire && point &&
    Math.abs(point.lat-observation.latitude) < .00001 && Math.abs(point.lon-observation.longitude) < .00001
    ? "Scenario point weather" : observation.name;
  const distance = (observation: IncidentObservation): string => {
    if (!point) return "";
    const radians = Math.PI/180, latitude = (observation.latitude-point.lat)*radians,
      longitude = (observation.longitude-point.lon)*radians;
    const a = Math.sin(latitude/2)**2 + Math.cos(point.lat*radians)*Math.cos(observation.latitude*radians)*
      Math.sin(longitude/2)**2;
    return ` · ${(3958.76*2*Math.atan2(Math.sqrt(a),Math.sqrt(Math.max(0,1-a))))
      .toLocaleString("en-US",{maximumFractionDigits:1})} miles from scenario point`;
  };
  return <section className="incident-conditions" aria-label="Location conditions">
    <h2>Locations &amp; conditions</h2>
    <p>Select a location to preview it. Start its workflow below to change the scenario. Weather belongs to the returned point.</p>
    {!observations.length && <p role="status">{task?.status === "completed"
      ? "No usable location observations were returned. Review the plan’s source limitations."
      : task?.status === "failed" ? "The investigation failed. Available partial evidence is retained."
      : "Waiting for sourced fire and point-weather observations."}</p>}
    {selected?.fire && selected.sourceObservationId && startFire && <div className="selected-fire-action">
      <button disabled={busy} onClick={() => startFire(selected)}>Start workflow for {selected.name}</button>
      <p>Uses this saved Esri fire point. Earlier investigations and simulations keep their own results.</p>
    </div>}
    <div className="incident-locations" role="group" aria-label="Observed locations">
      {observations.map(observation => <button className="incident-location" key={observation.id}
        aria-pressed={selected?.id === observation.id} onClick={() => select(observation.id)}>
        <strong>{label(observation)}</strong>
        <span>{observation.fire ? `Reported fire${distance(observation)}` : "Weather point"}</span>
        <span>{observation.weather ? weatherMetrics(observation.weather).slice(0, 4)
          .filter(([, value]) => value !== "Unavailable").map(([name, value]) => `${name}: ${value}`).join(" · ") || "Measurements unavailable"
          : "Weather unavailable at this point"}</span>
      </button>)}
    </div>
    {selected && <article className="selected-observation" aria-label="Selected location details">
      <h3>{label(selected)}</h3><small>{selected.latitude.toFixed(4)}, {selected.longitude.toFixed(4)} · WGS84</small>
      {selected.fire && !selected.sourceObservationId && startFire && <p>
        A saved source receipt is required to start this fire’s workflow. Refresh its investigation to obtain one.</p>}
      {selected.fire && <p>{selected.fire.acresBurned == null ? "Area unavailable" : `${Number(selected.fire.acresBurned).toLocaleString()} acres`}
        {" · "}{selected.fire.percentContained == null ? "Containment unavailable" : `${selected.fire.percentContained}% contained`}</p>}
      <p>{selected.weather ? String(selected.weather.weatherDescription || "Point weather available") : "Point weather unavailable"}</p>
      {selected.weather && <><dl className="weather-metrics">{weatherMetrics(selected.weather).map(([name, value]) =>
        <div key={name}><dt>{name}</dt><dd>{value}</dd></div>)}</dl>
        <small>Weather as of {observationTime(selected.weather.timestamp)}</small></>}
      <details><summary>Location source details</summary>
        <dl>{observationDetails(selected).map(([name, value]) => <div key={name}><dt>{name}</dt><dd>{value}</dd></div>)}</dl>
        <p>Returned fire and weather record. These observations do not drive simulated hazard growth.</p>
        <pre>{JSON.stringify({fire:selected.fire, weather:selected.weather}, null, 2)}</pre>
      </details>
    </article>}
    <section className="incident-forecast"><h3>Weather forecast</h3>
      <p>{forecastLocation?.name || (forecastLocation?.latitude != null && forecastLocation.longitude != null
        ? `Forecast point ${forecastLocation.latitude.toFixed(4)}, ${forecastLocation.longitude.toFixed(4)}`
        : "Forecast location not supplied")} · separate from the selected location’s current weather</p>
      {first.forecastData ? <ForecastSection forecastData={first.forecastData as ForecastData}
        usesAccordion={false} timeZone={INCIDENT_TIME_ZONE} /> : <p>Forecast pending or unavailable; values are not inferred.</p>}
    </section>
  </section>;
}

/** Wide detail area for sourced plans and real tool activity, independent of the map and radio player. */
export default function IncidentInvestigation({ initial, updated, error, section, result, runId, inspect, onRetry, onCancel }: {
  initial: IncidentInvestigationResponse | null; updated: IncidentInvestigationResponse | null; error: string;
  result: Result | null; runId?: string;
  inspect: (policy: string, view: "overview" | "bottlenecks" | "crews") => void;
  section: "plans" | "activity"; onRetry: (runId?: string) => void; onCancel: (runId?: string) => void;
}): React.JSX.Element {
  const matched = !!result?.input_bundle_id && !!result.input_bundle_sha256 && !!runId &&
    updated?.run_id === runId && updated.input_bundle_id === result.input_bundle_id &&
    updated.input_bundle_sha256 === result.input_bundle_sha256;
  const hasUpdated = updated?.status === "completed";
  return <section className="incident-investigation" aria-label="Incident investigation">
    <h2>{section === "plans" ? "Response plans" : "Agent activity"}</h2>
    <p>Fire and weather investigation runs alongside road preparation. The updated plan uses completed simulation evidence.</p>
    {error && <p role="alert">{error}</p>}
    {[initial, updated].filter((task): task is IncidentInvestigationResponse => !!task).map(task =>
      <div className="investigation-status" key={task.task_id} role="status">
        <b>{task.stage === "initial" ? "Initial investigation" : "Simulation-informed plan"}</b> · {task.status}
        {task.error && <p>{task.error}</p>}
        {task.status === "running" && <button className="secondary" onClick={() => onCancel(task.run_id || undefined)}>Cancel this investigation</button>}
        {["failed", "interrupted", "cancelled"].includes(task.status) &&
          <button onClick={() => onRetry(task.run_id || undefined)}>Retry this investigation</button>}
      </div>)}
    {section === "plans" ? <div className="planning-assessments">
      {hasUpdated || updated?.status === "running" ? <>
        <section className="updated-assessment"><h3>Simulation-informed planning assessment</h3>
          <p>{hasUpdated ? "New evidence from the completed simulation" : "Simulation evidence is ready; the cited assessment is still running."}</p>
          {matched && result ? <MissionFindings result={result} inspect={inspect}/> :
            <p className="recording-gap">Matching run and frozen-input evidence is unavailable. New policy highlights cannot be verified against this saved assessment.</p>}
          <Plan value={content(updated)} task={updated} foldText/>
          <p>These findings add measurements to the initial assessment. Policy differences above compare with baseline;
            the initial investigation had no simulated outcomes.</p>
        </section>
        <details className="initial-assessment"><summary>Initial assessment · before simulation</summary>
          <Plan value={content(initial)} task={initial}/></details>
      </> : <>
        <section><h3>Initial planning assessment</h3>
          <p>Sourced observations and published guidance before simulation. No simulated outcomes are available in this assessment.</p>
          <Plan value={content(initial)} task={initial}/></section>
        <section className="pending-assessment"><h3>Simulation-informed planning assessment</h3>
          <p>{result ? "The simulation is complete. Build the cited assessment using this run’s frozen evidence." :
            "Review and run the prepared scenario. Simulation findings become available after the run completes."}</p>
          {result && runId && <button onClick={() => onRetry(runId)}>Build simulation-informed assessment</button>}
        </section>
      </>}
    </div> : <>
      <AgentActivity events={investigationActivity(initial)} run={savedSupervisorRun(initial)}
        title="Fire, weather & guidance agents" />
      <AgentActivity events={investigationActivity(updated)} run={savedSupervisorRun(updated)}
        title="Simulation-informed planning agent" />
      <p>Response received means a tool returned data. Source availability and limitations belong to the evidence; it does not certify a response plan or field safety.</p>
    </>}
  </section>;
}
function Plan({ value, task, foldText = false }: {
  value: Record<string, unknown>; task: IncidentInvestigationResponse | null; foldText?: boolean;
}): React.JSX.Element {
  const text = <p className="planning-assessment-text">{typeof value.response === "string" ? value.response :
    task?.status === "running" ? "Agents are gathering evidence and drafting this plan." : "This plan is not available yet."}</p>;
  return <div>
    {foldText ? <details><summary>Full saved assessment and guidance</summary>{text}</details> : text}
    {task?.completed_at && <small>Saved {observationTime(task.completed_at*1000)}</small>}
    {task?.run_id && <details><summary>Plan lineage</summary><p>Run {task.run_id} · frozen input bundle {task.input_bundle_id || "unavailable"}</p></details>}
    {Array.isArray(value.knowledgeBaseCitations) && value.knowledgeBaseCitations.length > 0 &&
      <details className="planning-sources"><summary>Sources · {value.knowledgeBaseCitations.length} articles</summary>
        <CitationsSection citations={value.knowledgeBaseCitations as KnowledgeBaseCitation[]} foldArticles usesAccordion={false}/>
      </details>}
  </div>;
}
