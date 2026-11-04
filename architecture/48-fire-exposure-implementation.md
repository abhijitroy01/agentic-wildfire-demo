# Fire exposure implementation

This guide describes the fire exposure feature: component ownership, request flow, what the map
means, and the design corrections that replaced the initial homogeneous model.

## Ownership and paths

The browser-facing FastAPI application assembles in `backend/app.py`. Investigation, Evacuation & Crew Access,
and Traffic Lab own their code and tests in `backend/investigation/`, `backend/evacuation/`, and
`backend/traffic_lab/`. Cross-capability access, limits, signing, persistence adapters and broker tests remain
directly under `backend/`. The prior workspace-api directory is removed; no compatibility facade remains.

The independent `fire-risk-modeler/` component owns calculations, geometry, native Pyrothermel integration,
private HTTP transport, its Dockerfile, launcher and tests. `backend/fire_risk_broker.py` owns visitor authority,
weather enrichment, immutable source/result records and read-only retrieval. `backend/fire_risk_jobs.py`
owns the dedicated renewable preparation lease and frozen inputs. The shared
`traffic-simulator/traffic_simulator/transport_work.py` controller makes blocking transport explicitly cancellable
through one spawned process; `backend/fire_risk_transport.py` owns the fixed fire-risk operations and domain errors.
`ui/src/features/fire-exposure/` owns evidence interpretation and timeline controls; the existing map feature
renders returned geometry. The existing radio and firetruck service interfaces are preserved.

## Data and request flow

1. A successful Investigation turn supplies tool-backed Esri fire observations.
2. The API resolves each observed fire point, preserving zero coordinates and source ID. A place named in chat
   controls discovery; it never becomes the model's ignition point.
3. Suitable retained DTN weather must have matching query coordinates, explicit units and a fresh timestamp.
   Otherwise one bounded IAM invocation calls the existing DTN Lambda for those fire coordinates.
4. One deterministic owned preparation job is committed with an outbox before any additional DTN or model work.
   The final chat snapshot contains its `fireExposureJobs` reference. Preparation proceeds in the existing API
   ECS tasks using its own encrypted SQS queue, renewable lease and cancellation fence.
5. The worker acquires aligned 30-metre numeric LANDFIRE fuel and elevation crops at each reported coordinate.
   Canonical products are LF2024 FBFM40 and LF2020 elevation, with the appropriate CONUS, Alaska or Hawaii CRS.
   Metadata, crop bytes, dimensions, hashes, measured wind and explicit moisture assumptions are sealed together.
   Only a truncated modeled domain expands the same products; a saved input is never replaced after corruption.
6. The stateless private ECS service computes `POST /v1/landscape/assessments`. Schema 2 receipts contain native
   Pyrothermel/Behave behavior, terrain derivatives, arrival footprints and the input manifest. An explicit
   model-capacity response retries the same sealed request using its `Retry-After`. Source errors remain errors.
7. `GET /api/v1/fire-exposure/jobs/{job_id}` and the existing assessment GET return owned saved evidence.
   Selection, navigation, markers, reload and timeline controls perform no provider/model work.
   POST cancellation terminates the local blocking transport and fences publication; an already executing
   stateless remote calculation may finish without publishing its result.

The modeler is one private Fargate service, reached through Cloud Map on port 8085. Only the API security group
can enter that port; its bearer key comes from Secrets Manager. It has no provider credentials, table/bucket
permissions, browser cookie or agent invocation. The API receives permission to invoke only the configured DTN
Lambda. Existing API and stateful CDK construct identities are retained.

## What the map means

New assessments use fuel-specific native directional spread rates and elevation-derived slope/aspect across
the grid, with nonburnable cells acting as barriers. Footprints retain their actual physical size; selecting a
small footprint zooms the map instead of enlarging the geometry. Historical distance/wind context remains
available as an explicitly selected layer, disabled by default.

The timed layer uses an explicit moisture preset, constant measured DTN wind, open fuels and the reported point
treated as demo ignition.
The default returned horizons are 15, 30 and 60 minutes. Measured calm is valid, and a valid zero-growth result
is shown honestly. Missing wind, landscape gaps, or a nonburnable ignition do not produce substitute circles,
flat elevation or relocated ignition. The UI can select only horizons returned by that
assessment; it does not invent future geometry. This scenario does not establish an active front, actual fuel,
spotting, crown fire, suppression, forecast certainty or safety outside its outline. Relative humidity remains
weather context and is never substituted for fuel moisture.

