"""Backend release operations used by deploy_workspace.py; no implicit .env or cleanup."""
import hashlib
import io
import json
import re
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import yaml
from botocore.exceptions import (
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)

from deployment.change_sets import execute_guarded
from deployment.deployment_target import inspect_stack, parameter_values, safe_outputs
from deployment.image_scans import verify_image
from deployment.package_source import archive, git_files
from deployment.runtime_binding import runtime_version
from deployment.validate_infra_baseline import load_template

EXCLUDED = {".git", ".env", ".venv", "__pycache__", "node_modules", ".runtime"}
# Lambda packages target the functions' python3.14 runtime in 2-lambda-functions.yaml.
LAMBDA_PYTHON = "3.14"
# AllowedValues of the Environment parameter in backend/deployment/templates/wildfire-backend-stack.yaml.
ENVIRONMENTS = ("development", "staging", "production")
# The Runtime image starts this module (backend/runtime/Dockerfile CMD).
RUNTIME_ENTRYPOINT = "wfm_agui_entrypoint.py"


def require_environment(stack, environment):
    """Stop when an existing backend stack was deployed for another Environment; never re-tag it silently."""
    if environment not in ENVIRONMENTS:
        raise ValueError(f"[aws] environment must be one of {', '.join(ENVIRONMENTS)}")
    current = parameter_values(stack).get("Environment")
    if current and current != environment:
        raise ValueError(f"{stack.get('StackName', 'The backend stack')} was deployed with environment {current}, "
                         f"not {environment}. Set [aws] environment = \"{current}\" in the installation file; "
                         "changing it would rename and re-tag the existing backend. Use another [aws] name for a "
                         "separate installation.")


def retain_runtime(template, deployed):
    """Keep the running image's resource configuration until its replacement passes build gates."""
    current = deployed if isinstance(deployed, dict) else load_template(deployed)
    resource = current.get("Resources", {}).get("AgentCoreRuntime")
    if not resource or resource.get("Type") != "AWS::BedrockAgentCore::Runtime":
        raise ValueError("Existing Runtime template is unavailable; deployment stopped before preparation")
    pattern = r"(?ms)^  AgentCoreRuntime:\n.*?(?=^  [A-Za-z][A-Za-z0-9]+:\n|^Outputs:|\Z)"
    if isinstance(deployed, dict):
        block = "".join("  "+line+"\n" for line in yaml.safe_dump(
            {"AgentCoreRuntime": resource}, sort_keys=False).splitlines())+"\n"
    else:
        match = re.search(pattern, deployed)
        if match is None:
            raise ValueError("Existing Runtime resource cannot be captured safely")
        block = match.group()
    updated, count = re.subn(pattern, lambda _: block, template.decode())
    if count != 1:
        raise ValueError("Expected exactly one replacement Runtime resource")
    return updated.encode()


def runtime_source(root):
    """Build the AgentCore Runtime CodeBuild source from files git tracks or would add (never ignored ones).

    The root is the image context: runtime code, schemas and the lock. release-tests/ holds the release
    checks (backend/runtime/tests and the code they load); the runtime .dockerignore keeps it out of the image
    and the buildspec mounts it read-only.
    """
    visible = git_files(root)
    runtime = root / "backend/runtime"
    files = {}
    for p in runtime.rglob("*"):
        if (not p.is_file() or p.relative_to(root) not in visible
                or p.relative_to(runtime).parts[0] == "tests"  # release-tests/ carries them
                or any(x in EXCLUDED or x.startswith(".env") for x in p.relative_to(runtime).parts)):
            continue
        if p.is_symlink():
            raise ValueError(f"Runtime source cannot contain symlinks: {p.relative_to(runtime)}")
        if p.suffix in {".py", ".json", ".yaml", ".yml", ".txt"} or p.name in {"Dockerfile", ".dockerignore"}:
            files[p.relative_to(runtime).as_posix()] = p.read_bytes()
    missing = [name for name in (RUNTIME_ENTRYPOINT, "Dockerfile", "requirements.txt") if name not in files]
    if missing:
        raise ValueError("Checkout is missing backend/runtime/" + ", backend/runtime/".join(missing))
    named = ("schemas/api-schema.yaml", "requirements-lock.txt",
             # The release checks parse the Gateway template with the infra baseline loader.
             "deployment/validate_infra_baseline.py")
    ignored = [name for name in named if Path(name) not in visible]
    if ignored:
        raise ValueError(f"Runtime release files are ignored by git: {', '.join(ignored)}. "
                         "Track them or fix .gitignore.")
    files["schemas/api-schema.yaml"] = (root / "schemas/api-schema.yaml").read_bytes()
    files["requirements-lock.txt"] = (root / "requirements-lock.txt").read_bytes()
    for folder in ("backend/runtime", "backend/gateway/simulation", "backend/gateway/geocode", "backend/gateway/weather",
                   "backend/gateway/wildfire", "backend/deployment/templates",
                   "backend/deployment/lambda/custom-resources"):
        for p in sorted((root / folder).rglob("*")):
            if (p.is_file() and not p.is_symlink() and p.suffix in {".py", ".yaml", ".json"}
                    and p.relative_to(root) in visible
                    and not any(x in EXCLUDED for x in p.relative_to(root).parts)):
                files["release-tests/"+p.relative_to(root).as_posix()] = p.read_bytes()
    for name in named[2:]:
        files["release-tests/"+name] = (root / name).read_bytes()
    return archive(files)


