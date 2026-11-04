import type {
  DraftResponse, IncidentInvestigationResponse, IncidentResponse, SessionResponse,
} from "../generated/contracts";
import type { IncidentObservation } from "@/shared/domain/incidentObservations";
import type { ScenarioLocationInput } from "./locationInput";

export type ReportedFireReference = NonNullable<IncidentInvestigationResponse["fire_observations"]>[number];
export interface WorkflowStartIntent {
  readonly version: 1;
  readonly id: string;
  readonly entryMode: "radio" | "lab";
  readonly source: ScenarioLocationInput;
  readonly label: string;
  readonly sessionRequestId: string;
  readonly incidentRequestId: string;
  readonly preparationRequestId: string;
  readonly radiusKm?: number;
  readonly retryAtMs?: number;
  readonly geocoderOutcomeUnknown?: boolean;
  readonly session?: SessionResponse;
  readonly incident?: IncidentResponse;
  readonly draftId?: string;
}
export interface RetainedWorkflow {
  readonly sessionId: string;
  readonly incidentId: string;
  readonly label: string;
}
export interface WorkflowStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}
export type WorkflowRequest = <T>(path: string, body?: unknown) => Promise<T>;
const intentKey = "fireguard_workflow_start";
const catalogKey = "fireguard_reported_fire_catalog";
const historyKey = "fireguard_retained_workflows";
const sourceKeys = ["reported_address", "reported_place", "coordinates", "fire_assessment_id",
  "fire_observation_id", "preset_id"] as const;
const object = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);
const text = (value: unknown): value is string => typeof value === "string" && value.length > 0;

function read(storage: WorkflowStorage, key: string): unknown {
  try { return JSON.parse(storage.getItem(key) || "null") as unknown; } catch { return null; }
}
function write(storage: WorkflowStorage, key: string, value: unknown): void {
  storage.setItem(key, JSON.stringify(value));
}

/** One explicit start retains the same request identities through lost responses and reloads. */
export function createWorkflowStart(entryMode: "radio" | "lab", source: ScenarioLocationInput, label: string,
  uuid: () => string = () => crypto.randomUUID()): WorkflowStartIntent {
  return {version: 1, id: uuid(), entryMode, source, label, sessionRequestId: uuid(),
    incidentRequestId: uuid(), preparationRequestId: uuid()};
}
export function readWorkflowStart(storage: WorkflowStorage): WorkflowStartIntent | null {
  const value = read(storage, intentKey);
  const accepted = object(value) && object(value.session) && text(value.session.session_id) &&
    object(value.incident) && text(value.incident.incident_id) &&
    value.incident.session_id === value.session.session_id;
  if (!object(value) || value.version !== 1 || !["radio", "lab"].includes(String(value.entryMode)) ||
      !object(value.source) || !text(value.label) ||
      !["id", "sessionRequestId", "incidentRequestId", "preparationRequestId"].every(key => text(value[key])) ||
      Object.keys(value.source).some(key => !sourceKeys.includes(key as typeof sourceKeys[number])) ||
      sourceKeys.filter(key => value.source && object(value.source) && value.source[key] != null).length !==
        (accepted && !Object.keys(value.source).length ? 0 : 1)) {
    return null;
  }
  return value as unknown as WorkflowStartIntent;
}
export function saveWorkflowStart(storage: WorkflowStorage, intent: WorkflowStartIntent): void {
  write(storage, intentKey, intent);
  if (intent.session && intent.incident) {
    const previous = readRetainedWorkflows(storage);
    write(storage, historyKey, [{sessionId: intent.session.session_id, incidentId: intent.incident.incident_id,
      label: intent.incident.location.matched_address},
    ...previous.filter(item => item.sessionId !== intent.session?.session_id)].slice(0, 20));
  }
}
export function clearWorkflowStart(storage: WorkflowStorage, id: string): void {
  if (readWorkflowStart(storage)?.id === id) storage.removeItem(intentKey);
}
export function readRetainedWorkflows(storage: WorkflowStorage): RetainedWorkflow[] {
  const value = read(storage, historyKey);
  return Array.isArray(value) ? value.filter((item): item is RetainedWorkflow => object(item) &&
    text(item.sessionId) && text(item.incidentId) && text(item.label)).slice(0, 20) : [];
}

