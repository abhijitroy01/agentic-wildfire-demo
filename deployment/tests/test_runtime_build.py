"""Runtime overlap requires source/build proof; activation and scans remain ordered."""
import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from deployment import runtime_build
from deployment.deployment_target import Target

ROOT = Path(__file__).resolve().parents[2]
TARGET = Target("123456789012", "us-west-2", "runtime-demo")
INSTALLATION = SimpleNamespace(environment="production")
BUCKET = "fixture-runtime-sources"
PROJECT = TARGET.stack("backend")+"-runtime-build-us-west-2"
URI = f"{TARGET.account}.dkr.ecr.{TARGET.region}.amazonaws.com/runtime-repository"
MANIFEST = '{"schemaVersion":2,"mediaType":"application/vnd.docker.distribution.manifest.v2+json","layers":[]}'
IMAGE_DIGEST = "sha256:"+hashlib.sha256(MANIFEST.encode()).hexdigest()


@pytest.fixture
def captured():
    return {
        "payload": b"captured Runtime source with offline tests",
        "packages": {"geocode-service": b"fixture"}, "template": "root-template",
        "templates": {"4-agentcore-resources.yaml": (
            ROOT / "backend/deployment/templates/nested/4-agentcore-resources.yaml").read_bytes()},
    }


@pytest.fixture
def setup(captured, tmp_path):
    release = tmp_path / "release-current"
    release.mkdir()
    session = MagicMock()
    clients = {name: MagicMock() for name in ("cloudformation", "codebuild", "s3", "ecr")}
    session.client.side_effect = clients.__getitem__
    clients["cloudformation"].describe_stacks.return_value = {"Stacks": [{
        "StackStatus": "UPDATE_COMPLETE", "Tags": [{"Key": k, "Value": v} for k, v in TARGET.tags.items()],
        "Parameters": [{"ParameterKey": key, "ParameterValue": value} for key, value in {
            "Environment": "production", "DeployRuntime": "true"}.items()],
        "Outputs": [{"OutputKey": "CodeBuildProjectName", "OutputValue": PROJECT},
                    {"OutputKey": "RuntimeECRRepositoryUri", "OutputValue": URI}],
    }]}
    clients["cloudformation"].describe_stack_resource.side_effect = lambda **kw: {
        "StackResourceDetail": {"PhysicalResourceId":
            "owned-agentcore-stack" if kw["LogicalResourceId"] == "AgentCoreStack" else PROJECT}}
    properties = runtime_build.build_properties(captured)
    variables = {
        "AWS_DEFAULT_REGION": TARGET.region, "AWS_ACCOUNT_ID": TARGET.account,
        "IMAGE_REPO_NAME": "runtime-repository", "IMAGE_TAG": "deployment-required",
        "RUNTIME_SOURCE_BUCKET": BUCKET, "ENVIRONMENT": "production",
    }
    project = {
        "name": PROJECT, "arn": f"arn:aws:codebuild:{TARGET.region}:{TARGET.account}:project/{PROJECT}",
        "serviceRole": f"arn:aws:iam::{TARGET.account}:role/{TARGET.stack('backend')}-runtime-build",
        "environment": {
            "type": "ARM_CONTAINER", "image": properties["Environment"]["Image"],
            "computeType": properties["Environment"]["ComputeType"], "privilegedMode": True,
            "environmentVariables": [{"name": name, "value": value, "type": "PLAINTEXT"}
                                     for name, value in variables.items()],
        },
        "source": {"type": "S3", "location": BUCKET+"/sources/deployment-required.zip",
                   "buildspec": properties["Source"]["BuildSpec"]},
    }
    clients["codebuild"].batch_get_projects.return_value = {"projects": [project]}
    clients["codebuild"].start_build.return_value = {"build": {"id": PROJECT+":new-build"}}
    clients["s3"].put_object.return_value = {"VersionId": "current-source-version"}
    from deployment.deploy_backend import capture_backend
    (release / "backend-assets.zip").write_bytes(capture_backend(captured))
    return session, clients, release, project