def lambda_sources(root):
    """List each Gateway and custom-resource Lambda with its handler file and requirements file."""
    gateway = root / "backend/gateway"
    custom = root / "backend/deployment/lambda/custom-resources"
    # (name, handler, requirements file, bundles boto3). Only the three handlers that import boto3
    # (geocode-service, weather-dtn-service and the custom resource) bundle the reviewed SDK, so they never
    # depend on the Lambda runtime's own version (geocode-service calls the recent geo-places API).
    functions = [
        ("geocode-service", gateway / "geocode/handler.py", None, True),
        ("wildfire-service", gateway / "wildfire/handler.py", None, False),
        ("simulation-service", gateway / "simulation/handler.py", None, False),
        ("weather-dtn-service", gateway / "weather/lambda_weather_dtn.py", gateway / "weather/requirements-dtn.txt",
         True),
        ("get-cognito-client-secret", custom / "get_cognito_client_secret.py", None, True),
    ]
    result = {}
    constraints = root / "requirements-lock.txt"
    if not constraints.is_file():
        raise ValueError("requirements-lock.txt (the repository Python constraints lock) is required "
                         "for Lambda packaging")
    for name, source, requirements, sdk in functions:
        with tempfile.TemporaryDirectory(prefix="wildfire-lambda-") as temporary:
            target = Path(temporary)
            packages = (["boto3"] if sdk else []) + (["-r", str(requirements)] if requirements else [])
            if packages:
                subprocess.run(["uv", "pip", "install", "--quiet", "--only-binary=:all:",
                    "--python-platform", "x86_64-manylinux2014", "--python-version", LAMBDA_PYTHON,
                    "--target", str(target), "--constraint", str(constraints), *packages], check=True)
            files = {p.relative_to(target).as_posix(): p.read_bytes()
                     for p in target.rglob("*") if p.is_file()}
            files[source.name] = source.read_bytes()
            result[name] = archive(files)
    return result


def put_version(s3, bucket, key, data):
    """Upload an object to the versioned release bucket and return its S3 version ID."""
    response = s3.put_object(Bucket=bucket, Key=key, Body=data)
    version = response.get("VersionId")
    if not version or version == "null":
        raise ValueError("Release source bucket must have versioning enabled")
    return version


def wait_build(client, build_id, label, *, stopped, timeout=3600):
    """Poll a CodeBuild build until it ends, riding out dropped connections and printing each state change once.

    label names the build in progress lines ("Runtime build"); stopped says what a failed build means.
    """
    deadline = time.monotonic()+timeout
    previous = None
    while True:
        try:
            result = client.batch_get_builds(ids=[build_id])["builds"][0]
        except (EndpointConnectionError, ConnectionClosedError, ConnectTimeoutError, ReadTimeoutError):
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{label} polling connection failed; inspect {build_id} before rerunning "
                                   "deploy-all.sh --apply") from None
            if previous != ("connection_interrupted", ""):
                print(f"{label} polling connection interrupted; AWS build continues. Retrying.", flush=True)
                previous = ("connection_interrupted", "")
            time.sleep(15)
            continue
        status = result["buildStatus"]
        state = (status, result.get("currentPhase", ""))
        if state != previous:
            print(f"{label}:", *state, flush=True)
            previous = state
        if status != "IN_PROGRESS":
            if status != "SUCCEEDED":
                raise RuntimeError(f"{label} {build_id} failed ({status}); {stopped}")
            return result
        if time.monotonic() >= deadline:
            raise TimeoutError(f"{label} wait expired; inspect {build_id} before rerunning deploy-all.sh --apply")
        time.sleep(15)


