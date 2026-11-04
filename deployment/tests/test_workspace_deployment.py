import hashlib
import io
import json
import re
import secrets
import subprocess  # nosec B404 - runs only node with this module's fixed synth script, never a shell
import sys
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from deployment import deploy_backend as deploy_backend_module
from deployment import deploy_workspace
from deployment.deploy_backend import (
    ENVIRONMENTS,
    archive,
    capture_backend,
    deploy_backend,
    put_version,
    require_environment,
    restore_backend,
    retain_runtime,
    verify_contracts,
    wait_build,
)
from deployment.deployment_target import Target, inspect_stack, validate_arn
from deployment.installation import load_settings, operator_parameters

REPOSITORY = Path(__file__).resolve().parents[2]
STAGING = {"access": {"mode": "staging", "staging_allowed_cidrs": ["198.51.100.0/24", "2001:db8::/32"]}}
CAMERA_ON = {"camera": {"api_base_url": "https://wildfireai.com", "device_id": "fixture-device",
                        "camera_alias": "camera_b"}}
# A Delphire-shaped key, built at run time.
CAMERA_KEY = "-".join(("cam", secrets.token_hex(8)))


def missing():
    return ClientError({"Error": {"Code": "ValidationError", "Message": "Stack does not exist"}},
                       "DescribeStacks")


def acceptance_command(calls, camera_receipt=None, timeouts=None):
    """Answer each live acceptance run with a passed receipt; the investigation's carries camera_receipt, and
    timeouts, when given, records each run's timeout."""
    def command(args, *_, timeout=None):
        calls.append(args)
        if timeouts is not None:
            timeouts.append(timeout)
        assert str(args[0]) == sys.executable
        output = Path(args[args.index("--output")+1])
        report = {"status": "passed"}
        if args[2] == "deployment.acceptance.smoke_simulation_api" and camera_receipt is not None:
            report["camera"] = camera_receipt
        output.write_text(json.dumps(report))
    return command


def test_live_acceptance_uses_the_selected_deployer_interpreter(tmp_path):
    from deployment.release_checks import live_checks
    calls = []
    checks = live_checks(acceptance_command(calls, {"status": "passed"}), {}, tmp_path, "https://workspace.invalid")
    assert set(checks) == {"investigation", "response", "camera"}
    assert len(calls) == 2 and "--radio" in calls[0]
    # Run as modules from the repository root, so the scripts can import the shared visitor client.
    assert [call[1:3] for call in calls] == [["-m", "deployment.acceptance.smoke_simulation_api"],
                                            ["-m", "deployment.acceptance.accept_firetruck"]]


def test_the_investigation_run_checks_the_camera_against_the_installation(tmp_path):
    from deployment.release_checks import live_checks
    for camera, expected in ((False, "not_configured"), (True, "configured")):
        calls = []
        checks = live_checks(acceptance_command(calls, {"status": "passed"}), {}, tmp_path, "https://w.invalid",
                             camera=camera)
        assert [str(arg) for arg in calls[0][-2:]] == ["--camera", expected]
        assert checks["camera"] == {"status": "passed", "expected": expected,
                                    "receipt": "acceptance-investigation.json"}
    for receipt in (None, {"status": "failed"}):
        with pytest.raises(RuntimeError, match="Required camera acceptance did not pass"):
            live_checks(acceptance_command([], receipt), {}, tmp_path, "https://workspace.invalid")


def test_each_live_acceptance_run_has_a_whole_process_timeout(tmp_path):
    """Phase 2 S2 (E1): the deployer bounds each acceptance run as a whole, so a host that stalls the status line,
    the headers or the chunk framing ends the run with an actionable error instead of holding the release."""
    from deployment.release_checks import ACCEPTANCE_TIMEOUT_SECONDS, live_checks
    timeouts = []
    live_checks(acceptance_command([], {"status": "passed"}, timeouts), {}, tmp_path, "https://workspace.invalid")
    assert ACCEPTANCE_TIMEOUT_SECONDS == 3600 and timeouts == [3600, 3600]

    def stalled(args, *_, timeout):
        raise subprocess.TimeoutExpired(args, timeout)
    with pytest.raises(RuntimeError) as stopped:
        live_checks(stalled, {}, tmp_path, "https://workspace.invalid")
    assert str(stopped.value) == (
        "Required investigation acceptance did not finish within 60 minutes, so the deployer stopped it. "
        "acceptance-investigation.json shows the last step it completed. Check the workspace and its providers, "
        "then run deploy-all.sh --apply again.")


def test_the_deployer_command_forwards_its_timeout(tmp_path, monkeypatch):
    """E1: deploy_workspace.command hands its timeout to subprocess.run, which kills the child at the deadline."""
    runs = []
    monkeypatch.setattr(deploy_workspace.subprocess, "run", lambda args, **options: runs.append((args, options)))
    deploy_workspace.command(["tool", tmp_path], tmp_path, {"FIXTURE": "1"}, timeout=7)
    deploy_workspace.command(["tool"], tmp_path)
    assert runs == [(["tool", str(tmp_path)], {"cwd": tmp_path, "env": {"FIXTURE": "1"}, "check": True, "timeout": 7}),
                    (["tool"], {"cwd": tmp_path, "env": None, "check": True, "timeout": None})]


def pinned_playwright(root, installed):
    """Lay out ui/package.json pinning Playwright 1.64.0 and, unless None, an installed module of that version."""
    (root / "ui").mkdir()
    (root / "ui/package.json").write_text(json.dumps({"devDependencies": {"playwright": "1.64.0"}}))
    if installed is not None:
        (root / "ui/node_modules/playwright").mkdir(parents=True)
        (root / "ui/node_modules/playwright/package.json").write_text(json.dumps({"version": installed}))
    return root / "ui/node_modules/playwright"


def test_browser_harness_defaults_to_the_pinned_playwright_and_keeps_the_override(tmp_path):
    from deployment.release_checks import browser_environment
    module = pinned_playwright(tmp_path, "1.64.0")
    env = browser_environment({"SIMULATION_FIXTURE": "run"}, root=tmp_path)
    assert env == {"SIMULATION_FIXTURE": "run", "PLAYWRIGHT_MODULE": str(module)}
    override = {"SIMULATION_FIXTURE": "run", "PLAYWRIGHT_MODULE": "/opt/playwright"}
    assert browser_environment(override, root=tmp_path) == override


@pytest.mark.parametrize("env, installed, message", [
    ({}, "1.64.0", "SIMULATION_FIXTURE"),
    ({"SIMULATION_FIXTURE": "run"}, None, "pinned Playwright 1.64.0"),
    ({"SIMULATION_FIXTURE": "run"}, "1.63.0", "pinned Playwright 1.64.0"),
])
def test_browser_harness_refuses_a_missing_fixture_or_an_unpinned_playwright(tmp_path, env, installed, message):
    from deployment.release_checks import browser_environment
    pinned_playwright(tmp_path, installed)
    with pytest.raises(ValueError, match=message):
        browser_environment(env, root=tmp_path)


def test_runtime_binding_keeps_accepted_slots_and_never_reads_default():
    from deployment.runtime_binding import initial_parameters, verify_endpoint
    arn = "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/fixture"
    control = MagicMock()
    control.get_agent_runtime_endpoint.return_value = {
        "status": "READY", "liveVersion": "39", "targetVersion": "39", "agentRuntimeArn": arn}
    # A new installation, with no Runtime bound yet, starts both slots at version 1 without any AWS read.
    assert initial_parameters(control, "", {}) == {"SimulationRuntimeEndpoint": "RELEASE_A",
                                                   "RuntimeEndpointAVersion": "1", "RuntimeEndpointBVersion": "1"}
    assert control.mock_calls == []
    # A bound installation keeps its accepted slots; only the active endpoint is read, never the Runtime or DEFAULT.
    accepted = {"SimulationRuntimeEndpoint": "RELEASE_A", "RuntimeEndpointAVersion": "39",
                "RuntimeEndpointBVersion": "40"}
    assert initial_parameters(control, arn, {**accepted, "PublicDemoMode": "true"}) == accepted
    control.get_agent_runtime.assert_not_called()
    control.get_agent_runtime_endpoint.assert_called_once_with(agentRuntimeId="fixture", endpointName="RELEASE_A")
    # A bound stack without both slots predates the endpoint migration W2.11 removed; nothing is read for it.
    control.reset_mock()
    for previous in ({}, {"SimulationRuntimeEndpoint": "RELEASE_A", "RuntimeEndpointAVersion": "39"}):
        with pytest.raises(ValueError):
            initial_parameters(control, arn, previous)
    assert control.mock_calls == []
    control.get_agent_runtime_endpoint.return_value["targetVersion"] = "40"
    with pytest.raises(RuntimeError, match="not READY"):
        verify_endpoint(control, arn, "RELEASE_A", "39")


def test_an_old_api_task_prevents_reusing_an_endpoint_slot():
    from deployment.runtime_binding import require_api_binding
    ecs = MagicMock()
    lab = {"ClusterName": "cluster", "ApiServiceName": "api"}
    service = {"deployments": [{}], "pendingCount": 0, "runningCount": 1,
               "desiredCount": 1, "taskDefinition": "current"}
    ecs.describe_services.return_value = {"services": [service]}
    ecs.get_paginator.return_value.paginate.return_value = [{"taskArns": ["one"]}]
    task = {"lastStatus": "RUNNING", "taskDefinitionArn": "old"}
    ecs.describe_tasks.return_value = {"tasks": [task]}
    with pytest.raises(RuntimeError, match="old API"):
        require_api_binding(ecs, lab, "RELEASE_A", "39")
    task["taskDefinitionArn"] = "current"
    ecs.describe_task_definition.return_value = {"taskDefinition": {"containerDefinitions": [{
        "name": "Api", "environment": [{"name": "SIMULATION_RUNTIME_ENDPOINT", "value": "RELEASE_A"},
                                    {"name": "SIMULATION_RUNTIME_VERSION", "value": "39"}]}]}}
    assert require_api_binding(ecs, lab, "RELEASE_A", "39") == {"endpoint": "RELEASE_A", "version": "39"}
    task["overrides"] = {"containerOverrides": [{"name": "Api", "environment": [
        {"name": "SIMULATION_RUNTIME_ENDPOINT", "value": "RELEASE_B"}]}]}
    with pytest.raises(RuntimeError, match="expected Runtime"):
        require_api_binding(ecs, lab, "RELEASE_A", "39")


def test_runtime_preparation_keeps_image_compatible_configuration():
    from deployment.validate_infra_baseline import load_template
    template = (REPOSITORY / "backend/deployment/templates/nested/4-agentcore-resources.yaml").read_bytes()
    # The running Runtime still has a variable the new template dropped (RTGW-8's output budget).
    deployed = template.decode().replace('SIMULATION_MODEL_ID: !Ref ModelId',
        'SIMULATION_MODEL_ID: !Ref ModelId\n        SIMULATION_MAX_TOKENS: "8000"')
    assert deployed != template.decode()
    for body in (deployed, {"Resources": load_template(deployed)["Resources"]}):
        updated = load_template(retain_runtime(template, body).decode())
        original = load_template(template.decode())
        assert updated["Resources"]["AgentCoreRuntime"] == load_template(deployed)["Resources"]["AgentCoreRuntime"]
        for key, resource in original["Resources"].items():
            if key != "AgentCoreRuntime":
                assert updated["Resources"][key] == resource
    with pytest.raises(ValueError, match="unavailable"):
        retain_runtime(template, {"Resources": {}})
    resource = {"Type": "AWS::BedrockAgentCore::Runtime", "Properties": {
        "EnvironmentVariables": {"OLD_MODEL_ID": {"Ref": "ModelId"}},
        "AgentRuntimeArtifact": {"ContainerConfiguration": {"ContainerUri": {"Fn::Sub": "${RuntimeImageTag}"}}}}}
    updated = load_template(retain_runtime(template, {"Resources": {"AgentCoreRuntime": resource}}).decode())
    assert updated["Resources"]["AgentCoreRuntime"] == resource


