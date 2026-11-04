# U.S. scenario support and build efficiency

The requested scope is a U.S. demonstration. Seattle is an example, not a preset
boundary. New evacuation scenarios originate from a U.S. address, locality,
latitude/longitude or retained reported-fire assessment. They use a bounded free
OpenStreetMap road snapshot and the existing SUMO worker. No commercial modeler or
new runtime service is introduced.

## Capability ownership

| Path | Responsibility |
|---|---|
| `backend/evacuation/` | Incident location receipts, owned fire-assessment references, preparation drafts and explicit review/submission |
| `backend/traffic_lab/` | Existing road discovery and source-context API |
| `traffic-simulator/traffic_simulator/osm_corridors.py` | Bounded OSM snapshot parsing, source-way corridor identity and discovery |
| `traffic-simulator/traffic_simulator/network_preparation.py` | Convert the same snapshot, validate a usable corridor/gate, seal a concrete scenario |
| `ui/src/features/evacuation/` | Coordinate entry, location retry, source labels, retained replay and geodesic study geometry |
| `ui/src/features/fire-exposure/` | Saved fire exposure and timed surface-fire scenario display; reported-fire handoff |
| `deployment/container_build.py` | Fixed build groups, immutable native-artifact inputs and verification-before-publication gates |
| `deployment/component_images.py` | Captured component fingerprint, original successful-build proof and exact-digest reuse |
| `deployment/runtime_build.py` | Compatible upgrade compilation overlap and verified Runtime reuse/rejoin |

Domain tests remain beside their capability. Cross-capability API tests remain in
`backend/tests/`; SUMO tests remain in `traffic-simulator/tests/`; deployer tests
remain in `deployment/tests/`. The thin `backend/app.py`, existing gateway/runtime,
private ECS modeler and UI feature boundaries are preserved.

## Road-aware areas and nearby-fire workflows

An omitted road radius requests automatic study-area planning. An explicitly
requested radius or reference recording retains fixed-area semantics. The
worker keeps the incident coordinate unchanged and pins the canonical OSM
source set for the bounded maximum search envelope before examining smaller
areas. It expands only when a successfully examined area lacks drivable roads
or a routable civilian/crew approach. A mapped road outside the corridor's
interior boundary supplies the next radius from its measured distance.
Provider, source-integrity and native-tool errors remain failures.

Existing network-size limits may produce a smaller proposal for review. The
worker accepts an area only after validating modeled civilian exits and crew
vehicle approaches when crews are requested. Individual unresolved routes
remain in the demand ledger; selecting an area does not optimize away
nonarrival. The review shows the chosen extent and the road-staging distance
from the incident. A road staging point does not establish access to the fire,
apparatus passability or a safe evacuation destination.

If no usable network fits the configured limits, preparation completes with
an `unavailable` coverage receipt and no runnable network bundle. The receipt
records the examined areas, source identity and structural reason. Operational
failures use `failed` and retain their error code. Neither outcome submits SUMO
or silently replaces the road source.

Conditions receives immutable, owned fire observation IDs from the trusted
saved Investigation snapshot. These references are independent of guidance
success and fire-risk simulation. “Start workflow for this fire” submits that
reference to Evacuation, which copies the exact source point and provenance
into a new incident. It does not geocode the fire's name or reuse another
point's weather. Existing saved snapshots can receive references through the
explicit Investigation POST rejoin without another provider or model request.

The initial road preparation uses the existing REST draft endpoint directly;
the agent remains available for subsequent questions and explicit variants.
The session retains its latest draft ID for reload. Stable start intents retain
acknowledged session, incident and preparation IDs so a lost response can rejoin
the same request. Source cards and earlier workflows remain available when the
user changes fires. Generation checks also fence late activity and error
callbacks. Owner capacity and initial-investigation leases are shown as waiting
states with an explicit resume action; a new session does not bypass them.

## Location and simulation behavior

The shared forward geocoder retains its USA filter. Precise address receipts,
representative city/place receipts and explicitly supplied coordinates have
distinct provenance. A reported fire uses its retained owned assessment ID and
exact source coordinates, without a second geocoder or fire-model invocation.
Expired, missing or foreign-owned references fail explicitly.

