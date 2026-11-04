# FireGUard backend API

FastAPI service behind the FireGUard CloudFront distribution (`/simulation-api/*`
→ `/api/*`). It is the only browser-facing backend: it owns sessions and
ownership checks, brokers AgentCore Runtime turns, the private radio and
firetruck services, and serves the simulation v1 API that the UI and the
Gateway simulation adapter use. SUMO work is queued for the
[traffic simulator](../traffic-simulator/README.md) worker.

There is no user sign-in. Deployed visitors are anonymous demo sessions (an
HttpOnly cookie from `POST /api/v1/demo/session`) that reach the API only through
the CloudFront workspace route while public access is open (`PUBLIC_DEMO_MODE=true`).
Any other deployed request gets 403 `WORKSPACE_CLOSED`. Local mode serves every
request as one local researcher.

| Module | Role |
|---|---|
| `app.py` | App assembly, security headers, `/health`; local mode starts the worker in-process and serves the interactive docs (`/docs`, `/redoc`, `/openapi.json`), which AWS mode turns off |
| `evacuation/` | Simulation v1 API: sessions, incidents, drafts, runs, variants, evidence (published contract) |
| `investigation/broker.py`, `evacuation/agent_broker.py`, `trusted_context.py` | AgentCore Runtime turns and investigations with signed service context (the Runtime takes actor, session and tools only from it) |
| `investigation/saved.py` | Saved incident investigations (`/api/v1/incidents/{id}/investigation*`): start, rejoin, cancel and the bounded planning context, independent of the browser connection |
| `runtime_client.py`, `leases.py`, `budgets.py` | The Runtime call every broker shares (pinned endpoint, AG-UI payload, deadline and byte budget, one sanitized failure, the daily agent budget) and the per-owner leases, committed before that budget is spent |
| `radio_broker.py`, `firetruck_broker.py` | Private radio WebSocket and firetruck/report brokers |
| `fire_risk_broker.py`, `fire_risk_jobs.py` | Source-bound terrain scenarios, immutable owner records and provider-free retained GET; a dedicated preparation queue renews leases independently of the completed chat |
| `fire_risk_transport.py` | Fixed DTN, LANDFIRE and private-model operations plus domain error normalization; uses the shared `traffic_simulator/transport_work.py` owned-process controller |
| `camera.py` | Read-only Delphire camera broker (`/api/v1/camera/latest`, `/images/{id}`): the configured device only, the modal's fields only, an image only when the latest records name it; shared reuse window, daily budget and back-off; 503 `CAMERA_NOT_CONFIGURED` without a device ID and key |
| `demo_access.py`, `request_limits.py`, `http_errors.py` | Workspace access (demo sessions per viewer IP, local developer or closed), request body size and read-time limits, error envelope with `Retry-After` (request rates are limited by the WAF) |
| `traffic_lab/` | Traffic Lab routes (`/api/v1/research/*`) for the same demo-session owners; shares the v1 active-job and radius limits and artifact reader; job views include the worker's per-phase `timings` (seconds); serves the saved example areas of `traffic_simulator.examples`, which the worker also pre-builds; not in the published contract |
| `security_events.py` | Security event log: one JSON line for each refused request (401, 403, 404, 429), new demo session and agent tool call, with the owner digest and edge request ID; never tokens, cookies, prompts or addresses |
| `dependencies.py`, `simulation_jobs.py`, `simulation_artifacts.py` | Explicit request storage, shared SUMO ownership/admission/discovery, and retained artifact readers |
| `api_contracts.py`, capability `contracts.py` modules | Pure canonical models imported by the offline contract exporter |
| `api_settings.py` | `FABRIC_PUBLIC_URL`, `PUBLIC_DEMO_MODE`, the API's operational limits and the optional camera binding; storage settings are read from `traffic_simulator.settings` |

Capability packages are assembled by `backend/app.py` into one API service. They do not import the
application entrypoint. Saved investigations receive owned context and evidence readers explicitly; Runtime and
Gateway deployment remain separate.

Configuration: `STORAGE_MODE` (`local` or `aws`) is required. AWS mode also requires
`FABRIC_PUBLIC_URL` and `PUBLIC_DEMO_MODE` (`true` opens anonymous access, `false` keeps
every workspace route closed). Local mode leaves `PUBLIC_DEMO_MODE` unset (closed) and serves
every request as one local researcher. The local launcher sets `STORAGE_MODE=local` unless it
is already set.

Operational limits come from `deployment/settings.toml` ([decision D23](../architecture/decisions.md)) as
upper-case environment variables, listed with their defaults, ranges and refusal codes in the
[settings table](../architecture/29-workspace-operations.md#operational-settings). AWS mode requires each
one, which the stack sets from the file, and stops at start-up naming one that is missing or outside its
range; local runs and tests use the file's values. Daily counters live in the jobs table and expire
through its TTL ([daily budgets and capacity limits](../architecture/29-workspace-operations.md#daily-budgets-and-capacity-limits)).

Optional service bindings are read when a request needs them, so a missing one
disables only that feature:
`SIMULATION_RUNTIME_ARN` (a full ARN; `settings.runtime_arn()` treats any other value as unset, for the
brokers, the Runtime client and `workspace-config` alike), `GEOCODER_FUNCTION_ARN`
(`settings.geocoder_function()`, used by `evacuation/locations.py`), `FIRETRUCK_SERVICE_URL` and
`FIRETRUCK_SERVICE_KEY` (`firetruck_broker.py`), `RADIO_SERVICE_URL` and
`BROADCASTIFY_PRIVATE_KEY` (`radio_broker.py`), `FIRE_RISK_SERVICE_URL` / `FIRE_RISK_SERVICE_KEY`
(`fire_risk_broker.py`, private modeler), `WEATHER_FUNCTION_ARN` (existing IAM-only DTN Lambda), and `SIMULATION_CONTEXT_SECRET_ARN`, or
`SIMULATION_CONTEXT_SECRET` in local runs (`trusted_context.py`; agent turns, investigations and
firetruck reports need it), and the camera's `CAMERA_API_BASE_URL`, `CAMERA_DEVICE_ID` and `CAMERA_ALIAS`
with its key, the `DELPHIRE_API_KEY` field of `PROVIDER_CREDENTIALS` in AWS mode or the `DELPHIRE_API_KEY`
variable in local runs (`camera.py`; a value that fails the deployer's rules keeps the camera off). The API reads that signing key again every five minutes, the longest
lifetime a signed context may claim, so a rotated key applies without a restart.
They are not module-level settings because the offline contract exporter
imports most of these modules without deployment settings.

After changing contract models, run `.venv/bin/python scripts/export_integration_contracts.py`
from the repository root and commit the regenerated files. See
[public demo security](../architecture/36-public-demo-security.md), the
[current system](../architecture/00-current-system.md) and
[data flow, ownership and memory](../architecture/04-data-flow-and-session-management.md).

```bash
.venv/bin/python backend/scripts/local_server.py   # http://127.0.0.1:8000
.venv/bin/python -m pytest backend/tests backend/investigation/tests backend/evacuation/tests backend/traffic_lab/tests
```

The image (`Dockerfile.api`, built from the repository root) contains no SUMO
binaries. Its verification target runs these tests with the worker verification
image's source-built SUMO, the contract `--check`, and the UI metric parity check.
