# Agent runtime, tools and providers

## Responsibility and boundaries

`backend/runtime/wfm_agui_entrypoint.py` is the IAM-protected AG-UI entry point. It validates signed broker context and selects one bounded use case. Deployed investigation always constructs the public supervisor; an unsigned `workspace_mode` does not enable private tools. Simulation selects its specialist directly, and firetruck reports use a separate specialist without tools.

| Module | Responsibility |
|---|---|
| `agents/supervisor_agent.py` | Select and compose detection, cited guidance and explicit communication tasks |
| `agents/detection_agent/` | Explicit geocode/weather tool allowlist and coordinate grounding |
| `agents/communication_agent/` | Source-bound spoken draft; owner-scoped parallel Translate/Polly/S3 artifacts, without sending alerts |
| `agents/response_agent/tools/knowledge_base_tool.py` | Public collection retrieval, metadata validation and official citations |
| `agents/simulation_agent/` | Five generated simulator tool contracts, hidden trusted context, tool-call budget and deadline |
| `agents/firetruck_report_agent.py` | Structured report from owned, signed saved suppression evidence |
| `utils/structured_response.py` | Shared model-input projection, exact fact rendering and terminal response validation |

The Runtime owns reasoning; the workspace service owns job admission, authorization and evidence. A chat disconnect does not cancel or resubmit a durable simulation. Tool results carry authoritative receipts. A model's prose cannot create a job or authorize submission.

## Gateway and provider adapters

The Runtime obtains a Cognito client-credentials token with the secret it reads by ARN. AgentCore Gateway uses a `CUSTOM_JWT` authorizer that checks the app client ID, and its role invokes the Lambda targets declared in `backend/deployment/templates/nested/4-agentcore-resources.yaml`. The inline tool schemas in that template are the Gateway tool contract.

| Target | Implementation | Boundary |
|---|---|---|
| `geocode-service` | `backend/gateway/geocode/` | Amazon Location IAM forward geocoding |
| `wildfire-service` | `backend/gateway/wildfire/` | Fixed public ArcGIS wildfire feed: a WGS84 point and radius, fixed endpoint, SQL predicate and coordinate system, bounded results with truncation metadata |
| `weather-dtn-service` | `backend/gateway/weather/lambda_weather_dtn.py` | Fixed DTN weather operations, finite numeric measurements only; plain JSON with stable error codes, like the other targets |
| `simulation-service` | `backend/gateway/simulation/` | Forward the generated operation payload and hidden signed context to the workspace API |

Public guidance retrieval uses IAM `bedrock:Retrieve` directly from a Strands tool. It has no Gateway target and never falls back to a private knowledge base.

`scripts/export_integration_contracts.py` generates the simulator contracts and the model-visible tool inputs. The Gateway transport is a fixed `payload`/`trusted_context` envelope. The simulation adapter verifies the operation's shape; the workspace API verifies the signed owner, session, incident, revision, turn and attempt scope. The adapter holds no signing key and implements no business rule. It rejects arbitrary URLs and redirects and returns `SIMULATOR_UNAVAILABLE` for replies outside the v1 error contract. A new provider adapter needs a fixed destination, bounded responses and timeouts, sanitized errors and explicit tool registration. The model never supplies a URL, token or query.

### Geocoding and weather

Amazon Location replaces Esri for forward geocoding (D44). One Lambda calls `geo-places.Geocode` with `geo-places:Geocode` on the Region's `provider/default`. The agent calls it as `geocode-service___geocode_address({"address": "..."})` through Gateway, and the workspace API invokes it with its task role to resolve presets and radio addresses; the worker, radio and firetruck task roles cannot invoke it. The browser receives no AWS credential or geocoding key, and there is no Lambda URL.

- Every call uses `IntendedUse=Storage`, USA filtering and at most six candidates, so the returned location may be kept as scenario evidence.
- Amazon Location returns `[longitude, latitude]`; receipts use named WGS84 `lat` and `lon` and name `Amazon Location`, `amazon-location-v1`, the matched address, place type, timestamp and the score's meaning.
- `MatchScores.Overall` is scaled from 0–1 to 0–100. It is not a probability or an Esri-equivalent score. Automatic acceptance requires at least 90, California, supported coordinates and a suitable place type: `PointAddress`, `InterpolatedAddress` and `Intersection` for radio; presets also accept `Locality`, `District` and `PointOfInterest`. Candidates more than 250 m apart are rejected as ambiguous.
- Saved Esri receipts stay readable with their original provenance. A historical fire name selects a place; road, traffic and weather use current data.
- Missing configuration, IAM denial, provider errors and malformed responses are failures, never invented coordinates. An unavailable wildfire feed fails the tool instead of reporting zero fires. A resolved address is not proof of an active fire or a historical ignition.

