import {
  supervisorActivity, type SupervisorRun, type SupervisorStep, type SynthesisStatus, type ToolEvent,
} from "@/shared/services/toolActivity";

/** A finished call reads "Response received" live and saved: it returned data, which certifies nothing else. */
const labels: Record<ToolEvent["status"], string> = {
  running: "Calling tool", awaiting_result: "Waiting for result",
  result_received: "Response received", done: "Response received", error: "Failed", unverified: "Completion status unavailable",
};
/** Synthesis rows reuse the row styles: in progress as running, completed as done and failed as error. */
const synthesisStates: Record<SynthesisStatus, { status: string; label: string }> = {
  in_progress: { status: "running", label: "In progress" }, completed: { status: "done", label: "Completed" },
  failed: { status: "error", label: "Failed" }, cancelled: { status: "cancelled", label: "Cancelled" },
  interrupted: { status: "interrupted", label: "Interrupted" },
};

const clock = (time: number): string => new Date(time).toLocaleTimeString();
/** When an event arrived, plus its duration once it has ended. */
const timing = (startedAt: number, endedAt?: number): string =>
  endedAt ? `${clock(startedAt)} · ${Math.max(0, (endedAt-startedAt)/1000).toFixed(1)}s` : clock(startedAt);

/** Observable actions and results, never inferred reasoning or animated fake steps. */
export default function AgentActivity({ events, run, title = "Agent activity" }: {
  events: ToolEvent[]; run?: SupervisorRun; title?: string;
}): React.ReactElement {
  // Only a supervisor run's own tool events become its hierarchy; anything else keeps the flat list.
  const supervisor = supervisorActivity(events, run);
  return <section className="agent-activity" aria-label={title}>
    <h3>{title}</h3>
    {supervisor ? <ol><li data-kind="supervisor">
      <div><strong>Supervisor: received the request</strong></div>
      {supervisor.startedAt !== undefined && <small>{clock(supervisor.startedAt)}</small>}
      {supervisor.steps.length > 0 &&
        <ol aria-label="Supervisor delegation and synthesis">{supervisor.steps.map(supervisorStep)}</ol>}
    </li></ol> : !events.length ? <p>Actions appear here when the agent starts using tools.</p> :
      <ol>{events.map(event => <li key={event.id} data-status={event.status}>
        <div><strong>{event.label}</strong><span>{labels[event.status]}</span></div>
        <small>{timing(event.startedAt, event.endedAt)}</small>
      </li>)}</ol>}
  </section>;
}

function supervisorStep(step: SupervisorStep): React.ReactElement {
  switch (step.kind) {
    case "delegation":
    case "completion_attempt": {
      const { event } = step, by = step.kind === "delegation" ? "Delegated by Supervisor" : "Supervisor";
      return <li key={`call:${event.id}`} data-kind={step.kind} data-status={event.status}>
        <div><strong>{event.label}</strong><span>{labels[event.status]}</span></div>
        <small>{`${by} · ${timing(event.startedAt, event.endedAt)}`}</small>
      </li>;
    }
    case "synthesis": {
      const state = synthesisStates[step.status];
      // Timed from the supervisor's completion call; without one, only the run's end time is known.
      const time = step.startedAt !== undefined ? timing(step.startedAt, step.endedAt)
        : step.endedAt !== undefined ? clock(step.endedAt) : undefined;
      return <li key="synthesis" data-kind="synthesis" data-status={state.status}>
        <div><strong>{step.label}</strong><span>{state.label}</span></div>
        {time && <small>{time}</small>}
      </li>;
    }
    default: {
      const unexpected: never = step;
      return unexpected;
    }
  }
}
