"""What an installation keeps (D53): every log group's events for 30 days, set after publication (L3), and the
newest image-repository images, which a release may push only while every image in use stays among them (L5)."""
import json
import re

from botocore.exceptions import BotoCoreError, ClientError

from deployment.deployment_target import parameter_values, safe_outputs, stack_resources

# Fixed (D37, D53): equal to IMAGES_KEPT in deployment/infra/lib/stacks.ts and the Runtime repository's lifecycle
# policy in 1-infrastructure.yaml; tests keep the three equal.
IMAGES_KEPT = 10
# The foundation's five repositories and the backend's Runtime repository, as their stacks' outputs name them.
REPOSITORY_OUTPUTS = {"foundation": ("RepositoryUri", "TrafficSimulatorRepositoryUri", "FiretruckRepositoryUri",
                                     "RadioRepositoryUri", "FireRiskRepositoryUri"), "backend": ("RuntimeECRRepositoryUri",)}
# The application stack's ECS services, by output, with the name each refusal gives them.
SERVICE_OUTPUTS = {"ApiServiceName": "API", "WorkerServiceName": "worker", "FiretruckServiceName": "firetruck",
                   "RadioServiceName": "radio", "FireRiskServiceName": "fire-risk"}

# Fixed (D37, D53): the CDK app's log retention (logs.RetentionDays.ONE_MONTH); the backend templates' stack-owned
# log groups write the same 30 (RetentionInDays).
LOG_RETENTION_DAYS = 30
# GetAgentRuntime's agentRuntimeId pattern (botocore model). The Runtime log-group query runs only for such an ID:
# an empty logGroupNamePattern matches every log group in the Region.
AGENT_RUNTIME_ID = re.compile(r"[a-zA-Z][a-zA-Z0-9_]{0,99}-[a-zA-Z0-9]{10}")
# The default log group Lambda and CodeBuild create for a function or project that names no group of its own.
DEFAULT_LOG_GROUPS = {"AWS::Lambda::Function": "/aws/lambda/", "AWS::CodeBuild::Project": "/aws/codebuild/"}


def error_code(exc):
    """Name an AWS failure for a receipt: the ClientError code, or the exception type; never its message."""
    return exc.response["Error"]["Code"] if isinstance(exc, ClientError) else type(exc).__name__


def installation_log_groups(cfn, stack_names):
    """Name the default log groups of every Lambda function and CodeBuild project in the named stacks and their
    nested stacks; return (names, failures), recording a stack whose walk fails instead of raising."""
    names, failed = set(), []
    for stack in stack_names:
        try:
            names |= {DEFAULT_LOG_GROUPS[kind] + physical for _, kind, physical in stack_resources(cfn, stack)
                      if kind in DEFAULT_LOG_GROUPS}
        except (ClientError, BotoCoreError) as exc:
            failed.append({"name": f"{stack} resources", "error_code": error_code(exc)})
    return sorted(names), failed


def log_groups(logs, **query):
    """List the CloudWatch log groups a DescribeLogGroups query finds, every page."""
    return [group for page in logs.get_paginator("describe_log_groups").paginate(**query)
            for group in page.get("logGroups", [])]


def retain_installation_logs(session, stack_names, runtime_id, folder, days=LOG_RETENTION_DAYS):
    """Give every installation log group that keeps its events forever a retention; record, never raise, failures.

    The groups are the default groups of the named stacks' functions and projects and the AgentCore Runtime's
    groups, whose names contain its ID. A group with any retention keeps it, so an operator's choice survives and a
    rerun changes nothing. log-retention.json in folder lists the names set, already retained and failed; the next
    release retries a failure.
    """
    logs = session.client("logs")
    names, failed = installation_log_groups(session.client("cloudformation"), stack_names)
    if isinstance(runtime_id, str) and AGENT_RUNTIME_ID.fullmatch(runtime_id):
        try:
            # A pattern answer carries no retention, so each name it finds goes through the exact lookup below.
            names += sorted({group["logGroupName"] for group in log_groups(logs, logGroupNamePattern=runtime_id)}
                            - set(names))
        except (ClientError, BotoCoreError) as exc:
            failed.append({"name": "Runtime log groups", "error_code": error_code(exc)})
    else:
        failed.append({"name": "Runtime log groups", "error_code": "InvalidRuntimeId"})
    retained, kept = [], []
    for name in names:
        try:
            for group in log_groups(logs, logGroupNamePrefix=name):
                if group["logGroupName"] != name:
                    continue
                if "retentionInDays" in group:
                    kept.append(name)
                else:
                    logs.put_retention_policy(logGroupName=name, retentionInDays=days)
                    retained.append(name)
        except (ClientError, BotoCoreError) as exc:
            failed.append({"name": name, "error_code": error_code(exc)})
    (folder / "log-retention.json").write_text(json.dumps(
        {"days": days, "set": retained, "already_retained": kept, "failed": failed}, indent=2))
    print(f"Log retention: {len(retained)} log groups now keep {days} days; {len(failed)} could not be set "
          "(log-retention.json); the next release retries them.", flush=True)