`CoordinateGrounding` authorizes a dependent provider request only after a successful Amazon Location result, an explicit coordinate in the specialist request, or a returned fire location. It copies coordinates without rounding and keeps its state per invocation; a failed or low-score geocode never authorizes a point or creates shared location state.

The DTN adapter canonicalizes `us` to `us-std`, validates coordinates and forecast bounds, and rejects other units before a provider call. Its output uses Fahrenheit, mph and inches. Zero precipitation stays zero and missing precipitation stays unavailable; a provider failure is distinct from an empty successful response.

The Palisades preset uses a 6 km (about 3.7 mi) road radius to fit the road-data and edge limits (`simulation.overpass_response_max_mb`, `simulation.max_network_edges`). It is a bounded planning area, not the 2025 fire perimeter.

## Investigation topic routing

Before task selection, the Runtime reads up to 40 native messages through the existing AgentCore session manager, using the verified actor and session that execution will use. The supervisor selects a typed `InvestigationPlan` from the latest question, bounded prior user requests and a source-location inventory. Selection has no specialist tools and permits an empty scope for clarification. Prior requests remain historical messages, outside the latest-user guardrail input. Execution registers only selected specialists; detection further registers only selected providers. Its grounding hook blocks unrequested providers and forces fire-only weather enrichment off. Prompt instructions alone are not the task boundary.

Fire-only questions retrieve reported fires without weather or guidance. Weather “now” requires a fresh current-weather lookup; future requests such as “What about tomorrow?” select forecast. A comparison selects both. Guidance questions use previous source observations from Memory and the broker. Explicit combined questions retrieve requested observations before guidance. The supervisor uses the native sequential tool executor, and guidance independently checks that all requested source capabilities have receipts before starting. Independent provider lookups and simulation jobs still run concurrently. The automatic radio investigation explicitly requests fires, weather, forecast and guidance; frozen simulation plans remain guidance-only and skip task selection.

Detection receives a scoped observation task and fixed selected topics. The original human request supplies location, radius and forecast-horizon reference; the same authoritative reference drives grounding, so a conflicting place in a restated tool query cannot relabel the lookup. Planning belongs to the guidance specialist. A native completion hook requires successful provider receipts for every selected topic before `ObservationBriefing`. Repeated dispatches within that invocation reuse the complete receipt under an invocation-local lock; partial results and failures are not reusable completions. Each new turn still performs its requested fresh lookups.

The public completion is a typed status, not the displayed answer. Detection supplies a qualitative weather/forecast briefing; code adds compact exact provider measurements. Guidance supplies bounded scenario applications with verified retrieved passage IDs; code attaches actual source titles. Combined answers preserve both. The supervisor does not rewrite specialist output. A guidance-failure status requires an actual failed specialist receipt, and completed requested guidance requires a specialist result. No location yields a clarification rather than an invented default map point.

A shared source projection excludes uninterpreted `dtnSpecific` indices from all three specialists' reasoning inputs, retaining complete raw records for evidence inspection. Guidance binds the original human question in code; a supervisor-authored risk interpretation cannot replace it. Weather alone does not authorize evacuation activation or crew placement.

Task selection also chooses an exact retained source-location ID when resolving a reference. “There” and omitted weather targets use the latest search point; explicit fire references may choose that fire's separate coordinates. Newly named places remain eligible for geocoding. Code binds the selected point to detection and rejects re-geocoding or moving to a nearby place. The model selects a source identity rather than copying coordinates into a new question.

The general Investigation broker keeps bounded observations from a final snapshot followed by successful `RUN_FINISHED`, scoped to the owner and conversation in the existing metadata table. The Runtime's `context_envelope` carries only these `previous_observations`; the broker drops every client-supplied envelope key, including `run_id` and `evidence`. Weather follow-ups at the same source point retain earlier fire records for later guidance. A new location replaces the previous area's context; a fresh fire search with zero records clears previous fires. Failed/partial turns do not advance these receipts. Records use existing session TTL; expiry is checked before use because DynamoDB TTL removal is asynchronous. Source timestamps remain observations of their retrieval time, not a claim of current conditions.

Runtime forwards only locations, names and available capabilities to task selection. Guidance receives exact successful detection receipts from bounded Memory history and previous/current broker receipts through its invocation-bound tool. Measurements are neither duplicated in routing prompts nor copied by the supervisor. Old assistant suggestions cannot add tasks. A native pre-tool hook rejects premature completion with a recoverable error until every selected fresh lookup and guidance step has its own receipt. Terminal validation checks the same evidence boundary. The published guardrail evaluates the supervisor's latest user/context message across tool cycles, and generated output remains guarded. The guidance and communication specialists send the visitor question, every provider or conversation text value and every key that no schema model or context builder defines as `guardContent` blocks, with text as written rather than `\u` escapes, and the Runtime's `[UI_CONTEXT]` framing, code-owned field names and numbers as plain text (`guarded_context`). The prompt-attack filter then checks the untrusted text without classifying that framing, which had blocked an active-fire draft. The models receive the same text, in the same order, as content blocks, and the guardrail settings are unchanged.

