# Deploy the secure FireGUard workspace

One command deploys a complete, named installation from this repository: the
workspace API and SUMO traffic-simulator worker, the private firetruck and radio
services, the AgentCore Runtime/Gateway backend, the credential-free UI and,
optionally, the public SOP collection. The complete procedure, retries, rollback
and acceptance are in [Workspace operations](../architecture/29-workspace-operations.md).

## First deploy

Once per checkout, run `bash scripts/bootstrap-dev.sh` (Python 3.14 environment plus
locked UI and CDK dependencies). Then:

1. Copy the example and fill it in:
   `cp deployment/installation.example.toml deployment/installation.toml`
2. Check and plan, with no AWS changes: `bash deployment/deploy-all.sh`
3. Deploy: `bash deployment/deploy-all.sh --apply`

## Update

1. `git pull`
2. `bash deployment/deploy-all.sh` (check and plan)
3. `bash deployment/deploy-all.sh --apply`

`--apply` bootstraps CDK when the Region has none yet, and resumes an interrupted
release automatically when it was built from the same source and Runtime activation
has not started. A verified upgrade build that started early can be rejoined before
backend preparation; serial Runtime requests retain their existing recovery boundary.
Staging the Runtime ZIP or completing backend preparation
does not close that window. Preparation is replayed through a guarded change set
on a stable stack, retaining the running Runtime image.

After fixing the deployment coordinator, use `--resume-release RELEASE` with
`--apply` to reuse that release's verified images and captured UI/backend assets.
Application code, Runtime source, installation and settings must still match.
This recovery stops at a serial Runtime build request, or preparation/activation
after early compilation; later interruptions require inspection and a new release.
Original source identity and the prior preparation
receipt are preserved in the recovery record.

## Change a setting

- **For every installation:** edit the value in `deployment/settings.toml` (checked in),
  then deploy. Each setting lists its unit and allowed range.
- **For this installation only:** add it to the optional `[settings]` table of your
  installation file, under the same section and key:

  ```toml
  [settings.rate_limits]
  requests_per_ip_per_5_minutes = 1200
  ```

The plan lists every value that differs from `settings.toml` and where it came from.

## Where every value comes from

| Value | Source |
|---|---|
| Installation identity: profile, account, Region, name, environment, operator roles | `[aws]` in `deployment/installation.toml` (git-ignored) |
| Public or staging access, staging CIDR allowlist | `[access]` |
| Agent and report model IDs | `[models]` |
| Radio recordings | `[radio]` |
| Public guidance choice | `[knowledge]` |
| Provider keys | A Secrets Manager secret named by `[providers] secret_arn`; neither file holds a secret |
| Limits, budgets, rates, sizes, TTLs, concurrency, task sizes | `deployment/settings.toml`, or `[settings]` for one installation |

