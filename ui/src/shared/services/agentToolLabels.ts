/**
 * Human-friendly labels for tool calls emitted by the supervisor and its
 * sub-agents. These power the live "what's happening" indicator in the chat
 * panel by translating raw AG-UI TOOL_CALL_START events into readable text.
 */

export const TOOL_LABELS: Record<string, string> = {
  simulation_agent_tool: "Simulation agent: investigating evacuation & crew access",
  prepare_simulation: "Preparing the scenario at the verified point",
  submit_simulation: "Submitting the reviewed scenario",
  get_simulation_status: "Checking simulation progress",
  get_simulation_evidence: "Reading retained simulation evidence",
  create_simulation_variant: "Preparing a controlled comparison",
  // Supervisor sub-agent dispatchers
  detection_agent_tool: "Detection agent: checking requested observations",
  public_response_agent_tool: "Guidance agent: finding published guidance",
  AnswerResponse: "Preparing the final response",
  PublicAnswerResponse: "Preparing the final response",
  communication_agent_tool: "Communication agent: preparing multilingual voice drafts",

  // Direct tools used inside the detection/communication agents
  fire_weather_tool: "Fetching fire & weather data",
  generate_voice_messages: "Generating multi-lingual voice messages",
};

/**
 * Specialists the investigation supervisor registers as its tools (`create_supervisor_agent` in
 * backend/runtime/agents/supervisor_agent.py), so a call to one of them is the supervisor delegating.
 * `simulation_agent_tool` is not registered there and is left out on purpose.
 */
export const SUPERVISOR_SPECIALIST_TOOLS: ReadonlySet<string> = new Set([
  "detection_agent_tool", "public_response_agent_tool", "communication_agent_tool",
]);
/** The supervisor's own structured-output tool, which Strands names after its answer model. */
export const SUPERVISOR_COMPLETION_TOOL = "PublicAnswerResponse";

/** Map an MCP-style tool name (e.g. `weather-dtn-service___get_dtn_forecast`) */
const MCP_PREFIX_LABELS: Array<{ prefix: string; label: string }> = [
  { prefix: "weather-dtn-service", label: "Querying DTN weather forecast" },
  { prefix: "wildfire-service", label: "Querying ArcGIS wildfire data" },
  { prefix: "geocode-service", label: "Resolving address with Amazon Location" },
];

/**
 * Resolve a tool name to a user-friendly status message.
 * Falls back to a prettified version of the raw name.
 */
export function labelForTool(toolName: string | undefined | null): string {
  if (!toolName) return "Working on it…";
  const direct = TOOL_LABELS[toolName];
  if (direct) return direct;

  for (const { prefix, label } of MCP_PREFIX_LABELS) {
    if (toolName.startsWith(prefix)) return label;
  }

  // Generic prettifier: "fire_weather_tool" → "Fire weather tool"
  const pretty = toolName
    .replace(/[_-]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
  return pretty.charAt(0).toUpperCase() + pretty.slice(1);
}
