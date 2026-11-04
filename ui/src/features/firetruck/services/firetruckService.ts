/**
 * Fire Truck Simulator API Service
 *
 * Starts, stops and reads the synthetic firetruck exercise through the workspace broker, and requests its reports.
 *
 * Features:
 * - Type guards for runtime validation at system boundaries
 * - Type-safe API responses
 */

import { BROKER_PATH,brokerJson } from "@/shared/services/workspaceAccess";
import { isNonEmptyString } from "@/shared/utils/typeGuards";

// ============================================================================
// TYPE DEFINITIONS
// ============================================================================

/** Fire location for simulation */
export interface FireLocation {
  latitude: number;
  longitude: number;
  name?: string;
}

export interface SimulationConfig {
  fire_location: FireLocation;
  initial_truck_count?: number;
  max_truck_count?: number;
}

export interface SimulationStartResponse {
  session_id: string;
  websocket_url: string;
  config: Record<string, unknown>;
  status?: string;
}

export interface SimulationStopResponse {
  session_id: string;
  final_stats: Record<string, unknown> | null;
}

export interface SimulationStatus {
  status?: string;
  phase?: string;
  incident_elapsed_seconds?: number;
  active: boolean;
  session_id: string;
  trucks?: unknown[];
  config?: Record<string, unknown>;
}

// ============================================================================
// TYPE GUARD VALIDATORS
// ============================================================================

/**
 * Validate start simulation response
 */
function isValidStartResponse(
  value: unknown
): value is { session_id: string } {
  if (!value || typeof value !== "object") return false;
  const resp = value as Record<string, unknown>;
  return isNonEmptyString(resp.session_id);
}

// ============================================================================
// API FUNCTIONS
// ============================================================================

/**
 * Start a new simulation for a fire incident
 */
export async function startSimulation(
  config: SimulationConfig,
  requestId: string = crypto.randomUUID(),
): Promise<SimulationStartResponse> {
  const result = await brokerJson("/firetruck/runs", {body: {...config, request_id: requestId}});
  if (!isValidStartResponse(result)) throw new Error("Invalid simulation response");
  return {session_id: result.session_id, config: {...config}, status: (result as {status?: string}).status,
    websocket_url: `${location.origin.replace(/^http/, "ws")}${BROKER_PATH}/firetruck/runs/${result.session_id}/stream`};
}

/**
 * Stop an active simulation
 */
export async function stopSimulation(
  sessionId: string
): Promise<SimulationStopResponse> {
  const result = await brokerJson(`/firetruck/runs/${encodeURIComponent(sessionId)}`, {method: "DELETE"});
  return {...(result as SimulationStopResponse), final_stats: null};
}

/**
 * Get current status of a simulation
 */
export async function getSimulationStatus(
  sessionId: string,
  signal?: AbortSignal,
): Promise<SimulationStatus> {
  const result = await brokerJson(`/firetruck/runs/${encodeURIComponent(sessionId)}`, {signal}) as SimulationStatus;
  if (!result || result.session_id !== sessionId || typeof result.status !== "string") {
    throw new Error("The simulation returned an unusable status.");
  }
  return {...result, active: result.status === "running" || result.status === "starting"};
}

/**
 * Health check for the simulator API
 */
export async function checkHealth(signal?: AbortSignal): Promise<boolean> {
  try { await brokerJson("/firetruck/health", {signal}); return true; } catch { return false; }
}

export interface FiretruckReport { report_id: string; status: string }
export async function generateFiretruckReport(runId: string, question: string,
  requestId: string = crypto.randomUUID()): Promise<FiretruckReport> {
  return await brokerJson(`/firetruck/runs/${encodeURIComponent(runId)}/reports`, {
    body: {request_id: requestId, question},
  }) as FiretruckReport;
}
export async function getFiretruckReport(reportId: string, signal?: AbortSignal): Promise<FiretruckReport> {
  const result = await brokerJson(`/firetruck/reports/${encodeURIComponent(reportId)}`, {signal}) as FiretruckReport;
  if (!result || result.report_id !== reportId || !["running", "completed", "failed", "interrupted"].includes(result.status)) {
    throw new Error("The simulation report returned an unusable status.");
  }
  return result;
}

export interface SimulationRequestReceipt { request_id: string; session_id: string; status: string }
export async function getSimulationRequest(requestId: string, signal?: AbortSignal): Promise<SimulationRequestReceipt> {
  const result = await brokerJson(`/firetruck/requests/${encodeURIComponent(requestId)}`, {signal}) as SimulationRequestReceipt;
  if (!result || result.request_id !== requestId || !isNonEmptyString(result.session_id)
    || typeof result.status !== "string") throw new Error("The simulation returned an unusable request receipt.");
  return result;
}

export async function getFiretruckReportRequest(runId: string, requestId: string,
  signal?: AbortSignal): Promise<FiretruckReport> {
  const result = await brokerJson(`/firetruck/runs/${encodeURIComponent(runId)}/report-requests/${encodeURIComponent(requestId)}`,
    {signal}) as FiretruckReport;
  if (!result || !isNonEmptyString(result.report_id) ||
    !["running", "completed", "failed", "interrupted"].includes(result.status)) {
    throw new Error("The simulation report returned an unusable request receipt.");
  }
  return result;
}
