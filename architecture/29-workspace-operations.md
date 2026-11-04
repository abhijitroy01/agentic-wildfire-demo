# FireGUard deployment and operations

This is the deployment and operations procedure for the complete workspace, run from
this repository: the workspace API (`backend/`), the SUMO worker
(`traffic-simulator/`), the private radio, firetruck and fire exposure services, the AgentCore
backend and the UI, whose Evacuation & Crew Access step has Guided and Research modes.
See [the deployer layout](../deployment/README.md#layout).

Open release work is listed under [known limits and backlog](00-current-system.md#known-limits-and-backlog).

## Deployment contract

One installation is identified by **AWS account + AWS region + deployment name**.
The optional public-guidance stack shares that identity and has its own retained data resources.
The installation file names all three (`[aws] account_id`, `region` and `name`)
plus the AWS CLI `profile`. A stable hash of
these inputs namespaces CloudFormation stacks, Cognito domains, AgentCore names
and service discovery. S3 buckets, IAM roles, tables and service resources use
CloudFormation-generated names where possible. Repeating the same identity
updates that installation; changing any identity creates an independent one.
Names must be 3–20 lowercase letters/digits/hyphens, beginning with a letter.

The script refuses to adopt untagged/unrelated stacks, deploy through failed or
in-progress stacks, or trust a deleted Runtime behind stale stack outputs.
It never deletes old stacks, recordings, knowledge bases or other applications.

This release supports commercial AWS regions with AgentCore Runtime/Gateway,
Bedrock models, CloudFront VPC origins, Fargate, and CodeBuild ARM availability.
It does not promise that every AWS region has every required service. Preflight
checks identity, stack ownership, AgentCore access, two standard availability
zones, model/KB metadata, templates, provider secrets and recordings before AWS
writes. Preflight also rejects a checkout whose generated simulator contracts
(`scripts/export_integration_contracts.py --check`) have drifted, and an
[image-scan policy](../deployment/image-scan-policy.json) that still lists exceptions
on or after its expiry date, before any build rather than at the image scan gate
([exception re-review](36-public-demo-security.md#exception-re-review)). Model metadata
access does not prove inference entitlement or available quota. Those are tested
by the deployed acceptance workflow.

## Prerequisites

Use macOS, Linux, or WSL; Python 3.14, Node 22.13+, AWS CLI v2 and `uv` are required.
Docker is not required on the operator's computer.

```bash
cd /path/to/agentic-wildfire-demo
bash scripts/bootstrap-dev.sh   # Python 3.14 .venv, locked UI and CDK dependencies
aws --profile YOUR_PROFILE sts get-caller-identity
```

The operator uses a deployment role/profile authorized for CloudFormation/CDK
bootstrap, IAM role creation and passing, ECS/VPC, S3/ECR, CodeBuild, Secrets
Manager, Cognito, Bedrock/AgentCore, WAF, CloudFront and the other resources in
the templates. Application task roles are separate and scoped. Organizational
SCPs, quotas, model access and region enablement still apply. The standard CDK
bootstrap is shared per account/region; it is not owned exclusively by this app
and must not be deleted as application cleanup.

`[aws] operator_role_arns` lists 1-10 full IAM role ARNs in `account_id`: the role
behind `[aws] profile` and any break-glass role. The bucket policies refuse object
access to every other principal outside the installation (D52), so preflight
requires the deploying role session to be one of them and reads each listed role
with `iam:GetRole` to confirm its exact ARN, path included, before any change.

Create a Secrets Manager **JSON secret in the selected account and region**
using the AWS console or an existing secure provisioning workflow. Required keys:

```json
{
  "DTN_CLIENT_ID": "<DTN client ID>",
  "DTN_CLIENT_SECRET": "<DTN client secret>",
  "TOMTOM_API_KEY": "<TomTom credential>",
  "NASA_FIRMS_API_KEY": "<optional FIRMS map key>",
  "DELPHIRE_API_KEY": "<optional Delphire camera key>"
}
```

`DELPHIRE_API_KEY` is required only with `[camera] device_id`; the check stops when only one of the two is set.

Forward geocoding needs no provider key. The deployment creates one Lambda with
`geo-places:Geocode` on the selected region’s `provider/default`. AgentCore Gateway
and the simulation API use that shared service through IAM; the API also resolves
radio addresses. Results are
requested with `IntendedUse=Storage`; stored scenario receipts identify Amazon
Location and retain its match score.

Put only the provider-input secret ARN in `[providers] secret_arn`. Values are read into memory and copied to
deployment-owned server secrets; they are never written to the UI, command-line
arguments, deployment receipts, or `.env`. Neither deployment configuration nor
an existing account ID is implicitly loaded from `.env`.

List one to ten prerecorded MP3/WAV/M4A/OGG/FLAC files (each at most 50 MiB) in
`[radio] recordings`. Use a local path or `s3://bucket/key`. The script captures the
original S3 version (or an ETag condition), writes a content-addressed copy to
the new private audio bucket, and leaves the original intact.

Set two model IDs or inference profile IDs available to this account/region in
`[models]`: `agent_model_id` for the investigation/simulation agents and radio
extraction, `report_model_id` for the report specialist. Choose them explicitly;
the deployer has no developer-account model default.
`[knowledge] public_guidance = "managed"` creates, ingests, verifies and binds the official public guidance collection in a separate retained stack. It uses private S3 and S3 Vectors with Titan Text Embeddings V2; the region must support both Bedrock Knowledge Bases and S3 Vectors. `"existing"` binds `public_knowledge_base_id`, a separate public KB conforming to the collection metadata contract. An installation whose own public-guidance stack exists uses `"managed"`, because `"existing"` and `"none"` never deploy that stack, so changes to it, such as the Documents bucket's D52 Deny, would not reach it; preflight refuses them. `"none"` leaves public SOP retrieval unavailable, with no private fallback. The installation file keeps the choice for later deployments. See [PUBLIC_SOP_COLLECTION.md](28-public-sop-collection.md) for sources, refresh, isolation and verification.

The optional `[camera]` section names one Delphire camera that the public demo may show through the workspace
API broker: `api_base_url` (an `https://` origin with only a host name, `https://wildfireai.com`), `device_id`
(`""` keeps the camera off) and `camera_alias` (`camera_a`, `camera_b`, `infrared_camera_a`, `infrared_camera_b`
or `test_camera_a`). An installation file without the section deploys with the camera off. The key never goes in
the file: it is `DELPHIRE_API_KEY` in the provider-input secret. Map popups offer "View Camera Feed" only while
`/simulation-api/v1/workspace-config` reports `camera_configured`; see
[Turn the camera on later](#turn-the-camera-on-later).

## One entry point

Deployment reads two files and takes no other options:

- `deployment/installation.toml` (git-ignored, one per installation) holds the
  identity and choices of the installation. Copy
  `deployment/installation.example.toml`, which documents every field.
- `deployment/settings.toml` (checked in) holds the shared operational settings:
  limits, budgets, rates, sizes, TTLs, concurrency and task sizes. An
  installation can override any of them in the optional `[settings]` table of
  its installation file, so local changes never conflict with `git pull`.

```bash
cp deployment/installation.example.toml deployment/installation.toml   # once, then edit it
bash deployment/deploy-all.sh            # check and plan; no AWS changes
bash deployment/deploy-all.sh --apply    # deploy or update
```

An optional first argument selects another installation file, relative to the
repository root, for example `deployment/installations/staging.toml` (also
git-ignored). Before any AWS call the deployer validates both files: an unknown
section or key is an error, a missing field is named with a pointer to the
example, and every setting is checked against its type and range in
`settings.toml`, plus the Fargate CPU and memory combinations. It refuses an
installation file that git tracks, and the `[aws] profile` credentials must
belong to `[aws] account_id`.

The check prints and saves a plan: the installation identity, every effective
setting that differs from `settings.toml` with the file it came from, and what
`--apply` will do. `--apply` bootstraps CDK when the Region has no `CDKToolkit`
stack and says so, resumes an interrupted release that was built from the same
source, and otherwise starts a new release. The launcher runs
`.venv/bin/python -m deployment.deploy_workspace` from the repository root;
`WILDFIRE_DEPLOY_PYTHON` may select another prepared interpreter (a path, not a
credential).

`[aws] environment` (`development`, `staging` or `production`) sets the backend
`Environment` parameter, which also selects the Lambda package keys and
guardrail names, so an installation keeps the value it was created with; the
deployer refuses a different value for an existing backend. The live
installation uses `production`.

Every stack update is a CloudFormation change set, and `deployment/change_sets.py`
checks each one before executing it:

- **Hard stops (no override).** The deployer stops if the change set would
  replace or delete a stateful resource: a DynamoDB table, S3 bucket, ECR
  repository, secret, KMS key, Cognito user pool or client, log group, or
  knowledge base (including the vector store and data source of the public
  collection). Backend change sets include their nested stacks, so the same
  check covers the nested templates. A change set lists a removed or replaced
  nested stack as a single entry, so the deployer lists that stack's resources
  and stops if any of them is stateful, or if it cannot list them.
- **Review stops.** The deployer also stops before an in-place change to a
  secret's `KmsKeyId`, `Name`, `SecretString`, `GenerateSecretString` or
  `ReplicaRegions`, or to a KMS key's `KeyPolicy` or `Enabled`. It also stops
  before any operation of `GatewayClientSecretCopy`, whose Create and Update
  write the Gateway client secret. The exception is a value the change set shows
  as `{{changeSet:KNOWN_AFTER_APPLY}}`, which is a nested-stack parameter fed by
  a sibling stack's output. `deployment/change_proof.py` lets it through only if
  the property, the parent's parameter, the sibling's output and the output's
  source resource are the same in the deployed and the new templates, and the
  source is not replaced. Every backend release carries two such entries, the
  secret's `KmsKeyId` and the copy's `ServiceToken`; both are proven and run as
  before.
- **Acknowledgments.** A review stop prints the exact flag that approves each
  change: `--acknowledge 'STACK/RESOURCE=TYPE:SCOPE'`. RESOURCE is the logical ID
  path under the root stack, and SCOPE lists the changed properties, or `Add`,
  `Remove` or `Replace`. Use one flag per resource, together with `--apply`. An
  acknowledgment covers only that stack, resource, type and scope, and it never
  overrides a hard stop. For example, removing `NASA_FIRMS_API_KEY` from the
  backend's `APIKeysSecret` needs
  `--acknowledge '<backend stack>/InfrastructureStack/APIKeysSecret=AWS::SecretsManager::Secret:SecretString'`.
- **After execution** the deployer reads each changed stack's new events. It
  stops the release if CloudFormation changed a resource the review proved
  unchanged, ran `GatewayClientSecretCopy` without an acknowledgment, or changed
  a stateful resource the change set did not list. Only a change limited to
  properties can be proven unchanged. A change whose scope also names a policy
  or `Tags` may update the resource's record, as the Retain release does for
  `GatewayClientSecret`.

Nothing is executed after a stop; review the change set in the CloudFormation
console. Each change set's summary is saved in the release folder as
`<stack>-<phase>-changes.json`. It holds every change with both DescribeChangeSet
views of its details (never a before or after value), the review with its proofs
and acknowledgments, and the resources each stack actually changed. A rerun of
the same phase keeps the earlier summary as `<name>.1.json`.

All four root stacks have termination protection. The three CDK stacks set it in
code (D53), because the CDK CLI applies each stack's own value on every deploy.
After the backend's first change set the deployer turns it on for the backend
root stack; the nested stacks follow it. The result
is recorded in `backend-<phase>-changes.json`. Protection blocks `DeleteStack`
only: an update that removes a nested stack still deletes it, which the hard stop
above covers. To delete a root stack on purpose, first run
`aws cloudformation update-termination-protection --no-enable-termination-protection --stack-name <stack>`.

### Gateway client secret after a release

A release must leave the Runtime's copy of the Gateway client secret alone:
- DescribeSecret's `LastChangedDate`, version count and `KmsKeyId` stay the same.
- CloudTrail shows no Secrets Manager write to the secret during the release.
- `GatewayClientSecretCopy` never changes.
- The `GatewayClientSecret` CloudFormation record changes only in the release
  that adds its `DeletionPolicy` and `UpdateReplacePolicy` Retain.

To check, capture a baseline before `--apply`. Verify it at least 15 minutes after
the deploy, because CloudTrail can take that long to show a call:

```bash
.venv/bin/python -m deployment.acceptance.check_gateway_secret capture --output .runtime/gateway-secret-baseline.json
bash deployment/deploy-all.sh --apply
.venv/bin/python -m deployment.acceptance.check_gateway_secret verify \
  --baseline .runtime/gateway-secret-baseline.json --output .runtime/gateway-secret-check.json
```

The check reads metadata only, never the value. It compares the secret's key,
tags, policy, rotation, replication and every version (deprecated ones
included), and the KMS key's state and policy. It also lists each write found in
CloudTrail by ARN, by name and by event name. A finding (exit status 1) names
what changed, or each write with its caller: CloudFormation, the copy Lambda, or
someone outside CloudFormation, such as a rotation by hand
([credential rotation](45-credential-rotation-runbook.md)).

### Operational settings

Change a shared value in `deployment/settings.toml`, or one installation's value
in its `[settings]` table, then run `deploy-all.sh --apply`. Every deploy applies
exactly the effective values: they reach the application stack as CloudFormation
parameters (the key in CamelCase), which configure the WAF rules or become
container environment variables (the key in upper case), and the release folder
records them with their sources (`identity.json` and `deployment.json`). The
table below is generated from `settings.toml`; `.venv/bin/python -m deployment.installation --check`
fails when it is stale.

<!-- settings-table:start (generated from deployment/settings.toml by .venv/bin/python -m deployment.installation; do not edit) -->

| Setting | Default | Unit | Allowed | CloudFormation parameter | Description |
|---|---|---|---|---|---|
| `budgets.agent_requests_per_day` | 500 | requests | 0–100000 | `AgentRequestsPerDay` | AgentCore agent requests (Bedrock) all visitors may start per UTC day: agent turns, investigations, radio investigations and firetruck reports. Past it, 429 DAILY_LIMIT_REACHED. |
| `budgets.camera_calls_per_day` | 1000 | calls | 0–100000 | `CameraCallsPerDay` | Delphire camera calls (latest records, refreshes and images) all visitors may cause per UTC day; reused answers are free. Each API process fetches on its own, so steady demand costs up to 2 x 86400 / cache.camera_cache_seconds latest calls per API task per day (2,880 at 60 s) plus images. Past it, 429 DAILY_LIMIT_REACHED. |
| `budgets.fire_risk_assessments_per_day` | 1000 | assessments | 0–100000 | `FireRiskAssessmentsPerDay` | New fire and mode assessments all visitors may start per UTC day; retained reads consume no budget. |
| `budgets.fire_risk_weather_calls_per_day` | 1000 | calls | 0–100000 | `FireRiskWeatherCallsPerDay` | DTN fire-coordinate batch lookups all visitors may cause per UTC day; retained reads consume no budget. |
| `budgets.geocoding_requests_per_day` | 1000 | requests | 0–100000 | `GeocodingRequestsPerDay` | Amazon Location address lookups (new incidents) all visitors may start per UTC day. Past it, 429 DAILY_LIMIT_REACHED. |
| `budgets.tomtom_calls_per_day` | 1000 | calls | 0–100000 | `TomtomCallsPerDay` | TomTom traffic requests all visitors may cause per UTC day (runs and source lookups; reused cached readings are free). Past it, runs continue with traffic speeds marked unavailable. |
| `cache.cached_network_mib_per_worker` | 4096 | MiB | 1–20480 | `CachedNetworkMibPerWorker` | Scratch disk one worker's cached road networks may use (the worker task has 30 GiB). Past it, the least recently used are deleted, never the one a run is using. |
| `cache.cached_networks_per_worker` | 50 | networks | 1–1000 | `CachedNetworksPerWorker` | Road networks one worker keeps on its scratch disk for reuse. Past it, the least recently used are deleted, never the one a run is using. |
| `cache.camera_cache_seconds` | 60 | seconds | 10–600 | `CameraCacheSeconds` | How long one API process reuses the camera's latest records and image for every visitor before calling Delphire again; only the image ID in those records is served. |
| `cache.demo_areas_to_prebuild` | paradise | areas | paradise, malibu | `DemoAreasToPrebuild` | Traffic Lab example areas (/api/v1/research/examples/<name>) whose road networks a starting worker prepares while no job waits, one area at a time, so first runs there skip the network build. [] turns this off. |
| `cache.network_cache_days` | 30 | days | 1–365 | `NetworkCacheDays` | How long the artifacts bucket keeps a shared road network under network-cache/ before a lifecycle rule deletes it. The next run of that area builds it again from current OpenStreetMap data. |
| `cache.osm_index_cache_seconds` | 86400 | seconds | 3600–86400 | `OsmIndexCacheSeconds` | Reuse the publisher regional index for its daily publication interval; every new job resolves a dated immutable source and never substitutes an older snapshot on resolution failure. |
| `cache.osm_source_cache_mib_per_worker` | 8192 | MiB | 512–16384 | `OsmSourceCacheMibPerWorker` | Regional source cache budget within 30 GiB scratch, separate from the 4 GiB derived network cache; exact free disk is checked before downloading. |
| `cache.tomtom_cache_minutes` | 10 | minutes | 0–30 | `TomtomCacheMinutes` | How long one process reuses a TomTom reading for the same point, and how old a reading a run still applies (at least the response age above). 0 turns reuse off. |
| `capacity.active_jobs_all_visitors` | 5 | jobs | 1–100 | `ActiveJobsAllVisitors` | Queued, running and cancelling simulation jobs (runs and road-network preparations) across all visitors. Past it, 503 SIMULATOR_BUSY with Retry-After. |
| `capacity.active_jobs_per_session` | 2 | jobs | 1–10 | `ActiveJobsPerSession` | Queued, running and cancelling simulation jobs one demo session (or the local developer) may have at once. Past it, 429 ACTIVE_RUN_LIMIT. |
| `capacity.api_memory_gib` | 4 | GiB | 2–120 | `ApiMemoryGib` | Memory of the workspace API task. |
| `capacity.api_vcpu` | 1 | vCPU | 1–16 | `ApiVcpu` | vCPUs of the workspace API task (Fargate size with capacity.api_memory_gib). |
| `capacity.artifact_write_max_attempts` | 3 | attempts | 1–5 | `ArtifactWriteMaxAttempts` | Total attempts to persist frozen simulation artifacts without recapturing paid inputs. |
| `capacity.camera_image_max_kib` | 2048 | KiB | 64–10240 | `CameraImageMaxKib` | Largest camera image (WebP or PNG) the broker reads from Delphire and serves. A larger image gets 502 CAMERA_IMAGE_TOO_LARGE. |
| `capacity.camera_request_timeout_seconds` | 10 | seconds | 2–25 | `CameraRequestTimeoutSeconds` | Longest one Delphire camera call may take, from connecting to its last byte. A slower call gets 503 CAMERA_UNAVAILABLE. |
| `capacity.demo_session_days` | 7 | days | 1–30 | `DemoSessionDays` | Lifetime of a demo session cookie and of every record it creates (sessions, incidents, drafts, turns, jobs, receipts), which DynamoDB TTL then deletes. |
| `capacity.discoveries_per_api_process` | 8 | discoveries | 1–64 | `DiscoveriesPerApiProcess` | Legacy synchronous discovery process slots; new OSM discoveries use the SUMO worker queue and shared admission. |
| `capacity.dtn_max_attempts` | 2 | attempts | 1–3 | `DtnMaxAttempts` | Total attempts per DTN authentication or read, bounded by the aggregate deadline. |
| `capacity.dtn_operation_timeout_seconds` | 100 | seconds | 10–150 | `DtnOperationTimeoutSeconds` | Aggregate DTN adapter budget, clamped to Lambda remaining time; earlier point results are retained. |
| `capacity.dtn_request_timeout_seconds` | 15 | seconds | 1–30 | `DtnRequestTimeoutSeconds` | Maximum one DTN authentication or provider request wait within the aggregate budget. |
| `capacity.dtn_serialization_reserve_seconds` | 3 | seconds | 1–10 | `DtnSerializationReserveSeconds` | Lambda time reserved for safe DTN result serialization. |
| `capacity.fire_risk_calculation_timeout_seconds` | 8 | seconds | 1–10 | `FireRiskCalculationTimeoutSeconds` | Maximum private calculation wall time; a slower model returns unavailable. |
| `capacity.fire_risk_calculations_per_process` | 1 | calculations | 1–16 | `FireRiskCalculationsPerProcess` | One CPU-bound terrain calculation at a time on the default one-vCPU modeler, avoiding duplicate grid/native-state memory. Excess requests retain their sealed inputs and wait on explicit model admission. |
| `capacity.fire_risk_landscape_acquisition_timeout_seconds` | 0 | seconds | 0–3600 | `FireRiskLandscapeAcquisitionTimeoutSeconds` | Zero removes the total LANDFIRE acquisition wall deadline; finite connect/read idle waits, source bytes, numerical dimensions and renewed leases remain bounded. |
| `capacity.fire_risk_landscape_calculation_timeout_seconds` | 0 | seconds | 0–3600 | `FireRiskLandscapeCalculationTimeoutSeconds` | Zero removes application wall deadlines from bounded numerical work. The durable worker renews its lease; cell, directional and concurrency bounds constrain work. |
| `capacity.fire_risk_landscape_idle_timeout_seconds` | 30 | seconds | 1–120 | `FireRiskLandscapeIdleTimeoutSeconds` | Finite LANDFIRE connect and read idle wait; distinct from an allowed long acquisition. |
| `capacity.fire_risk_landscape_max_attempts` | 3 | attempts | 1–5 | `FireRiskLandscapeMaxAttempts` | Durably counted canonical landscape acquisition attempts per aligned crop window. |
| `capacity.fire_risk_landscape_request_max_kib` | 3072 | KiB | 1024–4096 | `FireRiskLandscapeRequestMaxKib` | Bounded sealed numerical request: at most 250000 cells with int16 fuel and float32 elevation, zlib/base64 encoded, plus manifest. |
| `capacity.fire_risk_landscape_source_max_mib` | 8 | MiB | 2–16 | `FireRiskLandscapeSourceMaxMib` | Maximum bytes of each numeric LANDFIRE crop response. The largest permitted 500 by 500 float32 crop has one MiB of samples; reserve bounded TIFF metadata overhead. |
| `capacity.fire_risk_max_fires_per_turn` | 4 | fires | 1–4 | `FireRiskMaxFiresPerTurn` | Maximum valid observed fires enriched after one completed chat turn. |
| `capacity.fire_risk_memory_gib` | 2 | GiB | 2–120 | `FireRiskMemoryGib` | Memory of the private fire risk modeler Fargate task. |
| `capacity.fire_risk_model_admission_seconds` | 120 | seconds | 10–600 | `FireRiskModelAdmissionSeconds` | Total calculator admission budget per sealed input, distinct from numerical execution time. |
| `capacity.fire_risk_perimeter_max_vertices` | 4096 | vertices | 32–16384 | `FireRiskPerimeterMaxVertices` | Maximum observed perimeter vertices accepted by the modeler. |
| `capacity.fire_risk_postprocessing_seconds` | 30 | seconds | 15–60 | `FireRiskPostprocessingSeconds` | Time reserved inside the investigation lease for final source receipts and durable risk outbox creation. Weather and landscape jobs run independently. |
| `capacity.fire_risk_request_body_max_kib` | 256 | KiB | 16–2048 | `FireRiskRequestBodyMaxKib` | Maximum normalized private model request body. |
| `capacity.fire_risk_response_max_kib` | 256 | KiB | 16–256 | `FireRiskResponseMaxKib` | Maximum private response retained in one owner-scoped assessment: 256 KiB accommodates the measured 198,183-byte maximum-grid result; the independent 350 KiB DynamoDB attribute-envelope guard still applies. |
| `capacity.fire_risk_retry_max_seconds` | 10 | seconds | 1–60 | `FireRiskRetryMaxSeconds` | Maximum terrain source or calculator admission recovery delay, including Retry-After. |
| `capacity.fire_risk_vcpu` | 1 | vCPU | 1–16 | `FireRiskVcpu` | vCPUs of the private fire risk modeler Fargate task. |
| `capacity.fire_risk_weather_timeout_seconds` | 330 | seconds | 330–900 | `FireRiskWeatherTimeoutSeconds` | Background DTN invocation wait: the existing Lambda maximum execution of 300 seconds plus one 30-second adapter socket interval for cold-start/delivery. Independent of the chat lease. |
| `capacity.firetruck_active_runs_per_session` | 2 | runs | 1–10 | `FiretruckActiveRunsPerSession` | Firetruck runs one demo session (or the local developer) may have starting or running at once. Past it, 429 RUN_LIMIT until one stops or reaches its lifetime. |
| `capacity.firetruck_evidence_events_max` | 500 | events | 50–5000 | `FiretruckEvidenceEventsMax` | Latest timeline events each running firetruck evidence snapshot carries; earlier events are counted, not stored, and a finished run's final snapshot keeps every event. |
| `capacity.firetruck_memory_gib` | 2 | GiB | 2–120 | `FiretruckMemoryGib` | Memory of the private firetruck service task. |
| `capacity.firetruck_run_lifetime_minutes` | 120 | minutes | 10–120 | `FiretruckRunLifetimeMinutes` | Longest a firetruck run lasts before the service stops it and saves its final evidence; the broker also stops counting an older run as active. |
| `capacity.firetruck_runs_per_session` | 100 | runs | 1–200 | `FiretruckRunsPerSession` | Firetruck runs one demo session may start in its lifetime. Past it, 429 RUN_LIMIT. |
| `capacity.firetruck_runs_per_task` | 50 | runs | 1–200 | `FiretruckRunsPerTask` | Firetruck runs one private firetruck service task simulates at once. Past it, a new run gets 429 FIRETRUCK_UNAVAILABLE (retryable). |
| `capacity.firetruck_stream_lifetime_minutes` | 15 | minutes | 1–60 | `FiretruckStreamLifetimeMinutes` | Longest one live firetruck update stream stays open, never past its demo session; the stream then closes. |
| `capacity.firetruck_streams_per_session` | 4 | streams | 1–20 | `FiretruckStreamsPerSession` | Live firetruck update streams (WebSockets) one demo session may have open at once. Past it, the new stream is closed with code 1008 (STREAM_LIMIT). |
| `capacity.firetruck_vcpu` | 1 | vCPU | 1–16 | `FiretruckVcpu` | vCPUs of the private firetruck service task (Fargate size with capacity.firetruck_memory_gib). |
| `capacity.investigations_per_api_process` | 4 | investigations | 1–32 | `InvestigationsPerApiProcess` | Saved background investigations executing per API process (two processes per API task). Includes cancelled calls still returning; excess requests get 503 INVESTIGATION_BUSY before billing or starting a task. |
| `capacity.radio_memory_gib` | 2 | GiB | 2–120 | `RadioMemoryGib` | Memory of the private radio service task. |
| `capacity.radio_vcpu` | 1 | vCPU | 1–16 | `RadioVcpu` | vCPUs of the private radio service task (Fargate size with capacity.radio_memory_gib). |
| `capacity.request_body_max_kib` | 160 | KiB | 64–1024 | `RequestBodyMaxKib` | Largest API request body. A larger body gets 413 BODY_TOO_LARGE before any model or provider sees it. |
| `capacity.request_body_seconds` | 20 | seconds | 5–120 | `RequestBodySeconds` | Time a client has to send an API request body. A slower body gets 408 REQUEST_TIMEOUT. |
| `capacity.worker_memory_gib` | 8 | GiB | 2–120 | `WorkerMemoryGib` | Memory of the SUMO worker task. |
| `capacity.worker_tasks` | 2 | tasks | 1–5 | `WorkerTasks` | Independent SUMO worker tasks consuming the shared queue. Each runs one job at a time; two tasks allow two scenarios or road preparations to execute concurrently without sharing mutable SUMO state. |
| `capacity.worker_vcpu` | 4 | vCPU | 1–16 | `WorkerVcpu` | vCPUs of the SUMO worker task (Fargate size with capacity.worker_memory_gib); each policy simulated at once uses one. |
| `rate_limits.demo_sessions_per_ip_per_day` | 20 | sessions | 1–100000 | `DemoSessionsPerIpPerDay` | New demo sessions one viewer IP may start per UTC day; each session has its own job limits. Past it, 429 DEMO_SESSION_LIMIT. |
| `rate_limits.requests_per_ip_per_5_minutes` | 600 | requests | 100–100000 | `RequestsPerIpPer5Minutes` | WAF rate rule on the API load balancer: requests one viewer IP may send in any 5-minute window before WAF blocks it. |
| `rate_limits.work_requests_per_ip_per_5_minutes` | 20 | requests | 10–100000 | `WorkRequestsPerIpPer5Minutes` | WAF rate rule for requests that start work (runs, variants, drafts, Traffic Lab runs and lookups, agent turns, radio streams, firetruck runs and reports): requests one viewer IP may send in any 5-minute window before WAF answers 429. |
| `simulation.caltrans_response_max_mb` | 20 | MB | 1–100 | `CaltransResponseMaxMb` | Largest Caltrans highway-service response one highway discovery request reads, in megabytes of 1,000,000 bytes. A larger response stops the discovery with a size error. |
| `simulation.fire_risk_inner_km` | 5.0 | km | 0.1–100 | `FireRiskInnerKm` | Inner proximity context band; an illustrative distance, not a predicted spread time. |
| `simulation.fire_risk_landscape_directional_radius` | 5 | cells | 1–5 | `FireRiskLandscapeDirectionalRadius` | Directional stencil radius, validated by native ellipse and spatial/angular convergence tests; crossed cells prevent jumping nonburnable barriers. |
| `simulation.fire_risk_landscape_initial_radius_m` | 1500.0 | metres | 300.0–7470.0 | `FireRiskLandscapeInitialRadiusM` | Initial crop centred on each reported fire, including a two-cell halo. Expand the same canonical products only if the modeled front reaches its domain edge. |
| `simulation.fire_risk_landscape_max_cells` | 250000 | cells | 10000–250000 | `FireRiskLandscapeMaxCells` | Maximum 30-metre aligned terrain/fuel cells, matching the pure model contract and bounded private request. |
| `simulation.fire_risk_max_wind_stretch` | 2.0 | ratio | 1.0–3.0 | `FireRiskMaxWindStretch` | Maximum illustrative downwind elongation of the context overlay. |
| `simulation.fire_risk_middle_km` | 15.0 | km | 0.1–150 | `FireRiskMiddleKm` | Middle proximity context band, strictly above the inner band. |
| `simulation.fire_risk_outer_km` | 25.0 | km | 0.1–200 | `FireRiskOuterKm` | Outer proximity context band, strictly above the middle band. |
| `simulation.fire_risk_ring_points` | 72 | points | 32–360 | `FireRiskRingPoints` | Vertices used in each generated geodesic ring. |
| `simulation.fire_risk_surface_first_minutes` | 15.0 | minutes | 1.0–120.0 | `FireRiskSurfaceFirstMinutes` | First surface-fire scenario horizon. |
| `simulation.fire_risk_surface_fuel_model` | 1 | Anderson model | 1–13 | `FireRiskSurfaceFuelModel` | Assumed homogeneous surface fuel model for the explicitly illustrative timed scenario. |
| `simulation.fire_risk_surface_moisture_profile` | moderate | profile | low, moderate, high | `FireRiskSurfaceMoistureProfile` | Assumed dead and live fuel moisture preset for the illustrative surface-fire scenario; humidity is never substituted for fuel moisture. |
| `simulation.fire_risk_surface_second_minutes` | 30.0 | minutes | 1.0–120.0 | `FireRiskSurfaceSecondMinutes` | Second surface-fire scenario horizon, strictly above the first. |
| `simulation.fire_risk_surface_third_minutes` | 60.0 | minutes | 1.0–120.0 | `FireRiskSurfaceThirdMinutes` | Third surface-fire scenario horizon, strictly above the second. |
| `simulation.fire_risk_weather_max_age_seconds` | 7200 | seconds | 60–86400 | `FireRiskWeatherMaxAgeSeconds` | Oldest measured DTN observation accepted for a new landscape scenario; stale readings never produce substitute geometry. |
| `simulation.fire_risk_weather_max_distance_km` | 2.0 | km | 0.0–25.0 | `FireRiskWeatherMaxDistanceKm` | Maximum separation between a retained weather query coordinate and an observed fire. |
| `simulation.fire_risk_wind_shape_scale` | 0.04 | per m/s | 0.0–1.0 | `FireRiskWindShapeScale` | Wind-informed context elongation per meter per second; not a calibrated spread rate. |
| `simulation.max_network_edges` | 14000 | edges | 1000–14000 | `MaxNetworkEdges` | Largest road network a run or road-network preparation accepts, counted as SUMO edges open to cars and longer than 15 m. A larger network fails a run; a preparation tries a smaller extent. |
| `simulation.max_study_radius_km` | 15.0 | km | 2.0–15.0 | `MaxStudyRadiusKm` | Largest study-area radius a visitor may simulate, discover or look up sources for. A larger radius gets 422 RADIUS_TOO_LARGE. |
| `simulation.osm_road_xml_max_mb` | 50 | MB | 1–100 | `OsmRoadXmlMaxMb` | Maximum bounded extracted OSM XML consumed by netconvert; distinct from the regional PBF source size. |
| `simulation.osm_source_http_timeout_seconds` | 0 | seconds | 0–3600 | `OsmSourceHttpTimeoutSeconds` | Zero removes application socket waits for immutable regional OSM delivery; renewable job lease, explicit cancellation, byte and disk bounds remain enforced. |
| `simulation.osm_source_max_attempts` | 3 | attempts | 1–5 | `OsmSourceMaxAttempts` | Total public OSM read attempts; frozen source identity and cancellation apply to every attempt. |
| `simulation.osm_source_max_mib` | 4096 | MiB | 512–16384 | `OsmSourceMaxMib` | Maximum exact regional PBF size accepted before download; dynamic regional selection avoids a whole-US download. |
| `simulation.osm_source_retry_max_seconds` | 10 | seconds | 1–60 | `OsmSourceRetryMaxSeconds` | Maximum public OSM transient recovery delay, including Retry-After. |
| `simulation.overpass_response_max_mb` | 50 | MB | 1–100 | `OverpassResponseMaxMb` | Largest OpenStreetMap road download from Overpass, in megabytes of 1,000,000 bytes. The download stops there and counts as a network that is too large. |
| `simulation.policy_parallelism` | 3 | policies | 1–3 | `PolicyParallelism` | Scenario policies one worker simulates at once, each in its own process with its own SUMO; 1 runs them one after another. Results are identical either way. |
| `simulation.road_preparation_timeout_seconds` | 0 | seconds | 0–3600 | `RoadPreparationTimeoutSeconds` | Zero removes the former 300-second subprocess wall cap. Worker cancellation and renewable leases control extraction and conversion. |
| `simulation.tomtom_request_concurrency` | 4 | requests | 1–12 | `TomtomRequestConcurrency` | TomTom flow requests one traffic-speed run sends at once; 1 sends them one after another. Each still spends the daily budget first, in sample-point order, and the saved traffic snapshot is the same either way. |
| `simulation.tomtom_requests_per_run` | 12 | requests | 1–50 | `TomtomRequestsPerRun` | TomTom flow readings one traffic-speed run captures, at sample points at least 1 km apart on the study area's highways. |
| `simulation.tomtom_response_max_age_seconds` | 300 | seconds | 60–900 | `TomtomResponseMaxAgeSeconds` | Oldest provider response date accepted when a TomTom reading is fetched; an older or future-dated response is rejected. |

<!-- settings-table:end -->

### Daily budgets and capacity limits

The public application has no sign-in, so the workspace API bounds what all
visitors together can spend (decision D20). Each daily budget is one counter
per UTC day in the jobs table (key `budget:<name>:<date>`, with a digest of the
viewer IP for per-network counts and never the address itself), raised by a
conditional update so concurrent API processes and workers never pass it; the
table's TTL deletes it a day after its date ends.

| Limit | Setting | When it is reached |
|---|---|---|
| TomTom calls (runs and source lookups) | `budgets.tomtom_calls_per_day` | Runs continue: a run with no matched reading saves its traffic snapshot as `unavailable` (`daily_budget_reached`) and keeps OSM speed limits; a source lookup shows TomTom as unavailable |
| AgentCore agent requests (agent turns, investigations, radio investigations, firetruck reports) | `budgets.agent_requests_per_day` | 429 `DAILY_LIMIT_REACHED`, with `Retry-After` until the next UTC day |
| Amazon Location lookups (new incidents) | `budgets.geocoding_requests_per_day` | 429 `DAILY_LIMIT_REACHED`, with `Retry-After` until the next UTC day |
| Delphire camera calls (latest records, refreshes and images) | `budgets.camera_calls_per_day` | 429 `DAILY_LIMIT_REACHED`, with `Retry-After` until the next UTC day. Each API process fetches on its own, so steady demand costs up to 2 × 86,400 / `cache.camera_cache_seconds` latest calls per API task per day (2,880 at 60 s) plus images, and requests for image IDs a process has not seen can add one refresh per process per window; size the budget at turn-on |
| New demo sessions per viewer IP | `rate_limits.demo_sessions_per_ip_per_day` | 429 `DEMO_SESSION_LIMIT`; a browser keeps using its existing session |
| Queued, running and cancelling jobs of all visitors | `capacity.active_jobs_all_visitors` | 503 `SIMULATOR_BUSY`, retryable, `Retry-After: 60` |
| Active jobs of one demo session | `capacity.active_jobs_per_session` | 429 `ACTIVE_RUN_LIMIT` |
| Study-area radius | `simulation.max_study_radius_km` | 422 `RADIUS_TOO_LARGE` before any provider call; the setting can only lower the published 15 km limit |
| Firetruck runs one demo session has starting or running | `capacity.firetruck_active_runs_per_session` | 429 `RUN_LIMIT` until one stops or is older than `capacity.firetruck_run_lifetime_minutes`, when the service stops it and keeps its final evidence |
| Firetruck runs one demo session starts in its lifetime | `capacity.firetruck_runs_per_session` | 429 `RUN_LIMIT` |
| Live firetruck update streams of one demo session | `capacity.firetruck_streams_per_session` | The new stream is closed with code 1008 (`STREAM_LIMIT`, logged as a refusal); each stream lasts at most `capacity.firetruck_stream_lifetime_minutes` |
| Firetruck runs one firetruck service task simulates | `capacity.firetruck_runs_per_task` | 429 `FIRETRUCK_UNAVAILABLE`, retryable |

The two job limits count only jobs created or updated within the last hour
(`STALE_JOB_SECONDS` in `backend/evacuation/service.py`). A worker updates the job
it runs every 30 seconds, so a job whose queue message was dead-lettered or
expired stops holding a place after an hour instead of until the TTL deletes it.

A process reuses a TomTom reading for the same request for
`cache.tomtom_cache_minutes`, so repeat runs of an area within that window
spend no calls; a run applies a reading up to that age (or
`simulation.tomtom_response_max_age_seconds`, if longer), and the run's saved
requests mark each reused reading. A traffic-speed run sends up to
`simulation.tomtom_request_concurrency` TomTom requests at once; keep it within
your TomTom plan's queries-per-second limit, because TomTom answers requests
past that limit with 429, which stops the run, and 1 sends them one at a time.
Every session, incident, draft, turn, job and
request receipt carries the TTL attribute for `capacity.demo_session_days`, the
demo cookie's lifetime. The API, worker and firetruck tasks stop at start-up, naming the
variable, when one of these values is missing or outside its range.

### Expo configuration

Visitors behind one shared address (expo Wi-Fi, an office NAT) share `rate_limits.demo_sessions_per_ip_per_day`, `rate_limits.requests_per_ip_per_5_minutes` and `rate_limits.work_requests_per_ip_per_5_minutes`; incognito windows, cleared cookies and new browsers each start a new session, while a reload reuses its cookie. For an event, raise those three in the installation's `[settings.rate_limits]` table and deploy; keep the work-start limit below the general one. Higher limits do not raise `budgets.agent_requests_per_day`, `capacity.active_jobs_all_visitors` or `capacity.investigations_per_api_process`, and busy work still gets a retryable answer. Bedrock model quotas are separate from these budgets: check the applied values before the event. Rehearse with `rehearse_expo` ([post-deployment tests](#post-deployment-tests)) and size budgets from measured traffic.

### Options that moved into the files

The deployer no longer takes identity or setting options. For an existing
installation, copy each option you used into the installation file. Passing a
removed option prints one line that names its replacement.

| Removed option | Replacement |
|---|---|
| `--profile` | `[aws] profile` |
| `--account` | `[aws] account_id` |
| `--region` | `[aws] region` |
| `--name` | `[aws] name` |
| `--environment` | `[aws] environment` |
| `--stage-only` | `[access] mode = "staging"` with `staging_allowed_cidrs` |
| `--model-id` | `[models] agent_model_id` |
| `--report-model-id` | `[models] report_model_id` |
| `--radio-audio` (repeated) | `[radio] recordings` |
| `--knowledge-base-id` | None: the Runtime has no private knowledge-base retrieval (W2.11) |
| `--with-public-sop` | `[knowledge] public_guidance = "managed"` |
| `--public-knowledge-base-id` | `[knowledge] public_guidance = "existing"` and `public_knowledge_base_id` |
| `--provider-secret-arn` | `[providers] secret_arn` |
| `--bootstrap` | Automatic on `--apply` when the `CDKToolkit` stack is missing |

Installations created before the Corridor Lab merge update in place with the
same `[aws] name`: stack and resource IDs are unchanged.

For private staging, set `[access] mode = "staging"` and list the CIDR ranges
allowed to reach it in `staging_allowed_cidrs`, then run `--apply`. This builds,
binds and publishes the full installation, and the API load balancer's WAF
blocks every request from any other address with 403 `NETWORK_NOT_ALLOWED`.
From the listed networks it is the normal application with anonymous demo
sessions. List IPv6 ranges too: CloudFront accepts IPv6 viewers, and a browser
on a dual-stack network often connects over IPv6. Write a single address as
`/32` (IPv4) or `/128` (IPv6); `/0` is refused. Both application passes apply
the allowlist, so an existing public installation is limited to the listed
networks from the first update on. The static UI files come from S3 without
WAF, so other addresses can still load the page, but none of its API calls.
The static smoke check can record the block from outside, but the canonical release
now also requires real functional acceptance before publication. Run it from an
allowlisted network; a WAF block cannot pass that gate. Exact-digest image scans and
explicit temporary dispositions precede activation. Resolve provider configuration
before setting `mode = "public"` and running
`--apply` again. That run builds a new captured release rather than reusing
unverified checkout changes or silently activating an old image.

## What the deployment does

`--apply` first runs the full local quality and production-browser gates. Set the
retained SUMO fixture and Playwright runtime described in [deployment prerequisites](../deployment/README.md).
After image scanning and backend binding, real radio, investigation/SUMO/updated-plan
and response/report acceptance must pass before UI publication and advancing the accepted
manifest. Before AWS writes, existing demos with public guidance also run
`deployment.acceptance.check_guidance_model`: bounded real-model cases for initial and
updated plans, the public SOP browser check's question, routing, remembered and new locations,
communication drafts and guardrail refusal, against the installation's published guardrail and collection, with provider
observations, translation, speech and storage stubbed from a retained fixture. It spends
model calls but submits no simulation. The investigation acceptance also checks the camera routes against `[camera]` in the
same demo session (the `camera` object of `acceptance-investigation.json`). Services update in place during these checks; use a demo maintenance window.
The Runtime uses two native endpoint slots: verify the inactive candidate before
promotion, then reuse an owned conversation to prove that execution changed while
Memory stayed intact. Promotion or functional acceptance failure restores and verifies
the prior Runtime binding. This rollback covers the Runtime binding, not other services.
See [the deployment guide](../deployment/README.md) for the binding contract.

1. Validates the installation file, the settings and the AWS target and inputs,
   then prints the plan. The default command stops here.
2. Installs frozen npm dependencies, builds the public UI, blanks configured
   `VITE_*` credentials and checks the captured assets for credential values.
3. Captures a bounded repository source bundle (`deployment/package_source.py`),
   the UI assets, backend sources, Lambda wheels (Python 3.14) and templates.
   All Lambda packages include the constrained AWS SDK, including the AgentCore
   control service models; deployment does not depend on the Lambda runtime's
   bundled SDK version. Recordings, `.runtime/` and local environment files are
   excluded. Every image installs with the root `requirements-lock.txt` as
   constraints: the traffic-simulator, API and fire-risk-modeler images read it from the
   repository-root build context, and CodeBuild copies it into the Runtime,
   radio and firetruck contexts. `deployment/check_lockfiles.py` rejects a
   component pin that disagrees with the lock.
   Synthesizes a release-specific CDK cloud assembly before provisioning.
4. Creates or updates the namespaced foundation: versioned private source bucket,
   immutable ECR repositories, build logs and regional audit trail. This trail
   adds a regional management-event copy; it does not replace an organization
   trail or its global-service event coverage.
5. Runs Linux verification and builds the traffic-simulator, API,
   radio, firetruck and fire-risk-modeler images in CodeBuild (`imageBuildCommands` in
   `deployment/infra/lib/stacks.ts`). CDK tests/synth, real-SUMO simulator tests,
   API tests (including generated-contract and UI metric parity checks)
   and firetruck/radio/modeler tests must pass before any image is pushed. The script
   checks the cloud UI lock against the captured `ui/package-lock.json` and
   records every resulting image digest. Builds use an exact S3 source version.
   The native SUMO, Xerces, PROJ and FFmpeg builds and their linkage gates are
   described in [supply chain and image gate](36-public-demo-security.md#supply-chain-and-image-gate).
   Source compilation uses a larger CodeBuild worker with a 60-minute limit.
6. Deploys private API, SUMO worker, radio, firetruck and fire exposure services. On upgrades,
   preserve the accepted native Runtime endpoint/version. A fresh installation
   keeps public mode closed until the Runtime is bound.
7. Copies the selected recordings and server provider credentials. Deploys the
   backend foundation and Gateway; builds and tests the ARM64 Runtime using an
   immutable release tag, then enables or advances the Runtime. The Runtime
   image must import the real entrypoint, complete startup, answer `/ping`,
   reject an invalid simulation context, and close its MCP HTTP client after a
   transport error in a network-disabled check before push. Live invocation
   acceptance is still required to verify model and Gateway permissions.
8. Stages the new version on the inactive endpoint and verifies its actual executing
   build, requested tools and completion. Seeds an owned conversation on the prior
   version, binds the API to the candidate endpoint/version, and checks the same
   conversation's Memory and scoped follow-ups after the rollout. All running API
   tasks must use the verified binding. Runtime promotion does not restart SUMO
   workers. After functional acceptance, refreshes services when credentials changed,
   publishes the captured UI, invalidates CloudFront, and checks HTTPS/configuration.

If the local controller loses its connection while polling the application or Runtime
build, AWS may still finish successfully. Both builds use one poller, which retries
transient connection errors within its deadline. To resume an interrupted release **before its Runtime
image build starts**, run `bash deployment/deploy-all.sh --apply` again: when the
newest release stopped in that phase and the checkout still produces the same
source bundle, the deployer resumes it automatically. It validates the
installation, source hash, captured cloud assembly, exact build/S3 version and
image tag, scans the captured UI again, and finishes without rebuilding the
service images. It preserves the original source/build receipts. Every release
captures the backend before starting the application build; a release without
that `backend-assets.zip` snapshot (an older deployer's) cannot resume.
Staging `runtime-source.zip` or completing backend preparation does not mean a
Runtime image build has started. Recovery replays preparation through a guarded
change set after the stack is stable, retaining the running Runtime image. AWS
stack/change-set status is authoritative: a local `executed: false` receipt written
before execution can be stale after a connection failure.

If only the deployment coordinator needed a fix, run
`bash deployment/deploy-all.sh --apply --resume-release RELEASE`. It uses the
captured assets, requires matching application and Runtime code, installation and
settings, and preserves the original preparation receipt in `resume-request.json`.
The existing build identity and scan gates still apply. This recovery rejects a
saved `backend-build-request.json` or final deployment receipt. Later-phase resume
remains backlog work; inspect phase-specific receipts before a new release.

Cross-stack bindings are explicit parameters passed by the script. There are
no CloudFormation exports/imports between the installation stacks. On upgrades,
the accepted Runtime endpoint remains selected through candidate verification.
DEFAULT may advance during activation; the broker invokes its explicit endpoint.
Application image updates precede backend updates, so an upgrade is not an
atomic multi-stack transaction. Keep adjacent versions compatible; use a
second installation name for a release requiring an incompatible migration.

### Installations from before October 7, 2026

The deployer's preflight (the default check and `--apply`) refuses two kinds of
stacks that earlier deployers created, before any AWS change:

- A backend stack without the `GeocodeServiceS3Version` parameter. Its geocode
  and wildfire Gateway targets still use the OpenAPI schemas that Lambda targets
  replaced on September 29, 2026.
- An application stack bound to a Runtime (`SimulationRuntimeArn` set) without
  `RuntimeEndpointAVersion` and `RuntimeEndpointBVersion`. It predates the
  `RELEASE_A` and `RELEASE_B` endpoints (October 7, 2026), so it still invokes
  DEFAULT, and its job outboxes may still need the `pending-outbox` index
  backfill.

This repository does not include the earlier deployer that migrated such
installations. Deploy a new installation with its own `[aws] name` instead,
move anything you need to keep, then retire the old installation.

A release that an older deployer captured without `backend-assets.zip` cannot
resume, with or without `--resume-release`; start a new release instead.

## Network and browser boundaries

[Public demo security](36-public-demo-security.md) describes the edge, the WAF rules,
the private services and the security event log. Internal service traffic is HTTP or
WebSocket inside the private VPC with rotatable service credentials, not end-to-end TLS.
`deployment/tests/test_edge_rules.py`
fails when a POST route is added without deciding whether it starts work. Each refused
request, new demo session and agent tool call is a JSON line with a `security_event`
field in the API task's log group (stream prefix `Api`). For example, this CloudWatch
Logs Insights query counts attempts to read other sessions' records:

```
filter security_event = "request_refused" and reason = "other_owner"
| stats count(*) by owner_digest, route
```

## State, evidence, and retries

All local receipts live under:

```
.runtime/deployments/ACCOUNT/REGION/NAME/
  plan.json
  deployment.lock
  latest.json
  releases/RELEASE/
    identity.json
    source.zip
    ui-assets.zip
    cdk.out/
    application-build-request.json
    images.json
    backend-assets.zip
    backend-build-request.json
    backend-prepare-changes.json
    backend-activate-changes.json
    backend-deployment.json
    acceptance-runtime-candidate.json
    acceptance-cutover.json
    deployment.json
    acceptance.json
    log-retention.json
```

Receipts contain identifiers and outputs, not secret values. `latest.json`
advances only after the final smoke checks pass.

Every installation log group keeps 30 days (D53). The stacks own the groups of
the tasks and functions they configure; after publication the deployer gives every
other Lambda, CodeBuild and AgentCore Runtime group that keeps its events forever a
30-day retention. `log-retention.json` lists the groups it set, the ones that
already had a retention (which keep it) and each failure, which never stops the
release: the next release retries it. A function removed later leaves its
stack-owned group (`Retain`, like every stateful type); the group is empty 30 days
later and may then be deleted by its exact name. Preserve release directories
and the source bucket for investigation and rollback. Do not run two releases
against the same identity concurrently. A local file lock prevents concurrent
local runs; CloudFormation status checks also reject in-progress stacks.
Use separate clones for concurrently deploying different installations because
npm installations and UI build directories are shared within a checkout.

A failed build does not deploy that build's application/Runtime image. A failed
stack update is left for diagnosis; the deployer does not automatically delete
it or force recovery. Inspect the saved build ID and stack events, resolve the
cause, wait for the stack to reach `CREATE_COMPLETE`, `UPDATE_COMPLETE`, or
`UPDATE_ROLLBACK_COMPLETE`, then rerun the same command. `DELETE_FAILED`,
`ROLLBACK_COMPLETE`, and `UPDATE_ROLLBACK_FAILED` require explicit operator
recovery or a new installation name. Do not blindly rerun destroy commands.

Firetruck evidence is stored in DynamoDB plus private S3 snapshots. A service
restart or failed heartbeat changes the run to **interrupted**, retaining the
last saved snapshot. It does not recreate the engine or claim completion.
Reports survive stopping the simulation. A report whose worker is lost becomes
interrupted after its deadline and can be requested again. Browser ownership
lasts `capacity.demo_session_days` (seven days by default); clearing cookies loses
that browser's access, not the stored evidence. Firetruck trajectories and
suppression telemetry are synthetic.

To roll back application images, use the prior release's captured `cdk.out`
with `cdk deploy WildfireLabApplication --exclusively --previous-parameters`.
Review the diff and preserve the current Runtime/repository/audio parameters.
To roll back the Runtime binding, use the application stack's captured assembly
and prior `SimulationRuntimeEndpoint`, preserving both endpoint-version parameters.
Verify endpoint readiness and every API task's binding as the deployer does, then
repeat conversation acceptance. Changing backend `RuntimeImageTag` alone advances
DEFAULT and does not change the broker's explicit endpoint.
Republish the prior captured `ui-assets.zip` through the same validated
publication function, then invalidate CloudFront and repeat acceptance.
Never retag an immutable image, erase evidence, or restore old browser keys.
A rollback can use only a release whose images are still among each repository's
newest 10 (D53); preflight's image check always keeps the previous accepted
release's, and stops a release whose push would expire an image in use, naming the
newer unused images that an operator may delete with `aws ecr batch-delete-image`.
A rollback to an assembly or backend templates captured before the release that
added D52 and D53 also removes that stack's bucket Deny statements, image lifecycle
rules, log-group bindings, trail selector and the DTN Lambda's read by ARN. It
returns the Runtime build role to a generated name that BuildSource's policy
denies, so Runtime builds fail until the next current release, and it turns
termination protection off: run
`aws cloudformation update-termination-protection --enable-termination-protection --stack-name <stack>`
on each rolled-back root. The next `deploy-all.sh --apply` from a current checkout
restores all of them.

## Required deployed acceptance

The automatic HTTPS/config checks are smoke tests, not functional acceptance.
Record the actual deployed URL, release ID, test times and run/report IDs.

Contract regeneration and the local checks are in [developer setup](12-developer-setup.md);
preflight, backend packaging and the API image build (`backend/Dockerfile.api`) reject contract drift.

The map, comparison and evidence components in `ui/src/features/evacuation/components/`
are the single copy used by both the Evacuation workspace and the Traffic Lab;
`npm --prefix ui run test:metrics` checks their metric rules, and the
API evidence tests (`backend/evacuation/tests/test_evidence.py`) compare
those TypeScript metrics with the Python evidence facts. Current-input scenarios now require background road validation and
explicit draft review before submission; existing unvalidated drafts must be
prepared again. Preparation uses the existing worker/queue/admission budget.
See [background road preparation](32-traffic-simulator-architecture.md#background-road-preparation) for states and acceptance.

| Workflow | Required result |
|---|---|
| Radio | Prerecorded audio plays, address is extracted, Amazon Location resolves coordinates, investigation and simulation use that resolved location |
| Simulation Lab | Paradise (2018) and Palisades, Los Angeles (2025) use newly collected current observations; source timestamps, assumptions and unavailable data remain visible |
| SUMO | Baseline and alternatives display both residents and crew outcomes, waiting, exposure, unresolved demand and nonarrivals; enlarged map and reload preserve the selected run |
| Response Tracker | Firetruck dispatch streams positions and stages; stop retains final evidence; reload restores it; report answers cite only the saved synthetic evidence |
| Isolation | A second browser cannot read, stop, stream or report on the first browser's identifiers |
| Network | No public task IPs or public service ALBs; direct S3 origin and unauthenticated Runtime access denied; no credentials in downloaded assets or browser storage |
| Recovery | Interrupt a test firetruck task and a SUMO worker; evidence/recovery status is truthful and no duplicate job is created |
| Public SOP | Clean ingestion; relevant official source retrieval; public answer and clickable source citations; no private KB fallback |
| Providers/model | Real Amazon Location, TomTom, configured weather, radio transcription and AgentCore inference succeed in the selected region; failures remain explicit |

To retire an installation, for example after moving the demo to its own account,
first inventory bucket
**versions**, retained Memory/KB dependencies, service consumers and old stack
resources; keeping an encrypted bucket while deleting its key is not preservation.
Then disable old traffic entry points and stop old compute, and verify the new URL
again. Only then remove reviewed obsolete infrastructure, deleting a nested stack
through its parent; a failed deletion does not mean its resources are gone.
Preserve recordings, KB content, retained Memory and unrelated applications.
The general deployer intentionally has no `--delete-old` switch.

## Post-deployment tests

`--apply` already runs live acceptance before publication. These commands stay available for bounded manual checks after a release. Each script opens an isolated demo session on the public URL, as a visitor's browser does, and stops when the workspace is not open to visitors (for example a staged installation). Without `--submit` it checks only the API and the preset contract.

```sh
.venv/bin/python -m deployment.acceptance.smoke_simulation_api --base-url "$INTEGRATED_URL"
.venv/bin/python -m deployment.acceptance.smoke_simulation_api --base-url "$INTEGRATED_URL" \
  --preset paradise-2018 --submit
.venv/bin/python -m deployment.acceptance.smoke_simulation_api --base-url "$INTEGRATED_URL" \
  --preset palisades-2025 --submit
.venv/bin/python -m deployment.acceptance.smoke_simulation_api --base-url "$INTEGRATED_URL" \
  --submit --parallel --output .runtime/verification/parallel-runs.json
.venv/bin/python -m deployment.acceptance.smoke_simulation_api --base-url "$INTEGRATED_URL" \
  --submit --investigate --radio --vehicles 200 --radius-km 2 \
  --output .runtime/verification/incident-workflow.json
.venv/bin/python -m deployment.acceptance.rehearse_expo --base-url "$INTEGRATED_URL" \
  --submit --users 20 --ai-users 2 --output .runtime/verification/expo-rehearsal.json
```

A submitted smoke test verifies Amazon Location resolution, current-input preparation, idempotent submission, completion and paginated evidence. It waits for the draft's road network and submits the ready draft's revision, as the browser does; `--wait-seconds` (1,800 by default) bounds preparation and the run together. It fails if preparation or the job fails or times out, saves a nonsecret receipt once a run is submitted, and cannot turn a missing TomTom snapshot into assumed traffic.

- `--investigate` also starts the saved fire/weather/guidance workflow alongside road preparation. It requires scenario-point weather, forecast, actual tool activity and official citations, complete policy accounting, crew plans and replay, then a cited updated plan with the exact retained bundle ID and hash and a deterministic summary of all five evidence views. Completed starts must rejoin their receipts without recomputation. `--radio` also requires a real fixed-recording detection with a location and transcript. Both make real model and provider calls, and `--investigate` cannot be combined with `--parallel`.
- `--parallel` submits two owned Research scenarios with different seeds and assumed traffic, observes both inside SUMO at once, then checks all three policies, accounting, crew plans, replay and frozen-input provenance in each. It needs at least two worker tasks and fails if overlap or completion is unverified.
- `rehearse_expo` creates fresh visitors on one network, checks reload and session reuse, bursts starts against the all-visitor job limit, polls from every visitor, runs two real cited investigations, checks foreign-owner denial and every policy ledger, and requires cancellation to finish before admission recovers. It saves job identities and policy summaries even when validation fails. It is a bounded HTTP rehearsal, not a venue-network, browser-rendering or process-loss test.

In the browser:

- Open Evacuation & Crew Access. Switch between Guided and Research; edits and active runs are kept, legacy saved research links open, and "Start another research scenario" does not cancel the first. Admission limits apply to both workflows together.
- Start each preset. Check the historical year, resolved point, current source capture times and actual TomTom road matches.
- Start Radio Investigation. Hear the recording, inspect the extracted address and Amazon Location receipt, and watch the initial fire/weather/forecast/guidance work run alongside road preparation, with actual tool events and unavailable sources.
- Inspect nearby-fire and weather markers and their measurements, fit observations, and undock and restore the map with Escape in all three workspaces.
- Review the initial plan and cited sources, submit the prepared draft, and inspect the simulation-informed plan with its run and frozen-input identity. Reopening missing plan work needs an explicit build action; an interruption offers retry or cancel.
- Switch policy, seek, navigate away and back, and refresh. The run ID must not change.
- Ask which residents remain unresolved and which crews never stage, and distinguish full-horizon timing from replay counts. Ask to inspect a policy at ten minutes: the typed action changes the replay without creating a job.
- Propose 600 vehicles or a 20-minute departure window, review the draft and run it. Compare the same input bundle and hash; current sources are not refetched for that controlled child.
- Ask for wind-driven fire spread or contraflow. The answer must explain that those controls are unsupported.
- Cancel a running job. It shows `cancelling` until acknowledged, then `cancelled`, without publishing an incomplete result.

## Traffic Lab access

There is no user sign-in. The Traffic Lab view (`ui/src/features/traffic-lab/`) is
part of the public app and calls the `/simulation-api/v1/research/*` routes
(`backend/traffic_lab/routes.py`) with the visitor's anonymous
demo session, like the v1 routes. They accept free-form coordinates and start SUMO
runs directly, share the session's and all visitors' active-job limits and the
study-radius limit with the v1 API (see "Daily budgets and capacity limits"), and
are absent from the published OpenAPI contract. A staged (`[access] mode = "staging"`)
installation serves them only to the networks in `staging_allowed_cidrs`. Run
creation, discovery and source lookups count toward the per-IP limit on
requests that start work, and source lookups spend the daily TomTom budget.
The application stack's Cognito user pool and app client (`UserPoolId` output) are
legacy and unused; they are retained so an update never deletes them, and can be
removed by hand.

The TomTom and FIRMS keys used by Traffic Lab runs and the optional Delphire camera key
come from the deployment-owned provider secret described above. After changing them outside the deployer,
force a new deployment of both ECS services so the tasks receive the new values.
Keys enable observations, not validated traffic assimilation. For every other
credential, follow the [credential rotation runbook](45-credential-rotation-runbook.md).

## Turn the camera on later

Releases ship with the camera off until an installation configures it.

1. Add `DELPHIRE_API_KEY` to the provider-input secret that `[providers] secret_arn` names, with the Secrets
   Manager console or your provisioning workflow. Never put the key in a file or a Vite variable.
2. In `deployment/installation.toml`, set `[camera] device_id` to the Delphire device and `camera_alias` to its
   camera (`camera_a`, `camera_b`, `infrared_camera_a`, `infrared_camera_b` or `test_camera_a`); keep
   `api_base_url = "https://wildfireai.com"`. Size `budgets.camera_calls_per_day` for the expected use (see the
   daily budgets table) with `[settings.budgets]` if the default is too small. Once the camera is on, every demo
   visitor receives the device ID, its position to 4 decimals (about 11 m) and its wind, humidity, pressure, TVOC
   and CO2 readings with the latest image, so choose a device whose location and readings may be public.
3. Run `bash deployment/deploy-all.sh`. The check stops and names the missing value when only one of the device
   ID and the key is set.
4. Run `bash deployment/deploy-all.sh --apply`. The deployer copies `DELPHIRE_API_KEY` into the application's
   provider secret (`configure_providers`) and, because the secret changed, restarts the API and worker.
5. Confirm that the release's `acceptance-investigation.json` has a `camera` check with `expected: configured` and
   `status: passed`, that `/simulation-api/v1/workspace-config` returns `"camera_configured": true`, and that a
   fire or weather popup offers 📷 View Camera Feed. Open the camera modal and confirm that its detections and
   sensors match Delphire's own view of that image; the `Api` task's log names how many records and claims the
   broker could not use ("Camera latest projection dropped ...").

If the release stops at the camera check (for example, Delphire redirects image requests or its images exceed
`capacity.camera_image_max_kib`), the deployer rolls back the Runtime binding as for any failed live check. The
receipt's codes say which rule refused an answer; for 503 `CAMERA_UNAVAILABLE`, the `Api` task's CloudWatch log
names the Delphire status or exception type: 401 means the key, 404 the device ID, a 3xx a redirect. To release
without the camera meanwhile, set `device_id = ""` and remove the key, then apply. To turn the camera off, do the
same. To replace the key, edit it in the secret and apply; keep the old key valid until the deploy ends.

## Cost and operation

Always-on baseline: one API task, `capacity.worker_tasks` SUMO workers (two by
default), the private radio, firetruck and fire exposure tasks (sizes in the settings table), ALB, one NAT gateway, CloudFront, WAF, Cognito, S3, DynamoDB, SQS, logs
and occasional CodeBuild. Charges continue while idle. Obtain an
account/region-specific estimate before deployment.

Each task size is a pair of settings, `capacity.<task>_vcpu` and
`capacity.<task>_memory_gib` for `api`, `worker`, `firetruck`, `radio` and `fire_risk`. To
resize a task, change its pair in `deployment/settings.toml` (or the
installation's `[settings]`) and run `deploy-all.sh --apply`. The check rejects a
pair Fargate cannot run and names the memory that vCPU count takes; ECS then
replaces the tasks from a new task definition revision, and no stored data
changes. A smaller worker costs less but runs fewer policies at once:
`simulation.policy_parallelism` (3 by default, one policy per vCPU) may not
exceed `capacity.worker_vcpu`, so lower both together.

The worker service does not autoscale. `capacity.worker_tasks` sets its task count
(two by default); each isolated task runs one job at a time, so whole runs can overlap.
Within a run it
simulates up to `simulation.policy_parallelism` policies at once, each in its
own process with its own SUMO; 1 runs them one after another, with identical
results (see [parallel runs and policies](32-traffic-simulator-architecture.md#parallel-runs-and-policies)). SQS
visibility is 180 seconds, extended every 30 seconds, and conditional DynamoDB
leases prevent concurrent duplicate execution. Interrupted workers leave
messages unacknowledged so another worker can retry; repeated infrastructure
deliveries reach the DLQ after five receives. CloudWatch DLQ/target-5xx alarms
have no notification recipient until one is supplied. One API, two workers and one
NAT are demo cost choices, not incident-grade availability.

Converted road networks are cached (see
[road-network cache](32-traffic-simulator-architecture.md#road-network-cache)). The
worker keeps up to `cache.cached_networks_per_worker` of them in
`cache.cached_network_mib_per_worker` of its scratch disk, and shares each one under
`network-cache/` in the artifacts bucket, where a lifecycle rule deletes it after
`cache.network_cache_days`. A new or restarted worker therefore copies a network
another worker built instead of downloading and converting it again; to rebuild
every network from current OpenStreetMap data sooner, lower
`cache.network_cache_days`. After a deploy, the new worker prepares the
`cache.demo_areas_to_prebuild` areas (the Paradise example by default) while no job
waits, so the first runs there skip the network build; set the list to `[]` to turn
this off. Changing any of these settings updates the worker task definition or the
bucket's lifecycle rule in place; no stored data is replaced.

While a run's road network is prepared, the worker fetches its public feeds in the
background, and a traffic-speed run sends up to `simulation.tomtom_request_concurrency`
TomTom requests at once (see
[overlapped preparation](32-traffic-simulator-architecture.md#overlapped-preparation)). A
run whose scenario and frozen inputs match a completed earlier run, such as a variant
with the same changes as an earlier one, completes from that run's result without
simulating and says so (see
[reusing identical runs](32-traffic-simulator-architecture.md#reusing-identical-runs)).
The jobs table holds one `reuse:<key>` record per simulated run, which expires with the
run's own record. Neither changes a result.

To see where a run's time goes, read its `timings` map: phase name to seconds,
for example `queue_wait`, `overpass_download`, `network_cache_hit` or
`network_cache_download`, `netconvert`, each `policy_<name>` (or `result_reuse`) and `total` (the phases are listed in
[run timings](32-traffic-simulator-architecture.md#run-timings)). The Traffic Lab
job views return it, and the worker's log group (stream prefix `Worker`) has one
`job_timings` line per job with the job ID, outcome and the same map, never URLs
or keys. This CloudWatch Logs Insights query lists the latest:

```
fields @timestamp, @message
| filter @message like /job_timings/
| sort @timestamp desc
| limit 20
```

Each demo area a starting worker prepares logs one `demo_area_ready` line with its
phase timings; filter on that text the same way.

## Local development

[Developer setup](12-developer-setup.md) covers the local API, UI, `STORAGE_MODE` and tests.
The release browser harness (`ui/scripts/check_evacuation_workspace.cjs`) checks the Traffic Lab view against a
mocked broker and the fixture recording, using the Playwright that `ui/package.json` pins.