def unresolved(template):
    """Name each Ref, GetAtt, Sub variable, condition or DependsOn of a parsed template that it does not declare."""
    resources, conditions = template.get("Resources", {}), template.get("Conditions", {})
    names = {*template.get("Parameters", {}), *resources}
    missing = set()

    def need(name, declared):
        if not (name in declared or name.startswith("AWS::")):
            missing.add(name)

    def walk(value):
        for key, inner in value.items() if isinstance(value, dict) else enumerate(value):
            kind = str(key).removeprefix("Fn::")
            if kind == "Ref":
                need(inner, names)
            elif kind == "GetAtt":
                need((inner.split(".", 1) if isinstance(inner, str) else inner)[0], resources)
            elif kind == "Sub":
                # A ${!Literal} is no variable; a list form also declares its own variables.
                text, variables = (inner, {}) if isinstance(inner, str) else inner
                for variable in re.findall(r"\$\{([^!}][^}]*)\}", text):
                    need(variable.split(".", 1)[0], names | set(variables))
                inner = variables
            elif kind == "If" or (kind == "Condition" and isinstance(inner, str)):
                need(inner[0] if kind == "If" else inner, conditions)
            elif kind == "DependsOn":
                for name in [inner] if isinstance(inner, str) else inner:
                    need(name, resources)
            if isinstance(inner, dict | list):
                walk(inner)

    walk({section: template.get(section, {}) for section in ("Conditions", "Resources", "Outputs")})
    return missing


def test_preparation_keeps_the_deployed_runtime_block_resolvable():
    """RTGW-3: prepare keeps the deployed Runtime block (retain_runtime) until the new image passes its gates, and
    everything that block references must still resolve in the new template, or CloudFormation rejects the prepare
    change set."""
    from deployment.validate_infra_baseline import load_template
    template = (REPOSITORY / "backend/deployment/templates/nested/4-agentcore-resources.yaml").read_bytes()
    # The deployed block differs from the new one, as after any Runtime configuration change.
    current = '        MODEL_TEMPERATURE: "0.3"\n'
    deployed = template.decode().replace(current, '        MODEL_TEMPERATURE: "0.2"\n')
    assert deployed != template.decode()
    prepared = load_template(retain_runtime(template, deployed).decode())
    assert prepared["Resources"]["AgentCoreRuntime"] == load_template(deployed)["Resources"]["AgentCoreRuntime"]
    assert unresolved(prepared) == set()
    assert unresolved(load_template(template.decode())) == set()


@pytest.mark.parametrize("build_status", ["FAILED", "SUCCEEDED", "SCAN_FAILED", "INTERRUPTED"])
def test_runtime_configuration_changes_only_after_a_verified_build(
        tmp_path, monkeypatch, make_installation, build_status):
    from deployment import deploy_backend as operations
    installation = make_installation()
    target = installation.target
    session = MagicMock()
    clients = {key: MagicMock() for key in ("cloudformation", "s3", "bedrock-agentcore-control", "codebuild", "ecr")}
    session.client.side_effect = clients.__getitem__
    stack = {"Parameters": [{"ParameterKey": key, "ParameterValue": value} for key, value in {
        "Environment": installation.environment, "DeployRuntime": "true", "RuntimeImageTag": "old-tag"}.items()],
        "Outputs": [{"OutputKey": "RuntimeId", "OutputValue": "runtime"},
                    {"OutputKey": "GatewayIdentifier", "OutputValue": "gateway"}]}
    monkeypatch.setattr(operations, "inspect_stack", lambda *_: stack)
    clients["cloudformation"].get_template.return_value = {"TemplateBody":
        (REPOSITORY / "backend/deployment/templates/nested/4-agentcore-resources.yaml").read_text()}
    clients["s3"].put_object.return_value = {"VersionId": "version"}
    clients["bedrock-agentcore-control"].get_agent_runtime.return_value = {
        "status": "READY", "agentRuntimeVersion": "40",
        "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": "registry:"+tmp_path.name}},
        "environmentVariables": {"DEPLOYED_RuntimeBuild": tmp_path.name}}
    clients["codebuild"].start_build.return_value = {"build": {"id": "build"}}
    clients["codebuild"].batch_get_builds.return_value = {"builds": [
        {"buildStatus": "SUCCEEDED" if build_status in ("SCAN_FAILED", "INTERRUPTED") else build_status}]}
    if build_status == "INTERRUPTED":
        # A dropped connection while polling the Runtime build must not stop the release after its build request.
        clients["codebuild"].batch_get_builds.side_effect = [
            EndpointConnectionError(endpoint_url="https://codebuild.example"),
            clients["codebuild"].batch_get_builds.return_value]
        monkeypatch.setattr(operations.time, "sleep", lambda _: None)
    clients["ecr"].describe_images.return_value = {"imageDetails": [{"imageDigest": "digest"}]}
    from deployment.change_sets import parse_acknowledgment
    # N1 (C6): the operator's acknowledgment reaches the prepare and activate change sets unchanged.
    operator_acknowledgments = (parse_acknowledgment(ACKNOWLEDGED),)
    calls = []
    def change(_cfn, _target, _template, parameters, _release, phase, _folder, *, acknowledged):
        assert acknowledged == operator_acknowledgments
        calls.append((phase, dict(parameters)))
        return {"CodeBuildProjectName": "builder", "RuntimeECRRepositoryUri": "registry/repository",
                "RuntimeId": "runtime"}
    monkeypatch.setattr(operations, "change_stack", change)
    def scan(*_, **__):
        if build_status == "SCAN_FAILED":
            raise RuntimeError("Image scan blocked")
    monkeypatch.setattr(operations, "verify_image", scan)
    prepared = {"payload": b"runtime", "packages": {key: b"lambda" for key in (
        "simulation-service", "geocode-service", "wildfire-service", "weather-dtn-service")},
        "templates": {"4-agentcore-resources.yaml": clients["cloudformation"].get_template.return_value["TemplateBody"].encode()},
        "template": "parent-template"}
    lab = dict.fromkeys(("SimulationInternalUrl", "SimulationContextSecretArn",
                         "SimulationAdapterSecurityGroupId", "SimulationAdapterSubnetIds"), "configured")
    args = (session, target, REPOSITORY, "source-bucket", lab,
            {"DTN_CLIENT_ID": "client", "DTN_CLIENT_SECRET": "secret"}, installation, tmp_path)
    if build_status == "FAILED":
        with pytest.raises(RuntimeError, match="existing Runtime image was not advanced"):
            operations.deploy_backend(*args, captured=prepared, acknowledged=operator_acknowledgments)
        assert len(calls) == 1
    elif build_status == "SCAN_FAILED":
        with pytest.raises(RuntimeError, match="Image scan blocked"):
            operations.deploy_backend(*args, captured=prepared, acknowledged=operator_acknowledgments)
        assert len(calls) == 1, "Rejected images must not activate"
    else:
        operations.deploy_backend(*args, captured=prepared, acknowledged=operator_acknowledgments)
        assert calls[1][0] == "activate"
        assert calls[1][1]["RuntimeImageTag"] == tmp_path.name
        assert calls[1][1]["TemplatePrefix"].endswith("/templates/nested")
        assert clients["codebuild"].start_build.call_count == 1
        assert clients["codebuild"].batch_get_builds.call_count == (2 if build_status == "INTERRUPTED" else 1)
    assert calls[0][0] == "prepare"
    assert calls[0][1]["RuntimeImageTag"] == "old-tag"
    assert "/runtime-prepare/" in calls[0][1]["TemplatePrefix"]
    # Every Gateway target is a Lambda target, so no release lists targets to migrate them (W2.11, DEPL-4).
    clients["bedrock-agentcore-control"].list_gateway_targets.assert_not_called()
    # The deployer passes exactly the parameters the backend root template declares, no removed one.
    from deployment.validate_infra_baseline import load_template
    root = load_template((REPOSITORY / "backend/deployment/templates/wildfire-backend-stack.yaml").read_text())
    assert set(calls[0][1]) == set(root["Parameters"])
    # The backend's three bucket policies admit the installation file's operator roles (L1, D52).
    assert calls[0][1]["OperatorRoleArns"] == operator_parameters(installation)["OperatorRoleArns"]


def test_names_are_stable_and_isolated_by_account_region_and_installation():
    target = Target("123456789012", "us-west-2", "training")
    assert target.prefix == Target("123456789012", "us-west-2", "training").prefix
    variations = [target, Target(target.account, "us-east-1", target.name),
                  Target("999999999999", target.region, target.name),
                  Target(target.account, target.region, "exercise")]
    assert len({v.stack("application") for v in variations}) == 4
    assert all("-" not in v.project and len(v.project+"_Memory") < 48 for v in variations)
    for name in ("xx", "Name", "has spaces", "../escape", "a"*21):
        with pytest.raises(ValueError):
            Target(target.account, target.region, name)


def test_stack_state_and_ownership_fail_closed():
    target = Target("123456789012", "us-west-2", "training")
    client = MagicMock()
    client.describe_stacks.side_effect = missing()
    assert inspect_stack(client, target.stack("backend"), target) is None
    client.describe_stacks.side_effect = None
    row = {"StackStatus": "DELETE_FAILED", "Tags": [{"Key": k, "Value": v} for k, v in target.tags.items()]}
    client.describe_stacks.return_value = {"Stacks": [row]}
    with pytest.raises(ValueError, match="DELETE_FAILED"):
        inspect_stack(client, target.stack("backend"), target)
    row.update(StackStatus="UPDATE_COMPLETE", Tags=[])
    with pytest.raises(ValueError, match="not owned"):
        inspect_stack(client, target.stack("backend"), target)
    row["Tags"] = [{"Key": k, "Value": v} for k, v in target.tags.items()]
    assert inspect_stack(client, target.stack("backend"), target) == row
    client.delete_stack.assert_not_called()


def test_identity_mismatch_precedes_all_other_preflight_calls(make_installation):
    session = MagicMock()
    session.client.return_value.get_caller_identity.return_value = {"Account": "999999999999"}
    with pytest.raises(ValueError, match=r"does not match \[aws\] account_id"):
        deploy_workspace.preflight(session, make_installation())
    assert [c.args[0] for c in session.client.call_args_list] == ["sts"]


def test_provider_arn_cannot_cross_account_or_region():
    target = Target("123456789012", "us-west-2", "training")
    good = "arn:aws:secretsmanager:us-west-2:123456789012:secret:provider"
    assert validate_arn(good, target, "secretsmanager") == good
    for wrong in (good.replace("us-west-2", "us-east-1"), good.replace(target.account, "999999999999")):
        with pytest.raises(ValueError):
            validate_arn(wrong, target, "secretsmanager")


def test_audio_capture_pins_s3_version_and_preserves_local_original(tmp_path):
    session = MagicMock()
    client = session.client.return_value
    client.head_object.return_value = {"ContentLength": 5, "VersionId": "original-version"}
    client.get_object.return_value = {"Body": io.BytesIO(b"audio")}
    key, data = deploy_workspace.read_audio(session, "s3://source/recording.mp3")
    assert key.startswith("input/") and data == b"audio"
    assert client.get_object.call_args.kwargs["VersionId"] == "original-version"
    recording = tmp_path / "original.wav"
    recording.write_bytes(data)
    assert deploy_workspace.read_audio(session, str(recording))[1] == data
    assert recording.read_bytes() == data
    client.delete_object.assert_not_called()
    with pytest.raises(ValueError):
        deploy_workspace.read_audio(session, "s3://source/recording.mp3?secret=value")


def test_failed_build_and_missing_source_version_stop_release():
    client = MagicMock()
    client.batch_get_builds.return_value = {"builds": [{"buildStatus": "FAILED"}]}
    with pytest.raises(RuntimeError, match=r"Application build fixture-build failed \(FAILED\); application "
                                           "deployment stopped"):
        wait_build(client, "fixture-build", "Application build", stopped="application deployment stopped")
    client.put_object.return_value = {}
    with pytest.raises(ValueError, match="versioning"):
        put_version(client, "source", "key", b"source")


def test_build_polling_recovers_connection_without_starting_another_build(monkeypatch):
    client = MagicMock()
    client.batch_get_builds.side_effect = [
        EndpointConnectionError(endpoint_url="https://codebuild.example"),
        {"builds": [{"buildStatus": "SUCCEEDED", "currentPhase": "COMPLETED"}]},
    ]
    monkeypatch.setattr(deploy_backend_module.time, "sleep", lambda _: None)
    assert wait_build(client, "original-build", "Application build",
                      stopped="application deployment stopped")["buildStatus"] == "SUCCEEDED"
    assert client.batch_get_builds.call_count == 2
    client.start_build.assert_not_called()


