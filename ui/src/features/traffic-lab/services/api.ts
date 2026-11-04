import type { RunStatus } from "@/features/evacuation/domain";
import type { Discovery, Scenario } from "@/features/evacuation/artifacts";
import { api,type ApiOptions } from "@/features/evacuation/services/api";

export { initialize } from "@/shared/services/workspaceAccess";

/** Server facts the Traffic Lab needs before offering provider-speed runs. */
export type ResearchConfig = { traffic_key_configured: boolean; deployment: string; purpose: string };

/** A Traffic Lab run from GET /research/jobs[/{id}]: the job record without owner and lease fields. */
export type ResearchJob = {
  readonly id: string;
  readonly status: RunStatus;
  readonly phase: string;
  /** Fraction complete, 0–1. */
  readonly progress: number;
  /** Epoch seconds. */
  readonly created: number;
  readonly updated?: number;
  /** The run's scenario; the server validates and always stores `highway`. */
  readonly scenario: { readonly highway: string } & Record<string, unknown>;
  readonly cancel?: boolean;
  readonly error?: string | null;
  readonly summaries?: unknown;
};

export type DiscoveryArea = Pick<Scenario, "lat" | "lon" | "radius_km">;

/** Road preparation stores an area, without the run's selected highway or vehicle assumptions. */
export type ResearchDiscoveryJob = Omit<ResearchJob, "scenario"> & {
  readonly kind: "road_discovery";
  readonly scenario: DiscoveryArea;
  readonly result?: Discovery;
};

/** Bind each receipt to the submitted area and existing job, before it can replace the road map. */
export function discoveryJob(value: unknown, area: DiscoveryArea, id?: string): ResearchDiscoveryJob {
  if (!value || typeof value !== "object") throw new Error("Road discovery returned an unusable receipt.");
  const job = value as Partial<ResearchDiscoveryJob>;
  if (typeof job.id !== "string" || !/^[a-f0-9-]{36}$/i.test(job.id) || (id && job.id !== id)
      || job.kind !== "road_discovery" || !job.scenario
      || !["queued", "running", "cancelling", "completed", "failed", "cancelled"].includes(String(job.status))
      || typeof job.phase !== "string" || typeof job.progress !== "number"
      || !Number.isFinite(job.progress) || job.progress < 0 || job.progress > 1
      || !["lat", "lon", "radius_km"].every(key => job.scenario?.[key as keyof DiscoveryArea]
        === area[key as keyof DiscoveryArea])) throw new Error("Road discovery returned an unusable receipt.");
  if (job.status === "completed" && (!job.result || job.result.geometry?.type !== "FeatureCollection"
      || !Array.isArray(job.result.geometry.features) || !Array.isArray(job.result.highways)
      || (job.result.area && !["lat", "lon", "radius_km"].every(key => job.result?.area?.[key as keyof DiscoveryArea]
        === area[key as keyof DiscoveryArea])))) throw new Error("Completed road discovery has no usable saved result.");
  return job as ResearchDiscoveryJob;
}

/** Call a Traffic Lab route (/simulation-api/v1/research/...): GET without a body, POST with one, bounded and
 * validated like every workspace call. */
export function research<T>(path: string, body?: unknown, options: ApiOptions = {}): Promise<T> {
  return api<T>(`/research${path}`, body, options);
}
