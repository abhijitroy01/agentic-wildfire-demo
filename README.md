# Wildfire Data Fabric / FireGuard

FireGUard demonstrates how agents can connect a reported wildfire location, current source data, evacuation/crew simulations and cited guidance. It combines a React UI, Strands/AgentCore agents, a FastAPI backend (`backend/`) and a SUMO traffic-simulator worker (`traffic-simulator/`); the former Wildfire Corridor Lab repository merged into this one on October 3, 2026.

Current architecture, capability boundaries and open work: [Current system](architecture/00-current-system.md), including its [known limits and backlog](architecture/00-current-system.md#known-limits-and-backlog). Deployment, operation and post-deployment tests: [Workspace operations](architecture/29-workspace-operations.md). The [documentation index](architecture/README.md) lists every guide.

## Disclaimer

This is the first public release (v0.1.0) of an art-of-the-possible demonstration of an agentic system. It is not an operational, emergency-management or life-safety system: do not use it for real evacuation, dispatch or firefighting decisions. Simulated, synthetic and illustrative values are labeled as such, and generated answers can be wrong.

Use it at your own risk. The software is provided "as is", without warranties or conditions of any kind; see sections 7 and 8 of the [Apache License 2.0](LICENSE). Deploying it creates billable AWS resources and calls third-party services under your own accounts, and you are responsible for their cost, security and terms of use. This is a personal project, not an official product of, or endorsed by, any company named in this repository.

## Experiences

- **Investigation:** resolve an address with Amazon Location, retrieve public wildfire and DTN weather data, and ask for official public guidance with source citations. The browser uses a same-origin broker and has no AWS/provider keys.
- **Evacuation & Crew Access:** one simulation workspace with Guided and Research modes. Guided starts from Radio Investigation or reference places, verifies the location, and investigates nearby fires, weather/forecast and cited response guidance alongside road preparation. Review the draft before submitting SUMO. Saved initial and simulation-informed plans remain separate. Both reference places use current inputs.
- **Fire exposure:** up to four reported fires in an Investigation answer get saved background assessments. Pyrothermel computes surface-fire arrival footprints for 15, 30 and 60 minutes from U.S. LANDFIRE fuel and elevation at each fire and measured DTN wind, with stated moisture and open-fuel assumptions. The reported point is treated as a demo ignition; footprints are illustrative scenarios, not incident forecasts, and do not change SUMO hazards. Missing weather, fuel or terrain data remains visible as unavailable. The earlier distance and wind context remains available as an optional map layer, off by default.
- **Map and questions:** nearby-fire and weather markers expose source measurements; undock the map to fill the viewport and restore with Escape. Compare baseline/coordinated/phased policies, seek replay, inspect resident and crew outcomes and source evidence, and prepare supported what-if drafts. Vehicle count and departure window are the exposed variant controls.
- **Research scenarios:** the former Traffic Lab is inside Evacuation & Crew Access. It preserves free-form U.S. coordinates and road corridors, demand, crews, hazard growth, seeds, optional TomTom speeds, saved runs and exports. Switching modes preserves scenarios and replay; another scenario can be started while a run continues. Two independent worker tasks execute whole runs concurrently, and each run can simulate three policies concurrently.
- **Response Tracker:** step 3 replays the selected stage-2 SUMO run and policy, with crew role labels and clearly labeled illustrative resource numbers. A separate standalone firetruck exercise, opened from step 3, demonstrates synthetic suppression stages, water/crew telemetry and reports from retained signed evidence. SUMO crew staging does not imply suppression.
- **Public guidance:** ten reviewed sections from FEMA Ready.gov, NPS and the Forest Service. [Collection specification](architecture/28-public-sop-collection.md) records sources, dates, checksums, isolation, deployment and maintenance. This is published guidance, not a local agency's adopted SOP or real incident orders.

## Architecture and deployment

The public application uses CloudFront, a private S3 origin, private ALB/VPC origin, WAF, a FastAPI broker and private ECS services (API, SUMO worker, radio, firetruck, fire risk modeler). The broker (`backend/`) issues opaque HttpOnly demo cookies, enforces ownership and signs purpose-specific request context. The deployed Runtime always uses the restricted public investigation toolset and verifies signed broker contexts for memory and specialist requests. Strands agents run in AgentCore Runtime behind IAM invocation. A Cognito-JWT-authorized AgentCore Gateway invokes bounded Lambda adapters with its IAM role: Amazon Location geocoding, the public wildfire feed, DTN weather, and the signed simulation adapter. Secrets stay server-side; the Runtime reads the Gateway client secret by its Secrets Manager ARN; the DTN Lambda reads its credentials by ARN. Agent and model IDs are configured explicitly at deployment.

Deploy with `bash deployment/deploy-all.sh` (check and plan) and `bash deployment/deploy-all.sh --apply` from this repository, configured by the git-ignored `deployment/installation.toml` and the checked-in `deployment/settings.toml`; [the operations guide](architecture/29-workspace-operations.md) has the complete procedure and the [deployer README](deployment/README.md) the quick start.

## Development and verification

Start with [Developer setup](architecture/12-developer-setup.md). `bash scripts/bootstrap-dev.sh` creates a Python 3.14 environment constrained by `requirements-lock.txt` and installs the UI and CDK apps from their npm lockfiles; `.venv/bin/python scripts/check_quality.py all` runs repository-wide lint, TypeScript, generated-contract checks and all active component suites. Do not put AWS credentials in Vite variables or JavaScript. The workspace API models are the source of the simulator contracts: `scripts/export_integration_contracts.py` generates `schemas/simulation/*.json`, the model-visible tool schemas (`backend/runtime/agents/simulation_agent/tool-inputs.json`) and UI contract types (`ui/src/features/evacuation/generated/contracts.ts`), and `--check` fails on drift. The Gateway transport schema is a fixed `payload`/`trusted_context` envelope declared in `backend/deployment/templates/nested/4-agentcore-resources.yaml`. Native Strands structured output and typed evidence validate transport/data shapes; they do not guarantee factual accuracy of generated prose.

Each release runs live radio/geocoding/weather, preset and radio simulation workflows, owned firetruck reports and browser restoration checks before publication. Container CVE remediation and simulation narrative accuracy remain open work. Fresh-account portability has automated coverage but has not been demonstrated by a second live installation.

## Interpretation

Evaluate resident evacuation and crew access together, including waiting, unresolved demand, exposure and crews that never arrive. Exiting the model is not verified safety; staging is not suppression; exposure is not a casualty estimate. Traffic speeds are current observations applied to matched roads, while demand, hazard growth and dispatch remain scenario assumptions. UI values use U.S. units and saved simulator artifacts retain documented SI units.

Social-media publishing, autonomous dispatch, contraflow, calibrated fire prediction and unrestricted report generation are not capabilities of this public demonstration. Camera detection is optional and off unless an installation configures a Delphire camera; the server broker then serves its latest image and detections, and the key stays server-side. No claim of zero hallucinations, guaranteed safety or production certification is made.

## License

Copyright 2025-2026 Abhijit Roy. Licensed under the [Apache License 2.0](LICENSE); see [NOTICE](NOTICE). Third-party software, data and services keep their own terms, listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
