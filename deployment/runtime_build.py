"""Upgrade-only Runtime compilation overlap and evidence-backed image reuse."""
import hashlib
import json
import math
import re
import zipfile

from botocore.exceptions import ClientError

from deployment.component_images import matching_manifest
from deployment.deployment_target import inspect_stack, parameter_values, safe_outputs, validate_arn
from deployment.image_scans import refresh_image_scan
from deployment.validate_infra_baseline import load_template

FINGERPRINT_VERSION = 1
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
RESUME_BLOCKERS = (
    "backend-prepare-changes.json", "backend-activate-changes.json", "backend-deployment.json",
    "application-candidate-changes.json", "application-bind-changes.json",
    "application-rollback-binding-changes.json", "acceptance-runtime-candidate.json",
    "acceptance-cutover.json", "runtime-binding-rollback.json", "deployment.json", "acceptance.json",
)


def build_properties(captured):
    template = load_template(captured["templates"]["4-agentcore-resources.yaml"].decode())
    return template["Resources"]["RuntimeContainerBuildProject"]["Properties"]


def runtime_fingerprint(captured):
    """Include all image source, offline review/startup checks and the actual build recipe."""
    properties = build_properties(captured)
    recipe = json.dumps({
        "version": FINGERPRINT_VERSION, "source_type": properties["Source"]["Type"],
        "buildspec": properties["Source"]["BuildSpec"], "environment": properties["Environment"],
        "service_role": properties["ServiceRole"], "encryption_key": properties["EncryptionKey"],
    }, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(captured["payload"]+b"\0"+recipe).hexdigest()


def source_identity(target, bucket, release_dir, captured):
    # Imported here to keep deploy_backend -> runtime_build dependency acyclic during startup.
    from deployment.deploy_backend import capture_backend
    checksum = hashlib.sha256(capture_backend(captured)).hexdigest()
    return {
        "account": target.account, "region": target.region,
        "source_key": f"releases/{checksum}/runtime-source.zip",
        "source_sha256": hashlib.sha256(captured["payload"]).hexdigest(),
        "runtime_fingerprint": runtime_fingerprint(captured), "fingerprint_version": FINGERPRINT_VERSION,
        "source_bucket": bucket, "image_tag": release_dir.name,
    }


def resumable_early_build(release_dir):
    """Admit a captured early request only before backend preparation or any Runtime promotion evidence."""
    from deployment.deploy_backend import restore_backend
    if any((release_dir / name).exists() for name in RESUME_BLOCKERS):
        return False
    try:
        receipt = json.loads((release_dir / "backend-build-request.json").read_text())
        if not isinstance(receipt, dict) or receipt.get("started_early") is not True:
            return False
        captured = restore_backend((release_dir / "backend-assets.zip").read_bytes())
        return (
            receipt.get("mode") in ("build", "reuse")
            and (release_dir / "runtime-source.zip").read_bytes() == captured["payload"]
            and receipt.get("source_sha256") == hashlib.sha256(captured["payload"]).hexdigest()
            and receipt.get("runtime_fingerprint") == runtime_fingerprint(captured)
            and receipt.get("fingerprint_version") == FINGERPRINT_VERSION
        )
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile):
        return False