Selection, investigation execution and their observation/guidance specialists require native model tool calls (`tool_choice=any`), including the SDK's structured result tool. This avoids the SDK's synthetic “format the previous response” user message, which the published prompt-attack policy blocks. `ToolResultModel` delegates to the same guarded model through the public Strands Model API; it does not change guardrail settings or replace structured validation.

Detection uses Pydantic's JSON output mode, and its wrapper returns serialized JSON to the native SDK. Non-JSON enum values previously made Strands fall back to Python `repr` text, which could not serve as a JSON source receipt. Memory projection accepts only valid matched successful receipts; it never evaluates Python text or promotes specialist narration to source evidence.

## Extension rules

Add operations to the workspace contracts first, generate the model-visible schemas, and implement bounded wrappers in the relevant specialist. Keep authorization context outside model arguments. Register only the tools the selected capability can use. Preserve the twelve-call simulation budget and 180-second reasoning deadline. They are Runtime protocol constants in `utils/limits.py`, not [workspace settings](29-workspace-operations.md).

Use native structured output for transport shape and deterministic evidence for factual claims. Keep selection and execution within the supervisor; do not add routing services or an agent per simulation policy. Investigation exposes only the explicitly selected detection, public guidance and communication capabilities. Reports and simulation retain their separate entry paths. Removed inactive private supervisor composition, duplicate prompts and a write-only detection cache. Signing, Memory reads, selection and MCP construction run off the event loop. Selection is limited to two model turns; public execution has a 12-tool-call budget. The broker holds an agent turn to the same twelve tool calls (`agent_broker.MAX_TOOL_CALLS_PER_TURN`).

`ToolExecutionCapture` retains native `AfterToolCallEvent` success/error status. Runtime reattaches that status to the AG-UI result that otherwise unwraps it. The UI displays tool completion, not a claim that fire/weather observations or guidance were independently verified. Unknown completion remains explicitly unavailable; nested domain failures take precedence over a successful SDK invocation.

## Prompts and evidence grounding

Prompts describe only the tools registered for a capability and stay short responsibility rules next to it under `backend/runtime/agents/`. Contracts live in code and generated schemas; do not keep a second JSON or API specification in a prompt. Derive capability guidance from the generated tool inputs and configured limits, and reject unsupported controls explicitly rather than simulating success. Outcome wording follows the [business justification](33-evacuation-crew-access-business-justification.md).

| Capability | Grounding rule |
|---|---|
| Investigation supervisor | Resolve references from bounded verified AgentCore history. Select tasks from the latest request and register only those capabilities. Guidance cannot precede required observation receipts, and an earlier guidance request never makes guidance mandatory later. |
| Communication | Bind the original request and one selected owned incident. Use weather only from records at that incident's coordinates. Draft for review without invented orders, thresholds, schedules or routes. Code owns translation, speech and private storage. |
| Detection | Resolve the supplied location with the shared geocoder; no invented fallback coordinates. Fire-only searches skip weather. "Now" selects current weather, "tomorrow" selects forecast, and comparisons select both. |
| Public procedures | Retrieve the reviewed public collection first. Distinguish published guidance from scenario interpretation and attach official citations. No private-KB fallback. |
| Simulation | Query saved evidence. Only vehicle count and departure window are frozen-parent variant controls. A proposal creates a draft; submission needs explicit user intent. |
| Firetruck report | Use signed saved evidence only; no tools or invented suppression measurements. |

Public guidance has up to five sections of at most 500 characters, each citing one to three retrieved passage IDs; a passage is a source sentence of 30–350 characters (`agents/response_agent/response_agent.py`). Cited plan synthesis has a four-turn budget. A native pre-tool hook rejects unknown passage IDs and returns the retrieved ID set for correction, and final rendering verifies the IDs against the exact source text again. A blocked guardrail or an exhausted budget leaves guidance unavailable, with no retry that bypasses the policy.

Weather briefing prose has no numeric values: code renders exact measurements and source timestamps, which the drawer shows. The briefing describes observed conditions and forecast changes without spread or suppression claims. Location labels come from the selected point or the provider. Current observations do not establish seasonal anomalies or future conditions, and weather or simulation alone cannot establish ignition risk, evacuation activation or crew placement. Source-day summaries are labeled UTC; numeric humidity and rain probability, including zero, render as values.

