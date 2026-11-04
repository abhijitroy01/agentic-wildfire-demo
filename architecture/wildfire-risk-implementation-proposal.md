# Wildfire risk: final refactor and implementation proposal

October 26, 2026. **Approved for implementation by the owner: “do all.”** This document consolidates the source refactor and the first wildfire exposure delivery. The source refactor and baseline/timed demo implementation are complete. Release validation and deployment status are recorded below. It supersedes the earlier API naming and placement proposals, including `incident-coordination-api`, `fireguard-api` and `application-api` directories.

## Outcome and scope

Use capability packages directly under `backend/`, assembled by a thin `backend/app.py`. Retain the existing single browser-facing API ECS service. Add an independently owned, private `fire-risk-modeler/` ECS service and replace the browser's distance-only calculation with saved weather-informed exposure geometry.

The first release is **illustrative fire exposure**, not a validated fire-spread forecast. The approved scope also includes a separate, explicitly assumed Pyrothermel surface-fire scenario. Its reference output, wind convention and native builds on macOS and Linux are validated. See the [implementation and verification record](48-fire-exposure-implementation.md) for final checks and publication status.

Requirements carried forward:

- Keep Esri fire observations and DTN weather, with credentials and provider calls server-side.
- Anchor every assessment to the actual reported fire coordinate, or a reliably matched reported perimeter already available in the evidence. Chat search coordinates select incidents; they are not model origins.
- Accept globally valid WGS84 coordinates, including zero, high latitudes and antimeridian crossings. Provider discovery coverage is separate from coordinate support.
- Require no predefined California/Seattle landscape, manual regional download, hand-written incident scenario or purchased simulator license.
- Preserve reported fire display when weather, enrichment or model execution is unavailable.
- New successful chat evidence may start a bounded assessment workflow once. Answer selection, marker focus, navigation and reload only restore/read retained evidence.
- Follow current Python 3.14, TypeScript, generated-contract, centralized-settings, component-test and supported deployment conventions.

## Backend source ownership

```text
backend/
  __init__.py
  app.py                         # FastAPI assembly, lifespan and health
  api_settings.py                # API service bindings and operational settings
  api_contracts.py               # Pure shared HTTP/error contract definitions
  config_routes.py                # Existing workspace-config endpoint
  dependencies.py                # Explicit request/storage dependency wiring
  demo_access.py
  request_limits.py
  http_errors.py
  security_events.py
  budgets.py
  leases.py
  runtime_client.py
  trusted_context.py
  streaming.py                   # Existing shared SSE relay helpers
  simulation_jobs.py             # Existing shared SUMO job admission/dispatch helpers
  simulation_artifacts.py        # Existing retained SUMO artifact readers
  camera.py                      # Existing cohesive camera broker
  radio_broker.py                 # Existing cohesive radio broker
  firetruck_broker.py             # Existing cohesive firetruck/report broker
  fire_risk_broker.py             # Source evidence -> private modeler -> saved result
  investigation/
    __init__.py
    broker.py                    # Investigation turn and owned source receipts
    saved.py                     # Saved incident investigation coordination
    contracts.py
    tests/
  evacuation/
    __init__.py
    routes.py
    service.py
    agent_broker.py               # Simulation turns and signed tool dispatch
    contracts.py
    evidence.py
    capabilities.py
    locations.py
    tests/
  traffic_lab/
    __init__.py
    routes.py
    service.py
    tests/
  scripts/
    local_server.py
  tests/                         # Shared API and cross-capability tests/fixtures
  Dockerfile.api
  Dockerfile.api.dockerignore
  requirements-api.txt
  gateway/                       # Existing Lambda adapters, retained
  runtime/                       # Existing AgentCore agents, retained
  deployment/                    # Existing helpers, retained
  knowledge/                     # Existing guidance assets, retained
```

A source folder does not create a service. Investigation, evacuation, Traffic Lab and the descriptive camera/radio/firetruck brokers remain in the existing API process. The latter brokers are already cohesive; this proposal does not create a directory around every single file.

`backend/app.py` registers router factories, supplies dependencies, installs shared middleware/error handling, and retains the current local SUMO-worker lifespan behavior. No capability imports `backend.app`. Runtime execution and Lambda tool deployment retain their current boundaries.

### Mapping the current API