def compatible_builder(session, target, bucket, installation, captured):
    """An existing owned ARM builder must run the captured recipe with its original bounded environment."""
    from deployment.deploy_backend import require_environment
    cfn = session.client("cloudformation")
    previous = inspect_stack(cfn, target.stack("backend"), target)
    require_environment(previous, installation.environment)
    if parameter_values(previous).get("DeployRuntime") != "true":
        return None
    outputs = safe_outputs(previous)
    name, uri = outputs.get("CodeBuildProjectName"), outputs.get("RuntimeECRRepositoryUri")
    registry = f"{target.account}.dkr.ecr.{target.region}.amazonaws.com/"
    if not isinstance(name, str) or not isinstance(uri, str) or not uri.startswith(registry):
        return None
    nested = cfn.describe_stack_resource(StackName=target.stack("backend"),
                                        LogicalResourceId="AgentCoreStack")["StackResourceDetail"]["PhysicalResourceId"]
    owned_project = cfn.describe_stack_resource(
        StackName=nested, LogicalResourceId="RuntimeContainerBuildProject")["StackResourceDetail"]["PhysicalResourceId"]
    if owned_project != name:
        return None
    projects = session.client("codebuild").batch_get_projects(names=[name]).get("projects", [])
    if len(projects) != 1:
        return None
    project = projects[0]
    try:
        validate_arn(project["arn"], target, "codebuild")
    except (ValueError, KeyError):
        return None
    desired = build_properties(captured)
    environment, source = project.get("environment", {}), project.get("source", {})
    if (project.get("name") != name or environment.get("type") != "ARM_CONTAINER"
            or environment.get("image") != desired["Environment"]["Image"]
            or environment.get("computeType") != desired["Environment"]["ComputeType"]
            or environment.get("privilegedMode") is not True or project.get("vpcConfig")
            or source.get("type") != "S3" or source.get("buildspec") != desired["Source"]["BuildSpec"]
            or not source.get("location", "").startswith(bucket+"/")
            or project.get("serviceRole") !=
            f"arn:aws:iam::{target.account}:role/{target.stack('backend')}-runtime-build"):
        return None
    variables = {entry["name"]: entry for entry in environment.get("environmentVariables", [])}
    expected = {
        "AWS_DEFAULT_REGION": target.region, "AWS_ACCOUNT_ID": target.account,
        "IMAGE_REPO_NAME": uri[len(registry):], "RUNTIME_SOURCE_BUCKET": bucket,
        "ENVIRONMENT": installation.environment,
    }
    if set(variables) != {*expected, "IMAGE_TAG"} or any(
            variables[name].get("value") != value or variables[name].get("type") != "PLAINTEXT"
            for name, value in expected.items()) or variables["IMAGE_TAG"].get("type") != "PLAINTEXT":
        return None
    return {"project_name": name, "repository_uri": uri, "environment": installation.environment}


def build_matches(build, receipt, target, captured):
    """Verify the original compilation, including an earlier release's source version when reusing."""
    desired = build_properties(captured)["Environment"]
    environment, source = build.get("environment", {}), build.get("source", {})
    variables = {value["name"]: value for value in environment.get("environmentVariables", [])}
    expected = {
        "AWS_DEFAULT_REGION": target.region, "AWS_ACCOUNT_ID": target.account,
        "IMAGE_REPO_NAME": receipt["repository_uri"].split("/", 1)[1],
        "RUNTIME_SOURCE_BUCKET": receipt["build_source_bucket"],
        "ENVIRONMENT": receipt["environment"], "IMAGE_TAG": receipt["build_image_tag"],
    }
    if (
        build.get("id") != receipt["build_id"] or build.get("projectName") != receipt["project_name"]
        or environment.get("type") != "ARM_CONTAINER" or environment.get("image") != desired["Image"]
        or environment.get("computeType") != desired["ComputeType"] or environment.get("privilegedMode") is not True
        or build.get("serviceRole") !=
        f"arn:aws:iam::{target.account}:role/{target.stack('backend')}-runtime-build"
        or source.get("type") != "S3" or source.get("buildspec") != receipt["buildspec"]
        or source.get("location") != f"{receipt['build_source_bucket']}/{receipt['build_source_key']}"
        or build.get("sourceVersion") != receipt["build_source_version"]
        or set(variables) != set(expected)
        or any(variables[name].get("value") != value or variables[name].get("type") != "PLAINTEXT"
               for name, value in expected.items())
    ):
        return False
    try:
        validate_arn(build["arn"], target, "codebuild")
    except (ValueError, KeyError):
        return False
    return True


def _reusable_receipts(release_dir, previous_receipt):
    if previous_receipt is not None:
        yield previous_receipt
        return
    for path in sorted(release_dir.parent.glob("*/backend-deployment.json"), reverse=True)[:32]:
        if path.parent == release_dir:
            continue
        try:
            value = json.loads(path.read_text())
            scan = json.loads((path.parent / "scan-runtime.json").read_text())
            payload = (path.parent / "runtime-source.zip").read_bytes()
            if hashlib.sha256(payload).hexdigest() != value.get("source_sha256"):
                continue
            value["verified_scan"] = scan
            yield value
        except (OSError, ValueError, TypeError):
            continue


