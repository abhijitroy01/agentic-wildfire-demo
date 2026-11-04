import { useEffect, useState } from "react";
import Accordion from "@/shared/components/Accordion";
import type { AssessmentResult, FireRiskPreparationError } from "../generated/contracts";
import type { FireRiskJobView } from "../hooks/useFireRiskJobs";
import {
  assessmentGroups, noAdditionalScenarioGrowth, scenarioHorizons, selectedScenarioMinute, type ScenarioSelection,
} from "../services/assessment";
import "./FireExposureSummary.css";

export interface FireExposureChoice {
  key: string;
  label: string;
  latitude: number;
  longitude: number;
}

interface FireExposureSummaryProps {
  assessments: readonly AssessmentResult[];
  selections: ScenarioSelection;
  onScenarioTime: (assessmentId: string, minute: number | null) => void;
  onSimulateReportedFire?: (assessmentId: string) => void;
  choices?: readonly FireExposureChoice[];
  selectedFire?: string;
  onSelectFire?: (key: string) => void;
  jobs?: readonly FireRiskJobView[];
  onRefreshStatus?: () => void;
  onCancelJob?: (jobId: string) => Promise<void>;
  onRetryJob?: (jobId: string) => Promise<void>;
  preparationError?: FireRiskPreparationError;
}

function timestamp(value: string | null | undefined): string {
  return value && Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString("en-US",
    {timeZone:"UTC",timeZoneName:"short"}) : "Unavailable";
}

function weatherLabel(assessment: AssessmentResult): string {
  const weather = assessment.weather;
  if (!weather || weather.availability !== "available") {
    return weather?.availability === "stale" ? "DTN weather stale" : "DTN weather unavailable";
  }
  if (weather.wind_speed_mps === 0) return "DTN measured calm wind";
  return typeof weather.wind_speed_mps === "number" && typeof weather.wind_from_degrees === "number"
    ? `DTN wind ${(weather.wind_speed_mps * 2.236936).toFixed(1)} mph from ${weather.wind_from_degrees.toFixed(0)}°`
    : "DTN weather available; usable wind unavailable";
}

