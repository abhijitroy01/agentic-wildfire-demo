import type { IncidentRequest,IncidentResponse } from "../generated/contracts";

export type ScenarioLocationInput = Omit<IncidentRequest,"request_id" | "session_id">;

/** Retry the saved source itself; a reported fire must never become a geocoded name or city point. */
export function incidentLocationInput(incident: IncidentResponse): ScenarioLocationInput {
  if (incident.preset_id) {
    if (!["paradise-2018","palisades-2025"].includes(incident.preset_id)) {
      throw new Error("This saved reference place is unavailable. Choose a new scenario location.");
    }
    return {preset_id:incident.preset_id as IncidentRequest["preset_id"]};
  }
  const location = incident.location, label = location.reported_address || location.matched_address;
  if (location.origin === "reported_fire") {
    if (location.source_observation_id) return {fire_observation_id:location.source_observation_id};
    if (!location.source_assessment_id) {
      throw new Error("The saved reported-fire reference is unavailable. Choose the fire again in Investigate.");
    }
    return {fire_assessment_id:location.source_assessment_id};
  }
  if (location.origin === "user_coordinates") return {coordinates:{lat:location.lat,lon:location.lon,label}};
  return location.origin === "geocoded_place" ? {reported_place:label} : {reported_address:label};
}