def prior_receipt(captured, release):
    value = {
        **runtime_build.source_identity(TARGET, BUCKET, release.parent / "release-old", captured),
        "source_version": "old-source-version", "project_name": PROJECT, "repository_uri": URI,
        "environment": "production",
        "build_id": PROJECT+":verified-build", "image_digest": IMAGE_DIGEST,
        "buildspec": runtime_build.build_properties(captured)["Source"]["BuildSpec"],
        "verified_scan": {"component": "runtime", "digest": IMAGE_DIGEST, "status": "passed"},
    }
    return value


def verified_build(prior, captured):
    properties = runtime_build.build_properties(captured)
    variables = {
        "AWS_DEFAULT_REGION": TARGET.region, "AWS_ACCOUNT_ID": TARGET.account,
        "IMAGE_REPO_NAME": "runtime-repository", "IMAGE_TAG": prior.get("build_image_tag", prior["image_tag"]),
        "RUNTIME_SOURCE_BUCKET": prior.get("build_source_bucket", prior["source_bucket"]),
        "ENVIRONMENT": "production",
    }
    return {
        "id": prior["build_id"],
        "arn": f"arn:aws:codebuild:{TARGET.region}:{TARGET.account}:build/{prior['build_id']}",
        "projectName": PROJECT, "buildStatus": "SUCCEEDED",
        "serviceRole": f"arn:aws:iam::{TARGET.account}:role/{TARGET.stack('backend')}-runtime-build",
        "environment": {
            "type": "ARM_CONTAINER", "image": properties["Environment"]["Image"],
            "computeType": properties["Environment"]["ComputeType"], "privilegedMode": True,
            "environmentVariables": [{"name": name, "value": value, "type": "PLAINTEXT"}
                                     for name, value in variables.items()],
        },
        "source": {"type": "S3", "location":
            f"{prior.get('build_source_bucket', prior['source_bucket'])}/"
            f"{prior.get('build_source_key', prior['source_key'])}", "buildspec": prior["buildspec"]},
        "sourceVersion": prior.get("build_source_version", prior["source_version"]),
    }


def test_upgrade_start_versions_captured_source_and_does_not_change_cloudformation(setup, captured):
    session, clients, release, _ = setup
    receipt = runtime_build.start_runtime_build(session, TARGET, BUCKET, INSTALLATION, release, captured)
    assert receipt["mode"] == "build"
    runtime_build.validate_early_build(receipt, TARGET, BUCKET, release, captured)
    clients["cloudformation"].create_change_set.assert_not_called()
    clients["cloudformation"].execute_change_set.assert_not_called()
    sent = clients["codebuild"].start_build.call_args.kwargs
    assert sent["sourceVersion"] == "current-source-version"
    assert sent["sourceLocationOverride"] == f"{BUCKET}/{receipt['source_key']}"
    assert sent["environmentVariablesOverride"] == [
        {"name": "IMAGE_TAG", "value": release.name, "type": "PLAINTEXT"}]
    assert (release / "runtime-source.zip").read_bytes() == captured["payload"]


@pytest.mark.parametrize("change", ["new-install", "x86", "recipe", "source-bucket", "role", "extra-env"])
def test_incompatible_builder_falls_back_before_any_upload_or_build(setup, captured, change):
    session, clients, release, project = setup
    if change == "new-install":
        clients["cloudformation"].describe_stacks.return_value["Stacks"][0]["Parameters"][1]["ParameterValue"] = "false"
    elif change == "x86":
        project["environment"]["type"] = "LINUX_CONTAINER"
    elif change == "recipe":
        project["source"]["buildspec"] += "\n# unreviewed recipe"
    elif change == "source-bucket":
        project["source"]["location"] = "other-source-bucket/source.zip"
    elif change == "role":
        project["serviceRole"] = "arn:aws:iam::123456789012:role/foreign-role"
    else:
        project["environment"]["environmentVariables"].append({"name": "EXTRA", "value": "x", "type": "PLAINTEXT"})
    assert runtime_build.start_runtime_build(session, TARGET, BUCKET, INSTALLATION, release, captured) is None
    clients["s3"].put_object.assert_not_called()
    clients["codebuild"].start_build.assert_not_called()