Geometry handles WGS84 coordinates, including antimeridian and polar boundaries. This release's location
and evacuation support is scoped to the U.S.; incident discovery and weather coverage remain provider
constraints. No fixed demo area or manual landscape download is required. The bounded feed still displays
at most four nearest reported incidents; it is not a complete inventory.

Pyrothermel is MIT licensed and wraps public-domain Behave; pyproj and Shapely are freely licensed with their
transitive license obligations retained. There is no commercial simulator subscription. ECS and existing
Esri/DTN services retain their operating costs and terms.

## Map presentation

The map displays the actual returned arrival footprints together up to the selected time, largest first.
Each horizon keeps its color across timeline changes, with explicit reached-by-minute legend labels.
Coincident returned footprints stay coincident and show a no-additional-growth explanation. Neither smoothing
nor distance buffers alter the saved polygons. The historical uniform-fuel scenario uses the same time labels.

`ui/src/features/map/services/mapContext.ts` owns the public visual context: OpenStreetMap and the USGS
3DEP elevation ImageServer's verified `Hillshade Multidirectional` raster function. Terrain shading is
requested through its supported anonymous `exportImage` operation; the custom ArcGIS dynamic layer avoids
unsupported raster-metadata probes. It inspects the returned image's alpha channel before reporting ready,
and cancels outstanding exports when its context is retired. Terrain shading is independent of the sealed
LANDFIRE inputs used for calculation. Native tile/image failures and empty pixel
responses remain visible; retry recreates those same providers and their view, without invoking another model.
The shading can be hidden without changing the assessment. These services retain their attribution.

The native map scale bar shows metric and imperial units. Selected terrain framing is capped at zoom 15,
manual zoom at 18, and the evidence card shows the returned model-cell resolution. Full provenance and
assumptions remain accessible through the closed `Model details` disclosure. Wind legend details are folded.
Source perimeters already present in retained observations draw as a separate dashed outline; no perimeter
is fetched or manufactured, and their source-record timestamp is not presented as an acquisition time.
The solver remains initialized from the reported point.

## Design corrections: causes and prevention

The reported road job failed during acquisition: public Overpass returned HTTP 502 and its alternate endpoint
timed out, before conversion or SUMO ran. A longer wait alone cannot fix an HTTP 502. Road preparation now resolves
dated Geofabrik U.S. state extracts from the actual study bounds and runs extraction in the existing SUMO
worker. The API returns a durable job immediately; progress, completion and cancellation use its saved identity.
Native Osmium preserves complete ways and restrictions. Corridor review uses the same seeded civilian and crew
demand that SUMO will use, preventing an isolated freeway segment from being presented as a usable corridor.
The selected source files, checksums, extracted geometry and network are retained together. An unavailable
source never selects a different provider or an older cached snapshot.
Linux validation also showed that closing a socket from another thread did not interrupt an idle body read.
The road operations therefore use the same owned-process controller: cancellation terminates and reaps the
process, while the parent keeps the source-cache lease, byte limits, hash verification and publication fence.

The earlier risk outlines came from a homogeneous fuel/wind calculation without a terrain grid.
Its weather enrichment waited only four seconds for a Lambda invocation that completed after about 6.4 seconds;
acquisition also shared the chat deadline. Slow but valid work could become a terminal unavailable assessment.
The new landscape path separates chat completion from preparation and uses real per-cell fuels
and elevation. Weather normalization also now accepts measured calm, preserves exact query coordinates and
isolates a malformed neighboring DTN response instead of aborting a valid batch.

Preparation freezes weather before numerical work, persists the sealed landscape input and renews its lease.
Navigation and polling do not restart acquisition. Cancellation or uncertain lease ownership interrupts the
owned transport and prevents result publication. A crash with an uncertain outstanding DTN invocation remains
explicitly uncertain rather than silently charging for another request. A modeler capacity response queues
the same sealed input using the service's `Retry-After`; source and scientific input failures stay visible.

All these paths are selected by the reported fire or explicitly submitted scenario coordinates. No incident,
city, or manual California landscape download is a production seed. U.S. coverage routing includes CONUS,
Alaska and Hawaii. Representative input checks establish those routes; they do not guarantee that every
provider cell, mapped road or external service is available at every location.

The U.S. scenario and build-efficiency changes preserve the capability structure above. Their
ownership, build design and remaining benchmark limits are documented in
[U.S. scenarios and build efficiency](49-us-scenarios-and-build-efficiency.md).

See the [approved implementation proposal](wildfire-risk-implementation-proposal.md), the
[component documentation](../fire-risk-modeler/README.md), [backend guide](../backend/README.md), and
[operations guide](29-workspace-operations.md) for contracts, settings, build and release procedures.
