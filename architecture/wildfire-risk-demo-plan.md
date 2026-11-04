# Wildfire exposure demo plan

Proposed October 23, 2026; reassessed October 26, 2026.

The owner wants a relatable demo, retaining the existing **Esri fire data and DTN weather**. The visualization must be driven by the **reported fire's latitude and longitude**, wherever that fire is located. Place names in chat select the search area; they do not select a regional model or replace the fire's coordinates. An ECS simulator is acceptable if it improves the map; adding commercial simulation software is not. The owner approved implementation with “do all”; source delivery and verification are recorded in the [implementation guide](48-fire-exposure-implementation.md).

The first release is a small **weather-informed exposure visualization**, using the existing broker and maps. The [approved implementation proposal](wildfire-risk-implementation-proposal.md) places model ownership in a top-level `fire-risk-modeler/` package and uses a private ECS HTTP service. The backend now uses capability packages directly under `backend/`, composed by `backend/app.py`, with `backend/fire_risk_broker.py` handling retained source/model evidence. It supersedes the earlier API-local model placement and API naming proposals. Geographic coverage of upstream providers remains separate: the existing public U.S. fire feed does not establish worldwide incident discovery.

## Coordinate-driven behavior

- Accept a valid fire coordinate in WGS84: latitude −90 to 90, longitude −180 to 180. Zero is valid. Do not gate drawing on a city, state, country, preset or cached landscape.
- Anchor each fire's exposure geometry to its own reported coordinates, or its matched reported perimeter when available. Keep the chat search center separate.
- Use weather at that fire's location for directional context. Compute geometry in distance units and return WGS84 GeoJSON; degree offsets must not stand in for fixed physical distances.
- Show labeled proximity context for every valid reported fire. Add downwind context when suitable wind observations exist. Optional timed scenario output must identify its calculator and assumptions separately.
- Missing fuel/terrain data leaves the coordinate-based exposure view available, with simulation explicitly unavailable. It must not hide the fire, move it to a supported region, invent local terrain or label a heuristic as engine output.
- Handle longitude wrapping, polygons crossing the antimeridian and high-latitude geometry. Coordinate-independent drawing requires those checks; merely removing a California bounds check is insufficient.

The same visualization path must work for any supplied valid fire location. Its available evidence determines the displayed model detail, not the place name. Extending worldwide incident discovery would additionally require a suitably covered fire provider; that is not accomplished by changing the renderer.

## Recommendation

Start with **wind-informed exposure polygons** around each reported fire, an observed perimeter when a reliable match exists, a wind arrow and a compact weather/source panel. The polygons make the downwind direction visible while preserving simple proximity context. Temperature, humidity and any reported hot-dry-windy index provide weather context; do not combine them into an invented probability or treat humidity as measured fuel moisture.

Use a small deterministic modeler package behind the existing workspace API, with one typed GeoJSON result. `pyproj==3.8.0` and `shapely==2.1.2` are already declared in the repository's traffic dependencies; declare the modeler's own dependency manifest using the shared lock. Exposure distances and any bounded wind-shape rule are documented demo settings, not calculated spread rates. Do not put arrival-time labels on this mode.

The implemented timed scenario uses **Pyrothermel**, an MIT-licensed Python binding to the public-domain Behave fire-behavior library. Standard fuel/moisture presets calculate surface spread without mandatory landscape rasters. The geometry adapter uses directional rates for an explicitly assumed homogeneous-fuel, constant-wind scenario. This can be anchored to any valid coordinate, but it does not establish actual fuel or slope there. Python 3.14/macOS and Linux builds, wind-input conventions and reference outputs passed.

**ForeFire is a deferred alternative for a landscape-aware enhancement**, rather than the first implementation requirement. It publishes a GPLv3 license, Linux/Docker execution, a batch interpreter, Python bindings and GeoJSON output. There is no software subscription or commercial license purchase, including for commercial use, subject to GPLv3 terms. Its extra landscape acquisition/conversion work is justified only if the visual benefit warrants it. The detailed ForeFire integration notes below apply only to that optional scope.

**ELMFIRE and Pyretechnics are excluded from this plan** because their current published licenses include commercial restrictions. The earlier model assessment retains those engines as research history, not as the chosen implementation.