def test_receipt_rejects_changed_source_or_release_before_deployment(setup, captured):
    session, _, release, _ = setup
    receipt = runtime_build.start_runtime_build(session, TARGET, BUCKET, INSTALLATION, release, captured)
    for changed in ({**captured, "payload": b"changed code or tests"},
                    {**captured, "templates": {"4-agentcore-resources.yaml":
                        captured["templates"]["4-agentcore-resources.yaml"].replace(
                            b"test -f schemas/api-schema.yaml", b"test -f unexpected.yaml")}}):
        with pytest.raises(ValueError, match="identity/source"):
            runtime_build.validate_early_build(receipt, TARGET, BUCKET, release, changed)


def test_reuse_preserves_original_build_proof_and_requests_scan_without_publishing_tag(setup, captured):
    session, clients, release, _ = setup
    prior = prior_receipt(captured, release)
    clients["codebuild"].batch_get_builds.return_value = {"builds": [verified_build(prior, captured)]}
    clients["ecr"].describe_images.return_value = {"imageDetails": [{"imageDigest": IMAGE_DIGEST}]}
    receipt = runtime_build.start_runtime_build(
        session, TARGET, BUCKET, INSTALLATION, release, captured, previous_receipt=prior)
    assert receipt["mode"] == "reuse" and receipt["image_digest"] == IMAGE_DIGEST
    assert receipt["build_source_version"] == prior["source_version"]
    assert receipt["source_version"] == "current-source-version"
    clients["ecr"].start_image_scan.assert_called_once()
    clients["ecr"].put_image.assert_not_called()
    clients["codebuild"].start_build.assert_not_called()
    # A second reuse still verifies the original compilation's version, not the intermediate release's upload.
    second = {**receipt, "verified_scan": prior["verified_scan"]}
    receipt2 = runtime_build.start_runtime_build(
        session, TARGET, BUCKET, INSTALLATION, release, captured, previous_receipt=second)
    assert receipt2["mode"] == "reuse" and receipt2["build_source_version"] == prior["source_version"]


@pytest.mark.parametrize("change", ["fingerprint", "scan", "build-status", "build-version", "build-architecture",
                                  "build-recipe", "build-role", "build-env", "digest"])
def test_missing_or_mismatched_proof_builds_normally(setup, captured, change):
    session, clients, release, _ = setup
    prior = prior_receipt(captured, release)
    build = verified_build(prior, captured)
    clients["ecr"].describe_images.return_value = {"imageDetails": [{"imageDigest": IMAGE_DIGEST}]}
    if change == "fingerprint":
        prior.pop("runtime_fingerprint")
    elif change == "scan":
        prior["verified_scan"]["status"] = "failed"
    elif change == "build-status":
        build["buildStatus"] = "FAILED"
    elif change == "build-version":
        build["sourceVersion"] = "different source version"
    elif change == "build-architecture":
        build["environment"]["type"] = "LINUX_CONTAINER"
    elif change == "build-recipe":
        build["source"]["buildspec"] = "docker build .\n# original checks omitted"
    elif change == "build-role":
        build["serviceRole"] = "arn:aws:iam::123456789012:role/foreign-role"
    elif change == "build-env":
        build["environment"]["environmentVariables"].append({"name": "EXTRA", "value": "x", "type": "PLAINTEXT"})
    else:
        clients["ecr"].describe_images.return_value["imageDetails"][0]["imageDigest"] = "sha256:"+"a"*64
    clients["codebuild"].batch_get_builds.return_value = {"builds": [build]}
    receipt = runtime_build.start_runtime_build(
        session, TARGET, BUCKET, INSTALLATION, release, captured, previous_receipt=prior)
    assert receipt["mode"] == "build"
    clients["codebuild"].start_build.assert_called_once()
    clients["ecr"].put_image.assert_not_called()