/** The session acknowledgement is durable before the incident request is sent. */
export async function acceptWorkflowLocation(start: WorkflowStartIntent, request: WorkflowRequest,
  remember: (intent: WorkflowStartIntent) => void): Promise<WorkflowStartIntent & {
    session: SessionResponse; incident: IncidentResponse;
  }> {
  let intent = start;
  if (!intent.session) {
    const session = await request<SessionResponse>("/sessions",
      {request_id: intent.sessionRequestId, entry_mode: intent.entryMode});
    intent = {...intent, session}; remember(intent);
  }
  if (!intent.incident) {
    const incident = await request<IncidentResponse>("/incidents",
      {request_id: intent.incidentRequestId, session_id: intent.session!.session_id, ...intent.source});
    intent = {...intent, incident}; remember(intent);
  }
  return intent as WorkflowStartIntent & {session: SessionResponse; incident: IncidentResponse};
}

/** Default extent discovery is server-owned. No radius, corridor, wind or fire point is guessed here. */
export async function prepareWorkflowRoads(intent: WorkflowStartIntent & {
  session: SessionResponse; incident: IncidentResponse;
}, request: WorkflowRequest, remember: (value: WorkflowStartIntent) => void): Promise<DraftResponse> {
  const draft = await request<DraftResponse>("/scenario-drafts", {
    request_id: intent.preparationRequestId, session_id: intent.session.session_id,
    incident_id: intent.incident.incident_id, location_revision: intent.incident.location_revision,
    ...(intent.radiusKm === undefined ? {} : {radius_km:intent.radiusKm}),
  });
  remember({...intent, draftId: draft.draft_id});
  return draft;
}

/** A new attempt at an accepted incident never creates a replacement session or source location. */
export function roadPreparationIntent(session: SessionResponse, incident: IncidentResponse,
  uuid: () => string = () => crypto.randomUUID()): WorkflowStartIntent & {
    session: SessionResponse; incident: IncidentResponse;
  } {
  return {...createWorkflowStart(session.entry_mode, {}, incident.location.matched_address, uuid),
    session, incident};
}

function isReportedFire(value: unknown): value is ReportedFireReference {
  return object(value) && text(value.observation_id) && text(value.source_id) &&
    typeof value.latitude === "number" && Number.isFinite(value.latitude) && Math.abs(value.latitude) <= 90 &&
    typeof value.longitude === "number" && Number.isFinite(value.longitude) && Math.abs(value.longitude) <= 180 &&
    (value.name == null || typeof value.name === "string") &&
    (value.source_timestamp == null || typeof value.source_timestamp === "string");
}
export function readFireCatalog(storage: WorkflowStorage): ReportedFireReference[] {
  const value = read(storage, catalogKey);
  return Array.isArray(value) ? value.filter(isReportedFire).slice(0, 40) : [];
}
/** Equivalent saved provider content has one picker card even when another task minted a new receipt. */
export function reportedFireIdentity(value: ReportedFireReference): string {
  const timestamp = value.source_timestamp;
  const instant = timestamp == null ? null : Date.parse(timestamp);
  return JSON.stringify([value.source_id, value.latitude, value.longitude, value.name,
    instant != null && Number.isFinite(instant) ? instant : timestamp]);
}
export function retainFireCatalog(storage: WorkflowStorage,
  incoming: readonly ReportedFireReference[]): ReportedFireReference[] {
  const selected = new Map<string, ReportedFireReference>();
  for (const value of [...incoming, ...readFireCatalog(storage)]) {
    if (isReportedFire(value) && !selected.has(reportedFireIdentity(value))) {
      selected.set(reportedFireIdentity(value), value);
    }
  }
  const result = [...selected.values()].slice(0, 40);
  write(storage, catalogKey, result);
  return result;
}

/** The source picker outlives the active scenario. Point weather only comes from its current investigation. */
export function workflowObservations(current: readonly IncidentObservation[],
  catalog: readonly ReportedFireReference[]): IncidentObservation[] {
  const used = new Set<string>();
  const sameTime = (a: unknown, b: string | null | undefined): boolean => a == null ||
    a === b || typeof a === "string" && b != null && Number.isFinite(Date.parse(a)) &&
      Date.parse(a) === Date.parse(b);
  const result = current.flatMap(item => {
    const source = catalog.find(value => !used.has(value.observation_id) && item.fire?.sourceId === value.source_id &&
      item.latitude === value.latitude && item.longitude === value.longitude &&
      (value.name ?? null) === (item.fire?.name ?? null) &&
      sameTime(item.fire?.lastUpdated, value.source_timestamp));
    if (!source) return [item];
    used.add(source.observation_id);
    return [{...item, id: `fire:${source.observation_id}`, sourceObservationId: source.observation_id}];
  });
  for (const source of catalog) {
    if (used.has(source.observation_id)) continue;
    result.push({id: `fire:${source.observation_id}`, sourceObservationId: source.observation_id,
      latitude: source.latitude, longitude: source.longitude, name: source.name || "Reported fire",
      fire: {name: source.name, sourceId: source.source_id, lastUpdated: source.source_timestamp}});
  }
  return result;
}