def test_the_application_build_wait_covers_the_codebuild_queue_and_run_timeouts(tmp_path, monkeypatch):
    """B-2: a slow but valid build must not fail the release while CodeBuild keeps building and pushing, so the wait
    is the ImageBuilder project's timeout plus queuedTimeout (deployment/infra/lib/stacks.ts)."""
    builder = re.search(r'new codebuild\.Project\(this, "ImageBuilder", \{[^;]*?'
                        r'timeout: Duration\.minutes\((\d+)\), queuedTimeout: Duration\.minutes\((\d+)\)',
                        (REPOSITORY / "deployment/infra/lib/stacks.ts").read_text())
    assert builder, "stacks.ts declares the ImageBuilder project's timeout and queuedTimeout"
    allowed = (int(builder[1]) + int(builder[2])) * 60
    waits = []
    monkeypatch.setattr(deploy_workspace, "wait_build",
                        lambda *args, **kwargs: waits.append((args[1:], kwargs)))
    monkeypatch.setattr(deploy_workspace, "put_version", lambda *_: "pinned-version")
    scans = []
    monkeypatch.setattr(deploy_workspace, "verify_image",
                        lambda _client, repo, digest, component, receipt, **_:
                        scans.append((repo, digest, component, receipt.name)))
    session = MagicMock()
    client = session.client.return_value
    client.start_build.return_value = {"build": {"id": "application-build"}}
    client.describe_images.return_value = {"imageDetails": [{"imageDigest": "sha256:verified"}]}
    foundation = {"SourceBucket": "sources", "BuildProject": "builder",
                  **{key: f"registry/{key}" for key in deploy_workspace.IMAGE_OUTPUTS}}
    release = SimpleNamespace(id="20261019000000000001", folder=tmp_path, source=b"zip", digest="f" * 64,
                              resumed=False)
    deploy_workspace.build_images(session, foundation, release)
    assert waits == [(("application-build", "Application build"),
                      {"stopped": "application deployment stopped", "timeout": allowed})]
    assert deploy_workspace.APPLICATION_BUILD_SECONDS == allowed
    expected = [("RepositoryUri", "api"), ("TrafficSimulatorRepositoryUri", "worker"),
                ("FiretruckRepositoryUri", "firetruck"), ("RadioRepositoryUri", "radio"),
                ("FireRiskRepositoryUri", "fire-risk")]
    assert deploy_workspace.IMAGE_OUTPUTS == tuple(key for key, _ in expected)
    assert scans == [(key, "sha256:verified", component, f"scan-{component}.json") for key, component in expected]
    assert json.loads((tmp_path / "images.json").read_text()) == dict.fromkeys(
        (key for key, _ in expected), "sha256:verified")


def test_release_tools_use_the_installation_profile_and_never_offline_synth(monkeypatch):
    monkeypatch.setenv("LAB_SYNTH_OFFLINE", "1")
    monkeypatch.setenv("AWS_PROFILE", "caller-profile")
    env = deploy_workspace.cdk_environment(Target("123456789012", "us-west-2", "training"), "fixture")
    assert "LAB_SYNTH_OFFLINE" not in env, "An offline synth flag must never reach a release's cdk"
    assert {key: env[key] for key in ("AWS_PROFILE", "AWS_REGION", "AWS_DEFAULT_REGION", "CDK_DEFAULT_ACCOUNT",
                                      "CDK_DEFAULT_REGION")} == {
        "AWS_PROFILE": "fixture", "AWS_REGION": "us-west-2", "AWS_DEFAULT_REGION": "us-west-2",
        "CDK_DEFAULT_ACCOUNT": "123456789012", "CDK_DEFAULT_REGION": "us-west-2"}


def test_resume_uses_captured_assets_and_rejects_wrong_identity_or_tampering(tmp_path):
    target = Target("123456789012", "us-west-2", "training")
    release = "20260929172026669302"
    folder = tmp_path / "releases" / release
    (folder / "cdk.out").mkdir(parents=True)
    source = archive({"ui/package-lock.json": b"{}"})
    (folder / "source.zip").write_bytes(source)
    identity = {"account": target.account, "region": target.region, "name": target.name,
                "image_tag": release, "source_sha256": hashlib.sha256(source).hexdigest()}
    (folder / "identity.json").write_text(json.dumps(identity))
    (folder / "cdk.out/manifest.json").write_text(json.dumps({"artifacts": {
        "WildfireLabApplication": {"environment": f"aws://{target.account}/{target.region}",
                                  "properties": {"stackName": target.stack("application")}},
    }}))
    prepared = {"payload": b"runtime", "template": "template", "templates": {"nested.yaml": b"nested"},
                "packages": {"function": b"package"}}
    (folder / "backend-assets.zip").write_bytes(capture_backend(prepared))
    (folder / "ui-assets.zip").write_bytes(archive({"index.html": b"<html>captured</html>"}))
    actual = deploy_workspace.resume_sources(tmp_path, target, release, {})
    assert actual == (folder, source, [("index.html", b"<html>captured</html>")], prepared)
    assert restore_backend(capture_backend(prepared)) == prepared
    (folder / "runtime-source.zip").write_bytes(b"runtime")
    assert deploy_workspace.resume_sources(tmp_path, target, release, {})[-1] == prepared
    (folder / "runtime-source.zip").write_bytes(b"wrong runtime")
    with pytest.raises(ValueError, match="Staged Runtime"):
        deploy_workspace.resume_sources(tmp_path, target, release, {})
    (folder / "runtime-source.zip").write_bytes(b"runtime")
    (folder / "backend-build-request.json").write_text("{}")
    with pytest.raises(ValueError, match="Runtime build starts"):
        deploy_workspace.resume_sources(tmp_path, target, release, {})
    (folder / "backend-build-request.json").unlink()
    with pytest.raises(ValueError, match="another installation"):
        deploy_workspace.resume_sources(tmp_path, Target("999999999999", target.region, target.name), release, {})
    with pytest.raises(ValueError, match="20-digit release ID"):
        deploy_workspace.resume_sources(tmp_path, target, "../escape", {})
    (folder / "source.zip").write_bytes(source+b"tampered")
    with pytest.raises(ValueError, match="integrity"):
        deploy_workspace.resume_sources(tmp_path, target, release, {})
    (folder / "source.zip").write_bytes(source)
    (folder / "ui-assets.zip").write_bytes(archive({"index.html": b"<html>configured-secret-value</html>"}))
    with pytest.raises(ValueError, match="Configured secret"):
        deploy_workspace.resume_sources(tmp_path, target, release, {"key": "configured-secret-value"})


def release_folder(state, release, digest, *files):
    folder = state / "releases" / release
    folder.mkdir(parents=True)
    (folder / "identity.json").write_text(json.dumps({"source_sha256": digest}))
    for name in files:
        (folder / name).write_text("{}")
    return folder


def test_interrupted_release_resumes_only_from_the_same_source_inside_the_build_window(tmp_path):
    state = tmp_path / "state"
    assert deploy_workspace.interrupted_release(state, "digest") is None
    folder = release_folder(state, "20261001000000000002", "digest", "application-build-request.json",
                            "backend-assets.zip")
    assert deploy_workspace.interrupted_release(state, "digest") == folder.name
    assert deploy_workspace.interrupted_release(state, "other-source") is None
    (folder / "runtime-source.zip").write_bytes(b"staged source is not a Runtime build")
    assert deploy_workspace.interrupted_release(state, "digest") == folder.name
    (folder / "backend-build-request.json").write_text("{}")
    assert deploy_workspace.interrupted_release(state, "digest") is None
    (folder / "backend-build-request.json").unlink()
    (state / "latest.json").write_text(json.dumps({"release": folder.name}))
    assert deploy_workspace.interrupted_release(state, "digest") is None, "a finished release is not resumed"
    release_folder(state, "20261001000000000003", "digest")
    assert deploy_workspace.interrupted_release(state, "digest") is None, "no build was started"
    release_folder(state, "20261001000000000004", "digest", "application-build-request.json", "deployment.json")
    assert deploy_workspace.interrupted_release(state, "digest") is None
    # An older deployer captured the backend only after the service rollout; such a release cannot resume (DEPL-4).
    older = release_folder(state, "20261001000000000005", "digest", "application-build-request.json")
    assert deploy_workspace.interrupted_release(state, "digest") is None
    (older / "backend-assets.zip").write_text("{}")
    assert deploy_workspace.interrupted_release(state, "digest") == older.name


def test_explicit_resume_allows_coordinator_fix_but_rejects_code_configuration_and_runtime_drift(
        tmp_path, monkeypatch, make_installation):
    installation = make_installation()
    release = "20261001000000000002"
    folder = release_folder(tmp_path, release, "unused", "application-build-request.json")
    identity = {"installation": installation.identity(), "settings": {"values": installation.settings}}
    (folder / "identity.json").write_text(json.dumps(identity))
    files = {"deployment/deploy_workspace.py": b"old coordinator", "service.py": b"verified application"}
    (folder / "source.zip").write_bytes(archive(files))
    prepared = {"payload": b"runtime", "template": "", "templates": {}, "packages": {}}
    (folder / "backend-assets.zip").write_bytes(capture_backend(prepared))
    current = {**files, "deployment/deploy_workspace.py": b"recovery fix"}
    monkeypatch.setattr(deploy_workspace, "source_bundle", lambda _: archive(current))
    monkeypatch.setattr(deploy_workspace, "runtime_source", lambda _: b"runtime")
    assert deploy_workspace.explicit_resume(tmp_path, installation, release) == release
    # An older deployer captured the backend only after the service rollout; such a release cannot resume (DEPL-4).
    captured = (folder / "backend-assets.zip").read_bytes()
    (folder / "backend-assets.zip").unlink()
    with pytest.raises(ValueError, match="no captured backend .*without --resume-release"):
        deploy_workspace.explicit_resume(tmp_path, installation, release)
    (folder / "backend-assets.zip").write_bytes(captured)
    current["service.py"] = b"new application"
    with pytest.raises(ValueError, match="Application source changed"):
        deploy_workspace.explicit_resume(tmp_path, installation, release)
    current["service.py"] = files["service.py"]
    monkeypatch.setattr(deploy_workspace, "runtime_source", lambda _: b"new runtime")
    with pytest.raises(ValueError, match="Runtime source changed"):
        deploy_workspace.explicit_resume(tmp_path, installation, release)
    identity["settings"]["values"] = {}
    (folder / "identity.json").write_text(json.dumps(identity))
    with pytest.raises(ValueError, match="Installation or settings changed"):
        deploy_workspace.explicit_resume(tmp_path, installation, release)
    (folder / "backend-build-request.json").write_text("{}")
    with pytest.raises(ValueError, match="Runtime build starts"):
        deploy_workspace.explicit_resume(tmp_path, installation, release)
    with pytest.raises(ValueError, match="20-digit"):
        deploy_workspace.explicit_resume(tmp_path, installation, "../other")
    (tmp_path / "latest.json").write_text(json.dumps({"release": release}))
    with pytest.raises(ValueError, match="unfinished release newer"):
        deploy_workspace.explicit_resume(tmp_path, installation, release)


