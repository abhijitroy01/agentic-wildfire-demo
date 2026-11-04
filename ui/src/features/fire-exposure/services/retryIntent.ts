import { tabStorage } from "@/shared/services/tabStorage";
import type { FireRiskJobReference } from "../generated/contracts";
import { uuid } from "./assessment";

export interface TerrainRetryIntent {
  requestId: string;
  reference?: FireRiskJobReference;
}

const key = (jobId: string): string => `fireguard_terrain_retry_${jobId}`;

/** The parent receipt is owner checked by the broker; storage never grants access to another visitor's job. */
export function savedRetryIntent(parent: FireRiskJobReference): TerrainRetryIntent | null {
  try {
    const value = JSON.parse(tabStorage.getItem(key(parent.job_id)) ?? "null");
    if (!value || !uuid(value.requestId)) return null;
    const reference = value.reference;
    if (reference !== undefined && (!reference || !uuid(reference.job_id)
      || !Array.isArray(reference.fire_refs) || !reference.fire_refs.length
      || new Set(reference.fire_refs).size !== reference.fire_refs.length
      || !reference.fire_refs.every((ref: unknown) => typeof ref === "string" && parent.fire_refs.includes(ref)))) {
      return null;
    }
    return {requestId: value.requestId, ...(reference ? {reference} : {})};
  } catch {
    return null;
  }
}

/** Persist before submission and after acknowledgement so reloads retain the same owned attempt. */
export function saveRetryIntent(parent: FireRiskJobReference, value: TerrainRetryIntent): void {
  tabStorage.setItem(key(parent.job_id), JSON.stringify(value));
}

/** Restoring an answer follows saved receipt references using GET; unresolved POSTs require an explicit retry. */
export function retainedRetryReferences(originals: readonly FireRiskJobReference[]): FireRiskJobReference[] {
  const seen = new Set(originals.map(item => item.job_id));
  const references = [...originals];
  const retained: FireRiskJobReference[] = [];
  for (let index = 0; index < references.length && index < 64; index++) {
    const next = savedRetryIntent(references[index])?.reference;
    if (next && !seen.has(next.job_id)) {
      seen.add(next.job_id);
      references.push(next);
      retained.push(next);
    }
  }
  return retained;
}