| Current `backend` module | Proposed ownership |
|---|---|
| `main.py` | `backend/app.py` |
| `settings.py` | `backend/api_settings.py`; `deployment/settings.toml` remains the settings authority |
| `contracts.py` | Pure shared definitions in `backend/api_contracts.py`; investigation and evacuation definitions in their capability contracts |
| `agent_broker.py` | Split investigation handling into `investigation/broker.py`, simulation handling into `evacuation/agent_broker.py`, and reusable SSE helpers into `backend/streaming.py` |
| `incident_investigation.py` | `investigation/saved.py` |
| `routes.py` | Evacuation endpoints into `evacuation/routes.py`; store wiring, workspace-config and shared SUMO artifact readers into the explicit root modules above |
| `service.py` | Evacuation operations into `evacuation/service.py`; daily counters into `budgets.py`; shared SUMO admission/dispatch/discovery helpers into `simulation_jobs.py`; Traffic Lab job creation into `traffic_lab/service.py` |
| `evidence.py`, `capabilities.py`, `locations.py` | `backend/evacuation/`; scenario geocoding/presets do not become a global fire resolver |
| `research.py` | `backend/traffic_lab/routes.py`; these are Traffic Lab endpoints |
| Access, errors, logging, leases, Runtime client and signed-context modules | Explicit root modules under `backend/`, preserving their current behavior |
| Camera, radio and firetruck brokers | Descriptive root modules under `backend/`, preserving their current behavior |

Move existing functions with their behavior intact before changing risk behavior. Shared modules must not import capability service implementations. In particular, extracting budgets removes the current dependence of Runtime calls, access controls and camera requests on evacuation's service module.

Saved investigations currently consume evacuation incident/run evidence. Supply owned readers and evidence construction through explicit dependencies; do not create mutually importing investigation and evacuation implementations. Pure contract modules can be imported offline without initializing settings, stores or AWS clients.

Reuse the existing Store/persistence interfaces. Moving the SUMO engine, its storage implementation or agent prompts into a new generic shared library is outside this refactor.

## Risk modeler ownership and execution

```text
fire-risk-modeler/
  README.md
  requirements.txt
  requirements-dev.txt
  Dockerfile
  Dockerfile.dockerignore
  fire_risk_modeler/
    __init__.py
    models.py                    # Pure canonical model input/result contracts
    exposure.py                  # Documented proximity/downwind exposure rule
    geometry.py                  # Physical distances and valid WGS84 GeoJSON
    surface_fire.py              # Native Pyrothermel/Behave directional rates
    service.py                   # Private authenticated HTTP calculation service
    settings.py
  scripts/local_server.py
  tests/
    conftest.py
    test_fire_risk_exposure.py
    test_fire_risk_geometry.py
    test_fire_risk_private_service.py
    test_fire_risk_settings.py
    test_fire_risk_surface_fire.py
```

Use already-declared pyproj and Shapely through the shared dependency lock, while declaring the modeler's own manifest. The model owns calculations and geometry; the API owns visitor authorization, authoritative source references, provider enrichment, input freezing, deduplication and retained results. The modeler owns no provider credentials, browser cookies, agent prompts, persistence or traffic policies.

The first mode returns distance context plus a bounded downwind shape when suitable DTN wind exists. Normalize wind units and meteorological "from" direction explicitly. Geometry distances and the bounded shaping rule are documented demo settings; they are not spread rates. Temperature, humidity, gusts and reported indices remain weather context, not an invented burn probability or measured fuel moisture.

Return versioned GeoJSON, fire/input/result identities, model mode, source timestamps, weather availability and assumptions. Use Polygon/MultiPolygon geometry with correct holes and longitude wrapping. Reported point geometry and a matched observed perimeter remain distinguishable. Missing/stale wind returns labeled proximity context; model-service failure preserves the reported fire and marks exposure unavailable.

### One assessment workflow

