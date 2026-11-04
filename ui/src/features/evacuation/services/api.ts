import { readFinalAgentSnapshot, type AgUiEvent } from "@/shared/services/AgentEventStream";
import { anySignal,brokerFetch,brokerJson,type BrokerRequest } from "@/shared/services/workspaceAccess";
import { validateArtifact } from "../artifacts";
import type { SimulationOutput,TurnRequest } from "../generated/contracts";
export { initialize } from "@/shared/services/workspaceAccess";

/** Shortest reported address the incident API accepts; mirrors `IncidentRequest.reported_address` `min_length`. */
export const REPORTED_ADDRESS_MIN_LENGTH = 5;
/** A search place deliberately accepts locality names rather than requiring a street address. */
export const REPORTED_PLACE_MIN_LENGTH = 3;

/** Per-call controls for api(): a method the body does not imply (for example PATCH), a signal and a timeout. */
export type ApiOptions = Omit<BrokerRequest, "body">;

/** Call the workspace v1 API with the shared visitor transport, bounds and error contract; validate saved artifacts. */
export async function api<T>(path: string, body?: unknown, options: ApiOptions = {}): Promise<T> {
  const value = await brokerJson(path, { ...options, body });
  validateArtifact(path, value);
  return value as T;
}

/** Read a completed, typed simulation answer; partial snapshots never imply success. `onCustom` receives each
 * broker CUSTOM notice (tool names and call IDs only) before its activity text. */
export async function turn(body: TurnRequest, activity: (message: string) => void,
  options: { signal?: AbortSignal; onCustom?: (event: AgUiEvent) => void } = {}): Promise<SimulationOutput> {
  const timeout = AbortSignal.timeout(210_000);
  const signal = options.signal ? anySignal([options.signal, timeout]) : timeout;
  const response = await brokerFetch("/agent/turns", {
    method: "POST", body: JSON.stringify(body), signal,
  });
  if (!response.body) throw new Error("Agent response stream was unavailable.");
  const snapshot = await readFinalAgentSnapshot(response.body, {
    onUnknown: event => {
      if (event.type !== "CUSTOM") return;
      options.onCustom?.(event);
      const value = event.value;
      const name = value && typeof value === "object" && "tool_name" in value ? value.tool_name : undefined;
      activity(typeof name === "string" ? name : "Simulator evidence tool");
    },
  }, { signal });
  const envelope = snapshot as Record<string, unknown>;
  const content = envelope.content;
  const candidate = content && typeof content === "object" && "simulationData" in content
    ? content.simulationData : snapshot;
  if (!candidate || typeof candidate !== "object") throw new Error("Invalid simulation answer.");
  const value = candidate as Record<string, unknown>;
  if (typeof value.answer !== "string" || typeof value.question !== "string"
      || value.session_id !== body.session_id || value.incident_id !== body.incident_id
      || typeof value.turn_id !== "string" || value.location_revision !== body.location_revision
      || !["submitted", "answer", "needs_input", "unsupported", "error"].includes(String(value.status))) {
    throw new Error("Invalid simulation answer.");
  }
  return candidate as SimulationOutput;
}