/** Status reads and returned geometry controls belong to this answer; selection starts no model work. */
export default function FireExposureSummary({
  assessments, selections, onScenarioTime, onSimulateReportedFire, choices = [], selectedFire,
  onSelectFire, jobs = [], onRefreshStatus, onCancelJob, onRetryJob,
  preparationError,
}: FireExposureSummaryProps): React.ReactElement | null {
  const [expanded, setExpanded] = useState(jobs.length > 0 || !!preparationError);
  const jobKey = jobs.map(job => job.reference.job_id).join(",");
  useEffect(() => { if (jobKey || preparationError) setExpanded(true); }, [jobKey, preparationError]);
  if (!assessments.length && !jobs.length && !preparationError) return null;
  const groups = assessmentGroups(assessments);
  return <aside className="fire-exposure-summary" aria-label="Fire exposure evidence">
    <Accordion title="Fire exposure evidence" badge={choices.length || groups.length}
      isExpanded={expanded} onToggle={() => setExpanded(open => !open)}>
      {preparationError && <p className="fire-exposure-warning" role="alert">
        {preparationError.message} ({preparationError.code}) Reported fires remain on the map.
      </p>}
      {choices.length > 0 && onSelectFire && <label className="fire-exposure-select">
        Focus reported fire
        <select value={selectedFire ?? choices[0].key} onChange={event => onSelectFire(event.currentTarget.value)}>
          {choices.map(choice => <option key={choice.key} value={choice.key}>{choice.label}</option>)}
        </select>
      </label>}
      {jobs.map(job => <div key={job.reference.job_id} className="fire-exposure-job" role="status">
        {job.readError ? <>
          <p>Assessment status could not be read. {job.readError}</p>
          {onRefreshStatus && <button type="button" onClick={onRefreshStatus}>Refresh saved status</button>}
        </> : !job.result ? <p>Reading saved terrain assessment status…</p>
          : job.result.status === "cancelled" ? <p>Terrain assessment cancelled. Reported fires remain on the map.</p>
            : job.result.status === "failed" ? <p className="fire-exposure-warning">
            Terrain assessment failed. {job.result.error || job.result.phase} Reported fires remain on the map.</p>
            : job.result.status !== "completed" ? <>
              <p>Terrain assessment {job.result.status}. {job.result.phase}</p>
              <progress max={1} value={job.result.progress} aria-label="Terrain assessment progress" />
              <p>The chat is complete; fuel, elevation and fire-point weather preparation continues.</p>
            </> : <p>Saved terrain assessment complete.</p>}
        {job.result && ["queued", "running", "cancelling"].includes(job.result.status) && onCancelJob
          && <button type="button" disabled={job.cancelling || job.result.status === "cancelling"}
            onClick={() => void onCancelJob(job.reference.job_id)}>
            {job.cancelling || job.result.status === "cancelling" ? "Cancelling…" : "Cancel terrain assessment"}</button>}
        {job.cancelError && <p className="fire-exposure-warning">{job.cancelError}</p>}
        {onRetryJob && job.result && (["failed", "cancelled"].includes(job.result.status)
          || (job.result.assessments ?? []).some(item => item.availability === "unavailable"))
          && <button type="button" disabled={job.retrying}
            onClick={() => void onRetryJob(job.reference.job_id)}>
            {job.retrying ? "Starting terrain retry…" : "Retry unavailable terrain with saved weather"}</button>}
        {job.retryError && <p className="fire-exposure-warning">{job.retryError}</p>}
      </div>)}
      {choices.length > 0 && !groups.length && <p>No saved terrain result for the selected fire.</p>}
      {groups.map(group => {
        const assessment = group.find(item => item.mode === "landscape" && item.availability === "available")
          ?? group.find(item => item.mode === "landscape")
          ?? group.find(item => item.mode !== "surface_fire") ?? group[0];
        return <section key={assessment.fire_ref} className="fire-exposure-item">
          <strong>{assessment.fire.name || "Reported fire"}</strong>
          <p>{assessment.availability === "unavailable" ? assessment.mode === "landscape"
            ? "Terrain assessment unavailable; reported fire retained" : "Saved exposure unavailable; reported fire retained"
            : assessment.mode === "landscape" ? "Modeled spread from an assumed point ignition"
              : assessment.mode === "surface_fire" ? "Historical uniform-fuel surface-fire demo"
                : "Historical distance context"}</p>
          {assessment.failure && <p className="fire-exposure-warning">
            {assessment.failure.message} ({assessment.failure.code})</p>}
          <p>{weatherLabel(assessment)}</p>
          <dl className="fire-exposure-observations">
            <dt>Fire record updated</dt><dd>{timestamp(assessment.fire.source_timestamp)}</dd>
            <dt>Weather observed</dt><dd>{timestamp(assessment.weather?.observed_at)}</dd>
          </dl>
          {assessment.landscape && <>
            <p className="fire-exposure-resolution">{assessment.landscape.manifest.resolution_m} m model cells</p>
            {assessment.landscape.spread_cells === 0 && <p>No modeled growth beyond the ignition cell.</p>}
            {assessment.landscape.nodata_cells > 0 && <p className="fire-exposure-warning">
              {assessment.landscape.nodata_cells} cells have missing inputs; they are not established as safe.</p>}
          </>}
          {group.filter(item => ["surface_fire", "landscape"].includes(item.mode)).map(item =>
            <ScenarioControls key={item.assessment_id} assessment={item} selections={selections}
              onScenarioTime={onScenarioTime} />)}
          {group.filter(item => ["proximity", "wind_informed"].includes(item.mode)
              && item.availability === "available").map(item =>
            <label key={item.assessment_id}><input type="checkbox"
              checked={typeof selections[item.assessment_id] === "number"}
              onChange={event => onScenarioTime(item.assessment_id, event.currentTarget.checked ? 1 : null)} />
              Show historical distance context</label>)}
          <AssessmentDetails assessment={assessment} />
          {onSimulateReportedFire && <button type="button" className="fire-exposure-action"
            onClick={() => onSimulateReportedFire(assessment.assessment_id)}>Plan evacuation for this fire</button>}
        </section>;
      })}
      <p className="fire-exposure-note">Illustrative spread, not evacuation guidance. Unshaded land is not established
        as safe.</p>
    </Accordion>
  </aside>;
}