def test_retag_verifies_the_manifest_and_refuses_a_conflicting_release_tag():
    client = MagicMock()
    receipt = {"image_digest": IMAGE_DIGEST, "image_tag": "release-new"}
    client.batch_get_image.return_value = {"images": [{
        "imageId": {"imageDigest": IMAGE_DIGEST}, "imageManifest": MANIFEST,
        "imageManifestMediaType": "application/vnd.docker.distribution.manifest.v2+json"}]}
    client.put_image.side_effect = ClientError({"Error": {"Code": "ImageTagAlreadyExistsException"}}, "PutImage")
    client.describe_images.return_value = {"imageDetails": [{"imageDigest": "sha256:"+"a"*64}]}
    with pytest.raises(ValueError, match="different digest"):
        runtime_build.retag_reused_image(client, "repository", receipt)
    client.put_image.side_effect = None
    client.put_image.return_value = {"image": {"imageId": {"imageDigest": IMAGE_DIGEST}}}
    runtime_build.retag_reused_image(client, "repository", receipt)
    client.batch_get_image.return_value["images"][0]["imageManifest"] += "tampered"
    with pytest.raises(ValueError, match="manifest"):
        runtime_build.retag_reused_image(client, "repository", receipt)


def runtime_manifest_alias(tag):
    return {"imageId": {"imageDigest": IMAGE_DIGEST, "imageTag": tag}, "imageManifest": MANIFEST,
            "imageManifestMediaType": "application/vnd.docker.distribution.manifest.v2+json"}


def test_repeated_runtime_reuse_tags_identical_aliases_and_keeps_original_proof(setup, captured):
    session, clients, second_release, _ = setup
    prior = prior_receipt(captured, second_release)
    clients["codebuild"].batch_get_builds.return_value = {"builds": [verified_build(prior, captured)]}
    clients["ecr"].describe_images.return_value = {"imageDetails": [{"imageDigest": IMAGE_DIGEST}]}
    second = runtime_build.start_runtime_build(
        session, TARGET, BUCKET, INSTALLATION, second_release, captured, previous_receipt=prior)
    second["verified_scan"] = deepcopy(prior["verified_scan"])
    third_release = second_release.parent / "release-third"
    third_release.mkdir()
    third = runtime_build.start_runtime_build(
        session, TARGET, BUCKET, INSTALLATION, third_release, captured, previous_receipt=second)
    assert third["mode"] == "reuse" and third["image_digest"] == IMAGE_DIGEST
    assert third["build_id"] == prior["build_id"]
    assert third["build_image_tag"] == prior["image_tag"]
    assert third["build_source_version"] == prior["source_version"]
    clients["ecr"].batch_get_image.return_value = {"images": [
        runtime_manifest_alias(prior["image_tag"]), runtime_manifest_alias(second["image_tag"])]}
    clients["ecr"].put_image.return_value = {"image": {"imageId": {"imageDigest": IMAGE_DIGEST}}}
    runtime_build.retag_reused_image(clients["ecr"], "repository", third)
    clients["ecr"].put_image.assert_called_once_with(
        repositoryName="repository", imageTag=third_release.name, imageManifest=MANIFEST,
        imageManifestMediaType="application/vnd.docker.distribution.manifest.v2+json", imageDigest=IMAGE_DIGEST)
    clients["codebuild"].start_build.assert_not_called()


@pytest.mark.parametrize("change", ["digest", "manifest", "media-type", "unsupported-type", "missing-manifest",
                                  "empty-manifest", "tampered-manifests", "failures", "missing-images"])