def reusable_image(session, target, identity, binding, release_dir, captured, previous_receipt=None):
    """Reuse only a source-matching completed ARM build and a passed exact-digest scan receipt."""
    for prior in _reusable_receipts(release_dir, previous_receipt):
        if any(prior.get(key) != identity[key] for key in (
                "account", "region", "source_sha256", "runtime_fingerprint", "fingerprint_version")):
            continue
        digest, scan = prior.get("image_digest"), prior.get("verified_scan", {})
        if (not isinstance(digest, str) or not DIGEST.fullmatch(digest)
                or prior.get("repository_uri") != binding["repository_uri"]
                or prior.get("project_name") != binding["project_name"]
                or prior.get("environment") != binding["environment"]
                or scan.get("status") != "passed" or scan.get("digest") != digest
                or scan.get("component") != "runtime" or not prior.get("build_id")
                or prior.get("buildspec") != build_properties(captured)["Source"]["BuildSpec"]):
            continue
        builds = session.client("codebuild").batch_get_builds(ids=[prior["build_id"]]).get("builds", [])
        if len(builds) != 1:
            continue
        build = builds[0]
        proof = {
            **prior, "build_source_bucket": prior.get("build_source_bucket", prior.get("source_bucket")),
            "build_source_key": prior.get("build_source_key", prior.get("source_key")),
            "build_source_version": prior.get("build_source_version", prior.get("source_version")),
            "build_image_tag": prior.get("build_image_tag", prior.get("image_tag")),
        }
        if build.get("buildStatus") != "SUCCEEDED" or not build_matches(build, proof, target, captured):
            continue
        try:
            repository = binding["repository_uri"].split("/", 1)[1]
            image = session.client("ecr").describe_images(
                repositoryName=repository,
                imageIds=[{"imageTag": prior.get("build_image_tag", prior["image_tag"])}])["imageDetails"][0]
            if image.get("imageDigest") != digest:
                continue
            return prior
        except (ClientError, ValueError, KeyError, IndexError):
            continue
    return None


def start_runtime_build(session, target, bucket, installation, release_dir, captured, *, previous_receipt=None):
    """Start compatible upgrades before service builds; first installations retain serial orchestration.

    No CloudFormation change or Runtime binding occurs here. Reuse starts a new scan before the release tag
    exists, so a scan refusal can conservatively fall back to a normal build without occupying that tag.
    """
    from deployment.deploy_backend import put_version
    binding = compatible_builder(session, target, bucket, installation, captured)
    if binding is None:
        return None
    identity = source_identity(target, bucket, release_dir, captured)
    prior = reusable_image(session, target, identity, binding, release_dir, captured, previous_receipt)
    scan_started = None
    if prior:
        try:
            scan_started = refresh_image_scan(
                session.client("ecr"), binding["repository_uri"].split("/", 1)[1], prior["image_digest"])
        except ClientError as exc:
            if exc.response["Error"]["Code"] not in ("LimitExceededException", "UnsupportedImageTypeException"):
                raise
            prior = None
        except RuntimeError:
            # The shared refresh guard refused an old scan at ECR's daily limit; compile a new image instead.
            prior = None
    version = put_version(session.client("s3"), bucket, identity["source_key"], captured["payload"])
    receipt = {**identity, **binding, "source_version": version,
               "buildspec": build_properties(captured)["Source"]["BuildSpec"], "started_early": True}
    if prior:
        receipt.update(mode="reuse", build_id=prior["build_id"], image_digest=prior["image_digest"],
                       prior_image_tag=prior["image_tag"], scan_not_before=scan_started,
                       build_source_bucket=prior.get("build_source_bucket", prior["source_bucket"]),
                       build_source_key=prior.get("build_source_key", prior["source_key"]),
                       build_source_version=prior.get("build_source_version", prior["source_version"]),
                       build_image_tag=prior.get("build_image_tag", prior["image_tag"]))
    else:
        build = session.client("codebuild").start_build(
            projectName=binding["project_name"],
            sourceLocationOverride=f"{bucket}/{identity['source_key']}", sourceVersion=version,
            environmentVariablesOverride=[{"name": "IMAGE_TAG", "value": release_dir.name, "type": "PLAINTEXT"}])["build"]
        receipt.update(mode="build", build_id=build["id"], build_source_bucket=bucket,
                       build_source_key=identity["source_key"], build_source_version=version,
                       build_image_tag=release_dir.name)
    (release_dir / "runtime-source.zip").write_bytes(captured["payload"])
    (release_dir / "backend-build-request.json").write_text(json.dumps(receipt, indent=2))
    return receipt


