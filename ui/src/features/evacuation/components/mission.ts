import { UNIT_TYPES,type UnitType } from "@/shared/utils/typeGuards";
import type { CrewRecord,Policy,Scenario } from "../artifacts";
const count = (n: unknown): n is number => Number.isInteger(n) && Number(n) >= 0;
const mean = (values: number[]) : number | null => values.length ? values.reduce((a, b) => a + b, 0) / values.length : null;

export type ExitThreshold =
  | { readonly seconds: null; readonly status: "unavailable" | "not_reached" }
  | { readonly seconds: number; readonly status: "reached" };

export interface MissionMetrics {
  peopleExited: number; peopleUnresolved: number; peopleWaiting: number; peopleExposed: number;
  staged: number; notStaged: number; crewExposed: number; crewWaiting: number; returned: number;
  t95: number | null; t95Status: ExitThreshold["status"];
}

export interface CrewTiming {
  response: number | null; departureWait: number | null; travel: number | null;
}

export interface PairedCrewTiming extends CrewTiming {
  count: number; total: number; earlier: number; later: number; unchanged: number;
  differences: { id: string; response: number; departureWait: number; travel: number }[];
  current: CrewTiming; baseline: CrewTiming;
}

export function missionMetrics(policy: Policy, scenario: Scenario): MissionMetrics | null {
  const s = policy?.summary;
  const horizon = scenario.duration_minutes * 60;
  if (s?.valid_engine_run !== true || s.valid_accounting !== true || !Number.isFinite(horizon)
    || horizon <= 0 || s.simulated_seconds !== horizon
    || ![scenario.vehicles, scenario.people_per_vehicle, scenario.crews,
      s.exited_vehicles, s.active_vehicles, s.not_departed_vehicles, s.exposed_vehicles,
      s.crews_staged, s.crews_returned, s.exposed_crews, s.crews_not_departed].every(count)
    || scenario.vehicles <= 0 || scenario.people_per_vehicle <= 0
    || s.exited_vehicles + s.active_vehicles + s.not_departed_vehicles !== scenario.vehicles
    || (s.unresolved_vehicles !== undefined && s.unresolved_vehicles !== s.active_vehicles + s.not_departed_vehicles)
    || s.exposed_vehicles > scenario.vehicles
    || s.crews_returned > s.crews_staged || s.crews_staged + s.crews_not_departed > scenario.crews
    || [s.crews_staged, s.crews_returned, s.exposed_crews, s.crews_not_departed].some(n => n > scenario.crews)) return null;
  return {
    peopleExited: s.exited_vehicles * scenario.people_per_vehicle,
    peopleUnresolved: (s.active_vehicles + s.not_departed_vehicles) * scenario.people_per_vehicle,
    peopleWaiting: s.not_departed_vehicles * scenario.people_per_vehicle,
    peopleExposed: s.exposed_vehicles * scenario.people_per_vehicle,
    staged: s.crews_staged, notStaged: scenario.crews - s.crews_staged,
    crewExposed: s.exposed_crews, crewWaiting: s.crews_not_departed, returned: s.crews_returned,
    t95: exitThreshold(policy, scenario).seconds,
    t95Status: exitThreshold(policy, scenario).status,
  };
}

// Explicit null means the threshold was not met; absent or contradictory data is unavailable.
export function exitThreshold(policy: Policy, scenario: Scenario): ExitThreshold {
  const s = policy?.summary, horizon = scenario.duration_minutes * 60;
  const unavailable = { seconds: null, status: "unavailable" } as const;
  if (s?.valid_engine_run !== true || s.valid_accounting !== true || s.simulated_seconds !== horizon
    || !Number.isFinite(horizon) || horizon <= 0 || !count(scenario.vehicles) || scenario.vehicles <= 0
    || !count(s.exited_vehicles) || s.exited_vehicles > scenario.vehicles) return unavailable;
  const reached = s.exited_vehicles >= Math.ceil(scenario.vehicles * 0.95);
  if (s.t95_seconds === null && !reached) return { seconds: null, status: "not_reached" } as const;
  if (reached && Number.isFinite(s.t95_seconds) && typeof s.t95_seconds === "number" && s.t95_seconds >= 0 && s.t95_seconds <= horizon) {
    return { seconds: s.t95_seconds, status: "reached" };
  }
  return unavailable;
}

export function crewRecords(policy: Policy, horizon: number): CrewRecord[] | null {
  const frame = policy?.frames?.at(-1);
  if (frame?.t !== horizon || !Array.isArray(frame.crews)) return null;
  const ids = new Set();
  for (const c of frame.crews) {
    if (!c || typeof c.id !== "string" || !c.id || ids.has(c.id) || !Number.isFinite(c.scheduled_departure_s)
      || c.scheduled_departure_s < 0) return null;
    ids.add(c.id);
    const times = [c.departed_s, c.reached_staging_s, c.returned_s];
    if (times.some(t => t !== null && (!Number.isFinite(t) || t < 0 || t > horizon))
      || (c.departed_s != null && c.departed_s < c.scheduled_departure_s)
      || (c.reached_staging_s != null && (c.departed_s == null || c.reached_staging_s < c.departed_s))
      || (c.returned_s != null && (c.reached_staging_s == null || c.returned_s < c.reached_staging_s))) return null;
  }
  return frame.crews;
}