def test_runtime_retag_rejects_inconsistent_or_unverified_aliases_before_any_write(change):
    client = MagicMock()
    response = {"images": [runtime_manifest_alias("release-first"), runtime_manifest_alias("release-second")]}
    if change == "digest":
        response["images"][1]["imageId"]["imageDigest"] = "sha256:" + "a" * 64
    elif change == "manifest":
        response["images"][1]["imageManifest"] += " "
    elif change == "media-type":
        response["images"][1]["imageManifestMediaType"] = "application/vnd.oci.image.manifest.v1+json"
    elif change == "unsupported-type":
        for image in response["images"]:
            image["imageManifestMediaType"] = "application/vnd.oci.image.index.v1+json"
    elif change == "missing-manifest":
        response["images"][1].pop("imageManifest")
    elif change == "empty-manifest":
        for image in response["images"]:
            image["imageManifest"] = ""
    elif change == "tampered-manifests":
        for image in response["images"]:
            image["imageManifest"] += " "
    elif change == "failures":
        response["failures"] = [{"failureCode": "ImageNotFound"}]
    else:
        response["images"] = []
    client.batch_get_image.return_value = response
    with pytest.raises(ValueError, match="different digest" if change == "digest" else "manifest"):
        runtime_build.retag_reused_image(client, "repository", {
            "image_digest": IMAGE_DIGEST, "image_tag": "release-third"})
    client.put_image.assert_not_called()


def test_local_docker_cache_keeps_arm_offline_review_and_startup_checks(captured):
    properties = runtime_build.build_properties(captured)
    assert properties["Cache"] == {"Type": "LOCAL", "Modes": ["LOCAL_DOCKER_LAYER_CACHE"]}
    assert properties["Environment"]["Type"] == "ARM_CONTAINER"
    commands = properties["Source"]["BuildSpec"]
    assert commands.count("--network none") == 2 and "unittest discover" in commands
    assert "/app/check_startup.py" in commands and "CODEBUILD_BUILD_SUCCEEDING" in commands


@pytest.mark.parametrize("status", ["IN_PROGRESS", "SUCCEEDED"])
def test_resume_rejoins_the_original_build_without_another_upload_or_start(setup, captured, status):
    session, clients, release, _ = setup
    receipt = runtime_build.start_runtime_build(session, TARGET, BUCKET, INSTALLATION, release, captured)
    build = verified_build(receipt, captured)
    build["buildStatus"] = status
    clients["codebuild"].batch_get_builds.return_value = {"builds": [build]}
    clients["s3"].reset_mock()
    clients["codebuild"].start_build.reset_mock()
    assert runtime_build.resumable_early_build(release)
    resumed = runtime_build.resume_runtime_build(session, TARGET, BUCKET, INSTALLATION, release, captured)
    assert resumed == receipt
    clients["s3"].put_object.assert_not_called()
    clients["codebuild"].start_build.assert_not_called()
    clients["cloudformation"].create_change_set.assert_not_called()
    clients["ecr"].start_image_scan.assert_not_called()


def test_resume_without_an_early_request_keeps_the_serial_path(setup, captured):
    session, _, release, _ = setup
    assert runtime_build.resume_runtime_build(session, TARGET, BUCKET, INSTALLATION, release, captured) is None
    session.client.assert_not_called()


@pytest.mark.parametrize("marker", runtime_build.RESUME_BLOCKERS)
def test_resume_stops_before_any_sdk_call_after_backend_prepare_or_promotion(setup, captured, marker):
    session, clients, release, _ = setup
    runtime_build.start_runtime_build(session, TARGET, BUCKET, INSTALLATION, release, captured)
    (release / marker).write_text("{}")
    session.reset_mock()
    assert not runtime_build.resumable_early_build(release)
    with pytest.raises(ValueError, match="before backend preparation"):
        runtime_build.resume_runtime_build(session, TARGET, BUCKET, INSTALLATION, release, captured)
    session.client.assert_not_called()
    clients["cloudformation"].execute_change_set.assert_not_called()


@pytest.mark.parametrize("change", ["source", "version", "recipe", "tag", "architecture", "role", "environment",
                                  "failed", "stopped"])
