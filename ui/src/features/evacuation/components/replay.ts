// Replay helpers shared by the Evacuation workspace and the Traffic Lab. Dependency-free for Node tests.
import type { RunResponse } from "../generated/contracts";

/** Run lifecycle states. Workspace runs and Traffic Lab research jobs share one store and status set. */
export type RunStatus = RunResponse["status"];

/** Statuses that can still change, so the UI keeps polling them and offers cancellation. */
export const ACTIVE_STATUSES: readonly RunStatus[] = ["queued", "running", "cancelling"];

/** True while a run is queued, running or cancelling. */
export const isActiveStatus = (status: string | null | undefined): boolean =>
  !!status && (ACTIVE_STATUSES as readonly string[]).includes(status);

/** Format simulated seconds as m:ss, truncating fractional seconds. */
export const clock = (seconds: number): string =>
  `${Math.floor(seconds / 60)}:${String(Math.floor(seconds % 60)).padStart(2, "0")}`;

/** Swatch and line colors for the baseline, coordinated and phased policies, in result order. */
export const POLICY_COLORS: readonly string[] = ["#3779ba", "#14836e", "#c47d23"];

/** The first sample with the highest slow/stopped count: when a policy's queue peaked. */
export const peakSample = <S extends { readonly queue: number }>(series: readonly S[]): S =>
  series.reduce((highest, sample) => sample.queue > highest.queue ? sample : highest);