/** The demo role of the crew at `index` in `result.crew_plan`. A display label only: the simulated vehicles do not change. */
export const crewRole = (index: number): UnitType => UNIT_TYPES[index % UNIT_TYPES.length];

/**
 * Illustrative pump rate per demo role, in US gallons per minute; null marks a support role that does not pump.
 * 450 gpm is the low end of the standalone exercise's illustrative 450–500 GPM suppression flow
 * (architecture/09-firetruck-simulation-spec.md), so the two views show no conflicting rate. Never sent or saved.
 */
export const ILLUSTRATIVE_PUMP_GPM: Readonly<Record<UnitType, number | null>> = {
  ENGINE: 450, TANKER: 450, BRUSH: 450, RESCUE: null, COMMAND: null,
};

/** Illustrative water use for one crew at one recorded time. Display only. */
export type StopIllustration =
  | { readonly kind: "support" }
  | { readonly kind: "unavailable" }
  | { readonly kind: "not_reached"; readonly seconds: 0; readonly gallons: 0 }
  | { readonly kind: "stopped"; readonly seconds: number; readonly gallons: number; readonly gpm: number;
      readonly stopSeconds: number };

/**
 * Illustrative water use of one crew at recorded sample time `t`. It accrues only between consecutive recorded
 * frames, both at or before `t`, in which the earlier frame shows the crew making its staging stop, and it is capped
 * at the planned stop length. Reaching the staging segment never counts. A recording with frames out of order, or a
 * frame without this crew's state, is unavailable rather than zero. Never sent to agents or saved.
 */
export function stopIllustration(policy: Policy, crewId: string, role: UnitType, serviceSeconds: number | null | undefined,
  t: number): StopIllustration {
  const gpm = ILLUSTRATIVE_PUMP_GPM[role];
  if (gpm === null) return { kind: "support" };
  const frames = policy?.frames;
  const states = Array.isArray(frames) ? frames.map(frame => frame.crews?.find(crew => crew.id === crewId)?.state) : [];
  if (typeof serviceSeconds !== "number" || !Number.isFinite(serviceSeconds) || serviceSeconds <= 0 || !states.length
    || states.some(state => typeof state !== "string")
    || frames.some((frame, i) => !Number.isFinite(frame.t) || (i > 0 && !(frame.t > frames[i - 1].t)))) {
    return { kind: "unavailable" };
  }
  let reached = false, seconds = 0;
  for (const [i, frame] of frames.entries()) {
    if (frame.t > t) break;
    if (states[i] !== "staging_stop") continue;
    reached = true;
    const next = frames[i + 1];
    if (next && next.t <= t) seconds += next.t - frame.t;
  }
  if (!reached) return { kind: "not_reached", seconds: 0, gallons: 0 };
  const stopped = Math.min(seconds, serviceSeconds);
  return { kind: "stopped", seconds: stopped, gallons: Math.round(gpm * stopped / 60), gpm, stopSeconds: serviceSeconds };
}

// Compare the same crews so a faster average cannot hide crews that never arrived.
export function pairedCrewTiming(policy: Policy, baseline: Policy, scenario: Scenario): PairedCrewTiming | null {
  if (!missionMetrics(policy, scenario) || !missionMetrics(baseline, scenario)) return null;
  const horizon = scenario.duration_minutes * 60;
  const current = crewRecords(policy, horizon), reference = crewRecords(baseline, horizon);
  if (!current || !reference || current.length !== scenario.crews || reference.length !== scenario.crews) return null;
  const byId = new Map(reference.map(c => [c.id, c]));
  if (current.some(c => !byId.has(c.id) || c.scheduled_departure_s !== byId.get(c.id)?.scheduled_departure_s)
    || current.filter(c => c.reached_staging_s != null).length !== policy.summary.crews_staged
    || reference.filter(c => c.reached_staging_s != null).length !== baseline.summary.crews_staged) return null;
  const paired = current.filter(c => c.reached_staging_s != null && byId.get(c.id)?.reached_staging_s != null);
  const differences = paired.map(c => {
    const b = byId.get(c.id)!;
    return {
      id: c.id,
      response: (c.reached_staging_s! - c.scheduled_departure_s) - (b.reached_staging_s! - b.scheduled_departure_s),
      departureWait: (c.departed_s! - c.scheduled_departure_s) - (b.departed_s! - b.scheduled_departure_s),
      travel: (c.reached_staging_s! - c.departed_s!) - (b.reached_staging_s! - b.departed_s!),
    };
  });
  const averages = (records: CrewRecord[]): CrewTiming => ({
    response: mean(records.map(c => c.reached_staging_s! - c.scheduled_departure_s)),
    departureWait: mean(records.map(c => c.departed_s! - c.scheduled_departure_s)),
    travel: mean(records.map(c => c.reached_staging_s! - c.departed_s!)),
  });
  return {
    count: paired.length, total: scenario.crews, differences,
    current: averages(paired), baseline: averages(paired.map(c => byId.get(c.id)!)),
    response: mean(differences.map(c => c.response)),
    departureWait: mean(differences.map(c => c.departureWait)),
    travel: mean(differences.map(c => c.travel)),
    earlier: differences.filter(c => c.response < 0).length,
    later: differences.filter(c => c.response > 0).length,
    unchanged: differences.filter(c => c.response === 0).length,
  };
}
