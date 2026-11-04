// Crew replay values shared by the Evacuation workspace and the Traffic Lab. Dependency-free for Node tests.
import type { Frame,Policy,Result } from "../artifacts";
import { clock } from "./replay";
import { speedMph } from "./units";

/** Crews returned to a gate at a replay frame, counted from the frame's recorded crew phases.
 * Older recordings save no per-frame phases, only the final total, so before the run's end that stays unavailable. */
export function crewsReturned(frame: Frame | null | undefined, policy: Policy | undefined,
  durationSeconds: number): number | undefined {
  if (!frame) return undefined;
  if (frame.crews) return frame.crews.filter(c => c.state === "returned").length;
  return frame.t >= durationSeconds ? policy?.summary.crews_returned : undefined;
}

/** Recorded speeds below this (m/s, about 1.1 mph) read as stopped or very slow, like the map's slow halos. */
export const STOPPED_M_S = 0.5;

/** Display text for each recorded crew phase. */
export const crewPhaseLabels: Readonly<Record<string, string>> = {
  no_route: "No initial route", origin_closed: "Held at closed origin", waiting_to_depart: "Waiting to depart",
  to_staging: "Travelling toward staging", staging_stop: "Making the assigned staging stop",
  on_staging_segment: "On the staging segment", returning: "Travelling toward an exit gate", returned: "Returned to a gate",
  on_road_unknown: "On road; phase not recorded", not_visible: "Not visible in this frame",
};

/** One assigned crew: its saved plan entry, or its demand entry in recordings without a crew plan. */
export interface CrewEntry { id: string; scheduled_departure_s: number; origin?: string; routable?: boolean }

/** The crews a recording assigned, in display order (Crew 1 first). */
export function crewPlan(result: Pick<Result, "crew_plan" | "demand">): CrewEntry[] {
  return result.crew_plan || result.demand.filter(d => d.id.startsWith("crew-"))
    .map(d => ({ ...d, scheduled_departure_s: d.depart }));
}

/** What the crew table row and the Locate map popup both say about one crew at a replay frame. */
export interface CrewDetails {
  id: string; number: number; name: string; state: string; phase: string;
  /** Recorded speed text, or null when the crew has no position in this frame. */
  speed: string | null;
  scheduled: string; entry: string; staging: string; returned: string | null;
  onRoad: boolean; plannedOrigin: boolean;
}

/** Describe a recorded crew speed in m/s; near-zero speeds read "stopped or very slow". */
export const crewSpeed = (metersPerSecond: number): string =>
  Number.isFinite(metersPerSecond) && metersPerSecond >= 0 && metersPerSecond < STOPPED_M_S
    ? "stopped or very slow" : speedMph(metersPerSecond);

/** Describe one crew at a replay frame: number, phase, speed, road entry and staging status, or null if unassigned. */
export function crewDetails(result: Pick<Result, "crew_plan" | "demand">, frame: Frame, id: string): CrewDetails | null {
  const plan = crewPlan(result);
  const index = plan.findIndex(c => c.id === id);
  if (index < 0) return null;
  const crew = plan[index];
  const vehicle = frame.vehicles.find(v => v[0] === id);
  const recorded = frame.crews?.find(s => s.id === id);
  const state = recorded?.state || (crew.routable === false ? "no_route" : vehicle ? "on_road_unknown" : "not_visible");
  return {
    id, number: index + 1, name: `Crew ${index + 1}`, state, phase: crewPhaseLabels[state] ?? "Phase unavailable",
    speed: vehicle ? crewSpeed(vehicle[3]) : null,
    scheduled: `${clock(crew.scheduled_departure_s)} scheduled`,
    entry: !recorded ? "Actual entry time not saved"
      : recorded.departed_s == null ? "Has not entered roads" : `Entered at ${clock(recorded.departed_s)}`,
    staging: recorded?.reached_staging_s != null ? `Reached segment ${clock(recorded.reached_staging_s)}`
      : recorded ? "Staging not reached" : "Individual timing not saved",
    returned: recorded?.returned_s != null ? `Returned ${clock(recorded.returned_s)}` : null,
    onRoad: !!vehicle,
    plannedOrigin: !vehicle && !!result.crew_plan && !!crew.origin
      && ["waiting_to_depart", "origin_closed", "no_route"].includes(state),
  };
}

/** The Locate popup's labelled lines, from the same details as the crew's table row. */
export function crewDetailLines(details: CrewDetails): [label: string, value: string][] {
  return [
    ["Phase", details.phase],
    ...(details.speed ? [["Recorded speed", details.speed] as [string, string]] : []),
    ["Road entry", `${details.scheduled} · ${details.entry}`],
    ["Staging", details.returned ? `${details.staging} · ${details.returned}` : details.staging],
  ];
}