1. Existing agents and Gateway tools retrieve Esri/DTN observations for the chat request.
2. `investigation/broker.py` retains an immutable owned per-answer source receipt alongside the current rolling conversation receipt.
3. `fire_risk_broker.py` resolves each valid retained displayed fire up to declared incident/concurrency limits, reuses suitable retained weather, and enriches missing weather through the existing DTN Lambda using a configured ARN and scoped IAM permission. Retain weather query coordinates, observation coordinates when supplied, and timestamps. Reuse requires declared freshness and spatial-distance bounds relative to the actual fire; weather for the chat search-city center must not automatically apply to every incident.
4. Freeze normalized inputs and deduplicate by owner, source/input identity and model version. Call the private modeler within a reserved postprocessing deadline.
5. Validate fire/input identity and output geometry, retain results in the existing store, and emit an enriched final `STATE_SNAPSHOT` before `RUN_FINISHED`. Broker-owned references replace any same-named agent-generated fields.
6. The UI renders the saved result for that answer. Late completions are bound to answer/assessment identity; selection and navigation create no work.

One workflow may assess multiple displayed fires, but it must have bounded counts, concurrency, provider budgets and execution time. A truncated incident list is explicit. Postprocessing time must be reserved so agent execution cannot consume the entire request deadline. An exposure failure must not turn an otherwise successful fire response into a failed chat answer.

Fix directly relevant discovery/normalization issues: preserve stable Esri source identity and timestamps, retain valid zero values, distinguish unknown containment from zero containment, and prevent acreage-priority shortlisting from silently excluding a requested nearby/named incident. No reported incident means no invented fire or silent search-radius expansion.

No new queue, table, bucket or generic job framework is needed. The existing SUMO queue remains traffic-specific. Reading retained assessment references does not introduce persistent chat history; chat history is currently in memory.

### Explicit API boundaries

These route names are proposed contracts, except for the existing investigation transport. Source refactoring preserves the browser/origin path mapping.

| Caller and purpose | API | Status and behavior |
|---|---|---|
| Browser starts a chat turn | `POST /simulation-api/v1/investigation/turns` maps to API `POST /api/v1/investigation/turns` | Existing, same-origin demo-session transport; returns AG-UI SSE |
| API invokes agents | AgentCore `InvokeAgentRuntime` | Existing server-side integration |
| API enriches weather | AWS Lambda `Invoke` for the configured existing DTN function ARN | Proposed API permission/binding; reuse the adapter and retain normalized evidence |
| API calls modeler | Private `POST /v1/exposure/assessments` | New, `200 OK` with completed bounded synchronous JSON calculation; modeler retains no assessment records |
| Browser restores a retained assessment | `GET /simulation-api/v1/fire-exposure/assessments/{assessment_id}` maps to API `GET /api/v1/fire-exposure/assessments/{assessment_id}` | New, `200 OK` owner-checked retained-only read; no provider, agent or model execution |
| ECS checks modeler health | Private `GET /health` | New, process health only |

The API discovers the private modeler through the installation's internal service namespace and a server-side binding. Use an internal service credential from Secrets Manager, in addition to API-only network ingress; the proposed modeler HTTP boundary checks that credential. Local configuration uses the same endpoint contract against the local modeler. No browser modeler URL, provider credential or new public calculation endpoint is required.

The private request contains `schema_version`, `assessment_id`, `fire_ref`, `input_hash`, normalized authoritative fire geometry, normalized weather/query coordinates and timestamps, supported model mode and bounded parameters. Define coordinate/distance/wind units explicitly; wind direction is meteorological degrees-from. It contains no visitor cookie, provider key or agent prompt.

The response echoes request/fire/input identities and carries `model_version`, availability/mode, calculation/source timestamps, GeoJSON FeatureCollection, weather provenance and assumptions. The API validates and saves it under the resolved owner before publishing typed `content.fireExposureData` in its final `STATE_SNAPSHOT`, followed by `RUN_FINISHED`. The proposed field is broker-authored and replaces any agent-authored value of the same name.

The first version needs no additional browser POST for assessment creation: successful new chat evidence supplies the single creation trigger. A later explicit assessment action would be a separately defined owner-checked source-reference request, not permission to submit arbitrary browser fire coordinates.

Frontend rendering follows the existing response parser, answer evidence and map-data path. `fireExposureGraphics.ts` converts saved WGS84 GeoJSON into ArcGIS `Polygon`/`Graphic` objects in the existing `GraphicsLayer`, preserving Polygon/MultiPolygon parts and holes. Coordinates use `[longitude, latitude]`; ring orientation/conversion and antimeridian output require tests. The map adapter performs presentation conversion, not model mathematics. No separate ArcGIS hosted feature service, tile publisher or new map SDK is proposed.

### API semantics and alignment with existing services

