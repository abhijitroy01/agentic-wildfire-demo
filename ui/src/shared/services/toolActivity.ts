import { labelForTool, SUPERVISOR_COMPLETION_TOOL, SUPERVISOR_SPECIALIST_TOOLS } from "./agentToolLabels";

/** Tool argument completion and tool execution results are separate AG-UI events. */
export interface ToolEvent {
  id: string;
  name: string;
  label: string;
  startedAt: number;
  endedAt?: number;
  status: "running" | "awaiting_result" | "result_received" | "done" | "error" | "unverified";
}

/** Only explicit result envelopes establish success; nested errors take precedence. */
export function toolResultStatus(content: unknown): "done" | "error" | "unverified" {
  const inspect = (value: unknown, depth: number): "done" | "error" | "unverified" => {
    if (depth > 8) return "unverified";
    if (typeof value === "string") {
      if (value.length > 65536) return "unverified";
      try { return inspect(JSON.parse(value), depth + 1); }
      catch { return "unverified"; }
    }
    if (Array.isArray(value)) {
      const statuses = value.map((item) => inspect(item, depth + 1));
      return statuses.includes("error") ? "error"
        : statuses.length > 0 && statuses.every((s) => s === "done") ? "done" : "unverified";
    }
    if (value === null || typeof value !== "object") return "unverified";
    const result = value as Record<string, unknown>;
    const status = typeof result.status === "string" ? result.status.toLowerCase() : "";
    if (result.isError === true || result.success === false || result.guidance_unavailable === true
      || ["error", "failed", "failure"].includes(status)
      || (result.error !== undefined && result.error !== null && result.error !== false && result.error !== "")) {
      return "error";
    }
    const children = ["content", "text", "result", "data"]
      .filter((key) => result[key] !== undefined)
      .map((key) => inspect(result[key], depth + 1));
    if (children.includes("error")) return "error";
    if (result.isError === false || result.success === true || ["ok", "success", "completed"].includes(status)) {
      return "done";
    }
    return children.length > 0 && children.every((s) => s === "done") ? "done" : "unverified";
  };
  return inspect(content, 0);
}

export function startToolEvent(
  events: ToolEvent[], id: string, name: string, label: string, now = Date.now(),
): ToolEvent[] {
  if (!id) return events;
  const existing = events.find((event) => event.id === id);
  if (existing) {
    return existing.name ? events : events.map((event) => event.id === id ? { ...event, name, label } : event);
  }
  return [...events, { id, name, label, startedAt: now, status: "running" }];
}

export function endToolArguments(events: ToolEvent[], id: string): ToolEvent[] {
  return events.map((event) => event.id === id && event.status === "running"
    ? { ...event, status: "awaiting_result" } : event);
}

export function receiveToolResult(
  events: ToolEvent[], id: string, content: unknown, now = Date.now(),
): ToolEvent[] {
  if (!id) return events;
  const existing = events.find((event) => event.id === id);
  // A late duplicate must not turn a recorded error into a successful action.
  const status = existing?.status === "error" ? "error" : toolResultStatus(content);
  if (!existing) return [...events, { id, name: "", label: "Tool response", startedAt: now, endedAt: now, status }];
  return events.map((event) => event.id === id ? { ...event, status, endedAt: now } : event);
}

/**
 * Fold one broker `simulation_activity` notice into the Simulation agent's activity. The broker forwards only a
 * tool's name, call ID and event type, never its arguments or result, so an arrived result reads "Response
 * received" and certifies nothing else. Any other or malformed event leaves `events` unchanged.
 */
export function receiveSimulationActivity(
  events: ToolEvent[], event: { readonly type?: unknown; readonly name?: unknown; readonly value?: unknown },
  now = Date.now(),
): ToolEvent[] {
  if (event.type !== "CUSTOM" || event.name !== "simulation_activity") return events;
  const value = event.value;
  if (!value || typeof value !== "object") return events;
  const { event: kind, tool_name: name, tool_call_id: id } = value as Record<string, unknown>;
  if (typeof id !== "string" || !id) return events;
  if (kind === "TOOL_CALL_START") {
    return typeof name === "string" && name ? startToolEvent(events, id, name, labelForTool(name), now) : events;
  }
  const existing = events.find(item => item.id === id);
  if (!existing) return events;
  if (kind === "TOOL_CALL_END") return existing.status === "running" ? endToolArguments(events, id) : events;
  if (kind !== "TOOL_CALL_RESULT" || existing.status === "error") return events;
  return events.map(item => item === existing ? { ...item, status: "result_received", endedAt: now } : item);
}

/** A call whose result has not arrived. */
const isOpen = (event: ToolEvent): boolean => event.status === "running" || event.status === "awaiting_result";

export function closeOpenEvents(
  events: ToolEvent[], status: "unverified" | "error", now = Date.now(),
): ToolEvent[] {
  return events.map((event) => isOpen(event) ? { ...event, status, endedAt: now } : event);
}