def test_resume_admits_captured_early_compilation_only_before_backend_preparation(
        tmp_path, monkeypatch, make_installation):
    from deployment.runtime_build import build_properties, source_identity
    installation = make_installation()
    target, release = installation.target, "20261001000000000002"
    folder = tmp_path / "releases" / release
    (folder / "cdk.out").mkdir(parents=True)
    source = archive({"deployment/image-scan-policy.json": b"{}"})
    digest = hashlib.sha256(source).hexdigest()
    captured = {
        "payload": b"captured runtime", "template": "template", "packages": {"function": b"package"},
        "templates": {"4-agentcore-resources.yaml": (
            REPOSITORY / "backend/deployment/templates/nested/4-agentcore-resources.yaml").read_bytes()},
    }
    identity = {
        "account": target.account, "region": target.region, "name": target.name, "image_tag": release,
        "source_sha256": digest, "installation": installation.identity(),
        "settings": {"values": installation.settings},
    }
    (folder / "identity.json").write_text(json.dumps(identity))
    (folder / "source.zip").write_bytes(source)
    (folder / "backend-assets.zip").write_bytes(capture_backend(captured))
    (folder / "runtime-source.zip").write_bytes(captured["payload"])
    (folder / "ui-assets.zip").write_bytes(archive({"index.html": b"<html>fixture</html>"}))
    (folder / "cdk.out/manifest.json").write_text(json.dumps({"artifacts": {
        "WildfireLabApplication": {"environment": f"aws://{target.account}/{target.region}",
                                  "properties": {"stackName": target.stack("application")}},
    }}))
    bucket = "fixture-runtime-sources"
    receipt = {
        **source_identity(target, bucket, folder, captured), "mode": "build", "started_early": True,
        "source_version": "captured-version", "build_id": "owned-builder:build-id",
        "project_name": "owned-builder", "repository_uri":
        f"{target.account}.dkr.ecr.{target.region}.amazonaws.com/runtime", "environment": installation.environment,
        "buildspec": build_properties(captured)["Source"]["BuildSpec"], "build_source_bucket": bucket,
        "build_source_key": source_identity(target, bucket, folder, captured)["source_key"],
        "build_source_version": "captured-version", "build_image_tag": release,
    }
    (folder / "backend-build-request.json").write_text(json.dumps(receipt))
    monkeypatch.setattr(deploy_workspace, "source_bundle", lambda _: source)
    monkeypatch.setattr(deploy_workspace, "runtime_source", lambda _: captured["payload"])
    assert deploy_workspace.interrupted_release(tmp_path, digest) == release
    assert deploy_workspace.explicit_resume(tmp_path, installation, release) == release
    assert deploy_workspace.resume_sources(tmp_path, target, release, {})[-1] == captured
    # The process may stop immediately after ARM compilation starts, before any application request exists.
    assert not (folder / "application-build-request.json").exists()
    current = deploy_workspace.Release(
        release, folder, source, digest, [("index.html", b"<html>fixture</html>")], captured, resumed=True)
    foundation = {
        "SourceBucket": bucket, "BuildProject": "application-builder", "NativeBuildRepositoryUri": "registry/native",
        **{key: "registry/"+component for component, key in deploy_workspace.OUTPUTS.items()},
    }
    clients = {name: MagicMock() for name in ("codebuild", "s3", "ecr")}
    session = MagicMock()
    session.client.side_effect = clients.__getitem__
    clients["codebuild"].start_build.side_effect = lambda **options: {"build": {"id": next(
        value["value"] for value in options["environmentVariablesOverride"] if value["name"] == "BUILD_GROUP")}}
    clients["ecr"].describe_images.return_value = {"imageDetails": [{"imageDigest": "sha256:"+"a"*64}]}
    uploads = []
    monkeypatch.setattr(deploy_workspace, "put_version", lambda _client, selected, key, payload:
                        uploads.append((selected, key, payload)) or "application-source-version")
    monkeypatch.setattr(deploy_workspace, "plan_images", lambda _ecr, _foundation, selected, **_:
                        {"build": list(deploy_workspace.OUTPUTS), "reused": {}, "source_sha256": selected.digest})
    monkeypatch.setattr(deploy_workspace, "wait_build", lambda *_args, **_: None)
    monkeypatch.setattr(deploy_workspace, "verify_image", lambda *_args, **_: None)
    monkeypatch.setattr(deploy_workspace, "build_recipe", lambda *_: {"fixture": "captured-recipe"})
    deploy_workspace.build_images(session, foundation, current)
    assert uploads == [(bucket, f"sources/{digest}.zip", source)]
    request = json.loads((folder / "application-build-request.json").read_text())
    assert set(request["builds"]) == {"platform", "services"}
    for call in clients["codebuild"].start_build.call_args_list:
        assert call.kwargs["sourceLocationOverride"] == f"{bucket}/sources/{digest}.zip"
        assert call.kwargs["sourceVersion"] == "application-source-version"
    assert json.loads((folder / "backend-build-request.json").read_text()) == receipt
    (folder / "backend-prepare-changes.json").write_text("{}")
    assert deploy_workspace.interrupted_release(tmp_path, digest) is None
    with pytest.raises(ValueError, match="This resume mode ends"):
        deploy_workspace.explicit_resume(tmp_path, installation, release)
    with pytest.raises(ValueError, match="This resume mode ends"):
        deploy_workspace.resume_sources(tmp_path, target, release, {})


def checked(stacks=None, bootstrap=False):
    return deploy_workspace.Preflight(stacks or dict.fromkeys(("foundation", "application", "backend")),
                                      {}, {"input/a.mp3": b"audio"}, bootstrap, "f" * 64)


def test_plan_shows_identity_changed_settings_and_what_apply_does(make_installation):
    installation = make_installation({**STAGING, "settings": {"rate_limits": {"requests_per_ip_per_5_minutes": 900}}})
    planned = deploy_workspace.plan(installation, checked(bootstrap=True), None)
    assert planned["installation"]["name"] == "training" and planned["installation"]["access"]["mode"] == "staging"
    assert planned["settings_changed"] == {"rate_limits.requests_per_ip_per_5_minutes": {
        "value": 900, "settings.toml": 600, "source": f"{installation.path} [settings]"}}
    steps = " | ".join(planned["apply_will"])
    assert "bootstrap CDK in 123456789012/us-west-2 (CDKToolkit is missing)" in steps
    assert "new release from source sha256 ffffffffffffffff" in steps
    assert "stopping before any change set that would replace or delete a stateful resource" in steps
    assert "admit only the [access] staging_allowed_cidrs networks through the WAF allowlist (staging)" in steps
    assert "leave public guidance retrieval unavailable" in steps
    assert planned["apply_will"][-1] == ("after publication, set a 30-day retention on the installation's log groups "
                                         "that keep logs forever")
    resumed = deploy_workspace.plan(make_installation(), checked(), "20261001000000000002")
    assert resumed["settings_changed"].startswith("none")
    assert not resumed["apply_will"][0].startswith("bootstrap")
    assert "resume interrupted release 20261001000000000002" in resumed["apply_will"][0]
    assert all(v["action"] == "create" for v in resumed["stacks"].values())
    # No deprecation notes: the one ignored key, [knowledge] private_knowledge_base_id, is refused since W2.11's
    # follow-up release (test_installation.py).
    assert set(planned) == set(resumed) == {"installation", "settings_changed", "stacks", "camera", "audio_objects",
                                            "apply_will", "cleanup"}


def test_check_mode_validates_the_installation_before_any_aws_call(tmp_path, monkeypatch):
    monkeypatch.setattr(deploy_workspace.boto3, "Session", MagicMock(side_effect=AssertionError("AWS session")))
    with pytest.raises(ValueError, match="not found. Copy deployment/installation.example.toml"):
        deploy_workspace.main([str(tmp_path / "missing.toml")])


@pytest.mark.parametrize(("argv", "again", "chosen"), [
    ([], "", ""),
    (["deployment/installations/other.toml"], " deployment/installations/other.toml",
     "--installation deployment/installations/other.toml "),
])
def test_check_mode_plans_without_deploying(tmp_path, monkeypatch, make_installation, capsys, argv, again, chosen):
    installation = make_installation()
    session = MagicMock()
    monkeypatch.setattr(deploy_workspace, "ROOT", tmp_path)
    monkeypatch.setattr(deploy_workspace, "load_installation", lambda path, root: installation)
    monkeypatch.setattr(deploy_workspace.boto3, "Session", lambda **kwargs: session)
    monkeypatch.setattr(deploy_workspace, "preflight", lambda *_: checked())
    monkeypatch.setattr(deploy_workspace, "changed_settings", lambda *_: {})
    monkeypatch.setattr(deploy_workspace, "deploy", MagicMock(side_effect=AssertionError("deployed")))
    deploy_workspace.main(argv)
    out = capsys.readouterr().out
    assert '"apply_will"' in out and f"Run bash deployment/deploy-all.sh{again} --apply" in out
    # The Gateway secret verify after a release needs a baseline from before it (doc 29), so the plan says so, for
    # the same installation file the plan read.
    assert ("capture the Gateway client secret baseline (architecture/29): .venv/bin/python -m "
            f"deployment.acceptance.check_gateway_secret {chosen}capture "
            "--output .runtime/gateway-secret-baseline.json") in out
    assert (tmp_path / ".runtime/deployments/123456789012/us-west-2/training/plan.json").is_file()


ACKNOWLEDGED = ("training-backend/InfrastructureStack/APIKeysSecret=AWS::SecretsManager::Secret:SecretString")


def test_acknowledgments_are_parsed_planned_and_passed_to_every_guarded_change_set(
        tmp_path, monkeypatch, make_installation):
    installation = make_installation()
    monkeypatch.setattr(deploy_workspace, "ROOT", tmp_path)
    monkeypatch.setattr(deploy_workspace, "load_installation", lambda path, root: installation)
    monkeypatch.setattr(deploy_workspace.boto3, "Session", lambda **kwargs: MagicMock())
    monkeypatch.setattr(deploy_workspace, "preflight", lambda *_: checked())
    monkeypatch.setattr(deploy_workspace, "changed_settings", lambda *_: {})
    deployed = MagicMock()
    monkeypatch.setattr(deploy_workspace, "deploy", deployed)
    deploy_workspace.main(["--apply", "--acknowledge", ACKNOWLEDGED])
    # deploy() then hands them to every guarded change set (test_deployment_binds_backend_before_enabling_...).
    acknowledged = deployed.call_args.kwargs["acknowledged"]
    assert [str(ack) for ack in acknowledged] == [ACKNOWLEDGED]
    plan = json.loads((tmp_path / ".runtime/deployments/123456789012/us-west-2/training/plan.json").read_text())
    assert f"accept these reviewed changes: {ACKNOWLEDGED}" in plan["apply_will"]


@pytest.mark.parametrize("argv, message", [
    (["--acknowledge", ACKNOWLEDGED], "use it with --apply"),
    (["--apply", "--acknowledge", "APIKeysSecret"], "must read STACK/RESOURCE=TYPE:SCOPE"),
])
def test_acknowledgments_need_apply_and_the_printed_form(monkeypatch, argv, message):
    monkeypatch.setattr(deploy_workspace, "load_installation", MagicMock(side_effect=AssertionError("loaded")))
    with pytest.raises((SystemExit, ValueError), match=message):
        deploy_workspace.main(argv)


@pytest.mark.parametrize("enabled, calls, recorded", [(False, 1, "enabled"), (True, 0, "already enabled")])
def test_backend_change_sets_turn_on_termination_protection_once(tmp_path, monkeypatch, make_installation,
                                                                 enabled, calls, recorded):
    from deployment import deploy_backend as operations
    target = make_installation().target
    cfn = MagicMock()
    cfn.create_change_set.return_value = {"Id": "arn:aws:cloudformation:change-set"}
    cfn.describe_stacks.return_value = {"Stacks": [{"EnableTerminationProtection": enabled,
                                                    "Outputs": [{"OutputKey": "RuntimeId", "OutputValue": "runtime"}]}]}
    monkeypatch.setattr(operations, "inspect_stack", lambda *_: {"StackStatus": "UPDATE_COMPLETE"})
    seen = []

    def guard(_cfn, name, change_set, *, created, receipt, acknowledged):
        seen.append(acknowledged)
        receipt.write_text("{}")
        return {"stack": name, "change_set": change_set, "result": "executed"}
    monkeypatch.setattr(operations, "execute_guarded", guard)
    from deployment.change_sets import parse_acknowledgment
    acknowledged = (parse_acknowledgment(ACKNOWLEDGED),)
    outputs = operations.change_stack(cfn, target, "template", {}, "release", "prepare", tmp_path,
                                      acknowledged=acknowledged)
    assert outputs == {"RuntimeId": "runtime"} and seen == [acknowledged]
    assert "RoleARN" not in cfn.create_change_set.call_args.kwargs, (
        "the backend's nested templates and Lambda packages are read from BuildSource as the deployer's operator "
        "role because no CloudFormation service role is used; a service role must first join BuildSource's "
        "exception list (D52)")
    assert cfn.update_termination_protection.call_count == calls
    if calls:
        cfn.update_termination_protection.assert_called_once_with(EnableTerminationProtection=True,
                                                                  StackName=target.stack("backend"))
    receipt = json.loads((tmp_path / "backend-prepare-changes.json").read_text())
    assert receipt["termination_protection"] == recorded


def test_secret_rotation_preserves_unrelated_fields_and_is_idempotent():
    session = MagicMock()
    client = session.client.return_value
    client.get_secret_value.side_effect = [
        {"SecretString": json.dumps({"TOMTOM_API_KEY": "old", "FIRMS_MAP_KEY": "", "unrelated": "keep"})}]
    provider = {"TOMTOM_API_KEY": "traffic"}
    lab = {"ProviderSecretArn": "traffic"}
    assert deploy_workspace.configure_providers(session, lab, provider)
    assert client.put_secret_value.call_count == 1
    written = client.put_secret_value.call_args.kwargs["SecretString"]
    assert json.loads(written)["unrelated"] == "keep"
    client.get_secret_value.side_effect = [{"SecretString": written}]
    assert not deploy_workspace.configure_providers(session, lab, provider)
    assert client.put_secret_value.call_count == 1