def validate_early_build(receipt, target, bucket, release_dir, captured):
    if not isinstance(receipt, dict):
        raise TypeError("Early Runtime build receipt must be an object")
    expected = source_identity(target, bucket, release_dir, captured)
    if any(receipt.get(key) != value for key, value in expected.items()):
        raise ValueError("Early Runtime build does not match the captured release identity/source")
    strings = ("source_version", "project_name", "repository_uri", "build_id", "environment",
               "build_source_bucket", "build_source_key", "build_source_version", "build_image_tag")
    if (receipt.get("mode") not in ("build", "reuse") or receipt.get("started_early") is not True
            or any(not isinstance(receipt.get(key), str) or not receipt[key] for key in strings)
            or receipt.get("buildspec") != build_properties(captured)["Source"]["BuildSpec"]):
        raise ValueError("Early Runtime build receipt is incomplete or uses another build recipe")
    if receipt["mode"] == "build" and any(receipt[key] != expected[value] for key, value in (
            ("build_source_bucket", "source_bucket"), ("build_source_key", "source_key"),
            ("build_image_tag", "image_tag"))):
        raise ValueError("Early Runtime compilation does not use this release's source/tag")
    if receipt["mode"] == "build" and receipt["build_source_version"] != receipt["source_version"]:
        raise ValueError("Early Runtime compilation does not use this release's source version")
    if receipt["mode"] == "reuse":
        cutoff = receipt.get("scan_not_before")
        if (not isinstance(receipt.get("image_digest"), str) or not DIGEST.fullmatch(receipt["image_digest"])
                or isinstance(cutoff, bool) or not isinstance(cutoff, (int, float))
                or not math.isfinite(cutoff) or cutoff <= 0):
            raise ValueError("Early Runtime reuse needs an exact digest and fresh scan cutoff")


def resume_runtime_build(session, target, bucket, installation, release_dir, captured):
    """Rejoin only a captured upgrade build; a reused digest renews its scan without another compilation."""
    path = release_dir / "backend-build-request.json"
    if not path.exists():
        return None
    if not resumable_early_build(release_dir):
        raise ValueError("Only an early Runtime build before backend preparation can resume")
    receipt = json.loads(path.read_text())
    validate_early_build(receipt, target, bucket, release_dir, captured)
    binding = compatible_builder(session, target, bucket, installation, captured)
    if binding is None or any(receipt[key] != value for key, value in binding.items()):
        raise ValueError("Early Runtime build no longer matches the owned ARM builder")
    builds = session.client("codebuild").batch_get_builds(ids=[receipt["build_id"]]).get("builds", [])
    if len(builds) != 1 or not build_matches(builds[0], receipt, target, captured):
        raise ValueError("Early Runtime build does not match the captured source/version/ARM recipe")
    allowed = ("SUCCEEDED",) if receipt["mode"] == "reuse" else ("IN_PROGRESS", "SUCCEEDED")
    if builds[0].get("buildStatus") not in allowed:
        raise ValueError("Early Runtime build failed or was stopped; use a new release")
    if receipt["mode"] == "reuse":
        ecr = session.client("ecr")
        repository = receipt["repository_uri"].split("/", 1)[1]
        images = ecr.describe_images(
            repositoryName=repository, imageIds=[{"imageTag": receipt["build_image_tag"]}]).get("imageDetails", [])
        if len(images) != 1 or images[0].get("imageDigest") != receipt["image_digest"]:
            raise ValueError("Reusable Runtime image no longer matches its original build")
        receipt["scan_not_before"] = refresh_image_scan(ecr, repository, receipt["image_digest"])
        path.write_text(json.dumps(receipt, indent=2))
    return receipt


def retag_reused_image(client, repository, receipt):
    """Publish the verified digest under this release's tag; existing tags must identify the same digest."""
    result = client.batch_get_image(repositoryName=repository,
                                    imageIds=[{"imageDigest": receipt["image_digest"]}])
    image = matching_manifest(result, receipt["image_digest"])
    if image is None:
        raise ValueError("Verified Runtime digest or matching manifest is unavailable for release retagging")
    manifest = image.get("imageManifest", "")
    if hashlib.sha256(manifest.encode()).hexdigest() != receipt["image_digest"].removeprefix("sha256:"):
        raise ValueError("Runtime manifest does not match its verified digest")
    try:
        response = client.put_image(repositoryName=repository, imageTag=receipt["image_tag"],
                                    imageManifest=manifest, imageManifestMediaType=image["imageManifestMediaType"],
                                    imageDigest=receipt["image_digest"])
        if response.get("image", {}).get("imageId", {}).get("imageDigest") != receipt["image_digest"]:
            raise ValueError("Release tag did not identify the verified Runtime digest")
    except ClientError as exc:
        if exc.response["Error"]["Code"] not in ("ImageAlreadyExistsException", "ImageTagAlreadyExistsException"):
            raise
        image = client.describe_images(repositoryName=repository,
                                       imageIds=[{"imageTag": receipt["image_tag"]}])["imageDetails"][0]
        if image.get("imageDigest") != receipt["image_digest"]:
            raise ValueError("Release Runtime tag already names a different digest") from None