## Model and SDK notes

Package versions are those in `requirements-lock.txt`; the cloud builds import and run those exact packages before activation. Strands' validated structured output is used here (D48). Bedrock's service-level constrained JSON output is a separate, model-specific capability and is not enabled; do not infer unsupported parameters from another model's documentation. The configured Claude model supports reasoning and tool use, and no extra thinking budget is needed for geocoding order, authorization or response schemas, which code enforces. Evaluate a reasoning or model change on retained evidence questions for factual accuracy, missing-data handling, tool count and latency. Strands also offers prompt caching; dynamic selections in a system prompt limit reuse, the AG-UI adapter's cloning must be part of any measurement, and no cache or token saving is claimed. The Runtime keeps AgentCore Runtime, Gateway and Strands (D46), and SUMO stays in the private worker (D47).

- [Strands structured output](https://strandsagents.com/docs/user-guide/sdk/agents/structured-output/index.md)
- [Strands agent API and streaming](https://strandsagents.com/docs/api/python/strands.agent.agent/index.md)
- [Strands hook events](https://strandsagents.com/docs/api/python/strands.hooks.events/index.md)
- [Tool executor ordering and cancellation](https://strandsagents.com/docs/user-guide/sdk/tools/executors/index.md)
- [Strands Bedrock prompt caching](https://strandsagents.com/docs/user-guide/sdk/model-providers/amazon-bedrock-prompt-caching/index.md)
- [AgentCore AG-UI protocol contract](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-agui-protocol-contract.html)
- [AgentCore session lifecycle](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-sessions.html)
- [Gateway inbound authentication and authorization](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-inbound-auth.html)
- [Bedrock structured-output model and endpoint considerations](https://docs.aws.amazon.com/bedrock/latest/userguide/claude-messages-structured-outputs.html)
- [Claude reasoning capabilities](https://docs.aws.amazon.com/bedrock/latest/userguide/claude-messages-extended-thinking.html)
- [Amazon Location geocode and storage use](https://docs.aws.amazon.com/location/latest/developerguide/geocode.html)
- [Places API IAM actions and resource scope](https://docs.aws.amazon.com/service-authorization/latest/reference/list_geo-places.html)

## Verification

`backend/runtime/tests/` and `backend/runtime/check_startup.py` check ownership, Memory handoff, scoped tools, provider restrictions, guidance prerequisites, SDK execution status and startup; `backend/runtime/tests/test_runtime_geocoding.py` and `backend/runtime/tests/test_runtime_weather_boundary.py` cover coordinate grounding, the provider and geocoder contracts and the DTN weather boundary. `backend/gateway/tests/` checks destination restrictions, request bounds, tool envelope forwarding and error handling. Real-model prechecks use recorded provider fixtures to cover the exact fire question, history, forecasts, exclusions, clarification, combined requests and communication drafts. Live acceptance independently exercises fire → current weather → forecast → comparison → guidance → combined → fire again → selected-incident communication → fire again in one owned conversation. When no fire is reported near Sacramento, communication drafts instead for the nearest usable named incident (a plain name of up to 120 characters with in-range coordinates) among the at most 100 records that the wildfire tool's 500-mile query returns, after a fire lookup at that incident's coordinates. Those records are not sorted by distance, so the receipt records `truncated` when the provider capped the list. It requires real providers, SDK completion receipts and successful playback downloads for all three translated recordings. Run component checks in [developer setup](12-developer-setup.md). See [data flow](04-data-flow-and-session-management.md) for retention.

## Communication and evidence presentation

Communication is an explicit supervisor topic, never an automatic addition to fire/weather/guidance. It waits for requested observations and guidance, uses one selected incident from owned source receipts, and asks for clarification when incident identity is ambiguous. It has no RSS fetch or independent memory. Code binds the original request and restricts weather/forecast records to the selected incident's coordinates; a supervisor-restated query cannot reintroduce another point's weather. Planning prose is not a source of weather measurements. The specialist drafts guarded English text; code translates and records English, Spanish and Hindi in parallel. Verified actor/session identity determines a hashed private S3 prefix; random artifact IDs and conditional writes prevent collision. Media clients have bounded timeouts/retries and each language reports its own failure. Playback links expire after one hour; recordings use the retained voice bucket's lifecycle. Nothing is sent as an official alert.

The UI retains typed evidence with each completed answer. Its details action restores that answer's map and opens the existing right drawer for citations, current weather, forecast or voice drafts. Source location and timestamps travel with the answer; another question cannot relabel old weather. Exact observations remain inspectable through the shared weather projection and folded raw provider data. Map distance buffers are illustrative geometry only, with no calculated hazard score or spread prediction.