@pytest.mark.parametrize("changes,key,message", [
    (CAMERA_ON, None, (r"\[camera\] device_id is set in .*installation.toml, but the provider secret has no "
                       r"DELPHIRE_API_KEY\. Add DELPHIRE_API_KEY to the secret named by \[providers\] secret_arn")),
    (CAMERA_ON, "", "but the provider secret has no DELPHIRE_API_KEY"),
    (None, CAMERA_KEY, r"The provider secret has DELPHIRE_API_KEY, but \[camera\] device_id is empty or missing"),
    ({"camera": {**CAMERA_ON["camera"], "device_id": ""}}, CAMERA_KEY, r"\[camera\] device_id is empty or missing"),
    (CAMERA_ON, CAMERA_KEY[:15], "DELPHIRE_API_KEY must be 16-8192 visible ASCII characters with no spaces"),
    (CAMERA_ON, CAMERA_KEY[:8] + " " + CAMERA_KEY[8:], "must be 16-8192 visible ASCII characters"),
    (CAMERA_ON, 1234567890123456789, "must be 16-8192 visible ASCII characters"),
])
def test_a_camera_device_and_its_key_come_together(make_installation, changes, key, message):
    provider = {"TOMTOM_API_KEY": "fixture"} | ({} if key is None else {"DELPHIRE_API_KEY": key})
    with pytest.raises(ValueError, match=message) as refused:
        deploy_workspace.check_camera_key(make_installation(changes), provider)
    if isinstance(key, str) and key:
        assert key not in str(refused.value), "No message contains the key"


def test_a_release_without_a_camera_or_with_both_values_passes_the_pairing_check(make_installation):
    deploy_workspace.check_camera_key(make_installation(), {"TOMTOM_API_KEY": "fixture"})
    deploy_workspace.check_camera_key(make_installation(), {"DELPHIRE_API_KEY": ""})
    deploy_workspace.check_camera_key(make_installation(CAMERA_ON), {"DELPHIRE_API_KEY": CAMERA_KEY})


OPERATOR_ROLE = "arn:aws:iam::123456789012:role/fixture-deployer"


def operator_ready(monkeypatch, client):
    """Let a MagicMock preflight pass the operator-role check (L1): the caller is BASE's listed deployer role; and
    skip the image-retention guard (L5), whose reads a MagicMock cannot answer."""
    client.get_caller_identity.return_value = {**client.get_caller_identity.return_value,
                                               "Arn": "arn:aws:sts::123456789012:assumed-role/fixture-deployer/test"}
    client.get_role.return_value = {"Role": {"Arn": OPERATOR_ROLE}}
    monkeypatch.setattr(deploy_workspace, "require_retained_images", lambda *_: None)


