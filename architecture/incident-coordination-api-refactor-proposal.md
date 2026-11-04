# Incident Coordination API: rename and refactor proposal

October 26, 2026. **Superseded proposal; not implemented.** The [final refactor and wildfire risk proposal](wildfire-risk-implementation-proposal.md) replaces this naming/layout recommendation with capability packages directly under `backend/` and a thin `backend/app.py`. The historical proposal below is retained for its audit evidence. No application files, imports, routes, containers or AWS resources have been renamed.

## Recommendation

The earlier recommendation was to rename `backend/` to **the earlier proposed incident-coordination-api directory**, and its Python package `backend` to **`incident_coordination_api`**. This is no longer the recommended layout. Retain one browser-facing API service.

The business responsibility is to coordinate investigations, evacuation workflows, response services, observations and owned evidence. It is an application coordination boundary, not the wildfire risk model itself. `incident-api` would suggest an incident CRUD authority, while `incident-management` would overstate this demo's operational role. The existing scenario `incident_id` is not necessarily an Esri wildfire identifier.

The folder arrived in this repository on October 3, 2026, with the traffic simulator and Traffic Lab merger. It already brokered agent, radio and firetruck requests at introduction. The name describes its technical/UI boundary but obscures its business purpose.

Pair the rename with targeted responsibility cleanup. Grouping every existing file into new directories is unnecessary; separate the capabilities whose current boundaries actually mix.

## Findings in current code

| Current module | Concrete issue | Proposed separation |
|---|---|---|
| `agent_broker.py` | General investigation turns and evacuation simulation turns/tool effects share one module/router. | Investigation conversation broker versus evacuation agent broker. |
| `service.py` | Most operations prepare/admit evacuation work, but shared daily-budget helpers are also used by Runtime calls, demo access and camera requests. | Evacuation orchestration versus application-wide budgets. |
| `routes.py` | Defines evacuation routes, shared `store_for()` dependency, and SUMO-specific artifact readers. Other brokers import it just for storage injection. | Application dependencies, evacuation artifacts and evacuation routes. |
| `contracts.py` | Common contract/error definitions, evacuation contracts and investigation contracts are collected under one generic module. | Pure common definitions and capability-owned contracts, with the same public schema identities. |
| `locations.py` | Contains scenario geocoding/preset verification, including traffic assumptions. | Explicit evacuation ownership; do not promote it into a globally valid wildfire observation resolver. |
| Single-purpose brokers | Radio, firetruck and camera modules already describe cohesive integrations. | Keep their descriptive files initially rather than create a directory around each file. |

## Target layout after the structural phase

```text
backend/incident-coordination-api/
  README.md
  Dockerfile
  Dockerfile.dockerignore
  requirements.txt
  scripts/
    local_server.py
  incident_coordination_api/
    __init__.py
    main.py
    settings.py
    contracts.py
    dependencies.py
    budgets.py
    demo_access.py
    request_limits.py
    http_errors.py
    security_events.py
    leases.py
    runtime_client.py
    trusted_context.py
    investigation/
      __init__.py
      broker.py
      saved.py
      contracts.py
    evacuation/
      __init__.py
      routes.py
      service.py
      agent_broker.py
      contracts.py
      artifacts.py
      evidence.py
      capabilities.py
      locations.py
      research.py
    firetruck_broker.py
    radio_broker.py
    camera.py
  tests/
    conftest.py
    builders.py
    investigation/
    evacuation/
    test_public_access.py
    test_budgets.py
    test_firetruck_broker.py
    test_radio_broker.py
    test_camera_broker.py
    ...remaining application/integration suites
```

Top-level modules have specific composition or infrastructure responsibilities. Do not create a generic `common/` or `shared/` package that accumulates unrelated code. `main.py` explicitly assembles routers; settings/access/transport remain visible.

Keep the new risk model in the separate top-level [fire-risk-modeler proposal](wildfire-risk-implementation-proposal.md). After this rename is approved and implemented, its thin `fire_risk_broker.py` belongs in `incident_coordination_api`; its model calculations and tests remain with the modeler.

## Exact ownership moves

- Move `investigation_turn()` and retained conversation observations from the current `agent_broker.py` to `investigation/broker.py`.
- Move `incident_investigation.py` to `investigation/saved.py`. Its use of evacuation incidents and run evidence remains an explicit capability relationship.
- Move simulation `agent_turn()`, `execute_tool()` and their routes to `evacuation/agent_broker.py`.
- Extract `store_for()` to `dependencies.py`, so callers no longer depend on evacuation route assembly.
- Extract `spend_daily()` and `daily_limit_reached()` to `budgets.py`, so unrelated callers no longer depend on evacuation orchestration.
- Extract `artifact_for()` and `result_for()` to `evacuation/artifacts.py`; they are typed simulation-artifact readers, not generic model storage.
- Move evacuation routes, services, evidence, capability descriptions, location preparation and research routes into `evacuation/`.
- Partition contracts only after the mechanical rename passes. Root `contracts.py` owns the pure common base/error definitions; capability files own their request/result definitions. Preserve field/type/schema names and let the existing exporter gather those authoritative definitions explicitly. Do not maintain duplicate contracts or circular re-export facades.