def change_stack(cfn, target, template, parameters, release_id, phase, receipt_dir, *, acknowledged=()):
    """Create the backend change set with its nested stacks, run it through the guard, then protect the stack.

    acknowledged holds the operator's change_sets.Acknowledgments for this release's review stops.
    """
    name = target.stack("backend")
    existing = inspect_stack(cfn, name, target)
    args = {"StackName": name, "ChangeSetName": f"release-{release_id}-{phase}-{time.time_ns()}",
            "ChangeSetType": "UPDATE" if existing else "CREATE", "TemplateBody": template,
            "Parameters": [{"ParameterKey": k, "ParameterValue": str(v)} for k, v in parameters.items()],
            "Capabilities": ["CAPABILITY_NAMED_IAM"], "IncludeNestedStacks": True,
            "Tags": [{"Key": k, "Value": v} for k, v in target.tags.items()]}
    result = cfn.create_change_set(**args)
    receipt = receipt_dir / f"backend-{phase}-changes.json"
    # Nested change sets make the stateful resources of the nested templates visible to the guard.
    summary = execute_guarded(cfn, name, result["Id"], created=not existing, receipt=receipt,
                              acknowledged=acknowledged)
    stack = cfn.describe_stacks(StackName=name)["Stacks"][0]
    summary["termination_protection"] = protect_stack(cfn, name, stack)
    receipt.write_text(json.dumps(summary, indent=2))
    return safe_outputs(stack)


def protect_stack(cfn, name, stack):
    """Turn on termination protection for the backend root stack (its nested stacks follow); a no-op once on.

    It blocks DeleteStack only: an update that removes a nested stack still deletes it, which is why the change-set
    guard checks nested-stack removals itself. To delete the stack, first turn protection off.
    """
    if stack.get("EnableTerminationProtection"):
        return "already enabled"
    cfn.update_termination_protection(EnableTerminationProtection=True, StackName=name)
    print(f"{name}: termination protection enabled; turn it off before deleting the stack", flush=True)
    return "enabled"


def verify_contracts(root):
    """Fail before any release step if root's generated simulator contracts are stale (generator --check)."""
    try:
        completed = subprocess.run(
            [sys.executable, str(root / "scripts/export_integration_contracts.py"), "--check"],
            cwd=root, capture_output=True, text=True, timeout=300, check=False)
    except subprocess.TimeoutExpired:
        raise ValueError("Simulator contract check timed out; "
                         "run .venv/bin/python scripts/export_integration_contracts.py --check") from None
    if completed.returncode:
        detail = (completed.stderr.strip() or completed.stdout.strip()).splitlines()
        raise ValueError("Simulator contract check failed" + (f" ({detail[-1]})" if detail else "")
                         + ": run .venv/bin/python scripts/export_integration_contracts.py")


def prepare_backend(root):
    """Capture Runtime source, Lambda packages and templates after verifying generated contracts."""
    verify_contracts(root)
    return {
        "payload": runtime_source(root), "packages": lambda_sources(root),
        "templates": {p.name: p.read_bytes()
                      for p in sorted((root / "backend/deployment/templates/nested").glob("*.yaml"))},
        "template": (root / "backend/deployment/templates/wildfire-backend-stack.yaml").read_text(),
    }


def capture_backend(prepared):
    """Archive prepared backend assets (Runtime source, main and nested templates, Lambda packages)."""
    return archive({
        "runtime-source.zip": prepared["payload"], "main.yaml": prepared["template"].encode(),
        **{"templates/"+k: v for k, v in prepared["templates"].items()},
        **{"packages/"+k: v for k, v in prepared["packages"].items()},
    })


def restore_backend(payload):
    """Read a captured backend-assets.zip back into the prepare_backend structure."""
    with zipfile.ZipFile(io.BytesIO(payload)) as bundle:
        files = {name: bundle.read(name) for name in bundle.namelist()}
    if not {"runtime-source.zip", "main.yaml"} <= files.keys():
        raise ValueError("Captured backend is incomplete")
    return {
        "payload": files["runtime-source.zip"], "template": files["main.yaml"].decode(),
        **{key: {name.removeprefix(prefix): data for name, data in files.items() if name.startswith(prefix)}
           for key, prefix in (("templates", "templates/"), ("packages", "packages/"))},
    }