def stub_offline_checks(monkeypatch, tmp_path, client):
    """Answer a MagicMock preflight's reads for a new installation and skip its local release checks."""
    client.describe_availability_zones.return_value = {"AvailabilityZones": [{"ZoneName": "a"}, {"ZoneName": "b"}]}
    client.get_secret_value.return_value = {"SecretString": json.dumps(
        dict.fromkeys(("DTN_CLIENT_ID", "DTN_CLIENT_SECRET", "TOMTOM_API_KEY"), "fixture-value"))}
    monkeypatch.setattr(deploy_workspace, "ROOT", tmp_path)
    monkeypatch.setattr(deploy_workspace, "inspect_stack", lambda *_, **__: None)
    monkeypatch.setattr(deploy_workspace, "read_audio", lambda *_: ("input/a.mp3", b"audio"))
    monkeypatch.setattr(deploy_workspace.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(deploy_workspace, "source_bundle", lambda root: b"zip")
    for name in ("require_unexpired_image_exceptions", "runtime_source", "verify_contracts"):
        monkeypatch.setattr(deploy_workspace, name, lambda root: None)


def test_preflight_stops_on_an_unpaired_camera_key_before_the_model_checks(monkeypatch, make_installation):
    session = MagicMock()
    session.get_partition_for_region.return_value = "aws"
    client = session.client.return_value
    client.get_caller_identity.return_value = {"Account": "123456789012"}
    operator_ready(monkeypatch, client)
    client.describe_availability_zones.return_value = {"AvailabilityZones": [{"ZoneName": "a"}, {"ZoneName": "b"}]}
    client.describe_stacks.side_effect = missing()
    client.get_secret_value.return_value = {"SecretString": json.dumps(
        dict.fromkeys(("DTN_CLIENT_ID", "DTN_CLIENT_SECRET", "TOMTOM_API_KEY"), "fixture-value"))}
    with pytest.raises(ValueError, match="but the provider secret has no DELPHIRE_API_KEY"):
        deploy_workspace.preflight(session, make_installation(CAMERA_ON))
    client.get_foundation_model.assert_not_called()


DEPLOYER_SESSION = "arn:aws:sts::123456789012:assumed-role/fixture-deployer/test"


@pytest.mark.parametrize("caller, role_arn, message", [
    (DEPLOYER_SESSION, OPERATOR_ROLE, None),
    ("arn:aws:sts::123456789012:assumed-role/other-role/test", OPERATOR_ROLE,
     (r"\[aws\] operator_role_arns does not list the deploying role other-role; add "
      r"arn:aws:iam::123456789012:role/\.\.\. for it \(aws iam get-role --role-name other-role shows its ARN\)")),
    ("arn:aws:iam::123456789012:user/fixture-user", OPERATOR_ROLE,
     r"The deployer must run as an IAM role session, not an IAM user; \[aws\] operator_role_arns lists roles only"),
    (DEPLOYER_SESSION, None, f"names {OPERATOR_ROLE}, which IAM does not have; fix or remove it"),
    (DEPLOYER_SESSION, "arn:aws:iam::123456789012:role/ops/fixture-deployer",
     r"but that role's ARN is arn:aws:iam::123456789012:role/ops/fixture-deployer \(its path differs\)"),
])
def test_preflight_requires_the_deployer_among_existing_operator_roles(
        tmp_path, monkeypatch, make_installation, caller, role_arn, message):
    """L1, D52: the bucket policies refuse every role outside the list, so a deployer missing from it would be
    refused its own uploads, and a listed ARN with the wrong path would refuse that operator."""
    session = MagicMock()
    session.get_partition_for_region.return_value = "aws"
    client = session.client.return_value
    client.get_caller_identity.return_value = {"Account": "123456789012", "Arn": caller}

    def get_role(RoleName):
        if role_arn is None:
            raise ClientError({"Error": {"Code": "NoSuchEntity", "Message": "fixture"}}, "GetRole")
        return {"Role": {"Arn": role_arn}}
    client.get_role.side_effect = get_role
    stub_offline_checks(monkeypatch, tmp_path, client)
    if message is None:
        deploy_workspace.preflight(session, make_installation())
        client.get_role.assert_called_once_with(RoleName="fixture-deployer")
    else:
        with pytest.raises(ValueError, match=message) as refused:
            deploy_workspace.preflight(session, make_installation())
        assert str(refused.value).endswith("No AWS changes made")
        client.get_secret_value.assert_not_called()
    # One STS call serves the account and operator checks; preflight writes nothing.
    assert client.get_caller_identity.call_count == 1
    writes = [name for name, *_ in client.method_calls
              if name.startswith(("put_", "create_", "update_", "delete_", "start_"))]
    assert writes == []


def test_preflight_checks_image_retention_before_any_write(tmp_path, monkeypatch, make_installation):
    """L5, D53: preflight runs the image-retention guard for the plan and --apply alike, before any AWS write."""
    session = MagicMock()
    session.get_partition_for_region.return_value = "aws"
    client = session.client.return_value
    client.get_caller_identity.return_value = {"Account": "123456789012"}
    operator_ready(monkeypatch, client)
    stub_offline_checks(monkeypatch, tmp_path, client)
    guarded = []
    monkeypatch.setattr(deploy_workspace, "require_retained_images", lambda *args: guarded.append(args))
    installation = make_installation()
    checked = deploy_workspace.preflight(session, installation)
    assert len(guarded) == 1
    assert guarded[0][:2] == (session, installation) and guarded[0][2] is checked.stacks
    assert [name for name, *_ in client.method_calls
            if name.startswith(("put_", "create_", "update_", "delete_", "start_"))] == []


@pytest.mark.parametrize("mode", ["existing", "none"])
@pytest.mark.parametrize("owned", [True, False])
def test_preflight_refuses_unmanaged_public_guidance_when_the_installation_owns_its_stack(
        tmp_path, monkeypatch, make_installation, owned, mode):
    """P7-1, D52: only "managed" deploys the installation's own public-guidance stack, so with "existing" or "none"
    changes to it, such as the Documents bucket's Deny, would silently not reach it. Preflight refuses that before
    any secret read."""
    knowledge = {"public_guidance": mode, "public_knowledge_base_id": "FIXTUREKB" if mode == "existing" else ""}
    installation = make_installation({"knowledge": knowledge})
    guidance = installation.target.stack("public-guidance")
    session = MagicMock()
    session.get_partition_for_region.return_value = "aws"
    client = session.client.return_value
    client.get_caller_identity.return_value = {"Account": "123456789012"}
    operator_ready(monkeypatch, client)
    stub_offline_checks(monkeypatch, tmp_path, client)
    inspected = []

    def inspect(_client, name, _target, **_):
        inspected.append(name)
        return {"StackName": name} if owned and name == guidance else None
    monkeypatch.setattr(deploy_workspace, "inspect_stack", inspect)
    if owned:
        with pytest.raises(ValueError) as refused:
            deploy_workspace.preflight(session, installation)
        message = str(refused.value)
        assert message.startswith(guidance) and '[knowledge] public_guidance = "managed"' in message
        assert f'public_guidance = "{mode}" never updates it' in message
        assert "(D52)" in message and message.endswith("No AWS changes made")
        client.get_secret_value.assert_not_called()
    else:
        deploy_workspace.preflight(session, installation)
        if mode == "existing":
            client.get_knowledge_base.assert_called_once_with(knowledgeBaseId="FIXTUREKB")
    assert guidance in inspected
    assert [name for name, *_ in client.method_calls
            if name.startswith(("put_", "create_", "update_", "delete_", "start_"))] == []


def test_the_camera_key_is_copied_only_when_it_changes(make_installation):
    session = MagicMock()
    client = session.client.return_value
    lab = {"ProviderSecretArn": "application-secret"}
    current = {"TOMTOM_API_KEY": "traffic", "FIRMS_MAP_KEY": "", "nonce": "kept"}
    client.get_secret_value.side_effect = lambda **_: {"SecretString": json.dumps(current)}
    # No camera: an absent key equals the application secret's absent field, so nothing is written or restarted.
    assert not deploy_workspace.configure_providers(session, lab, {"TOMTOM_API_KEY": "traffic"})
    client.put_secret_value.assert_not_called()
    for key in (CAMERA_KEY, CAMERA_KEY[::-1], ""):
        assert deploy_workspace.configure_providers(session, lab, {"TOMTOM_API_KEY": "traffic",
                                                                   "DELPHIRE_API_KEY": key})
        current = json.loads(client.put_secret_value.call_args.kwargs["SecretString"])
        assert current == {"TOMTOM_API_KEY": "traffic", "FIRMS_MAP_KEY": "", "nonce": "kept",
                           "DELPHIRE_API_KEY": key}
    assert client.put_secret_value.call_count == 3


def test_the_api_receives_only_the_pinned_provider_fields():
    # The API task receives the application provider secret whole (PROVIDER_CREDENTIALS), so a new copied field
    # reaches it; deployment/infra/test/stacks.test.ts pins the secret's generated template the same way.
    assert deploy_workspace.PROVIDER_FIELDS == (("TOMTOM_API_KEY", "TOMTOM_API_KEY"),
                                                ("FIRMS_MAP_KEY", "NASA_FIRMS_API_KEY"),
                                                ("DELPHIRE_API_KEY", "DELPHIRE_API_KEY"))


def test_the_plan_says_whether_the_camera_is_on(make_installation):
    assert deploy_workspace.plan(make_installation(), checked(), None)["camera"] == "off: [camera] device_id is empty"
    assert deploy_workspace.plan(make_installation(CAMERA_ON), checked(), None)["camera"] == (
        "Delphire device fixture-device, camera_b, through the broker")


@pytest.mark.parametrize("backend_fails", [False, True])
@pytest.mark.parametrize("acceptance_fails", [False, True])
@pytest.mark.parametrize("stage_only", [False, True])
def test_deployment_binds_backend_before_enabling_public_mode_or_publishing(
        tmp_path, monkeypatch, make_installation, capsys, backend_fails, stage_only, acceptance_fails):
    """Exercise the whole orchestration with AWS/build boundaries replaced."""
    # The upgrade (staging) variants also configure the camera, so both camera states run through the release, and
    # provision the managed public guidance collection.
    installation = make_installation({**STAGING, **CAMERA_ON, "knowledge": {"public_guidance": "managed"}}
                                     if stage_only else None)
    # The caller's shell holds another profile and key variables in both spellings the CDK CLI reads. The values are
    # long enough for the bundle's secret scan (12 or more characters) and built at runtime for pre-commit secret scanners.
    caller_keys = {name: f"caller-{name.lower()}-fixture" for name in (
        "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
        "AMAZON_ACCESS_KEY_ID", "AMAZON_SECRET_ACCESS_KEY", "AMAZON_SESSION_TOKEN")}
    for name, value in caller_keys.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("AWS_PROFILE", "caller-profile")
    session = MagicMock()
    clients = {name: MagicMock() for name in ("cloudformation", "s3", "codebuild", "ecr",
                                            "bedrock-agentcore-control", "ecs")}
    session.client.side_effect = lambda name: clients[name]
    runtime_arn = "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/fixture"
    clients["bedrock-agentcore-control"].get_agent_runtime_endpoint.return_value = {
        "status": "READY", "liveVersion": "1", "targetVersion": "1", "agentRuntimeArn": runtime_arn}
    clients["cloudformation"].describe_change_set.return_value = {
        "Status": "CREATE_COMPLETE", "ChangeSetId": "arn:aws:cloudformation:change-set", "Changes": []}
    foundation = {"SourceBucket": "sources", "BuildProject": "builder",
                  "RepositoryUri": "registry/app", "TrafficSimulatorRepositoryUri": "registry/traffic",
                  "FiretruckRepositoryUri": "registry/firetruck",
                  "FireRiskRepositoryUri": "registry/fire-risk", "FireRiskRepositoryArn": "arn:fire-risk",
                  "RadioRepositoryUri": "registry/radio", "RepositoryArn": "arn:app",
                  "TrafficSimulatorRepositoryArn": "arn:traffic",
                  "FiretruckRepositoryArn": "arn:truck", "RadioRepositoryArn": "arn:radio"}
    lab = {"RadioAudioBucket": "audio", "IntegratedWorkspaceUrl": "https://workspace.invalid",
           "WorkspaceAssetsBucket": "ui", "IntegratedDistributionId": "distribution"}

    def describe(StackName):
        outputs = foundation if StackName.endswith("-foundation") else lab
        return {"Stacks": [{"Outputs": [{"OutputKey": k, "OutputValue": v} for k, v in outputs.items()]}]}

    clients["cloudformation"].describe_stacks.side_effect = describe
    clients["s3"].put_object.return_value = {"VersionId": "pinned-version"}
    clients["codebuild"].start_build.return_value = {"build": {"id": "application-build"}}
    clients["codebuild"].batch_get_builds.return_value = {"builds": [{"buildStatus": "SUCCEEDED"}]}
    clients["ecr"].describe_images.return_value = {"imageDetails": [{"imageDigest": "sha256:verified"}]}
    commands, environments, sequence = [], [], []
    captured = {"payload": b"runtime", "template": "template", "templates": {"nested.yaml": b"nested"},
                "packages": {"function": b"package"}}
    early_receipt = {"build_id": "early-arm-build", "started_early": True} if stage_only else None
    clients["codebuild"].start_build.side_effect = lambda **_: (
        sequence.append("application-build") or {"build": {"id": "application-build"}})

    def early_build(_session, target, bucket, config, folder, sources):
        assert _session is session and target == installation.target and bucket == foundation["SourceBucket"]
        assert config.environment == installation.environment and sources == captured
        assert folder.parent == tmp_path / "state/releases"
        sequence.append("early-runtime" if stage_only else "serial-runtime")
        return early_receipt

    def command(args, _cwd=None, env=None):
        args = list(map(str, args))
        commands.append(args)
        environments.append(env)
        if "deploy" in args and "WildfireLabApplication" in args:
            sequence.append("public" if "PublicDemoMode=true" in args else "private")

    # Packaging needs a git checkout and Lambda packaging needs uv; both must receive this deploy's root.
    def package_source(root, *, destination):
        assert root == tmp_path
        destination.write_bytes(archive({"traffic-simulator/traffic_simulator/worker.py": b"source",
                                        "deployment/image-scan-policy.json": b"{}"}))

    def prepare_backend(root):
        assert root == tmp_path
        return captured

    guidance = []
    # The operator's acknowledgment reaches every guarded change set: each CDK stack pass and the backend's.
    from deployment.change_sets import parse_acknowledgment
    acknowledged, received = (parse_acknowledgment(ACKNOWLEDGED),), []
    guard = deploy_workspace.deploy_guarded
    monkeypatch.setattr(deploy_workspace, "deploy_guarded", lambda *args, **kwargs:
                        received.append(kwargs["acknowledged"]) or guard(*args, **kwargs))

    def deploy_public_sop(_session, _target, folder, *, root, run, parameters, install, acknowledged):
        guidance.append({"folder": folder, "root": root, "install": install, "acknowledged": acknowledged,
                         "parameters": parameters})
        run(["guidance-synth"])
        return {"PublicKnowledgeBaseId": "public-kb"}

    def backend(*args, **kwargs):
        received.append(kwargs["acknowledged"])
        assert args[2] == tmp_path and kwargs["captured"] == captured
        assert kwargs["early_runtime_build"] is early_receipt
        # The backend binds the public guidance collection this release provisioned.
        assert args[6].public_knowledge_base_id == ("public-kb" if stage_only else "")
        sequence.append("backend")
        if backend_fails:
            raise RuntimeError("Runtime build failed")
        # The real deploy_backend returns the backend stack's outputs, which always hold the Runtime ID.
        return {"RuntimeArn": runtime_arn, "RuntimeVersion": "2", "GeocodeServiceArn": "arn:geocoder",
                "WeatherDTNServiceArn": "arn:weather",
                "RuntimeId": "fixture_runtime-AbCdEf1234"}

    monkeypatch.setattr(deploy_workspace, "ROOT", tmp_path)
    monkeypatch.setattr(deploy_workspace, "command", command)
    monkeypatch.setattr(deploy_workspace, "inspect_stack", lambda *_: None)
    scanned = []
    monkeypatch.setattr(deploy_workspace, "verify_assets", lambda _dist, secrets: scanned.append(secrets)
                        or [("index.html", b"<html>fixture</html>")])
    monkeypatch.setattr(deploy_workspace, "package_source", package_source)
    monkeypatch.setattr(deploy_workspace, "git_commit", lambda root: {"commit": "fixture", "uncommitted_changes": False})
    monkeypatch.setattr(deploy_workspace, "prepare_backend", prepare_backend)
    monkeypatch.setattr(deploy_workspace, "start_runtime_build", early_build)
    monkeypatch.setattr(deploy_workspace, "configure_providers", lambda *_: False)
    monkeypatch.setattr(deploy_workspace, "deploy_backend", backend)
    monkeypatch.setattr(deploy_workspace, "browser_check", lambda *_: sequence.append("browser"))
    monkeypatch.setattr(deploy_workspace, "verify_image", lambda *_, **__: None)
    monkeypatch.setattr(deploy_workspace, "verify_endpoint", lambda _c, _arn, endpoint, version:
                        {"endpoint": endpoint, "version": version})
    monkeypatch.setattr(deploy_workspace, "require_api_binding", lambda _c, _lab, endpoint, version:
                        {"endpoint": endpoint, "version": version})
    monkeypatch.setattr(deploy_workspace, "candidate_check", lambda *_: sequence.append("candidate-verified"))
    class Cutover:
        def __init__(self, *_): self.report = {}
        def seed(self, _): sequence.append("seed")
        def finish(self, *_):
            sequence.append("cutover")
            return {"status": "passed"}
        def close(self): pass
        def save(self): pass
    monkeypatch.setattr(deploy_workspace, "CutoverAcceptance", Cutover)
    expectations = []
    def live(*_, camera):
        sequence.append("acceptance")
        expectations.append(("live", camera))
        if acceptance_fails:
            raise RuntimeError("Acceptance failed")
        return {"status": "passed"}
    def smoked(_url, _staging, *, camera):
        expectations.append(("smoke", camera))
        return {"passed": True}
    monkeypatch.setattr(deploy_workspace, "live_checks", live)
    monkeypatch.setattr(deploy_workspace, "publish", lambda *_: sequence.append("publish") or "ui-digest")
    retained = []
    monkeypatch.setattr(deploy_workspace, "retain_installation_logs", lambda _session, stacks, runtime_id, folder:
                        sequence.append("retention") or retained.append((stacks, runtime_id, folder)))
    monkeypatch.setattr(deploy_workspace, "smoke", smoked)
    monkeypatch.setattr("deployment.deploy_public_sop.deploy_public_sop", deploy_public_sop)
    state = tmp_path / "state"
    def run():
        stacks = dict.fromkeys(("foundation", "application", "backend"))
        if stage_only:
            # An existing public installation moving to staging keeps demo sessions open behind the allowlist.
            stacks["application"] = {"Parameters": [{"ParameterKey": key, "ParameterValue": value} for key, value in {
                "PublicDemoMode": "true", "SimulationRuntimeArn": runtime_arn, "SimulationRuntimeEndpoint": "RELEASE_A",
                "RuntimeEndpointAVersion": "1", "RuntimeEndpointBVersion": "1"}.items()]}
        return deploy_workspace.deploy(session, installation, stacks, {}, {"input/example.mp3": b"audio"}, state,
                                       acknowledged=acknowledged)
    # The first pass keeps the previous mode (closed for a new installation); only the bind pass opens it.
    first = "public" if stage_only else "private"
    build_order = ["browser", "early-runtime" if stage_only else "serial-runtime", "application-build"]
    if backend_fails:
        with pytest.raises(RuntimeError, match="Runtime build failed"):
            run()
        assert sequence == [*build_order, first, "backend"]
        assert not (state / "latest.json").exists()
    elif acceptance_fails:
        with pytest.raises(RuntimeError, match="Acceptance failed"):
            run()
        expected = [*build_order, first, "backend", first, "candidate-verified"]
        if stage_only:
            expected += ["seed"]
        expected += ["public"] + (["cutover"] if stage_only else []) + ["acceptance"]
        if stage_only:
            expected += ["public"]
            assert next((state / "releases").glob("*/runtime-binding-rollback.json")).exists()
        assert sequence == expected
        assert not (state / "latest.json").exists()
    else:
        run()
        # Log retention comes after publication, so a failure there never stops an accepted release (L3).
        assert sequence == ([*build_order, first, "backend", first, "candidate-verified"]
                            + (["seed"] if stage_only else []) + ["public"]
                            + (["cutover"] if stage_only else []) + ["acceptance", "publish", "retention"])
        components = ["foundation", "application", "backend"] + (["public-guidance"] if stage_only else [])
        assert retained == [([installation.target.stack(c) for c in components], "fixture_runtime-AbCdEf1234",
                             next((state / "releases").iterdir()))]
        assert (state / "latest.json").is_file()
        bound = next(c for c in commands if "deploy" in c and "PublicDemoMode=true" in c
                     and any("-application-bind-" in arg for arg in c))
        assert "PublicDemoMode=true" in bound
        assert "GeocoderFunctionArn=arn:geocoder" in bound
        assert "WeatherFunctionArn=arn:weather" in bound
        assert "TrafficSimulatorRepositoryArn=arn:traffic" in bound
        assert "FireRiskRepositoryArn=arn:fire-risk" in bound
        images = json.loads(next((state / "releases").glob("*/images.json")).read_text())
        assert set(images) == set(deploy_workspace.IMAGE_OUTPUTS)
        assert "--app" in bound and any("cdk.out" in arg for arg in bound)
        latest = json.loads((state / "latest.json").read_text())
        assert latest["anonymous_access"] == ("staging allowlist only" if stage_only else "enabled")
        # The release record names the commit, the installation and the settings each deploy applied.
        identity = json.loads(next((state / "releases").glob("*/identity.json")).read_text())
        assert identity["git"]["commit"] == "fixture" and identity["installation"]["name"] == "training"
        assert latest["settings"]["values"] == {key: setting.value for key, setting in load_settings(REPOSITORY).items()}
        assert [c["result"] for c in latest["change_sets"]] == ["executed"] * 4
        slot = "B" if stage_only else "A"
        assert f"SimulationRuntimeEndpoint=RELEASE_{slot}" in bound
        assert f"RuntimeEndpoint{slot}Version=2" in bound
        # The live checks and the final smoke check expect the camera state [camera] device_id gives.
        assert expectations == [("live", stage_only), ("smoke", stage_only)]
    # Only an accepted release names the manual post-deploy checks of docs 28, 32 and 29.
    manual = ("Manual post-deploy checks: deployment/acceptance/check_public_sop_browser.cjs (architecture/28), "
              "check_prepared_simulation_browser.cjs (architecture/32), "
              "and check_gateway_secret verify (architecture/29).")
    assert (manual in capsys.readouterr().out) is not (backend_fails or acceptance_fails)
    # Every stack update is a prepared change set that the stateful-resource guard executes.
    deploys = [c for c in commands if "deploy" in c]
    assert deploys and all(c[c.index("--method") + 1] == "prepare-change-set" for c in deploys)
    application = [c for c in deploys if "WildfireLabApplication" in c]
    assert not [arg for c in application for arg in c
                if arg.startswith(("EnableIntegratedWorkspace=", "AllowLegacyRuntimeDefault="))]
    assert all({"RequestsPerIpPer5Minutes=600", "WorkRequestsPerIpPer5Minutes=20", "TomtomCallsPerDay=1000",
                "AgentRequestsPerDay=500", "GeocodingRequestsPerDay=1000", "MaxStudyRadiusKm=15.0"} <= set(c)
               for c in application)
    # Every application pass carries the staging allowlist (both lists empty when public), so staging never opens
    # beyond its networks, not even between the passes.
    allowlist = ({"StagingAllowedIpv4Cidrs=198.51.100.0/24", "StagingAllowedIpv6Cidrs=2001:db8::/32"} if stage_only
                 else {"StagingAllowedIpv4Cidrs=", "StagingAllowedIpv6Cidrs="})
    assert application and all(allowlist <= set(c) for c in application)
    # Every application pass carries the installation's [camera] values ("" without the section).
    camera = ({"CameraApiBaseUrl=https://wildfireai.com", "CameraDeviceId=fixture-device", "CameraAlias=camera_b"}
              if stage_only else {"CameraApiBaseUrl=", "CameraDeviceId=", "CameraAlias="})
    assert all(camera <= set(c) for c in application)
    assert not any("bootstrap" in c for c in commands), "bootstrap runs only when the CDK stack is missing"
    assert clients["cloudformation"].execute_change_set.call_count == len(deploys)
    build = clients["codebuild"].start_build.call_args.kwargs
    assert build["sourceVersion"] == "pinned-version"
    assert build["sourceLocationOverride"].startswith("sources/sources/")
    installs = [c for c in commands if c[:2] == ["npm", "ci"]]
    assert len(installs) == 2 and all("--ignore-scripts" in c for c in installs)
    # Managed public guidance runs its cdk through the deployer's own runner, in this release's records folder,
    # without a second npm install.
    assert guidance == ([{"folder": next((state / "releases").iterdir()), "root": tmp_path, "install": False,
                          "acknowledged": acknowledged, "parameters": operator_parameters(installation)}]
                        if stage_only else [])
    # Every CDK stack's bucket policies admit the installation file's operator roles (L1, D52).
    operators = f"OperatorRoleArns={operator_parameters(installation)['OperatorRoleArns']}"
    assert [operators in c for c in deploys if "WildfireLabFoundation" in c] == [True]
    # The application stack has no WorkspaceAssetsBucket output yet in either case, so the trail names no site bucket.
    assert [("WorkspaceAssetsBucketName=" in c) for c in deploys if "WildfireLabFoundation" in c] == [True]
    assert all(operators in c for c in application)
    assert len(received) == len(deploys) + 1 and set(received) == {acknowledged}
    assert (["guidance-synth"] in commands) is stage_only
    # The CDK CLI prefers caller key variables to AWS_PROFILE, so every cdk command (public guidance's runs through
    # the same runner) gets the installation's profile and none of them.
    cdk_environments = [env for args, env in zip(commands, environments)
                        if args[0].endswith("/node_modules/.bin/cdk") or args == ["guidance-synth"]]
    # The UI bundle is still searched for the caller's key values, which never reach the UI build environment.
    ui_builds = [env for args, env in zip(commands, environments) if args[:3] == ["npm", "run", "build"]]
    # These environments and the scan set also hold the real shell's values, so failures show only names and counts.
    profiles = sorted({env["AWS_PROFILE"] for env in cdk_environments})
    carried = [sorted(env.keys() & caller_keys.keys()) for env in cdk_environments]
    unscanned = sorted(name for name, value in caller_keys.items() if not all(value in found for found in scanned))
    leaked = sorted({name for name, value in caller_keys.items() for env in ui_builds
                     if name in env or value in env.values()})
    counts = len(carried), len(ui_builds), len(scanned)
    assert profiles == [installation.profile] and carried == [[]] * counts[0]
    assert (counts[1:], unscanned, leaked) == ((1, 1), [], [])
    clients["s3"].get_object.assert_not_called()
    clients["cloudformation"].delete_stack.assert_not_called()


# Print each CDK stack's parameter names, synthesized offline in process as the infra tests do.
STACK_PARAMETERS = """
const {App} = require("aws-cdk-lib");
const {Template} = require("aws-cdk-lib/assertions");
const {Foundation, Application} = require("./lib/stacks");
const {PublicGuidance} = require("./lib/public-guidance");
const app = new App();
const foundation = new Foundation(app, "Foundation", {prefix: "training-alpha-0123456789"});
const stacks = {foundation, application: new Application(app, "Application", {foundation, imageTag: "checked"}),
  "public-guidance": new PublicGuidance(app, "Guidance", {})};
console.log(JSON.stringify(Object.fromEntries(Object.entries(stacks).map(([name, stack]) =>
  [name, Object.keys(Template.fromStack(stack).toJSON().Parameters)]))));
"""


def test_the_foundation_records_writes_to_the_application_site_bucket(make_installation):
    """L6, D53: the trail's data selector names the site bucket from the application stack's output; "" before that
    stack exists."""
    installation = make_installation()
    application = {"Outputs": [{"OutputKey": "WorkspaceAssetsBucket", "OutputValue": "fixture-site-bucket"}]}
    for stacks, bucket in (({"foundation": None, "application": application, "backend": None}, "fixture-site-bucket"),
                           ({"foundation": None, "application": None, "backend": None}, "")):
        assert deploy_workspace.foundation_parameters(installation, stacks)["WorkspaceAssetsBucketName"] == bucket


def test_the_deployer_passes_exactly_the_parameters_each_cdk_stack_declares(make_installation):
    """W2.11, DEPL-6: every parameter the deployer passes is declared, and each stack declares no other."""
    # Node with the fixed script above, in the CDK app's folder; no shell.
    # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
    completed = subprocess.run(["node", "--import", "tsx", "-e", STACK_PARAMETERS],  # nosec B603 B607
                               cwd=REPOSITORY / "deployment/infra", capture_output=True, text=True, timeout=300,
                               check=False)
    assert completed.returncode == 0, completed.stderr[-2000:]
    declared = {name: set(parameters) - {"BootstrapVersion"}
                for name, parameters in json.loads(completed.stdout.strip().splitlines()[-1]).items()}
    installation = make_installation()
    foundation = dict.fromkeys(("RepositoryArn", "TrafficSimulatorRepositoryArn", "FiretruckRepositoryArn",
                                "RadioRepositoryArn", "FireRiskRepositoryArn"), "arn:fixture")
    passed = {
        # A new installation: no stack exists yet.
        "foundation": deploy_workspace.foundation_parameters(installation, dict.fromkeys(
            ("foundation", "application", "backend"))),
        # The services pass of a new installation: no Runtime yet, so the binding is the endpoint defaults.
        "application": (deploy_workspace.application_parameters(installation, foundation, {}, {})
                        | deploy_workspace.initial_parameters(None, "", {})),
        # deploy() passes exactly these to deploy_public_sop (test_deployment_binds_backend_before_enabling_...).
        "public-guidance": operator_parameters(installation),
    }
    assert {name: set(values) for name, values in passed.items()} == declared


def test_apply_checks_locally_before_bootstrapping_cdk(tmp_path, monkeypatch, make_installation):
    commands = []

    class Stop(Exception):
        pass

    def command(args, *_):
        commands.append(list(map(str, args)))
        if "audit" in commands[-1]:
            raise Stop
    monkeypatch.setattr(deploy_workspace, "ROOT", tmp_path)
    monkeypatch.setattr(deploy_workspace, "command", command)
    for bootstrap in (True, False):
        commands.clear()
        with pytest.raises(Stop):
            deploy_workspace.deploy(MagicMock(), make_installation(), {}, {}, {}, tmp_path, bootstrap=bootstrap)
        bootstraps = [c for c in commands if "bootstrap" in c]
        assert not bootstraps, "A failed local check must precede even bootstrap writes"


def fake_urlopen(monkeypatch, *replies):
    """Answer smoke-check requests in order; a (status, body) reply other than 200 raises HTTPError, as urllib does."""
    pending = list(replies)

    def urlopen(request, timeout):
        assert request.full_url.startswith("https://workspace.invalid/") and timeout == 30
        status, body = pending.pop(0)
        if status != 200:
            raise urllib.error.HTTPError(request.full_url, status, "error", {}, io.BytesIO(body))
        response = io.BytesIO(body)
        response.status = status
        return response
    monkeypatch.setattr(deploy_workspace.urllib.request, "urlopen", urlopen)


BOUND = {"access_mode": "public_demo", "agent_configured": True, "geocoder_configured": True}
STAGING_BLOCK = json.dumps({"error": {"code": "NETWORK_NOT_ALLOWED", "message": "staging only"}}).encode()


@pytest.mark.parametrize("staging,status,config,error", [
    (False, 200, BOUND, None),
    # From an allowlisted machine, staging is the full demo workspace.
    (True, 200, BOUND, None),
    # From any other machine, the WAF staging block is the expected answer, not a failure.
    (True, 403, STAGING_BLOCK, None),
    (False, 403, STAGING_BLOCK, "Workspace check failed"),
    (True, 403, b"<html>403 Forbidden</html>", "Workspace check failed"),
    (True, 429, STAGING_BLOCK, "Workspace check failed"),
    (False, 200, {**BOUND, "access_mode": "closed"}, "binding is not ready"),
    (True, 200, {**BOUND, "access_mode": "closed"}, "binding is not ready"),
    (False, 200, {**BOUND, "access_mode": "local"}, "binding is not ready"),
    # A sign-in mode no longer exists, so a server still reporting one fails the check.
    (False, 200, {**BOUND, "access_mode": "authenticated"}, "binding is not ready"),
    (False, 200, {**BOUND, "agent_configured": False}, "binding is not ready"),
    (True, 200, {**BOUND, "geocoder_configured": False}, "binding is not ready"),
])
def test_smoke_checks_a_bound_demo_workspace_and_accepts_the_staging_block(monkeypatch, staging, status, config,
                                                                            error):
    body = config if isinstance(config, bytes) else json.dumps(config).encode()
    fake_urlopen(monkeypatch, (200, b"<html>workspace</html>"), (status, body))
    if error is None:
        checks = deploy_workspace.smoke("https://workspace.invalid", staging)
        assert checks["anonymous_access"] == ("staging allowlist only" if staging else "enabled")
        assert checks["broker_configuration"] == ("passed" if status == 200 else
                                                  "not checked: WAF blocked this machine, which is outside "
                                                  "[access] staging_allowed_cidrs")
    else:
        with pytest.raises(RuntimeError, match=error):
            deploy_workspace.smoke("https://workspace.invalid", staging)


@pytest.mark.parametrize("camera,reported,error", [
    (False, None, None), (False, False, None), (True, True, None),
    (True, False, "Workspace camera status does not match"), (False, True, "Workspace camera status does not match"),
])
def test_smoke_requires_the_camera_status_the_installation_gives(monkeypatch, camera, reported, error):
    config = BOUND if reported is None else {**BOUND, "camera_configured": reported}
    fake_urlopen(monkeypatch, (200, b"<html>workspace</html>"), (200, json.dumps(config).encode()))
    if error:
        with pytest.raises(RuntimeError, match=error):
            deploy_workspace.smoke("https://workspace.invalid", camera=camera)
    else:
        checks = deploy_workspace.smoke("https://workspace.invalid", camera=camera)
        assert checks["camera"] == ("configured" if camera else "not configured")


@pytest.mark.parametrize("page,error", [((404, b"missing"), "Workspace check failed: /"),
                                        ((200, b"plain text"), "did not return the UI")])
def test_smoke_requires_the_published_ui(monkeypatch, page, error):
    fake_urlopen(monkeypatch, page)
    with pytest.raises(RuntimeError, match=error):
        deploy_workspace.smoke("https://workspace.invalid", True)
    with pytest.raises(ValueError, match="requires HTTPS"):
        deploy_workspace.smoke("http://workspace.invalid")


@pytest.mark.parametrize("url", ["http://workspace.invalid/", "file:///etc/hosts", "ftp://workspace.invalid/"])
def test_smoke_fetch_refuses_every_scheme_but_https_before_opening(monkeypatch, url):
    """fetch() checks the scheme before urllib opens anything, so no file:// or plain-HTTP URL is read (B310)."""
    monkeypatch.setattr(deploy_workspace.urllib.request, "urlopen", lambda *_, **__: pytest.fail("urlopen ran"))
    with pytest.raises(ValueError, match="requires HTTPS"):
        deploy_workspace.fetch(url)


def test_environment_choices_match_backend_template():
    text = (REPOSITORY / "backend/deployment/templates/wildfire-backend-stack.yaml").read_text()
    block = re.search(r"^  Environment:\n((?:    .*\n)+)", text, re.MULTILINE)[1]
    assert tuple(re.findall(r"^      - (\S+)$", block, re.MULTILINE)) == ENVIRONMENTS


def test_environment_is_required_and_limited_to_template_values(make_installation):
    assert make_installation({"aws": {"environment": "staging"}}).environment == "staging"
    for value in (None, "prod"):
        with pytest.raises(ValueError, match=r"\[aws\] environment"):
            make_installation({"aws": {"environment": value}})


@pytest.mark.parametrize("existing,requested,allowed", [
    (None, "staging", True), ("production", "production", True), ("", "development", True),
    ("production", "staging", False), ("staging", "production", False),
])
def test_existing_backend_environment_cannot_change(existing, requested, allowed):
    stack = None if existing is None else {
        "StackName": "training-backend", "Parameters": [{"ParameterKey": "Environment", "ParameterValue": existing}]}
    if allowed:
        require_environment(stack, requested)
    else:
        with pytest.raises(ValueError, match=f"training-backend was deployed with environment {existing}, "
                                             rf'not {requested}. Set \[aws\] environment = "{existing}"'):
            require_environment(stack, requested)
    with pytest.raises(ValueError, match="must be one of"):
        require_environment(None, "prod")


def test_backend_environment_change_stops_before_any_upload(tmp_path):
    target = Target("123456789012", "us-west-2", "training")
    session = MagicMock()
    client = session.client.return_value
    client.describe_stacks.return_value = {"Stacks": [{
        "StackName": target.stack("backend"), "StackStatus": "UPDATE_COMPLETE",
        "Tags": [{"Key": k, "Value": v} for k, v in target.tags.items()],
        "Parameters": [{"ParameterKey": "Environment", "ParameterValue": "production"}]}]}
    with pytest.raises(ValueError, match="deployed with environment production"):
        deploy_backend(session, target, tmp_path, "sources", {}, {}, SimpleNamespace(environment="staging"), tmp_path)
    client.put_object.assert_not_called()
    client.create_change_set.assert_not_called()
    assert not (tmp_path / "backend-assets.zip").exists()


def test_lambda_packages_are_exactly_the_ones_the_backend_template_deploys(monkeypatch):
    from deployment import deploy_backend as backend
    installs = []
    monkeypatch.setattr(backend.subprocess, "run", lambda command, check: installs.append(command))
    packages = backend.lambda_sources(REPOSITORY)
    template = (REPOSITORY / "backend/deployment/templates/nested/2-lambda-functions.yaml").read_text()
    assert set(packages) == set(re.findall(r"/lambda-packages/([a-z-]+)-\$\{Environment\}\.zip", template))
    # RTGW-13: boto3 only for geocode-service, weather-dtn-service and the custom resource, the three handlers that
    # import it.
    assert [command[command.index("--constraint") + 2:] for command in installs] == [
        ["boto3"], ["boto3", "-r", str(REPOSITORY / "backend/gateway/weather/requirements-dtn.txt")], ["boto3"]]


def test_runtime_release_tests_carry_the_checks_and_only_deployed_handlers():
    import zipfile

    from deployment.deploy_backend import runtime_source
    names = set(zipfile.ZipFile(io.BytesIO(runtime_source(REPOSITORY))).namelist())
    tests = "release-tests/"
    assert {tests + "backend/runtime/tests/checks_support.py",
            tests + "backend/runtime/tests/test_runtime_geocoding.py",
            tests + "deployment/validate_infra_baseline.py",
            tests + "backend/deployment/lambda/custom-resources/get_cognito_client_secret.py"} <= names
    assert not [name for name in names if "api_key_provider" in name or "oauth_provider" in name]


def test_contract_check_runs_the_generator_of_the_given_root(tmp_path):
    script = tmp_path / "scripts/export_integration_contracts.py"
    script.parent.mkdir()
    script.write_text("import pathlib, sys\nassert sys.argv[1:] == ['--check']\n"
                      "pathlib.Path('checked-in').write_text(str(pathlib.Path.cwd()))\n")
    verify_contracts(tmp_path)
    assert Path((tmp_path / "checked-in").read_text()).resolve() == tmp_path.resolve()
    script.write_text("import sys\nprint('Generated contract drift (run without --check): schemas/x.json', "
                      "file=sys.stderr)\nsys.exit(1)\n")
    with pytest.raises(ValueError, match="contract check failed .*schemas/x.json"):
        verify_contracts(tmp_path)


CURRENT_BACKEND = {"Environment": "staging", "GeocodeServiceS3Version": "version"}
CURRENT_APPLICATION = {"SimulationRuntimeArn": "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/fixture",
                       "SimulationRuntimeEndpoint": "RELEASE_A", "RuntimeEndpointAVersion": "4",
                       "RuntimeEndpointBVersion": "3"}


@pytest.mark.parametrize("component, parameters, missing", [
    ("backend", {"Environment": "staging"}, "GeocodeServiceS3Version"),
    ("application", {"SimulationRuntimeArn": CURRENT_APPLICATION["SimulationRuntimeArn"]}, "RuntimeEndpointAVersion"),
    ("application", {**CURRENT_APPLICATION, "RuntimeEndpointBVersion": ""}, "RuntimeEndpointBVersion"),
    (None, None, None),
])
def test_preflight_refuses_installations_from_before_the_removed_migrations(
        tmp_path, monkeypatch, make_installation, component, parameters, missing):
    """W2.11, DEPL-4: stacks that predate the removed migrations stop before any secret or AWS write."""
    installation = make_installation({"aws": {"environment": "staging"}})
    target = installation.target
    session = MagicMock()
    session.get_partition_for_region.return_value = "aws"
    client = session.client.return_value
    client.get_caller_identity.return_value = {"Account": target.account}
    operator_ready(monkeypatch, client)
    client.describe_availability_zones.return_value = {"AvailabilityZones": [{"ZoneName": "a"}, {"ZoneName": "b"}]}
    client.get_secret_value.return_value = {"SecretString": json.dumps(
        dict.fromkeys(("DTN_CLIENT_ID", "DTN_CLIENT_SECRET", "TOMTOM_API_KEY"), "fixture-value"))}
    stacks = {"backend": CURRENT_BACKEND, "application": CURRENT_APPLICATION}
    if component:
        stacks[component] = parameters

    def inspect(_client, name, _target, allow_unmanaged=False):
        found = next((key for key in stacks if name == target.stack(key)), None)
        # The backend names a Runtime, which preflight reads only after the migration check.
        return found and {"StackName": name, "Outputs": [{"OutputKey": "RuntimeId", "OutputValue": "runtime"}],
                          "Parameters": [{"ParameterKey": key, "ParameterValue": value}
                                         for key, value in stacks[found].items()]}
    monkeypatch.setattr(deploy_workspace, "ROOT", tmp_path)
    monkeypatch.setattr(deploy_workspace, "inspect_stack", inspect)
    monkeypatch.setattr(deploy_workspace, "read_audio", lambda *_: ("input/a.mp3", b"audio"))
    monkeypatch.setattr(deploy_workspace.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(deploy_workspace, "source_bundle", lambda root: b"zip")
    for name in ("require_unexpired_image_exceptions", "runtime_source", "verify_contracts"):
        monkeypatch.setattr(deploy_workspace, name, lambda root: None)
    if component is None:
        assert deploy_workspace.preflight(session, installation).provider["TOMTOM_API_KEY"] == "fixture-value"
        client.get_agent_runtime.assert_called_once_with(agentRuntimeId="runtime")
        return
    with pytest.raises(ValueError) as refused:
        deploy_workspace.preflight(session, installation)
    message = str(refused.value)
    assert message.startswith(target.stack(component)) and missing in message
    assert "architecture/29-workspace-operations.md" in message and message.endswith("No AWS changes made.")
    client.get_secret_value.assert_not_called()
    client.get_agent_runtime.assert_not_called()


@pytest.mark.parametrize("backend_environment,bootstrapped", [(None, True), (None, False), ("production", True)])
def test_preflight_runs_offline_release_checks_for_its_root(
        tmp_path, monkeypatch, make_installation, backend_environment, bootstrapped):
    recording = tmp_path / "recording.mp3"
    recording.write_bytes(b"audio")
    installation = make_installation({"aws": {"environment": "staging"}, "radio": {"recordings": [str(recording)]}})
    target = installation.target
    session = MagicMock()
    session.get_partition_for_region.return_value = "aws"
    client = session.client.return_value
    client.get_caller_identity.return_value = {"Account": target.account}
    operator_ready(monkeypatch, client)
    client.describe_availability_zones.return_value = {"AvailabilityZones": [{"ZoneName": "a"}, {"ZoneName": "b"}]}
    client.get_secret_value.return_value = {"SecretString": json.dumps(
        dict.fromkeys(("DTN_CLIENT_ID", "DTN_CLIENT_SECRET", "TOMTOM_API_KEY"), "fixture-value"))}
    backend = backend_environment and {"StackName": target.stack("backend"), "Parameters": [
        {"ParameterKey": "Environment", "ParameterValue": backend_environment}]}
    calls = []

    def inspect(_client, name, _target, allow_unmanaged=False):
        if name == deploy_workspace.BOOTSTRAP_STACK:
            assert allow_unmanaged, "the shared CDK bootstrap stack carries no installation tags"
            return {"StackName": name} if bootstrapped else None
        return backend if name.endswith("-backend") else None
    monkeypatch.setattr(deploy_workspace, "ROOT", tmp_path)
    monkeypatch.setattr(deploy_workspace, "inspect_stack", inspect)
    monkeypatch.setattr(deploy_workspace.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(deploy_workspace, "source_bundle", lambda root: calls.append(("source_bundle", root)) or b"zip")
    for name in ("require_unexpired_image_exceptions", "runtime_source", "verify_contracts"):
        monkeypatch.setattr(deploy_workspace, name, lambda root, name=name: calls.append((name, root)))
    if backend_environment:
        with pytest.raises(ValueError, match="deployed with environment production"):
            deploy_workspace.preflight(session, installation)
        client.get_secret_value.assert_not_called()
        assert calls == []
    else:
        result = deploy_workspace.preflight(session, installation)
        assert result.provider["TOMTOM_API_KEY"] == "fixture-value" and len(result.audio) == 1
        assert result.bootstrap is not bootstrapped
        assert result.digest == hashlib.sha256(b"zip").hexdigest()
        assert calls == [(name, tmp_path) for name in (
            "require_unexpired_image_exceptions", "source_bundle", "runtime_source", "verify_contracts")]