def test_resume_rechecks_the_original_build_proof_before_joining(setup, captured, change):
    session, clients, release, _ = setup
    receipt = runtime_build.start_runtime_build(session, TARGET, BUCKET, INSTALLATION, release, captured)
    build = verified_build(receipt, captured)
    if change == "source":
        build["source"]["location"] = BUCKET+"/another-source.zip"
    elif change == "version":
        build["sourceVersion"] = "unreviewed-version"
    elif change == "recipe":
        build["source"]["buildspec"] += "\n# changed checks"
    elif change == "tag":
        build["environment"]["environmentVariables"][3]["value"] = "another-release"
    elif change == "architecture":
        build["environment"]["type"] = "LINUX_CONTAINER"
    elif change == "role":
        build["serviceRole"] = "arn:aws:iam::123456789012:role/foreign-role"
    elif change == "environment":
        build["environment"]["environmentVariables"][-1]["value"] = "staging"
    else:
        build["buildStatus"] = change.upper()
    clients["codebuild"].batch_get_builds.return_value = {"builds": [build]}
    clients["s3"].reset_mock()
    clients["codebuild"].start_build.reset_mock()
    with pytest.raises(ValueError, match="captured source/version/ARM recipe|failed or was stopped"):
        runtime_build.resume_runtime_build(session, TARGET, BUCKET, INSTALLATION, release, captured)
    clients["s3"].put_object.assert_not_called()
    clients["codebuild"].start_build.assert_not_called()


def test_resume_reused_digest_renews_scan_and_retains_original_source_proof(setup, captured):
    session, clients, release, _ = setup
    prior = prior_receipt(captured, release)
    clients["codebuild"].batch_get_builds.return_value = {"builds": [verified_build(prior, captured)]}
    clients["ecr"].describe_images.return_value = {"imageDetails": [{"imageDigest": IMAGE_DIGEST}]}
    receipt = runtime_build.start_runtime_build(
        session, TARGET, BUCKET, INSTALLATION, release, captured, previous_receipt=prior)
    receipt["scan_not_before"] = (datetime.now(UTC)-timedelta(days=2)).timestamp()
    (release / "backend-build-request.json").write_text(json.dumps(receipt))
    clients["s3"].reset_mock()
    clients["codebuild"].start_build.reset_mock()
    clients["ecr"].start_image_scan.reset_mock()
    resumed = runtime_build.resume_runtime_build(session, TARGET, BUCKET, INSTALLATION, release, captured)
    assert resumed["build_source_version"] == prior["source_version"]
    assert resumed["scan_not_before"] > receipt["scan_not_before"]
    assert json.loads((release / "backend-build-request.json").read_text()) == resumed
    clients["ecr"].start_image_scan.assert_called_once()
    clients["ecr"].put_image.assert_not_called()
    clients["codebuild"].start_build.assert_not_called()
    clients["s3"].put_object.assert_not_called()


def test_reuse_with_an_old_daily_limit_scan_compiles_a_new_image(setup, captured):
    session, clients, release, _ = setup
    prior = prior_receipt(captured, release)
    clients["codebuild"].batch_get_builds.return_value = {"builds": [verified_build(prior, captured)]}
    clients["ecr"].describe_images.return_value = {"imageDetails": [{"imageDigest": IMAGE_DIGEST}]}
    clients["ecr"].start_image_scan.side_effect = ClientError(
        {"Error": {"Code": "LimitExceededException"}}, "StartImageScan")
    clients["ecr"].describe_image_scan_findings.return_value = {
        "imageId": {"imageDigest": IMAGE_DIGEST}, "imageScanStatus": {"status": "COMPLETE"},
        "imageScanFindings": {"imageScanCompletedAt": datetime.now(UTC)-timedelta(days=2)},
    }
    receipt = runtime_build.start_runtime_build(
        session, TARGET, BUCKET, INSTALLATION, release, captured, previous_receipt=prior)
    assert receipt["mode"] == "build"
    clients["codebuild"].start_build.assert_called_once()
    clients["ecr"].put_image.assert_not_called()