def image_tag(uri):
    """Return the tag of an image URI (the text after its last colon)."""
    return uri.rsplit(":", 1)[-1]


def images_in_use(session, stacks):
    """Map each image tag that a Runtime endpoint version or any ECS service deployment uses to what uses it.

    Every release tags its images in all five repositories with its release ID, so the inactive endpoint's tag also
    protects the previous release's application images, which a rollback needs. A stack that does not exist yet, or
    an output it lacks, uses nothing. Read-only; runtime_binding.api_bindings reads only the API's running tasks.
    """
    used = {}

    def use(tag, user):
        used.setdefault(tag, [])
        if user not in used[tag]:
            used[tag].append(user)
    application, backend = parameter_values(stacks.get("application")), safe_outputs(stacks.get("backend") or {})
    if backend.get("RuntimeId"):
        control = session.client("bedrock-agentcore-control")
        for slot in ("A", "B"):
            if version := application.get(f"RuntimeEndpoint{slot}Version"):
                runtime = control.get_agent_runtime(agentRuntimeId=backend["RuntimeId"], agentRuntimeVersion=version)
                use(image_tag(runtime["agentRuntimeArtifact"]["containerConfiguration"]["containerUri"]),
                    f"the RELEASE_{slot} Runtime endpoint")
    outputs = safe_outputs(stacks.get("application") or {})
    services = {outputs[key]: name for key, name in SERVICE_OUTPUTS.items() if outputs.get(key)}
    if outputs.get("ClusterName") and services:
        ecs = session.client("ecs")
        # Every deployment: a rollout or a circuit-breaker rollback runs two task definitions at once.
        for service in ecs.describe_services(cluster=outputs["ClusterName"], services=list(services))["services"]:
            for definition in dict.fromkeys(item["taskDefinition"] for item in service.get("deployments", [])):
                for container in ecs.describe_task_definition(taskDefinition=definition)["taskDefinition"][
                        "containerDefinitions"]:
                    use(image_tag(container["image"]), f"the {services[service['serviceName']]} ECS service")
    return used


def require_retained_images(session, installation, stacks):
    """Refuse, before any AWS change, a release whose image push would expire an image in use.

    A release pushes at most one image per repository, and each repository keeps its newest IMAGES_KEPT images, so
    an image in use must be among the newest IMAGES_KEPT - 1 now. The refusal lists the newer images nothing uses,
    oldest first, with the command that deletes each; deleting is the operator's choice, never the deployer's.
    """
    used = images_in_use(session, stacks)
    ecr = session.client("ecr")
    target = installation.target
    for component, keys in REPOSITORY_OUTPUTS.items():
        outputs = safe_outputs(stacks.get(component) or {})
        for repository in (outputs[key].split("/", 1)[1] for key in keys if outputs.get(key)):
            images = sorted((image for page in ecr.get_paginator("describe_images").paginate(
                repositoryName=repository) for image in page.get("imageDetails", [])),
                key=lambda image: image["imagePushedAt"], reverse=True)
            for rank, image in enumerate(images):
                users = [user for tag in image.get("imageTags", []) for user in used.get(tag, [])]
                if not users or rank <= IMAGES_KEPT - 2:
                    continue
                tag = next(tag for tag in image["imageTags"] if tag in used)
                removable = [f"imageTag={newer['imageTags'][0]}" if newer.get("imageTags")
                             else f"imageDigest={newer['imageDigest']}" for newer in reversed(images[:rank])
                             if not any(newer_tag in used for newer_tag in newer.get("imageTags", []))]
                raise ValueError(
                    f"ECR repository {repository} holds {rank} images newer than {tag}, which "
                    f"{' and '.join(dict.fromkeys(users))} uses. This release pushes one more, and the repository "
                    f"keeps only its newest {IMAGES_KEPT} images, so {tag} would expire. These newer images are in no "
                    "Runtime endpoint or ECS service, oldest first; deleting enough of them lets the release proceed. "
                    "Deleting is your choice; the deployer deletes nothing:\n"
                    + "\n".join(f"  aws ecr batch-delete-image --repository-name {repository} --image-ids {image_id} "
                                f"--profile {installation.profile} --region {target.region}" for image_id in removable)
                    + "\nNo AWS changes made.")