def deploy_backend(session, target, root, bucket, lab, provider, installation, release_dir, captured=None, *,
                   acknowledged=(), early_runtime_build=None):
    """Publish captured assets, build ARM64 Runtime, then atomically advance its tag.

    acknowledged holds the operator's change_sets.Acknowledgments, passed to every backend change set.
    """
    cfn, s3 = session.client("cloudformation"), session.client("s3")
    environment = installation.environment
    previous = inspect_stack(cfn, target.stack("backend"), target)
    require_environment(previous, environment)
    previous_params = parameter_values(previous)
    if previous and previous_params.get("DeployRuntime") == "true":
        old = safe_outputs(previous)
        # Stale CloudFormation outputs are not evidence of a usable Runtime.
        session.client("bedrock-agentcore-control").get_agent_runtime(agentRuntimeId=old["RuntimeId"])
    prepared = captured or prepare_backend(root)
    from deployment.runtime_build import (
        build_matches,
        build_properties,
        retag_reused_image,
        source_identity,
        validate_early_build,
    )
    if early_runtime_build is not None:
        validate_early_build(early_runtime_build, target, bucket, release_dir, prepared)
    payload, packages, templates, template = (prepared[k] for k in ("payload", "packages", "templates", "template"))
    captured_bytes = capture_backend(prepared)
    checksum = hashlib.sha256(captured_bytes).hexdigest()
    (release_dir / "backend-assets.zip").write_bytes(captured_bytes)
    asset_prefix = "releases/"+checksum
    release_id = release_dir.name
    versions = {}
    for name, data in packages.items():
        # 2-lambda-functions.yaml reads lambda-packages/NAME-${Environment}.zip.
        versions[name] = put_version(s3, bucket, f"{asset_prefix}/lambda-packages/{name}-{environment}.zip", data)
    for name, data in templates.items():
        put_version(s3, bucket, f"{asset_prefix}/templates/nested/{name}", data)
    source_key = f"{asset_prefix}/runtime-source.zip"
    source_version = (early_runtime_build["source_version"] if early_runtime_build is not None else
                      put_version(s3, bucket, source_key, payload))
    (release_dir / "runtime-source.zip").write_bytes(payload)
    # Imported here: deployment.installation imports ENVIRONMENTS from this module.
    from deployment.installation import operator_parameters
    parameters = {
        # The roles besides the backend's own that its bucket policies admit (D52).
        **operator_parameters(installation),
        "ProjectName": target.project, "Environment": environment,
        "DtnOperationTimeoutSeconds": installation.settings["dtn_operation_timeout_seconds"],
        "DtnRequestTimeoutSeconds": installation.settings["dtn_request_timeout_seconds"],
        "DtnMaxAttempts": installation.settings["dtn_max_attempts"],
        "DtnSerializationReserveSeconds": installation.settings["dtn_serialization_reserve_seconds"],
        "DeployRuntime": previous_params.get("DeployRuntime", "false"),
        "RuntimeImageTag": previous_params.get("RuntimeImageTag", "not-deployed"),
        "RuntimeSourceBucket": bucket, "LambdaCodeBucket": bucket, "AssetPrefix": asset_prefix,
        "TemplatePrefix": asset_prefix+"/templates/nested", "DeploymentTimestamp": release_id,
        "PublicDemoKnowledgeBaseId": installation.public_knowledge_base_id or "",
        "ModelId": installation.agent_model_id, "ReportModelId": installation.report_model_id,
        "NasaFirmsApiKey": provider.get("NASA_FIRMS_API_KEY", ""),
        "DTNClientId": provider["DTN_CLIENT_ID"], "DTNClientSecret": provider["DTN_CLIENT_SECRET"],
        "SimulationServiceS3Version": versions["simulation-service"],
        "GeocodeServiceS3Version": versions["geocode-service"],
        "WildfireServiceS3Version": versions["wildfire-service"],
        "WeatherDTNServiceS3Version": versions["weather-dtn-service"],
        **{k: lab[k] for k in ("SimulationInternalUrl", "SimulationContextSecretArn",
                               "SimulationAdapterSecurityGroupId", "SimulationAdapterSubnetIds")},
    }
    preparation_templates = templates
    if previous_params.get("DeployRuntime") == "true":
        nested = cfn.describe_stack_resource(StackName=target.stack("backend"),
                                            LogicalResourceId="AgentCoreStack")["StackResourceDetail"]
        deployed = cfn.get_template(StackName=nested["PhysicalResourceId"],
                                    TemplateStage="Original")["TemplateBody"]
        preparation_templates = {**templates, "4-agentcore-resources.yaml": retain_runtime(
            templates["4-agentcore-resources.yaml"], deployed)}
        preparation_prefix = asset_prefix+"/runtime-prepare/"+hashlib.sha256(
            archive(preparation_templates)).hexdigest()
        for name, data in preparation_templates.items():
            put_version(s3, bucket, f"{preparation_prefix}/{name}", data)
        parameters["TemplatePrefix"] = preparation_prefix
    outputs = change_stack(cfn, target, template, parameters, release_id, "prepare", release_dir,
                           acknowledged=acknowledged)
    builder = session.client("codebuild")
    if early_runtime_build is not None:
        receipt = dict(early_runtime_build)
        if (receipt["project_name"] != outputs["CodeBuildProjectName"]
                or receipt["repository_uri"] != outputs["RuntimeECRRepositoryUri"]):
            raise ValueError("Prepared Runtime infrastructure differs from the early build binding")
    else:
        build = builder.start_build(projectName=outputs["CodeBuildProjectName"],
            sourceLocationOverride=f"{bucket}/{source_key}", sourceVersion=source_version,
            environmentVariablesOverride=[{"name": "IMAGE_TAG", "value": release_id, "type": "PLAINTEXT"}])["build"]
        receipt = {
            **source_identity(target, bucket, release_dir, prepared), "source_version": source_version,
            "build_id": build["id"], "mode": "build", "project_name": outputs["CodeBuildProjectName"],
            "environment": environment,
            "repository_uri": outputs["RuntimeECRRepositoryUri"],
            "buildspec": build_properties(prepared)["Source"]["BuildSpec"],
            "build_source_bucket": bucket, "build_source_key": source_key,
            "build_source_version": source_version, "build_image_tag": release_id,
        }
    (release_dir / "backend-build-request.json").write_text(json.dumps(receipt, indent=2))
    repository = outputs["RuntimeECRRepositoryUri"].split("/", 1)[1]
    ecr = session.client("ecr")
    if receipt["mode"] == "reuse":
        image = {"imageDigest": receipt["image_digest"]}
    else:
        completed = wait_build(builder, receipt["build_id"], "Runtime build",
                               stopped="the existing Runtime image was not advanced")
        if early_runtime_build is not None and not build_matches(completed, receipt, target, prepared):
            raise ValueError("Completed early Runtime build does not match the captured source/ARM project")
        image = ecr.describe_images(repositoryName=repository,
                                   imageIds=[{"imageTag": release_id}])["imageDetails"][0]
    verify_image(ecr, repository, image["imageDigest"], "runtime",
                 release_dir / "scan-runtime.json", policy_path=release_dir / "image-policy.json",
                 **({"scan_not_before": receipt["scan_not_before"]} if receipt["mode"] == "reuse" else {}))
    if receipt["mode"] == "reuse":
        retag_reused_image(ecr, repository, receipt)
    parameters.update(DeployRuntime="true", RuntimeImageTag=release_id,
                      TemplatePrefix=asset_prefix+"/templates/nested")
    outputs = change_stack(cfn, target, template, parameters, release_id, "activate", release_dir,
                           acknowledged=acknowledged)
    actual = session.client("bedrock-agentcore-control").get_agent_runtime(agentRuntimeId=outputs["RuntimeId"])
    if actual["status"] != "READY":
        raise RuntimeError("Runtime is not READY after CloudFormation completion")
    if (not actual["agentRuntimeArtifact"]["containerConfiguration"]["containerUri"].endswith(":"+release_id)
            or actual.get("environmentVariables", {}).get("DEPLOYED_RuntimeBuild") != release_id):
        raise RuntimeError("Activated Runtime does not match the verified release image/build identity")
    outputs["RuntimeVersion"] = runtime_version(actual.get("agentRuntimeVersion"))
    (release_dir / "backend-deployment.json").write_text(json.dumps({
        **receipt, "image_digest": image["imageDigest"], "outputs": outputs}, indent=2))
    return outputs