Feature services must not import router modules. The modeler must not import the coordination API. Cross-capability evidence use is explicit rather than hidden in generic utility files.

The existing use of `traffic_simulator` persistence, errors, store and settings is a known infrastructure coupling. Preserve it during this work. A reusable foundation extraction would require its own consumers/contracts review; it should not be disguised as a package rename.

## Migration sequence

1. **Mechanical rename, one reviewable change.** Rename the folder/package and update source/import/build/test/command references together. Keep the existing flat module structure and canonical contracts intact at this stage.
2. **Dependency seams, a separate change.** Extract storage injection, budget helpers and evacuation artifact readers; split investigation and simulation turn handling. Keep public behavior stable.
3. **Capability organization, a separate change.** Move the cohesive investigation/evacuation modules and their tests, then partition pure contracts with explicit exporter wiring.
4. **Risk feature, its own change.** Add the independently owned modeler/private ECS service and its thin coordinator broker after the relevant refactor gates pass.

Each phase must pass before the next. Keep independently reviewable Git changes so a failure can be reverted without reverting unrelated work.

## Rename impact

The audit found **81 tracked files** referencing the current source path/package/name at the current checkout. This is a reference count, not 81 behavior changes. It excludes current untracked planning documents and the local untracked/ignored `AGENTS.md`.

Required live-reference review includes:

- API imports, tests, string monkeypatch targets and `backend/scripts/local_server.py`.
- `pyproject.toml`, root requirements and `scripts/bootstrap-dev.sh`.
- `scripts/export_integration_contracts.py`.
- API Dockerfile and Docker ignore allowlists: source/requirements copies, import paths, tests and entrypoint.
- `deployment/infra/lib/stacks.ts`: image-build source paths and the ECS uvicorn command.
- `deployment/package_source.py` required files/allowlists and `deployment/check_lockfiles.py` component manifests.
- Cross-component gateway tests, acceptance scripts and packaging/image-closure/CDK assertions.
- Current developer instructions, module READMEs, architecture links and local AGENTS instructions.

`deployment/deploy_workspace.py` does not use the package/source literals. Its name, API release component and output mappings need not change. Product/UI terminology such as "workspace" also need not be globally replaced.

Nested test moves require attention to fixed parent-directory depths, `from builders` imports and monkeypatch strings. Reassess those explicitly rather than using a repository-wide text replacement.

Update runnable commands and live documentation. Preserve dated descriptions and Git history; fix links to current source without rewriting what existed at the time.

## Compatibility boundary

The rename/refactor changes source ownership and imports, while preserving:

- HTTP paths, operation IDs, request/response fields, schema names, contract versions and public tools.
- Browser `/simulation-api/*`, origin `/api/*`, cookie/header names and private-service bindings.
- Signed-context labels, ownership/thread derivation, record kinds, keys, revisions, idempotency and artifact prefixes.
- CloudFormation construct/logical IDs, resource names/outputs, image repositories and the existing API release-component identity.
- Ports, health endpoints and process counts.

Renaming those identities would be a separate protocol, data or resource migration. Preserve the current stateful-resource logical-ID protections.

## Verification

For the mechanical phase, generated public contracts should remain identical. Compare offline export output and infrastructure identity assertions against the current baseline.

Run API/gateway tests, lockfile and packaging/image-closure suites, the full quality gate, UI build, API verification/runtime image builds from the actual release bundle, offline synth/CDK tests and local owned-route smoke checks. Structural phases additionally verify shared budget behavior, lease/ownership handoffs, successful/failed AG-UI completion and saved investigation/simulation evidence.

An actual deployment uses the existing change-set/release acceptance flow and must show no unintended resource replacement. Local tests do not prove deployed parity.

Current status: source/module/import/reference audits complete; documentation format/link checks run. Application refactoring, behavioral tests, image builds, synth and deployment for this proposal are **not run**. Await owner approval.

Source evidence: [workspace API README](../backend/README.md), [mixed broker](../backend/evacuation/agent_broker.py), [routes and dependencies](../backend/evacuation/routes.py), [services and budgets](../backend/evacuation/service.py), [contracts](../backend/evacuation/contracts.py), [source packaging](../deployment/package_source.py), [dependency checks](../deployment/check_lockfiles.py), [contract exporter](../scripts/export_integration_contracts.py), [test configuration](../pyproject.toml) and [decision log](decisions.md).
