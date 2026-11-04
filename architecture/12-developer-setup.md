# Developer setup

Use the [workspace operations guide](29-workspace-operations.md) for
installation prerequisites, local dependencies, deployment inputs, recovery and
acceptance. The [deployment launcher](../deployment/README.md) is the supported
entry point for both fresh installations and updates.

## Repository layout and services

Everything that makes up the workspace lives in this repository; the former
Wildfire Corridor Lab repository is no longer used.

| Path | Container / role |
|---|---|
| `ui/` | React UI: Investigation, Evacuation & Crew Access, Traffic Lab (`src/features/traffic-lab/`), Response Tracker, Radio Investigation |
| `backend/runtime/` | AgentCore Runtime (Strands agents, AG-UI entrypoint), ARM64 image |
| `backend/gateway/` | Gateway Lambda tools (geocode, wildfire, weather, simulation adapter) |
| `backend/` | Workspace API image: simulation v1 API, agent/radio/firetruck brokers, demo sessions, Traffic Lab routes (`backend`) |
| `traffic-simulator/` | SUMO engine and job worker image (`traffic_simulator`): networks, demand, policies, feeds, traffic, job store |
| `firetruck-simulator/`, `broadcastify/` | Private firetruck and radio service images |
| `fire-risk-modeler/` | Private ECS exposure and Pyrothermel surface-fire calculator; local port 8085 |
| `backend/knowledge/public-sop/` | Reviewed public guidance collection |
| `deployment/` | Single deployer, CDK app (`deployment/infra/`), acceptance scripts and tests |
| `schemas/` | AG-UI schema and the generated simulation contracts (`schemas/simulation/`) |

The deployment builds containers in AWS CodeBuild. Local Docker is not required.
It deploys private services behind CloudFront, with server-side AWS/provider
credentials and an IAM-authorized Amazon Location geocoder. The public demo uses
an opaque HttpOnly browser cookie. Investigation, radio, simulation and report
requests pass through the workspace API.

## Deployment configuration

The destination is described by an installation file: copy
`deployment/installation.example.toml` to the git-ignored
`deployment/installation.toml` and set the named AWS CLI profile, account,
region, installation name, provider secret ARN, model identifiers, knowledge
bases and preserved audio locations. Operational limits live in the checked-in
`deployment/settings.toml`. See the operations guide for their handling.

```bash
bash deployment/deploy-all.sh            # check and plan; no AWS changes
bash deployment/deploy-all.sh --apply    # deploy or update
```

The launcher runs `.venv/bin/python -m deployment.deploy_workspace` unless
`WILDFIRE_DEPLOY_PYTHON` names another interpreter. The check is read-only;
`--apply` runs the deployment. Failed deployments retain state and recordings for recovery.

## Local environment

```bash
bash scripts/bootstrap-dev.sh
```

The bootstrap creates `.venv` with `PYTHON_BIN` (default `python3.14`; every
image, Lambda and `requirements-lock.txt` target Python 3.14), installs
`requirements.txt` constrained by `requirements-lock.txt` (including the local
`eclipse-sumo` binaries used by tests and the in-process worker), runs
`pip check`, then runs `npm ci` in `ui/` and `deployment/infra/` and
`npm run generate:types` in `ui/` (Node 22.13+, the `ui/package.json` engines floor).
Set `SKIP_UI=1` for a Python-only setup.

## Local UI and API

The UI no longer calls AgentCore with browser AWS credentials. Do not copy AWS
keys, provider keys or service tokens into Vite variables. The supported example
contains public configuration only:

```bash
cp ui/.env.example ui/.env
.venv/bin/python backend/scripts/local_server.py   # API + in-process SUMO worker on :8000
VITE_EVACUATION_ENABLED=true npm run dev --prefix ui              # UI on :3000
```

`generate:types` writes the gitignored `ui/src/schemas/generated/api-types.ts`
from `schemas/api-schema.yaml`. Only `npm run build` regenerates it
automatically, so `dev`, `lint` and type checks fail on a fresh clone until it
exists.

For local fire exposure, set `FIRE_RISK_SERVICE_URL=http://127.0.0.1:8085` on the API and supply
the same `FIRE_RISK_SERVICE_KEY` through both processes' environments. Start the modeler in a separate terminal
with `.venv/bin/python fire-risk-modeler/scripts/local_server.py`. Existing DTN bindings are optional locally;
missing suitable wind produces proximity context and an unavailable timed scenario. See the
[modeler guide](../fire-risk-modeler/README.md) for assumptions and native build checks.

