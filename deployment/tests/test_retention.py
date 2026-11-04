"""What an installation keeps (D53): its log groups' retention (L3) and the images its releases use (L5)."""
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from deployment import retention
from deployment.deployment_target import stack_resources
from deployment.retention import (
    IMAGES_KEPT,
    LOG_RETENTION_DAYS,
    images_in_use,
    require_retained_images,
    retain_installation_logs,
)

RUNTIME_ID = "fixture_runtime-AbCdEf1234"
REGISTRY = "123456789012.dkr.ecr.us-west-2.amazonaws.com"


def summary(logical, kind, physical="", status="CREATE_COMPLETE"):
    """One ListStackResources entry; an empty physical ID is left out, as the API leaves it out."""
    return {"LogicalResourceId": logical, "ResourceType": kind, "ResourceStatus": status,
            **({"PhysicalResourceId": physical} if physical else {})}


class Pages:
    """A boto3 paginator whose pages come from a function of the call's parameters."""

    def __init__(self, pages):
        self.pages = pages

    def paginate(self, **parameters):
        return self.pages(**parameters)


class FakeCloudFormation:
    """ListStackResources for fixed stacks, in two pages each; an unknown stack raises ValidationError, as the API
    does, and a stack in fail raises a dropped connection."""

    def __init__(self, stacks, fail=()):
        self.stacks, self.fail, self.walked = stacks, set(fail), []

    def get_paginator(self, name):
        assert name == "list_stack_resources"
        return Pages(self.pages)

    def pages(self, StackName):
        self.walked.append(StackName)
        if StackName in self.fail:
            raise EndpointConnectionError(endpoint_url="https://cloudformation.invalid")
        if StackName not in self.stacks:
            raise ClientError({"Error": {"Code": "ValidationError", "Message": f"Stack {StackName} does not exist"}},
                              "ListStackResources")
        resources = self.stacks[StackName]
        return [{"StackResourceSummaries": resources[:1]}, {"StackResourceSummaries": resources[1:]}]