Preparation queues an unresolved corridor/direction. The worker downloads one
bounded OSM snapshot, converts it with native `netconvert`, selects passenger
edges linked to the actual source ways and validates model gates. Only then does
the draft contain concrete corridor and direction values for review. Failures
remain visible; a failed location request preserves the selected recording.
Alaska, Hawaii and wrapped Aleutian geometry use geographic calculations rather
than California bounds or a fixed projection.

Synthetic traffic is the default. Optional provider speed limits require explicit
selection. Caltrans remains optional California context, with `out_of_coverage`
or `not_selected` status where applicable; it cannot block preparation elsewhere.
Weather/fire observations, assumed demand/hazard and SUMO outcomes remain
separate. Model exits are not verified safety, staging is not suppression, and
exposure is not casualties. Historical frozen California recordings retain their
original scenario fields and input-bundle semantics.

## Build claims and verified limits

1. **Native reuse:** SUMO, PROJ and Xerces are compiled into a minimal artifact;
   Pyrothermel wheels use a second artifact. Their pinned recipe/dependencies and
   platform produce immutable ECR tags. The builder pulls the identified digest
   and verifies its input label. Artifacts have separate 30-day retention, so
   compiler-cache publication cannot evict running service images. The full
   component verification target still runs. CodeBuild local Docker caching is
   best effort; a fresh builder can use the ECR artifacts independently.
2. **Affected components:** fingerprints include captured source, Dockerfiles,
   locks, shared contracts/settings and the reviewed CodeBuild recipe. Reuse
   requires an original successful build with the matching source version,
   group/tag, recipe, architecture and image digest. Repeated reuse carries that
   original proof. Missing proof or expired artifacts triggers a normal build.
   Current exact-digest vulnerability policy checks remain mandatory, including
   renewed scans for reused images and ECR's daily scan-window rules.
   ECS task definitions still use immutable release tags: unchanged image reuse
   skips compilation, but does not skip their normal service rollout.
3. **Concurrent service builds:** the existing project permits two jobs: SUMO/API,
   and modeler/firetruck/radio. SUMO verification precedes API verification.
   Independent groups start before waiting; a retry preserves started jobs and
   starts only missing ones. The account's CodeBuild concurrency quotas bound this;
   capacity is not a guarantee of immediate placement or faster execution.
4. **Runtime overlap:** compatible upgrades use the existing owned ARM project
   and captured source version before service rollout. First installation or an
   incompatible build binding uses the existing serial path. Compilation alone
   performs no CloudFormation changes or Runtime activation. Backend preparation,
   scans, inactive endpoint activation, promotion and acceptance remain ordered.
   Safe early interruptions rejoin the original build; preparation/activation
   evidence closes that recovery window.

Local regression checks prove those control paths. Concurrent AWS compute and
ECR storage retain their normal AWS charges.

ECR can return one identical manifest per retained release-tag alias. The shared
manifest validator admits multiple aliases only when every declared digest,
manifest byte string and supported media type agrees, and the manifest SHA-256
equals the recorded digest. Failures, missing manifests and conflicting aliases
cannot authorize a write. Runtime uses the same validator; original
successful-build/source-version proof, immutable tags and scan policy remain
required.

## Evidence boundaries

Live checks with free OpenStreetMap roads prove the workflow: real SUMO policies
complete with valid engine and vehicle accounting for synthetic demand. They do not
demonstrate evacuation effectiveness. Public OSM services are an external dependency
and can fail transiently (HTTP 502 or a read timeout); the UI keeps the verified
location and offers an explicit retry. Provider speeds are never fabricated: a run
without them reports provider traffic as `not_requested` with zero matched roads.

The native browser checks use mocked broker/provider responses and a retained
SUMO recording. They exercise U.S. input, exact reported-fire handoff, failed
resolution preserving selection and explicit scenario review. Local checks, AWS
image-build evidence and live published acceptance are separate release results;
publication is recorded only by the supported deployer.