@pytest.mark.parametrize("reuse,blocked", [(False, False), (True, False), (True, True)])
def test_early_receipt_joins_ordered_prepare_scan_and_activation(
        setup, captured, make_installation, monkeypatch, reuse, blocked):
    from deployment import deploy_backend
    session, clients, release, _ = setup
    installation = make_installation({"aws": {"environment": "production"}})
    runtime = MagicMock()
    clients["bedrock-agentcore-control"] = runtime
    stack = clients["cloudformation"].describe_stacks.return_value["Stacks"][0]
    stack["Parameters"].append({"ParameterKey": "RuntimeImageTag", "ParameterValue": "previous"})
    stack["Outputs"].append({"OutputKey": "RuntimeId", "OutputValue": "owned-runtime"})
    clients["cloudformation"].get_template.return_value = {
        "TemplateBody": captured["templates"]["4-agentcore-resources.yaml"].decode()}
    runtime.get_agent_runtime.return_value = {
        "status": "READY", "agentRuntimeVersion": "41",
        "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": URI+":"+release.name}},
        "environmentVariables": {"DEPLOYED_RuntimeBuild": release.name},
    }
    captured["packages"] = {name: b"fixture" for name in (
        "simulation-service", "geocode-service", "wildfire-service", "weather-dtn-service")}
    prior = prior_receipt(captured, release)
    clients["codebuild"].batch_get_builds.return_value = {"builds": [verified_build(prior, captured)]}
    clients["ecr"].describe_images.return_value = {"imageDetails": [{"imageDigest": IMAGE_DIGEST}]}
    receipt = runtime_build.start_runtime_build(
        session, TARGET, BUCKET, installation, release, captured, previous_receipt=prior if reuse else {})
    assert receipt["mode"] == ("reuse" if reuse else "build")
    if not reuse:
        build = verified_build(receipt, captured)
        clients["codebuild"].batch_get_builds.return_value = {"builds": [build]}
    events = []

    def change(_cfn, _target, _template, parameters, _release, phase, _folder, **_):
        events.append(phase)
        assert parameters["RuntimeImageTag"] == ("previous" if phase == "prepare" else release.name)
        return {"CodeBuildProjectName": PROJECT, "RuntimeECRRepositoryUri": URI, "RuntimeId": "owned-runtime"}

    def scan(*_, **kwargs):
        events.append("policy-scan")
        if reuse:
            assert kwargs["scan_not_before"] == receipt["scan_not_before"]
        if blocked:
            raise RuntimeError("Image scan blocked")

    monkeypatch.setattr(deploy_backend, "change_stack", change)
    monkeypatch.setattr(deploy_backend, "verify_image", scan)
    monkeypatch.setattr(runtime_build, "retag_reused_image", lambda *_: events.append("retag"))
    lab = dict.fromkeys(("SimulationInternalUrl", "SimulationContextSecretArn",
                         "SimulationAdapterSecurityGroupId", "SimulationAdapterSubnetIds"), "configured")
    args = (session, TARGET, ROOT, BUCKET, lab, {"DTN_CLIENT_ID": "fixture", "DTN_CLIENT_SECRET": "fixture"},
            installation, release)
    if blocked:
        with pytest.raises(RuntimeError, match="scan blocked"):
            deploy_backend.deploy_backend(*args, captured=captured, early_runtime_build=receipt)
        assert events == ["prepare", "policy-scan"]
    else:
        deploy_backend.deploy_backend(*args, captured=captured, early_runtime_build=receipt)
        assert events == (["prepare", "policy-scan", "retag", "activate"] if reuse else
                          ["prepare", "policy-scan", "activate"])
    assert clients["codebuild"].start_build.call_count == (0 if reuse else 1)
    source_uploads = [call for call in clients["s3"].put_object.call_args_list
                      if call.kwargs["Key"] == receipt["source_key"]]
    assert len(source_uploads) == 1