class FakeLogs:
    """DescribeLogGroups as the API answers it: a prefix query lists full entries, and a pattern query matches by
    substring (so "" matches every group) and lists only arn, creationTime and logGroupName."""

    def __init__(self, groups, fail=()):
        self.groups, self.fail, self.queries, self.puts = dict(groups), set(fail), [], []

    def get_paginator(self, name):
        assert name == "describe_log_groups"
        return Pages(self.pages)

    def pages(self, logGroupNamePrefix=None, logGroupNamePattern=None):
        self.queries.append({"prefix": logGroupNamePrefix, "pattern": logGroupNamePattern})
        if logGroupNamePattern is not None:
            return [{"logGroups": [{"arn": f"arn:fixture:{name}", "creationTime": 1, "logGroupName": name}
                                   for name in self.groups if logGroupNamePattern in name]}]
        return [{"logGroups": [{"logGroupName": name, "creationTime": 1, "storedBytes": 0,
                                **({"retentionInDays": days} if days else {})}
                               for name, days in self.groups.items() if name.startswith(logGroupNamePrefix)]}]

    def put_retention_policy(self, logGroupName, retentionInDays):
        if logGroupName in self.fail:
            raise ClientError({"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}},
                              "PutRetentionPolicy")
        self.puts.append((logGroupName, retentionInDays))
        self.groups[logGroupName] = retentionInDays


def session(cfn, logs):
    return SimpleNamespace(client={"cloudformation": cfn, "logs": logs}.__getitem__)


def test_stack_resources_walks_nested_stacks_with_physical_ids():
    nested, deeper, broken = ("arn:fixture:stack/nested", "arn:fixture:stack/deeper", "arn:fixture:stack/broken")
    cfn = FakeCloudFormation({
        "root": [summary("Bucket", "AWS::S3::Bucket", "root-bucket"),
                 summary("Nested", "AWS::CloudFormation::Stack", nested),
                 # Nothing the installation runs on: no physical ID, a failed create or delete, a failed nested stack.
                 summary("Pending", "AWS::S3::Bucket"),
                 summary("Failed", "AWS::Lambda::Function", "failed-function", "CREATE_FAILED"),
                 summary("Leaving", "AWS::S3::Bucket", "leaving-bucket", "DELETE_FAILED"),
                 summary("Broken", "AWS::CloudFormation::Stack", broken, "UPDATE_FAILED")],
        nested: [summary("Function", "AWS::Lambda::Function", "nested-function"),
                 summary("Deeper", "AWS::CloudFormation::Stack", deeper)],
        deeper: [summary("Project", "AWS::CodeBuild::Project", "deeper-project")],
        broken: [summary("Hidden", "AWS::S3::Bucket", "hidden-bucket")]})
    assert list(stack_resources(cfn, "root")) == [
        ("Bucket", "AWS::S3::Bucket", "root-bucket"), ("Nested", "AWS::CloudFormation::Stack", nested),
        ("Nested/Function", "AWS::Lambda::Function", "nested-function"),
        ("Nested/Deeper", "AWS::CloudFormation::Stack", deeper),
        ("Nested/Deeper/Project", "AWS::CodeBuild::Project", "deeper-project")]
    assert broken not in cfn.walked


def installation(*, extra_groups=None):
    """A root stack with a function and a nested stack holding a function and a project, and its log groups."""
    cfn = FakeCloudFormation({
        "fixture-backend": [summary("Function", "AWS::Lambda::Function", "fixture-backend-weather"),
                            summary("Nested", "AWS::CloudFormation::Stack", "arn:fixture:stack/nested")],
        "arn:fixture:stack/nested": [summary("Geocode", "AWS::Lambda::Function", "fixture-backend-geocode"),
                                     summary("Kept", "AWS::Lambda::Function", "fixture-backend-kept"),
                                     summary("Build", "AWS::CodeBuild::Project", "fixture-runtime-build")]})
    groups = {"/aws/lambda/fixture-backend-weather": None, "/aws/lambda/fixture-backend-geocode": None,
              "/aws/lambda/fixture-backend-kept": 7, "/aws/codebuild/fixture-runtime-build": None,
              # A prefix-only near match and an unrelated group, which are no installation group.
              "/aws/lambda/fixture-backend-weather-old": None, "/aws/lambda/other-application": None,
              f"/aws/bedrock-agentcore/runtimes/{RUNTIME_ID}-DEFAULT": None,
              f"/aws/vendedlogs/bedrock-agentcore/runtime/APPLICATION_LOGS/{RUNTIME_ID}": None,
              # An operator's own retention on a Runtime group stays.
              f"/aws/bedrock-agentcore/runtimes/{RUNTIME_ID}-RELEASE_A": 7, **(extra_groups or {})}
    return cfn, FakeLogs(groups)


@pytest.mark.parametrize("runtime_id", [RUNTIME_ID, "", "runtime"])
def test_retention_is_set_only_on_installation_groups_that_keep_logs_forever(tmp_path, capsys, runtime_id):
    cfn, logs = installation()
    retain_installation_logs(session(cfn, logs), ["fixture-backend"], runtime_id, tmp_path)
    walked = {"/aws/lambda/fixture-backend-weather", "/aws/lambda/fixture-backend-geocode",
              "/aws/codebuild/fixture-runtime-build"}
    runtime = {f"/aws/bedrock-agentcore/runtimes/{RUNTIME_ID}-DEFAULT",
               f"/aws/vendedlogs/bedrock-agentcore/runtime/APPLICATION_LOGS/{RUNTIME_ID}"}
    record = json.loads((tmp_path / "log-retention.json").read_text())
    if runtime_id == RUNTIME_ID:
        assert sorted(logs.puts) == sorted((name, LOG_RETENTION_DAYS) for name in walked | runtime)
        assert set(record["set"]) == walked | runtime and record["failed"] == []
        assert set(record["already_retained"]) == {"/aws/lambda/fixture-backend-kept",
                                                   f"/aws/bedrock-agentcore/runtimes/{RUNTIME_ID}-RELEASE_A"}
    else:
        # An empty or malformed Runtime ID would match every group in the Region, so no pattern query is made.
        assert all(query["pattern"] is None for query in logs.queries)
        assert sorted(logs.puts) == sorted((name, LOG_RETENTION_DAYS) for name in walked)
        assert record["failed"] == [{"name": "Runtime log groups", "error_code": "InvalidRuntimeId"}]
    assert logs.groups["/aws/lambda/fixture-backend-kept"] == 7
    assert logs.groups[f"/aws/bedrock-agentcore/runtimes/{RUNTIME_ID}-RELEASE_A"] == 7
    assert logs.groups["/aws/lambda/fixture-backend-weather-old"] is None
    assert logs.groups["/aws/lambda/other-application"] is None
    assert record["days"] == LOG_RETENTION_DAYS
    assert capsys.readouterr().out.startswith(f"Log retention: {len(logs.puts)} log groups now keep 30 days;")


def test_a_failed_retention_call_is_recorded_and_the_release_stays_complete(tmp_path, capsys):
    cfn, logs = installation()
    cfn.fail.add("fixture-application")
    logs.fail.add("/aws/lambda/fixture-backend-geocode")
    retain_installation_logs(session(cfn, logs), ["fixture-backend", "fixture-application"], RUNTIME_ID, tmp_path)
    record = json.loads((tmp_path / "log-retention.json").read_text())
    assert record["failed"] == [
        {"name": "fixture-application resources", "error_code": "EndpointConnectionError"},
        {"name": "/aws/lambda/fixture-backend-geocode", "error_code": "ThrottlingException"}]
    # Every other group is still set; the next release retries the failures.
    assert set(record["set"]) == {"/aws/lambda/fixture-backend-weather", "/aws/codebuild/fixture-runtime-build",
                                  f"/aws/bedrock-agentcore/runtimes/{RUNTIME_ID}-DEFAULT",
                                  f"/aws/vendedlogs/bedrock-agentcore/runtime/APPLICATION_LOGS/{RUNTIME_ID}"}
    assert capsys.readouterr().out.strip() == (
        "Log retention: 4 log groups now keep 30 days; 2 could not be set (log-retention.json); "
        "the next release retries them.")


def described(outputs=(), parameters=()):
    """A DescribeStacks entry with these outputs and parameters."""
    return {"Outputs": [{"OutputKey": key, "OutputValue": value} for key, value in dict(outputs).items()],
            "Parameters": [{"ParameterKey": key, "ParameterValue": value} for key, value in dict(parameters).items()]}


class FakeReleases:
    """AgentCore control, ECS and ECR, read-only (a write raises AttributeError), answering from fixed releases."""

    def __init__(self, versions=None, services=None, task_definitions=None, repositories=None):
        self.versions, self.services = versions or {}, services or {}
        self.task_definitions, self.repositories = task_definitions or {}, repositories or {}

    def get_agent_runtime(self, agentRuntimeId, agentRuntimeVersion):
        assert agentRuntimeId == RUNTIME_ID
        return {"agentRuntimeArtifact": {"containerConfiguration": {
            "containerUri": f"{REGISTRY}/fixture-runtime:{self.versions[agentRuntimeVersion]}"}}}

    def describe_services(self, cluster, services):
        assert cluster == "fixture-cluster"
        return {"services": [{"serviceName": name, "deployments": [
            {"status": status, "taskDefinition": definition} for status, definition in self.services[name]]}
            for name in services]}

    def describe_task_definition(self, taskDefinition):
        return {"taskDefinition": {"containerDefinitions": [
            {"image": image} for image in self.task_definitions[taskDefinition]]}}

    def get_paginator(self, name):
        assert name == "describe_images"
        return Pages(lambda repositoryName: [{"imageDetails": self.repositories.get(repositoryName, [])}])


APPLICATION = described({"ClusterName": "fixture-cluster", "ApiServiceName": "fixture-api",
                         "WorkerServiceName": "fixture-worker", "FiretruckServiceName": "fixture-firetruck",
                         "RadioServiceName": "fixture-radio", "FireRiskServiceName": "fixture-fire-risk"},
                        {"RuntimeEndpointAVersion": "4", "RuntimeEndpointBVersion": "3"})
BACKEND = described({"RuntimeId": RUNTIME_ID, "RuntimeECRRepositoryUri": f"{REGISTRY}/fixture-runtime"})
FOUNDATION = described({key: f"{REGISTRY}/fixture-{name}" for key, name in (
    ("RepositoryUri", "api"), ("TrafficSimulatorRepositoryUri", "traffic"), ("FiretruckRepositoryUri", "firetruck"),
    ("RadioRepositoryUri", "radio"), ("FireRiskRepositoryUri", "fire-risk"))})
CURRENT, PREVIOUS = "20261018000000000004", "20261017000000000003"


def test_images_in_use_come_from_both_endpoints_and_every_service_deployment():
    releases = FakeReleases(
        versions={"4": CURRENT, "3": PREVIOUS},
        # The API is mid-rollout: its PRIMARY and ACTIVE deployments run different task definitions.
        services={"fixture-api": [("PRIMARY", "api:2"), ("ACTIVE", "api:1")],
                  "fixture-worker": [("PRIMARY", "worker:2")], "fixture-firetruck": [("PRIMARY", "firetruck:2")],
                  "fixture-radio": [("PRIMARY", "radio:2")], "fixture-fire-risk": [("PRIMARY", "fire-risk:2")]},
        task_definitions={"api:2": [f"{REGISTRY}/fixture-api:{CURRENT}"],
                          "api:1": [f"{REGISTRY}/fixture-api:{PREVIOUS}"],
                          "worker:2": [f"{REGISTRY}/fixture-traffic:{CURRENT}"],
                          "firetruck:2": [f"{REGISTRY}/fixture-firetruck:{CURRENT}"],
                          "radio:2": [f"{REGISTRY}/fixture-radio:{CURRENT}"],
                          "fire-risk:2": [f"{REGISTRY}/fixture-fire-risk:{CURRENT}"]})
    session = SimpleNamespace(client=lambda name: releases)
    assert images_in_use(session, {"foundation": FOUNDATION, "application": APPLICATION, "backend": BACKEND}) == {
        CURRENT: ["the RELEASE_A Runtime endpoint", "the API ECS service", "the worker ECS service",
                  "the firetruck ECS service", "the radio ECS service", "the fire-risk ECS service"],
        PREVIOUS: ["the RELEASE_B Runtime endpoint", "the API ECS service"]}
    # A new installation has no endpoint and no service, so nothing is in use.
    assert images_in_use(session, {"foundation": None, "application": None, "backend": None}) == {}


def pushed(*tags):
    """ECR image details, newest first: one per tag, an untagged image for None."""
    start = datetime(2026, 10, 18, tzinfo=UTC)
    return [{"imageDigest": f"sha256:{rank:064x}", "imagePushedAt": start - timedelta(hours=rank),
             **({"imageTags": [tag]} if tag else {})} for rank, tag in enumerate(tags)]


@pytest.mark.parametrize("rank", [IMAGES_KEPT - 2, IMAGES_KEPT - 1])
@pytest.mark.parametrize("repository", ["api", "traffic", "firetruck", "radio", "fire-risk", "runtime"])
def test_a_push_that_would_expire_an_image_in_use_is_refused(monkeypatch, make_installation, rank, repository):
    """L5: one push per repository moves an image at rank 8 to the 10th newest (kept) and one at rank 9 out."""
    tags = [f"r{index:02}" for index in range(12)]
    tags[3] = None
    images = pushed(*tags)
    # The previous release's image at rank 1 is in use too, so it is no removable image.
    in_use = {f"r{rank:02}": ["the RELEASE_A Runtime endpoint"], "r01": ["the RELEASE_B Runtime endpoint"]}
    monkeypatch.setattr(retention, "images_in_use", lambda *_: in_use)
    releases = FakeReleases(repositories={f"fixture-{repository}": list(reversed(images))})
    stacks = {"foundation": FOUNDATION, "application": APPLICATION, "backend": BACKEND}
    session = SimpleNamespace(client=lambda name: releases)
    if rank == IMAGES_KEPT - 2:
        require_retained_images(session, make_installation(), stacks)
        return
    with pytest.raises(ValueError) as refused:
        require_retained_images(session, make_installation(), stacks)
    message = str(refused.value)
    assert message.startswith(f"ECR repository fixture-{repository} holds 9 images newer than r09, "
                              "which the RELEASE_A Runtime "
                              "endpoint uses. This release pushes one more, and the repository keeps only its newest "
                              "10 images, so r09 would expire.")
    # The newer images no endpoint or service uses, oldest first; the untagged one by its digest.
    removable = ["imageTag=r08", "imageTag=r07", "imageTag=r06", "imageTag=r05", "imageTag=r04",
                 f"imageDigest=sha256:{3:064x}", "imageTag=r02", "imageTag=r00"]
    assert [line.split("--image-ids ")[1].split()[0] for line in message.splitlines() if "batch-delete-image" in line
            ] == removable
    assert (f"  aws ecr batch-delete-image --repository-name fixture-{repository} "
            "--image-ids imageTag=r08 --profile fixture "
            "--region us-west-2") in message.splitlines()
    assert message.endswith("\nNo AWS changes made.")