/** A supervisor run's own state: running until its terminal signal, or a saved task's status, ends it. */
export type SupervisorRunStatus = "running" | "completed" | "failed" | "cancelled" | "interrupted";
type EndedRunStatus = Exclude<SupervisorRunStatus, "running">;
export interface SupervisorRun {
  readonly status: SupervisorRunStatus;
  /** When RUN_STARTED arrived. Saved tasks do not record it. */
  readonly startedAt?: number;
  readonly endedAt?: number;
}
export type SynthesisStatus = "in_progress" | EndedRunStatus;
/** A row under the supervisor: a specialist call, a completion call it made, or the final synthesis. */
export type SupervisorStep =
  | { readonly kind: "delegation" | "completion_attempt"; readonly event: ToolEvent }
  | { readonly kind: "synthesis"; readonly status: SynthesisStatus; readonly label: string;
      readonly startedAt?: number; readonly endedAt?: number };
export interface SupervisorView { readonly startedAt?: number; readonly steps: readonly SupervisorStep[] }

const RUN_STATUSES: ReadonlySet<string> =
  new Set<SupervisorRunStatus>(["running", "completed", "failed", "cancelled", "interrupted"]);
const isRunStatus = (status: string): status is SupervisorRunStatus => RUN_STATUSES.has(status);
/** Results that reached the answer. An error result does too: code adds the failure, for example missing guidance. */
const REACHED_ANSWER: ReadonlySet<ToolEvent["status"]> =
  new Set<ToolEvent["status"]>(["done", "result_received", "error"]);

/** RUN_STARTED: the supervisor received the request. */
export function startSupervisorRun(now = Date.now()): SupervisorRun {
  return { status: "running", startedAt: now };
}

/** The run's terminal signal. The first one wins, and a failure before RUN_STARTED shows no supervisor. */
export function endSupervisorRun(
  run: SupervisorRun | undefined, outcome: "completed" | "failed", now = Date.now(),
): SupervisorRun | undefined {
  if (run && run.status !== "running") return run;
  if (!run && outcome === "failed") return undefined;
  return { ...run, status: outcome, endedAt: now };
}

/** A saved investigation task as run state. Saved activity shows that the supervisor ran, not when it started. */
export function savedSupervisorRun(task: {
  readonly status: string; readonly completed_at?: number | null; readonly activity: readonly unknown[];
} | null): SupervisorRun | undefined {
  if (!task || !task.activity.length || !isRunStatus(task.status)) return undefined;
  return task.status !== "running" && task.completed_at != null
    ? { status: task.status, endedAt: task.completed_at * 1000 } : { status: task.status };
}

/**
 * The supervisor's delegations and synthesis, derived only from its own tool events and run outcome.
 * Null without run evidence or when any event is not a supervisor tool; callers then show the flat list,
 * as for direct-to-specialist runs and older saved activity.
 */
export function supervisorActivity(
  events: readonly ToolEvent[], run: SupervisorRun | undefined,
): SupervisorView | null {
  const specialist = (event: ToolEvent): boolean => SUPERVISOR_SPECIALIST_TOOLS.has(event.name);
  if (!run || !events.every(event => specialist(event) || event.name === SUPERVISOR_COMPLETION_TOOL)) return null;
  const ended = run.status === "running" ? undefined : run.status;
  // A call still open when its run ended has no recorded outcome. When it stopped is unknown, so no end time is added.
  const steps = events.map((event): SupervisorStep => ({ kind: specialist(event) ? "delegation" : "completion_attempt",
    event: ended && isOpen(event) ? { ...event, status: "unverified" } : event }));
  const last = events.length ? events[events.length - 1] : undefined;
  // The completion call starts the synthesis, unless the completion guard rejected it and the run continues.
  const anchor = last?.name === SUPERVISOR_COMPLETION_TOOL && (ended || last.status !== "error") ? last : undefined;
  if (anchor || ended) {
    const status = ended ?? "in_progress";
    const synthesis: SupervisorStep = { kind: "synthesis", status, label: synthesisLabel(status, steps),
      startedAt: anchor?.startedAt, endedAt: ended ? run.endedAt : undefined };
    if (anchor) steps[steps.length - 1] = synthesis;
    else steps.push(synthesis);
  }
  return { startedAt: run.startedAt, steps };
}

/** N counts distinct specialists whose result reached the answer; an unverified call is not counted. */
function synthesisLabel(status: SynthesisStatus, steps: readonly SupervisorStep[]): string {
  if (status !== "in_progress" && status !== "completed") return "Supervisor: answer not completed";
  const delegated = steps.flatMap(step => step.kind === "delegation" ? [step.event] : []);
  const count = new Set(delegated.filter(event => REACHED_ANSWER.has(event.status)).map(event => event.name)).size;
  const specialists = `${count} specialist${count === 1 ? "" : "s"}`;
  if (status === "in_progress") {
    return !delegated.length ? "Supervisor: answering directly"
      : count ? `Supervisor: combining results from ${specialists}` : "Supervisor: preparing the answer";
  }
  return !delegated.length ? "Supervisor: answered directly"
    : count ? `Supervisor: combined results from ${specialists} into the answer` : "Supervisor: completed the answer";
}
