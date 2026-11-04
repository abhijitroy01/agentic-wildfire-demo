import { brokerJson } from "@/shared/services/workspaceAccess";
import type { AssessmentResult, FireRiskJobReference, FireRiskJobResponse } from "../generated/contracts";
import { FireRiskReceiptError, isAssessmentResult, isFireRiskJobResponse, uuid } from "./assessment";

/** Read an owned saved assessment. This route performs no model execution or weather enrichment. */
export async function readFireExposure(assessmentId: string, signal?: AbortSignal): Promise<AssessmentResult> {
  if (!uuid(assessmentId)) {
    throw new Error("Invalid saved fire exposure reference.");
  }
  const result = await brokerJson(`/fire-exposure/assessments/${encodeURIComponent(assessmentId)}`, {
    method: "GET", signal, timeoutMs: 15_000, timeoutMessage: "Saved fire exposure did not answer in time.",
  });
  if (!isAssessmentResult(result) || result.assessment_id !== assessmentId) {
    throw new Error("Saved fire exposure returned an unusable result.");
  }
  return result;
}

/** Read a durable job. GET neither starts nor retries its model/provider work. */
export async function readFireRiskJob(reference: FireRiskJobReference, signal?: AbortSignal): Promise<FireRiskJobResponse> {
  if (!uuid(reference.job_id)) throw new Error("Invalid saved fire assessment job.");
  const result = await brokerJson(`/fire-exposure/jobs/${encodeURIComponent(reference.job_id)}`, {
    method: "GET", signal, timeoutMs: 15_000, timeoutMessage: "Saved fire assessment status did not answer in time.",
  });
  if (!isFireRiskJobResponse(result, reference)) {
    throw new FireRiskReceiptError("Saved fire assessment returned an unusable job receipt.");
  }
  return result;
}

/** Explicit user cancellation affects the saved job; selecting a fire or answer never calls this route. */
export async function cancelFireRiskJob(reference: FireRiskJobReference, signal?: AbortSignal): Promise<FireRiskJobResponse> {
  if (!uuid(reference.job_id)) throw new Error("Invalid saved fire assessment job.");
  const result = await brokerJson(`/fire-exposure/jobs/${encodeURIComponent(reference.job_id)}/cancel`, {
    method: "POST", body: {}, signal, timeoutMs: 15_000,
    timeoutMessage: "Fire assessment cancellation did not answer in time; its saved status remains available.",
  });
  if (!isFireRiskJobResponse(result, reference)) {
    throw new FireRiskReceiptError("Fire assessment cancellation returned an unusable job receipt.");
  }
  return result;
}

/** Explicit recovery creates a linked attempt with frozen sources; a lost POST response reuses requestId. */
export async function retryFireRiskJob(reference: FireRiskJobReference, requestId: string,
  signal?: AbortSignal): Promise<FireRiskJobResponse> {
  if (!uuid(reference.job_id) || !uuid(requestId)) throw new Error("Invalid terrain retry request.");
  const result = await brokerJson(`/fire-exposure/jobs/${encodeURIComponent(reference.job_id)}/retry`, {
    method: "POST", body: {request_id: requestId}, signal, timeoutMs: 15_000,
    timeoutMessage: "Terrain retry did not answer; retrying this request will rejoin its saved attempt.",
  });
  const value = result as Partial<FireRiskJobResponse> | null;
  if (!value || !uuid(value.job_id) || !Array.isArray(value.fire_refs)
    || !value.fire_refs.length || !value.fire_refs.every(ref => reference.fire_refs.includes(ref))
    || !isFireRiskJobResponse(value, {job_id: value.job_id, fire_refs: value.fire_refs})) {
    throw new FireRiskReceiptError("Terrain retry returned an unusable receipt.");
  }
  return value;
}