Use REST-style resource reads and documented HTTP calculation semantics. The complete application also uses AG-UI SSE and WebSockets for streaming; it is not accurately described as an entirely REST-only system.

- Private synchronous POST returns `200` with the computed representation. It does not return `201`/`Location` because the modeler retains no created resource, or `202` because there is no asynchronous job/status interface.
- Public assessment GET is safe and retained-only. Unknown and other-owner IDs follow the existing indistinguishable `404` behavior; expired/missing artifacts use existing availability/error conventions.
- Use typed validation, explicit units, versioned contracts and stable error codes. Preserve the public error envelope; distinguish invalid input (`422`), missing/invalid authentication (`401`), denied access (`403`), request-identity conflicts (`409`), capacity limits (`429`, with `Retry-After` where known), and unavailable dependencies (`503`). Do not return provider exception text or credentials.
- The broker assigns a stable assessment/request identity to one owned answer/fire/model intent and reserves it atomically before enrichment. Repeating that request uses its frozen inputs and existing result; reusing an identity with different inputs is a `409` conflict. A new answer/new source evidence is a new intent, even when some values match. Input hashes support validation/caching rather than serving as the sole request-intent identifier, following [AWS guidance on safe idempotent retries](https://aws.amazon.com/builders-library/making-retries-safe-with-idempotent-APIs/).
- The canonical input hash includes immutable fire observation identity/version, weather identity/time/query location, normalized geometry, effective parameters and model version. Do not hash only an Esri incident ID or a mutable conversation receipt.
- A private calculation may repeat after an ambiguous timeout. Frozen deterministic inputs make that safe; broker reservation/publication prevents duplicate authoritative results or repeated provider-budget spending. Do not promise exactly-once execution or retry indefinitely.
- Bound incident count, input/output geometry size, response bytes, concurrency and deadlines. If exposure fails after SSE headers were sent, report a typed exposure-unavailable field in the final snapshot while retaining the successful fire answer; an HTTP status cannot be changed midway through that stream.

The following source comparison was checked on October 26. It verifies code/configuration, not live deployment parity or production certification.

| Component | Browser/backend and private service interface | State ownership |
|---|---|---|
| Existing Broadcastify radio | Browser POST returns SSE; backend opens authenticated private WebSocket `/ws/monitor/start`; private `GET /health` | Transient monitor/socket/session state in the radio process; configured S3 input recordings; no durable radio-result resource exposed by this service |
| Existing firetruck simulator | Public POST/GET/DELETE run resources plus browser WebSocket; private authenticated PUT/GET/DELETE `/internal/runs/{run_id}` | Service owns DynamoDB run leases/state and S3 evidence; API owns viewer records and report coordination |
| Proposed fire-risk modeler | Existing chat SSE plus saved assessment GET; private versioned synchronous POST and `GET /health` | Pure calculation process; API owns immutable source inputs and retained assessment results |

All three follow browser -> application broker -> private ECS service separation. Existing CDK provides service discovery, private subnets without public IPs, API-only ingress, Secrets Manager credentials, restricted egress, read-only root filesystems, scratch storage, health checks, retained logs and rollback. Existing images use Python 3.14, locked dependencies and non-root execution. Apply the normal image/test/release gates to the new modeler.

Reuse this deployment/security pattern, not the radio's long-lived WebSocket lifecycle or firetruck's separate durable run engine/table. Current private service hops use HTTP with service bearer credentials and manual rotation, not mTLS; existing health routes chiefly report process/configuration health. Firetruck private routes are unversioned, and DELETE requests a stop while retaining evidence. These are actual differences to document, not reasons to redesign the other services for this demo.

Source anchors: [radio private gate](../broadcastify/monitor/private.py), [radio broker](../backend/radio_broker.py), [firetruck private service](../firetruck-simulator/api/private.py), [firetruck broker](../backend/firetruck_broker.py), [shared ECS pattern](../deployment/infra/lib/stacks.ts) and [public error handling](../backend/http_errors.py).

## UI and contracts

```text
ui/src/features/fire-exposure/
  generated/contracts.ts
  components/FireExposureSummary.tsx
  services/api.ts                # Only if retained-result retrieval needs a client
ui/src/features/map/utils/
  fireExposureGraphics.ts
ui/scripts/test/
  fire-exposure.test.cjs
schemas/fire-exposure/
  contracts.json
```

Update the existing response parser, answer-evidence binding, map graphics loader, legend and escaped popups. Use existing themes, panels and map lifecycle fencing. The browser stops calculating exposure formulas once the server path replaces `riskModel.ts`.

The map shows reported fires, translucent proximity/downwind bands, a wind arrow and a compact source/weather explanation. Labels describe **Illustrative fire exposure**. Do not add arrival times, calibrated High/Medium/Low burn probabilities or an "outside means safe" label. Use reported perimeters only when reliably matched; fetching a new perimeter feed is not a prerequisite.

Initial placement is Investigation's shared ArcGIS workspace map. Preserve the separately frozen SUMO hazard in MapLibre replay; exposure does not automatically close roads or change traffic/firetruck outcomes.

Use a single backend trigger. Do not add a UI effect that creates a second assessment on completion, tab entry or component mounting. A retained-result reader may perform GET requests, with no provider/model calls.

Canonical model definitions live in `fire_risk_modeler/models.py`. Capability request/reference wrappers live in `backend/investigation/contracts.py`. Extend `scripts/export_integration_contracts.py`, `schemas/api-schema.yaml` and generated UI contracts from those definitions; avoid duplicate handwritten result schemas. The API/exporter may import pure model definitions without loading the model's service or calculation runtime.

## Packaging, deployment and configuration

The API entrypoint becomes `uvicorn backend.app:app`. Its image includes explicit root API modules and capability packages, the existing needed traffic integration code, and pure model contracts. Do not indiscriminately copy all Runtime, Lambda assets or knowledge content into the API image. The local launcher moves to `backend/scripts/local_server.py`; import paths and repository discovery are updated consistently.

Move API dependencies/build assets to `backend/requirements-api.txt`, `backend/Dockerfile.api` and its paired ignore file. Keep Runtime and Gateway builds separate. Update root requirements/locks, pytest paths, bootstrap, quality registration, contract exports, source-package allowlists, image builds, monkeypatch paths, acceptance scripts and runnable documentation. Shared test fixtures use explicit namespaced imports; do not retain bare `from builders` imports or add nested pytest plugin registration.

Deploy the modeler as a **private stateless ECS Fargate HTTP service**, following existing radio/firetruck private-service conventions: service discovery, private subnets, API-only ingress, service authentication, non-root execution, health checks and normal verification/image scan gates. The browser keeps the same-origin public API; no model URL/key enters Vite configuration.

Extend existing `deployment/package_source.py`, `deployment/check_lockfiles.py`, `deployment/infra/lib/stacks.ts`, image/digest/release mappings in `deployment/deploy_workspace.py`, installation validation and acceptance checks. Use the supported deployer. Settings, units and ranges remain in `deployment/settings.toml`.

Measure bounded calculations before selecting task size within the deployment's currently supported mapping. Do not promise fractional vCPU sizes without validating the infrastructure mapping. Free libraries do not remove ECS, Esri or DTN operating costs.

## Compatibility and delivery sequence

**Milestone 1: source refactor.** Extract concrete shared helpers, split mixed contracts/brokers, move capability files/tests and change the entrypoint/build paths. Remove `backend/` only when imports and packaging are fully migrated. Verify that published contracts remain identical before adding the feature.

Preserve existing routes, operation IDs, cookie/header names, signed-context purposes/issuer/audience, owner/thread derivation, record keys and retention, idempotency, artifact prefixes, ports, health behavior, ECS API identity, CDK logical IDs, stateful resource names and release outputs. Source organization alone is not a protocol or infrastructure replacement.

**Milestone 2: first exposure delivery.** Add canonical model contracts, immutable source receipts, the private modeler, bounded broker enrichment, saved GeoJSON and the UI layer. Keep the refactor and feature changes separately reviewable. Integrate image/release/local paths and validate actual behavior.

### Build checkpoints and rollback

1. Record the current generated-contract and focused ownership/streaming/budget/evidence baseline. Preserve existing working-tree changes and record the source and release identities used for comparison.
2. Complete the source-only refactor, including imports, tests, startup, export and image/source packaging. Compare published contracts with the baseline before adding new exposure fields.
3. Add canonical assessment/input/result contracts and pure geometry calculations. Verify coordinate, distance, weather-direction and geometry edge cases before connecting providers or the UI.
4. Add private service authentication and bounded execution, then the owned broker workflow: immutable receipts, atomic request reservation, relevant weather reuse/enrichment, frozen inputs and saved-result reads. Verify provider/model failure behavior.
5. Integrate existing chat/map presentation. Verify answer identity, unavailable states and zero provider/model work on read-only interaction.
6. Complete centralized settings, private ECS wiring, lock/source/image/release registration and generated contracts. Run focused suites, full quality, UI build, offline synth and release-image verification. Measure runtime to choose supported task sizes and sensible deadlines.
7. Perform local acceptance for successful and failing owned flows. A live deployment is a separate release step: confirm AWS identity, review the concrete change set, and distinguish expected new modeler resources from unintended changes to existing resources.

Keep prior API/UI image digests and release bindings available. The refactor preserves public contracts and resource identities; exposure adds new records/contracts without destructive data migration. Keep optional modeler binding/availability behavior so the feature can be disabled or the prior coordinated release restored while preserving existing fire/incident evidence. A rollback must not delete retained assessment records merely because an older release does not display them.

**Approved and implemented milestone 3: timed surface-fire demo.** [Pyrothermel](https://github.com/j-tenny/pyrothermel), whose published [MIT license](https://github.com/j-tenny/pyrothermel/blob/master/LICENSE.txt) wraps the [public-domain Behave library](https://github.com/firelab/behave/blob/master/LICENCE.md), supplies directional rates. Python 3.14 compilation on macOS and Linux, reference outputs, units and wind-height/direction conventions passed. The geometry uses native direction-of-interest rates; the wind-shaped exposure ellipse remains a separate illustrative rule.

Only after that evidence, add `surface_fire.py` and explicitly assumed standard fuel/moisture, flat slope and constant wind to a separate **Surface-fire demo scenario** mode. A reported point is not a verified ignition/front, and relative humidity is not fuel moisture. Timed outlines and controls belong only to successful calculator output. Missing prerequisites leave the scenario unavailable and retain exposure; do not relabel a heuristic as simulation.

Defer ForeFire, automatic fuel/terrain downloads, fuel-class inference, landscape-aware propagation, queues and probability ensembles. They are not needed for the first coordinate-driven demo. Existing provider geographic coverage remains explicit.

## Validation and approval boundary

| Check | Evidence required after implementation |
|---|---|
| Refactor compatibility | Identical pre-feature contract export; API, Gateway and Runtime suites; ownership/budget/lease behavior and saved evidence unchanged |
| Model | Direction/units, calm/missing/stale wind, physical distances, zero coordinates, high latitudes, antimeridian, valid polygons/holes |
| Broker | Source/result identity, immutable per-answer references, weather-coordinate/freshness matching, duplicate/concurrent deduplication, owner isolation, provider/model failure, deadlines and saved reads |
| UI | Actual-fire anchoring versus search-city center, answer isolation, stale-completion fencing, source/assumption labels and escaped popups |
| Read-only interaction | Zero provider/model calls on saved-answer selection, navigation, marker focus and reload |
| Release | Lockfile/source/image closure, contract export, full quality gate, UI build, offline synth/CDK identity checks, API/modeler verification and runtime image builds from release sources |
| Local/live acceptance | Local successful/failing owned flow; live acceptance and change-set review remain a separate deployment step |

Focused checks are followed by the repository quality command, UI build and offline synth. Tests remain beside their capability/component; shared HTTP/cross-capability tests remain in `backend/tests/`, and model mathematics stay in `fire-risk-modeler/tests/`. Register all moved suites so the existing quality/coverage entrypoints still collect them. No coverage percentage threshold is currently configured.

Current source status: capability refactor, private modeler, broker, map exposure and the optional timed scenario are implemented. Pre-feature API contracts were unchanged; focused API, model, UI and CDK checks passed. See [implementation and validation record](48-fire-exposure-implementation.md) for final check results and deployment status.

Source evidence: [current API](../backend/README.md), [app assembly](../backend/app.py), [mixed broker](../backend/evacuation/agent_broker.py), [service and budgets](../backend/evacuation/service.py), [contract exporter](../scripts/export_integration_contracts.py), [quality entrypoint](../scripts/check_quality.py), [source packaging](../deployment/package_source.py), [private service infrastructure](../deployment/infra/lib/stacks.ts), [demo scope and research](wildfire-risk-demo-plan.md), [model assessment](wildfire-risk-model-assessment.md) and [decision log](decisions.md).