Sources: [pyproj and MIT license](https://github.com/pyproj4/pyproj), [Shapely and dependency licenses](https://pypi.org/project/shapely/), [Pyrothermel presets and calculator](https://github.com/j-tenny/pyrothermel), [Pyrothermel MIT license](https://github.com/j-tenny/pyrothermel/blob/master/LICENSE.txt), [Behave public-domain statement](https://github.com/firelab/behave/blob/master/LICENCE.md), [published fire-ellipse assumptions](https://owfflammaphelp62.firenet.gov/LegacyFARSITE/Tech_Elliptical_Dimensions.htm), [ForeFire repository](https://github.com/forefireAPI/forefire), [GPLv3 license](https://github.com/forefireAPI/forefire/blob/master/LICENSE), [ELMFIRE license](https://github.com/lautenberger/elmfire/blob/main/LICENSE.md), [Pyretechnics license](https://github.com/pyregence/pyretechnics/blob/main/LICENSE.md).

### Optional free geographic context

[ESA WorldCover](https://esa-worldcover.org/en/data-access) offers global 10 m land-cover maps for 2020 and 2021 under CC BY 4.0, including tree cover, shrubland, grassland, built-up areas and water. Public cloud-optimized tiles allow an adapter to select data around the actual fire coordinates automatically. It can add a useful context layer, but its classes and age do not establish a current fire-fuel model. In particular, built-up areas must not be treated as a guaranteed fire barrier.

[Copernicus DEM](https://registry.opendata.aws/copernicus-dem/) offers free elevation data: the public GLO-90 mirror provides worldwide land coverage at 90 m, while the cited public GLO-30 mirror has some missing tiles. These are digital surface models, including vegetation/buildings, rather than guaranteed bare-earth terrain. Automatic sampling and caching can later add slope context; preserve source/version, attribution and availability.

Neither dataset is required before drawing exposure. Do not silently convert generic land-cover classes into validated Rothermel fuels. Source-specific fuel mapping and slope interpretation are additional modeling work, separate from displaying a contextual layer.

## What the user sees

A chat question about fires near a place uses the existing geocoding and Esri lookup to locate reported incidents within the requested search radius. Display each fire and its exposure context at that fire's own coordinates through the existing observation flow. A new chat request that retrieves fire observations can initiate a bounded assessment for its selected incident once the broker saves the successful receipt. The user does not prepare input files or enter an engine script.

Selecting a saved answer, focusing a marker, panning, changing tabs or reloading only selects or restores existing evidence. It does not create another job. An additional fire without an assessment needs a new chat request or an explicit **Assess this fire** action. This preserves the repository's navigation rule while making the first assessment follow the actual question automatically.

Preserve the requested area and stable incident identity through discovery, enrichment and simulation. The current priority-ranked four-fire shortlist must not silently remove a nearby incident that the user asked about. Display limits and truncation should be explicit, and an explicitly named returned incident takes precedence over an automatic ranking.

The first map shows **Fire exposure**, with the reported marker/perimeter, translucent proximity/downwind bands, a wind arrow and weather/source times. Hovering a band explains its distance and why it is shown downwind. Highlight already-present map assets inside the displayed area where their coordinates are available; this is exposure context, not a damage estimate. No new asset-ingestion project is needed.

An optional successful calculator/engine assessment adds **Potential fire spread — demo scenario**, with elapsed-time outlines and a time slider. The panel identifies assumed fuel, moisture, slope and constant wind. Specific horizons such as 30/60/120 minutes are presentation candidates to verify, not a commitment in the baseline.

Missing or stale wind leaves proximity context visible and clearly identifies unavailable directional context. If an optional scenario needs downloads or a queued run, retain the baseline map and show its preparation status. No completion-time promise has been established, and the baseline must not carry simulated time labels or be described as engine output.

If the provider reports no matching incident, say **No reported fires found within the searched area**, show the searched area, and omit incident spread. Do not create a fire to demonstrate the model or silently widen the search radius. Retain source errors, observation age, unknown activity and truncated results; source records alone do not prove a fire is currently advancing.

For a selected location, the baseline explains proximity and downwind exposure. Only a successful timed scenario may display **Within the simulated footprint**, **Outside the simulated footprint**, or **Outside the modeled area**. Do not translate either mode into a percentage chance of burning or label an outside location as safe.

The explanation is concrete: “The shaded exposure area extends southeast because the reported wind comes from the northwest.” Timed or terrain-aware explanations must reflect the actual selected scenario and available inputs; a uniform-fuel scenario cannot claim different growth over mapped fuels and slopes.

Use one visible qualification appropriate to the displayed mode: **Illustrative fire exposure** or **Illustrative surface-fire scenario; not an evacuation forecast**. The initial release does not model crown fire, spotting, suppression or fire-atmosphere feedback.

Retain the existing distance bands behind an optional **Distance context** toggle. They help orient the audience but do not determine simulated spread. Use time labels on the spread colors instead of claiming calibrated High/Medium/Low risk.

## Alignment with the current architecture

Reviewed the relevant chat, provider, broker, contract, worker, storage, map and deployment paths on October 23, 2026. These are source findings, not proof of deployed behavior or ForeFire integration.

The current investigation path is:

```text
useAgentSubmission → AgentService / AgentEventStream → investigation broker
→ AgentCore supervisor → detection → existing Gateway provider adapters
→ source-grounded structured response → broker-owned receipt → map presentation
```

The exposure feature extends this path after authoritative observations exist. Deterministic modeler code owns geometry; agents retrieve and explain source evidence. The existing API owns brokering, source references and retained results. The revised proposal recommends private HTTP ECS execution, following the firetruck/radio service pattern; a long-running queue worker is a separate later decision.

The existing `SimulationScenario` is tied to California road/demand assumptions and is not the input contract for this feature. Use a separate globally valid coordinate/observation model, preserve source identity and ownership, and generate its shared contract. The UI renders the returned geometry and never substitutes the chat search center for fire coordinates. Existing source selection issues in the assessment must be addressed so the intended incident reaches this flow.

Suitable DTN weather can be reused from retained evidence. Fire-only grounding currently suppresses weather retrieval; preserve that behavior and add a bounded exposure-enrichment operation if needed. A configured IAM call to the existing DTN Lambda is a candidate transport, not an existing worker binding. Provider keys remain server-side, and navigation only reads saved results.

The following table and detailed queue/raster notes are **conditional integration requirements for the optional ForeFire/ECS enhancement**, not tasks required for the baseline:

| Concern | Existing authority / evidence | Planned extension |
|---|---|---|
| Chat observations | `backend/runtime/agents/detection_agent/coordinate_grounding.py`, `detection_agent.py`; `backend/investigation/broker.py` | Retain exact provider records and source times. Start from an owned, versioned observation reference saved by the broker. Generated prose and browser coordinates cannot authorize a run. |
| Public API | `backend/app.py`, `demo_access.py`, `contracts.py`, `routes.py` | A small `fire-risk-modeler/` module with owner-injected routes and strict request/status/result models, mounted in the existing FastAPI application. Keep the same browser-facing backend and error envelope. |
| Model execution | `traffic-simulator/traffic_simulator/worker.py`; separate firetruck service | Separate `fire-spread-simulator/` package/container owns its deterministic engine, input preparation and output validation. It does not import Runtime prompts or UI code. |
| Coordination | `traffic_simulator/persistence.py`, `store.py`; `backend/evacuation/service.py` | Reuse transactions, idempotency, admission and lease/publication semantics through narrow shared helpers. Introduce a distinct job kind and explicit queue routing before sending fire work. |
| Artifacts/cache | `Store.artifact`; `traffic_simulator/network_cache.py`; `deployment/infra/lib/artifact_cleanup.py` | Owned attempt-scoped JSON result/manifest artifacts, plus a bounded binary raster adapter and separate landscape-cache prefix. Preserve checksums, local/cloud parity and retention. |
| Browser presentation | `useAgentSubmission`, `WorkspaceMap`, `MapContainer`, `SimulationMap`; `shared/services/workspaceAccess.ts` | An isolated `features/fire-exposure/` service/hook/domain boundary, using `brokerJson` and generated types. Existing map renderers receive validated geometry and do not start jobs. |
| Contracts/release | `scripts/export_integration_contracts.py`; `deployment/settings.toml`, `installation.py`, CDK and existing deployer | Extend the exporter for the new capability without hand-editing generated files or registering additional SUMO agent tools. Declare settings and service bindings in the existing deployment flow. Verify native ForeFire/NetCDF compatibility in the worker image and target platform rather than assuming a published Python wheel matches this repository. |

### Concrete integration gaps to handle

1. **Geography:** `SimulationScenario` in `backend/evacuation/contracts.py` explicitly restricts latitude/longitude to California and inherits SUMO road/demand assumptions. Fire exposure must have its own globally valid coordinate model and separate enrichment availability. Broadening the SUMO class would also change traffic behavior; a fire assessment at another location is not an evacuation session there.
2. **Incident identity:** `create_mapdata_from_presorted` currently projects fire name, position and measurements without a stable provider incident ID. `incidentObservations` builds a presentation key from coordinates/name. Add a canonical provider identity when available and an immutable broker observation reference for every assessment. A local UI key or the evacuation workflow's scenario `incident_id` is not automatically an Esri/WFIGS incident ID. Match cross-feed perimeters using verified identifiers; ambiguous matches remain context.
3. **Weather authorization and transport:** `CoordinateGrounding.before` forces `include_weather=false` for fire-only tasks. Preserve that behavior. The assessment is a separate bounded enrichment operation: reuse a suitable saved DTN receipt, or invoke the existing DTN Lambda adapter through a configured ARN and narrowly scoped IAM permission, following the existing geocoder invocation pattern. Its credentials stay inside the DTN adapter. This requires a deployment binding and weather-call budget; that worker-to-adapter transport does not exist today.
4. **Queue routing:** both `Store.send_outbox` and `outbox_dispatcher.py` send to one configured SUMO queue. A second worker cannot safely share it and select messages afterward. Add fixed, validated job-kind routing to a dedicated spread queue in both dispatch paths, preserving the transactional outbox and scheduled repair. Reject unknown kinds without clearing pending work, and filter local worker selection by kind too.
5. **Artifact format:** `Store.artifact` accepts five fixed names and writes gzip JSON. It is not a GeoTIFF/NetCDF uploader. Keep public result envelopes typed and add a bounded binary storage adapter for input rasters. Shared public landscape cache and visitor-owned assessment outputs have different retention rules; copy/freeze the inputs a saved run needs so cache expiry cannot erase its evidence.
6. **Operational defaults:** D23 declares budgets, limits, timeouts, cache lifetime, task sizes and concurrency in `deployment/settings.toml`, with validated deployment/local wiring. The plan's initial one-worker configuration and two-hour maximum belong there, not in scattered module defaults. Feature-disabled behavior must leave investigation usable.
7. **Completion:** the main investigation UI currently applies map data through the successful `onComplete` callback; Guided investigations also retain progress observations. Do not assume that general chat already exposes a durable partial-result API. Add a separate assessment reference/status contract after saved observation completion, without holding the AG-UI response open for landscape acquisition. A completed chat answer does not certify a completed or valid spread run.

No new agent is required for the first version. The modeler produces deterministic exposure geometry, the broker validates and retains it, and the UI renders it. For an optional worker-backed simulation, the broker/service admits a job and the worker produces saved evidence. Explanatory reasoning consumes bounded evidence rather than receiving unrestricted calculation or worker control.

## Inputs by display mode

| Input | Use in this demo | Boundary or assumption |
|---|---|---|
| Existing Esri fire feed | Select the incident and obtain its reported point, attributes and source time. Reuse the existing WFIGS perimeter integration when a perimeter can be matched. | A reported perimeter is an observed footprint, not proof that its entire edge is actively advancing. No matched perimeter means point-centered exposure. A point-ignition scenario applies only to optional timed simulation and must be labeled separately. |
| Existing DTN weather | Current wind speed and direction drive directional exposure geometry. Show temperature and humidity as context. An optional timed scenario can explicitly hold the selected wind constant. | Forecast periods currently contain six-hour aggregates; they are not hourly wind trajectories. Relative humidity is not fuel moisture. |
| Optional public landscape | A later ForeFire enhancement requests fuel and elevation for the focused incident through LFPS where coverage exists. ESA WorldCover/Copernicus can instead supply optional global context. | The baseline requires no raster. LANDFIRE codes require a checked conversion to engine parameters; WorldCover classes are not fuel models. Existing federal land ownership polygons are not fuel data. |
| Optional fuel-moisture preset | A documented preset supports the Pyrothermel homogeneous-fuel scenario or a later full engine. | These are assumptions, not measurements inferred directly from DTN humidity. Show fuel, moisture and slope assumptions beside timed output. |
| Optional run horizon | A bounded deterministic scenario, with horizon selected after calculator checks. | Keep current wind fixed and label that assumption. A requested duration does not establish a validated forecast horizon. |

Only the optional landscape-aware mode derives a simulation area and raster request from the reported incident geometry plus a configurable margin. Its cell-count, byte, duration and concurrency limits must report unsupported coverage rather than silently clipping a large observed fire. The chat search radius discovers incidents; it is not a raster download size or simulation domain. The baseline needs no raster acquisition.

Sources: [LANDFIRE API access and asynchronous downloads](https://www.landfire.gov/faqs), [LFPS public task and input contract](https://lfps.usgs.gov/arcgis/rest/services/LandfireProductService/GPServer/LandfireProductService), [LANDFIRE landscape products](https://www.landfire.gov/fuel/landscape), [ForeFire landscape and units](https://forefire.readthedocs.io/en/latest/user_guide/landscape_file.html), [ForeFire companion fuel/model library](https://github.com/forefireAPI/wildfire_ROS_models).

## Optional later landscape simulator

This section preserves the architecture review for a possible ForeFire enhancement. It is not the baseline delivery plan. Pyrothermel can be evaluated before this larger raster/worker integration.

### One simulator module

Add `fire-spread-simulator/` alongside the existing traffic and firetruck modules. It owns on-demand landscape acquisition/cache, conversion, the fuel table, ForeFire execution and output geometry. Run the engine as a separate batch process inside its Linux container, behind a small Python job wrapper. The browser and agents do not invoke the engine interpreter directly.

For a cache miss, submit a bounded LFPS job using the incident's WGS84 west/south/east/north bounds and configured product layers. Poll its status with backoff, download the completed raster bundle, then convert it into ForeFire's NetCDF input and verified fuel table. LFPS documents optional output projection and a resolution in multiples of 30 meters. Select compatible layers from its catalog; do not guess release-specific layer identifiers. The LFPS form requires a contact email, which belongs in deployment configuration rather than in the chat flow.

Cache the raw and converted landscape in the existing artifact store, keyed by covered extent, product version, projection, resolution and converter/fuel-table version. Reuse a cached landscape only when it fully covers the requested incident domain; download missing coverage automatically. Fresh incident geometry and weather still belong to a new run. Deduplicate identical in-flight landscape requests so repeated chat questions do not submit duplicate provider jobs.

All grids must share extent, resolution and projection; choose a suitable projected CRS in meters from the actual incident coordinates, then transform output to WGS84 GeoJSON for the maps. Fuel classes need nearest-neighbor treatment during resampling, and missing data must remain missing. GDAL/Rasterio and NetCDF libraries can perform this conversion without an ArcGIS desktop license.

LFPS is a public HTTP data service hosted on ArcGIS; consuming it does not require purchasing commercial desktop simulation software. Its asynchronous design and published maintenance window mean uncached simulations cannot be promised as instant. Provider turnaround, failure handling and geographic coverage have not been benchmarked in this repository.

Convert DTN speed from mph to m/s and normalize meteorological “wind from” into the engine's required wind components. Verify the engine's wind-height convention; record any fixed wind-reduction assumption rather than silently treating reported wind as midflame wind. Check the fuel table against the model's actual required columns and units. The documentation names a semicolon-delimited fuel CSV, not a direct LANDFIRE import.

Capture the reported initial geometry separately from the simulated extensions. A matched perimeter must be initialized with the engine's supported front representation and tested; point ignition provides a simpler first prototype.

Each run freezes its incident, initial geometry, weather, landscape version, fuel mapping, moisture preset, engine version and duration. Export three cumulative footprints with elapsed time, plus the supported domain and run metadata. No ensemble, GPU, model training or coupled weather simulator is needed.

### Existing API and one ECS worker

Use the existing workspace API for a bounded start/status/result interface. Reuse the repository's job/artifact coordination pattern, with a dedicated fire-spread queue consumed by one CPU ECS worker and results stored in the existing artifact store. Keep concurrency at one initially; measure CPU/memory and run duration before choosing its task size.

The API resolves the requested owned observation reference, using saved Esri records and any suitable existing DTN receipt. If assessment weather is missing, its input-preparation step invokes the existing DTN Lambda adapter using the new configured IAM binding, not a new weather implementation or a model turn. The worker also accesses the public LFPS service for landscape acquisition; it receives no Esri/DTN credentials. Freeze the final input bundle, including acquired landscape and weather identity, before engine execution.

Use the existing workspace ownership checks and the established run/attempt separation so a failed or retried run cannot overwrite another result. Request the initial assessment once from a new chat's saved fire receipt, with an idempotency key and admission checks; an additional assessment starts only through a new chat or explicit assessment action. Saved-answer selection, marker focus, navigation, reload and time-slider movement only read results. Link each assessment to owner, conversation/request, immutable source observation and engine-input identity, so an old Seattle response cannot replace a new location's result.

ECS hosts the simulator; it does not supply a model license. There are **no new simulation-software or landscape-data subscription fees** in this plan. Existing Esri/DTN arrangements and AWS compute, storage, queue and data-transfer charges still apply. This is a free engine, not a promise of zero operating cost.

### Existing maps

Add one shared result contract and simple adapters for the existing ArcGIS main map and MapLibre evacuation map. Render server-produced GeoJSON, the observed perimeter and the wind arrow. The maps do not duplicate the spread calculation.

Distinguish original footprint from simulated extension. The time control should say **Minutes after scenario start**, especially for recorded or old perimeters. It must not imply that an old observed perimeter was automatically advanced to the current time.

Show missing/stale inputs and boundary clipping directly. A run reaching the landscape boundary has incomplete coverage beyond that boundary. On provider failure or missing landscape coverage, retain reported fires and labeled proximity/weather context with **Spread simulation unavailable**. Do not extrapolate outside the domain or substitute circles while labeling them as simulation output.

For the first release, SUMO and firetruck behavior remain independent. Making simulated fire close roads or alter crew safety is a separate feature with different acceptance requirements.

## Delivery sequence

1. **Source-grounded inputs.** Preserve the actual fire point, provider identity/source time and owned observation reference. Reuse or fetch bounded DTN weather through the existing server-side adapter. Clarify incident selection/count/truncation rather than hiding a nearby requested incident in the current four-fire shortlist.
2. **Small deterministic modeler.** Generate labeled proximity and wind-shaped GeoJSON in the independently owned modeler package, using free geometry libraries. The private ECS HTTP service receives normalized inputs from the existing broker. Use globally valid coordinates, explicit wind units/direction, configurable demo extents and a calm/missing-weather mode. Generate the contract and keep SUMO geography/behavior separate.
3. **Map presentation.** Render the same result in the existing maps, with the observed footprint, wind arrow, translucent exposure bands, source/weather panel and hover explanations. Initial enrichment is bounded/idempotent after a new chat receipt; selection, navigation and reload only restore existing evidence.
4. **Optional timed scenarios.** Test Pyrothermel against reference calculations in the modeler's runtime, verify DTN wind-height conventions, and add a disclosed uniform-fuel/moisture/slope scenario only if its output improves the demo. Measure execution before changing the bounded HTTP interface into asynchronous work.
5. **Optional geographic enrichment.** Add automatic public land-cover/elevation context if it adds a visible benefit. Consider ForeFire/LFPS only if a terrain/fuel-aware scenario warrants their extra data and infrastructure work. Missing enrichment never blocks baseline drawing.

No further broad simulator catalog is needed before implementing the baseline. Remaining feasibility checks are concrete: exposure geometry at coordinate edge cases, DTN metadata, and native calculator compatibility/reference output if timed scenarios are selected. A fixed demo location or manually prepared incident is not the product workflow. The baseline is useful on its own and has no additional simulation-software license fee; existing Esri/DTN terms and AWS operating costs remain.

## Acceptance

Baseline acceptance covers incident selection/identity, coordinate-driven geometry, missing/stale weather, source times, read-only navigation, map/result isolation, ownership and generated contracts. Engine, raster, queue and timed-footprint checks below apply only when that optional capability is implemented.

- **Engine/input checks:** the official example runs; selected fuel classes have traceable parameter values; zero coordinates and unit conversions are handled; missing/unknown fuels do not silently become burnable.
- **Coordinate-driven geometry:** use locations in both hemispheres, zero latitude/longitude, high latitudes and either side of the antimeridian. Every exposure polygon remains linked to the actual fire point/perimeter, with consistent physical distances and valid Polygon/MultiPolygon output as needed. The chat search center is never substituted for fire coordinates.
- **Enrichment coverage:** locations with and without landscape coverage use the same exposure contract and presentation path. Incident extent controls supported landscape requests; unavailable enrichment preserves labeled exposure context. Configured resolution/cell budgets are enforced.
- **Acquisition/cache:** cold-cache submit/status/download/convert works; warm-cache reuse preserves product identity; concurrent identical requests deduplicate; failed downloads and provider maintenance preserve the reported-fire map.
- **Chat behavior:** no reported incident produces no invented spread; a new chat starts at most one assessment per idempotency key; source discovery count/truncation is shown accurately. Old source records are not described as confirmed current fire fronts.
- **Read-only navigation:** marker focus, saved-answer selection, view switching, undock and reload make no new provider/model/job calls. An explicit new assessment uses the selected owned source reference.
- **Controlled scenarios:** a flat uniform-fuel case has expected directional behavior; reversing wind reverses the preferred growth direction; a documented nonburnable barrier behaves as intended; outputs remain within their stated domain.
- **Map checks:** footprints align with the source landscape/incident, cumulative time ordering is valid, observed and simulated areas differ visually, and changing selections cannot display another incident's results.
- **Provenance:** live versus recorded inputs, source times, fixed wind, moisture assumptions and landscape version appear in the result. A stale perimeter is not described as a current front.
- **Integration checks:** cross-owner reads/starts are refused; queue dispatch reaches the correct worker; duplicate delivery, cancellation and lease loss cannot publish another attempt's artifacts; global/session admission includes spread work. Local/cloud storage and worker-selection rules agree. Export generated contracts after API changes and run the required focused and repository gates for the code that changes.
- **Demo sanity comparison:** compare one historical case with a later reported perimeter to find gross direction/scale/geometry errors. This is a sanity check, not evidence of calibrated forecast accuracy.

Current status: research and plan only. October 26 primary-source research checked Pyrothermel/Behave licenses and presets, published ellipse assumptions, ESA WorldCover access/license and Copernicus public elevation coverage. Existing geometry dependencies, risk generation and DTN field/unit mappings were read again; pyproj 3.8.0 and Shapely 2.1.2 imported successfully under the project's Python 3.14.6. New exposure behavior, calculator installation/reference runs, runtime measurement, ECS integration and map acceptance are **not run**.

Architecture review verification on October 23, 2026:

| Check | Status | Evidence / limit |
|---|---|---|
| `.venv/bin/python scripts/export_integration_contracts.py --check` | Passed | `Integration contracts verified.` Existing generated contracts match their authority. |
| Focused broker, public access, cloud persistence, store and outbox suites | Passed | 91 tests passed in 22.36 seconds. Suites: `backend/investigation/tests/test_agent_broker.py`, `test_public_access.py`, `test_cloud_persistence.py`, `traffic-simulator/tests/test_store.py`, `deployment/tests/test_outbox_dispatcher.py`. Two existing Starlette/httpx and AnyIO deprecation warnings remain. |
| Full quality/build/synth and browser acceptance | Not run | This revision changes planning documentation only; the focused tests examine the existing architecture. |
| ForeFire, on-demand landscape acquisition, new routes/queue and deployment | Not run | These are proposed work, not implemented or validated behavior. |

Passing the existing checks supports the current integration patterns; it does not validate the proposed spread feature.

## Repository evidence

- _ui/src/shared/domain/riskModel.ts (removed in this refactor)_: current distance-band geometry.
- `ui/src/features/map/utils/mapGraphicsLoader.ts`: existing fire/weather graphics and wind-direction handling.
- `ui/src/features/evacuation/components/SimulationMap.tsx`: existing perimeter presentation.
- `traffic-simulator/traffic_simulator/feeds.py`: WFIGS perimeter parsing and source freshness.
- `backend/gateway/weather/lambda_weather_dtn.py`: current DTN fields and six-hour forecast aggregations.
- `backend/`: existing browser-facing broker and simulation API patterns.
- `backend/gateway/wildfire/handler.py`: existing location/radius-based public U.S. fire query.
- `backend/runtime/agents/detection_agent/detection_agent.py`: named-place geocoding and source-grounded fire/weather lookup.
- `traffic-simulator/`: separate worker/module pattern.

The [code review and wider model assessment](wildfire-risk-model-assessment.md) contains the source findings. Incident selection in this implementation must address the four-fire shortlist and missing-containment/zero-acreage inconsistencies so chat intent and displayed source records agree.