`deployment/installation.example.toml` documents every field. Several installations
can live in `deployment/installations/` (also git-ignored); pass the path:
`bash deployment/deploy-all.sh deployment/installations/staging.toml --apply`.
The options of earlier releases map to file keys as listed in
[Workspace operations](../architecture/29-workspace-operations.md#options-that-moved-into-the-files).

## Troubleshooting

| Message | Fix |
|---|---|
| `Installation file ... not found` | Copy `deployment/installation.example.toml` to `deployment/installation.toml`. |
| `unknown key [section] name` or `unknown section` | Fix the typo; the message lists the valid keys. |
| `missing [section] name` | Add the field; the example shows it. |
| `... is outside the allowed range` or `must be an integer` | Use a value within the range shown in `deployment/settings.toml`. |
| `... is not one of ...` | Use only the listed values; for `cache.demo_areas_to_prebuild`, the Traffic Lab example names (`[]` turns the warm-up off). |
| `is not a Fargate task size` | Pick a vCPU and memory pair from the AWS Fargate task size table; the message lists the memory that vCPU count takes. |
| `policy_parallelism = N is more than worker_vcpu = M` | Lower `simulation.policy_parallelism` or raise `capacity.worker_vcpu`; each policy simulated at once needs its own worker vCPU. |
| `... is tracked by git` | Run `git rm --cached` on the file (it stays on disk); installation files stay local. |
| `Profile account does not match [aws] account_id` | Point `[aws] profile` at the account, or fix `account_id`. |
| `--option was removed: ...` | Move the value into the installation file as the message says. |
| `Stopped before changing STACK: its change set would replace ...` | Nothing was executed. Review the change set in the CloudFormation console; a change that replaces or deletes a table, bucket, repository, secret, key, user pool, log group or knowledge base needs a deliberate migration. |

## How it works

The launcher runs `.venv/bin/python -m deployment.deploy_workspace` (or the interpreter
that `WILDFIRE_DEPLOY_PYTHON` names, with `requirements.txt` installed) from a git
checkout; it needs AWS CLI v2, Node 22.13+, `uv`, git and Python 3.14, and no local Docker.
`--apply` first runs `scripts/check_quality.py all` and the native browser harness against
the production UI build. For that release harness, set `SIMULATION_FIXTURE` to a completed local run
ID under `traffic-simulator/.runtime/runs` with `result.json.gz` and `network.json.gz`; run
directly without it, `ui/scripts/check_evacuation_workspace.cjs` uses the tracked, labeled
synthetic fixture in `ui/test-fixtures/browser-synthetic-grid/`. Also
install Google Chrome, which the harness drives through Playwright's `chrome` channel with
the Playwright version `ui/package.json` pins (`PLAYWRIGHT_MODULE` selects another one).
These are local acceptance inputs, not installation settings; missing ones fail before any
AWS change. Then the deployer builds and scans the images, guards every change set, binds
the Runtime, runs live acceptance and publishes the UI, as
[workspace operations](../architecture/29-workspace-operations.md#what-the-deployment-does)
describes, along with its [post-deployment tests](../architecture/29-workspace-operations.md#post-deployment-tests),
[image gate](../architecture/36-public-demo-security.md#supply-chain-and-image-gate) and the
[backlog](../architecture/00-current-system.md#known-limits-and-backlog) of release work.

## Layout

| Path | Role |
|---|---|
| `deploy-all.sh` | Launcher for `deployment.deploy_workspace`: check and plan, or `--apply` |
| `installation.example.toml` | Every installation-file field with a comment and a safe placeholder; copy it to `installation.toml` |
| `settings.toml` | Shared operational settings with units and allowed ranges (the only place to set them) |
| `installation.py` | Validates both files before any AWS call; generates the settings table in architecture/29 |
| `deploy_workspace.py` | Orchestration: preflight, plan, UI build, source bundle, CDK stacks, backend, publication, smoke check |
| `change_sets.py` | Prepares each CDK change set and runs every change set through the guard: stateful replacement or deletion stops, review stops with `--acknowledge`, event checks after execution |
| `change_proof.py` | Proves that a change set's placeholder value resolves to the current one, from the deployed and new templates |
| `deploy_backend.py` | AgentCore Runtime/Gateway nested stacks, Lambda packages (Python 3.14), Runtime image build |
| `container_build.py` | Fixed service build groups, native artifact verification, component test gates and image publication |
| `component_images.py` | Captured source fingerprints and successful CodeBuild/exact-digest proof for unchanged service reuse |
| `runtime_build.py` | Compatible upgrade compilation overlap and verified Runtime reuse; no activation or CloudFormation writes |
| `deploy_public_sop.py`, `build_public_sop.py` | Public guidance stack and the reviewed collection in `backend/knowledge/public-sop/` |
| `package_source.py`, `check_lockfiles.py` | Deterministic release source bundle and lock verification |
| `publish_workspace.py`, `deployment_target.py` | UI asset verification/publication; installation naming, ownership and ARN checks |
| `runtime_binding.py` | Native endpoint/version readiness and complete API task binding checks |
| `infra/` | CDK app: foundation (ECR, CodeBuild, audit), application (VPC, ECS, ALB, CloudFront, Cognito, DynamoDB, SQS) and public-guidance stacks |
| `acceptance/` | Real-model precheck, live workflow, firetruck/report and browser acceptance scripts, the read-only Gateway client secret check (`check_gateway_secret`), and the visitor client they share; run the Python ones as modules (`.venv/bin/python -m deployment.acceptance.<name>`) |
| `tests/` | Offline deployer tests (`.venv/bin/python -m pytest deployment/tests`) |

The optional `check_background_sources --submit --scope fire` check uses actual
reported fires and weather, replays their sealed terrain inputs offline, and
verifies the saved incident handoff. Its comparator permits only derived ignition
aspect to differ by a circular `1e-6` degree and slope by `1e-7` degree. Other
numeric values, geometry, source identity, counts and input hashes must match
exactly; integral JSON representations such as `315` and `315.0` are equivalent.
Changing this operator-side verifier requires tests and a new acceptance receipt,
without redeploying unchanged application code. Preserve earlier failed receipts.

The existing CodeBuild project runs at most two jobs: SUMO worker then API in one
group; modeler, firetruck and radio in the other. Each group's verification gates
must pass before its runtime images are pushed. Infrastructure checks still run
when every service image is unchanged. AWS account capacity can queue these jobs;
overlap can increase concurrent compute cost.
API verification consumes the worker verification image's source-built headless
SUMO artifact, or an unchanged verified worker digest, so preserve that dependency.
The API runtime contains no SUMO
binaries; the native tools and their libraries belong only to its verification stage.

SUMO, PROJ and Xerces compiler outputs and Pyrothermel wheels are stored in a separate
immutable ECR artifact repository. Their recipe, dependency pins, platform and
input label must match before pulling by digest. A cold or expired cache compiles
normally. Runtime system packages and complete verification targets are rebuilt
when the component changes. CodeBuild's local Docker cache is an additional
best-effort optimization; it does not guarantee cache availability on a fresh host.

Unchanged component reuse requires matching captured source, Dockerfiles, locks,
shared contracts/settings and the CodeBuild recipe, plus a successful original
build and matching original image digest. Every release applies the current
captured vulnerability policy, including a renewed exact-digest scan for reused
images (or ECR's already completed scan within its daily limit). Tags alone cannot
authorize reuse. Old receipts without this proof trigger normal builds.

On compatible upgrades, Runtime compilation starts alongside the service groups
using the existing owned ARM builder. First installation and incompatible recipes
retain serial compilation. Backend preparation, image scans, inactive-endpoint
activation, promotion and live acceptance remain ordered. The first release
populates caches; deployment speed improvements require measured warm releases.

## Retired commands and legacy resources

The supported entry point is `bash deployment/deploy-all.sh`; type generation uses the existing UI build and integration-contract exporter.

`publish_workspace.py` and `deploy_public_sop.py` remain internal deployer modules. Their standalone CLI commands deliberately reject execution so publication stays attached to one verified release.

The deployer performs no retirement deletion (D55 in the [decision log](../architecture/decisions.md)). AgentCore short-term Memory expires through its configured retention; removed manual deletion scripts are not current maintenance procedures.

The Runtime emits AgentCore observability without extra configuration. CloudWatch Transaction Search is a one-time account setting outside this deployer; see [enabling Transaction Search](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-get-started.html#enabling-transaction-search).