function AssessmentDetails({ assessment }: { assessment: AssessmentResult }): React.ReactElement {
  return <details className="fire-exposure-details">
    <summary>Model details</summary>
    <dl>
      <dt>Calculated</dt><dd>{timestamp(assessment.calculated_at)}</dd>
      <dt>Model</dt><dd>{assessment.model_version}</dd>
      {assessment.fire.source_id && <><dt>Fire source</dt><dd>{assessment.fire.source_id}</dd></>}
    </dl>
    {assessment.landscape && <>
      <p>{assessment.landscape.manifest.fuel_product}; {assessment.landscape.manifest.elevation_product}.
        {" "}{assessment.landscape.manifest.region}; {assessment.landscape.manifest.width}
        {" × "}{assessment.landscape.manifest.height} model cells.</p>
      <p>Landscape captured {timestamp(assessment.landscape.manifest.captured_at)}.</p>
      <p>Ignition-cell slope {assessment.landscape.ignition_slope_degrees.toFixed(1)}°;
        fuel model {assessment.landscape.ignition_fuel_model}.</p>
    </>}
    {assessment.scenario && <p>{assessment.scenario.initialization}.
      {" "}{assessment.scenario.wind_height_assumption}.</p>}
    {assessment.assumptions.length > 0 && <ul>{assessment.assumptions.map((assumption, index) =>
      <li key={index}>{assumption}</li>)}</ul>}
    {(assessment.warnings ?? []).length > 0
      && <p className="fire-exposure-warning">{assessment.warnings?.join(" ")}</p>}
    <p>These demo scenarios are not evacuation zones, burn probabilities or operational forecasts.
      Gaps inside shapes do not mean safe.</p>
  </details>;
}

function ScenarioControls({ assessment, selections, onScenarioTime }: {
  assessment: AssessmentResult; selections: ScenarioSelection;
  onScenarioTime: FireExposureSummaryProps["onScenarioTime"];
}): React.ReactElement | null {
  const horizons = scenarioHorizons(assessment);
  const selected = selectedScenarioMinute(assessment, selections);
  if (!horizons.length) return null;
  const landscape = assessment.mode === "landscape";
  return <div className="fire-exposure-timeline">
    <label><input type="checkbox" checked={selected !== null}
      onChange={event => onScenarioTime(assessment.assessment_id,
        event.currentTarget.checked ? horizons[horizons.length - 1] : null)} />
      {landscape ? "Show terrain and fuel informed scenario" : "Show surface-fire demo scenario"}</label>
    <label>
      Scenario time: {selected ?? horizons[horizons.length - 1]} minutes
      <span className="fire-exposure-timeline-caption">After assumed ignition</span>
      <input type="range" min={0} max={horizons.length - 1} step={1}
        disabled={selected === null} value={selected === null ? horizons.length - 1 : horizons.indexOf(selected)}
        aria-label={`Scenario time for ${assessment.fire.name || "reported fire"}`}
        aria-valuetext={`${selected ?? horizons[horizons.length - 1]} minutes after assumed initialization`}
        onChange={event => {
          const minute = horizons[Number(event.currentTarget.value)];
          if (minute !== undefined) onScenarioTime(assessment.assessment_id, minute);
        }} />
    </label>
    {assessment.scenario && <p>Assumed {assessment.scenario.moisture_profile} fuel moisture.</p>}
    {assessment.landscape?.spread_cells !== 0 && noAdditionalScenarioGrowth(assessment, selections)
      && <p className="fire-exposure-no-growth">No additional modeled growth since the previous time step.</p>}
  </div>;
}