Vite proxies `/simulation-api/*` to `${SIMULATION_LOCAL_URL}/api/*`, defaulting
to `http://127.0.0.1:8000`. `vite.config.ts` reads `SIMULATION_LOCAL_URL` from
the shell environment; the copy in `ui/.env` is not applied to the proxy. In
deployment, the same path routes through CloudFront to the private API.
`STORAGE_MODE` is required (`local` or `aws`, no default). `local_server.py` sets
`STORAGE_MODE=local` unless it is already set, so no sign-in is required and jobs are
kept in `traffic-simulator/.runtime/`. Any other Python entry point that imports the
simulator or API settings, such as `python -m traffic_simulator.worker`, needs it
set explicitly. Every UI build uses the broker; there is no user sign-in, and
the access mode (public demo, local or closed) comes from `/simulation-api/v1/workspace-config`. The former environment-sync command was removed; runtime workspace configuration is authoritative.

## Verification

The deployer runs the full local quality gate, a production dependency audit,
a sanitized build and a configured-credential asset scan. It requires the maintained
browser harness against that production build before AWS writes; configure the
retained SUMO fixture and Playwright runtime in [deployment prerequisites](../deployment/README.md).
CodeBuild then runs the CDK tests and synth, the
real-SUMO traffic-simulator tests, the API tests with contract and UI
metric parity checks, and the firetruck/radio tests before pushing images. The
Runtime CodeBuild project in `backend/deployment/templates/nested/4-agentcore-resources.yaml`
runs `backend/runtime/tests/` (with `python -P -m unittest discover`, because the image has no pytest) and
`check_startup.py` inside the built ARM64 image before pushing it. The tests are mounted
read-only from the source bundle's `release-tests/`, which the image never contains; locally
`scripts/check_quality.py test` runs them as their own pytest process. Complete exact-digest
image scans precede activation. Real radio/investigation/SUMO and response/report
acceptance is required after backend binding and before UI publication; a passing build
alone does not verify providers or the model. Backend updates remain in place and do
not have automatic rollback.

Local checks (run from the repository root after the bootstrap):

```bash
.venv/bin/python scripts/check_quality.py all
npm --prefix ui run build
LAB_SYNTH_OFFLINE=1 npm --prefix deployment/infra run synth
```

The canonical quality gate accepts `lint`, `test`, `coverage` or `all`. It checks all Python source with Ruff, UI lint with zero allowed warnings, UI/infrastructure TypeScript and generated-contract drift. It runs root pytest (including real SUMO), radio and firetruck suites, the Runtime and Gateway release suites in `backend/runtime/tests/`, the startup check, the UI node tests in `ui/scripts/test/` and CDK tests. Separate Python processes preserve the radio, firetruck and Runtime import layouts. Test commands use controlled offline AWS configuration; live acceptance remains a separate release step. `coverage` runs the four pytest suites under coverage.py (pinned in `requirements-dev.txt`) and writes one git-ignored LCOV report, `coverage.lcov`, which the Kiro Quality Loop reads to name changed lines no test executed; `all` does not include it.

For an individual scope, root pytest covers the paths in `pyproject.toml`; run firetruck tests from `firetruck-simulator/`, and radio tests separately. Node checks need `npm ci --prefix ui` first. `npm run build` type-checks `tsconfig.json` (`src/`) and `tsconfig.node.json` (`vite.config.ts`) with `tsc -p`; neither emits files. From a checkout, `backend/runtime/check_startup.py` generates schema models in a temporary folder and reads the repository lock; in the Runtime image it uses the staged generated models and lock. The startup test verifies that inactive models/clients are unnecessary and blocking MCP initialization stays off the event loop.

Use [agent runtime, tools and providers](02-multi-agent-orchestration.md) for the current tool
contracts, geocoding behavior and SDK notes.

## Public guidance and documentation

Set `[knowledge] public_guidance = "managed"` in the installation file to build and bind the official public collection. Source snapshots, extracted sections and manifests are in `backend/knowledge/public-sop/`. Run `.venv/bin/python -m deployment.build_public_sop --check` before deployment. The public path cannot fall back to the private KB.

All documentation is maintained here; nothing is synchronized from another repository. [The documentation index](README.md) lists every guide. Source updates require review, not automatic crawling.
