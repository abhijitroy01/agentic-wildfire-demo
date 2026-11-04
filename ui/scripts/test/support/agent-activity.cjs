/* Agent activity helpers shared by the agent-activity and evacuation-components node tests. */
const { load } = require("../../ts-module.cjs");

/** The Agent activity reducers and component, freshly loaded, with helpers to build events and render them. */
function loadActivity() {
  const { createElement } = require("react");
  const { renderToStaticMarkup } = require("react-dom/server");
  const { labelForTool } = load("shared/services/agentToolLabels.ts");
  const AgentActivity = load("shared/components/AgentActivity.tsx").default;
  return { ...load("shared/services/toolActivity.ts"), labelForTool,
    /** A recorded tool call, labelled as the stream reducers label it. */
    call: (id, name, status, startedAt, endedAt) => ({ id, name, label: labelForTool(name), startedAt,
      ...(endedAt === undefined ? {} : { endedAt }), status }),
    render: props => renderToStaticMarkup(createElement(AgentActivity, props)) };
}
/** Each rendered activity row as [data-kind, data-status, main text, status text, detail text]. */
const rowsOf = html => [...html.matchAll(new RegExp('<li(?: data-kind="([a-z_]+)")?(?: data-status="([a-z_]+)")?>'
  + "<div><strong>([^<]*)</strong>(?:<span>([^<]*)</span>)?</div>(?:<small>([^<]*)</small>)?", "g"))]
  .map(match => match.slice(1));
const clock = time => new Date(time).toLocaleTimeString();
const DIRECT_TITLE = "Agent activity · sent directly to the Simulation agent";
/** A broker notice about one Simulation agent tool call, as `agent_broker.py` streams it. */
const notice = (event, extra = {}) => ({ type: "CUSTOM", name: "simulation_activity",
  value: { event, tool_name: "get_simulation_evidence", tool_call_id: "r1", ...extra } });

module.exports = { DIRECT_TITLE, clock, loadActivity, notice, rowsOf };
