"""Captured component fingerprints and exact-digest image reuse for service releases."""
import hashlib
import io
import json
import zipfile

from botocore.exceptions import ClientError

from deployment.container_build import COMPONENTS, DIGEST

OUTPUTS = {"api": "RepositoryUri", "worker": "TrafficSimulatorRepositoryUri",
           "firetruck": "FiretruckRepositoryUri", "radio": "RadioRepositoryUri",
           "fire-risk": "FireRiskRepositoryUri"}
COMMON = {"requirements-lock.txt", "deployment/container_build.py", "deployment/infra/lib/stacks.ts"}
API_FILES = {"requirements-dev.txt", "pyproject.toml", "deployment/settings.toml",
             "ui/package.json", "ui/package-lock.json", "scripts/export_integration_contracts.py",
             "ui/src/shared/utils/typeGuards.ts", "ui/src/features/firetruck/utils/truckDisplayUtils.ts",
             "backend/runtime/agents/simulation_agent/tool-inputs.json",
             "fire-risk-modeler/tests/data/king-landscape.json"}
PREFIXES = {
    "api": ("backend/", "traffic-simulator/", "schemas/", "ui/scripts/",
            "ui/src/features/evacuation/components/", "ui/src/features/evacuation/generated/",
            "ui/src/features/fire-exposure/generated/"),
    "worker": ("traffic-simulator/",),
    "firetruck": ("firetruck-simulator/",),
    "radio": ("broadcastify/",),
    "fire-risk": ("fire-risk-modeler/",),
}
MODEL_CONTRACTS = {"fire-risk-modeler/fire_risk_modeler/" + name
                   for name in ("__init__.py", "models.py", "exposure.py", "geometry.py",
                                "landscape.py", "landscape_acquisition.py")}
MANIFEST_TYPES = {
    "application/vnd.docker.distribution.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
}


def fingerprints(payload):
    """Hash source, Docker recipes, locks, tests and shared build inputs from the immutable source ZIP."""
    with zipfile.ZipFile(io.BytesIO(payload)) as bundle:
        files = {name: bundle.read(name) for name in bundle.namelist()}
    values = {}
    for component in COMPONENTS:
        shared = COMMON | (API_FILES | MODEL_CONTRACTS if component == "api" else set())
        if component in ("worker", "fire-risk"):
            shared |= {"requirements-dev.txt", "pyproject.toml", "deployment/settings.toml"}
        required = shared | {COMPONENTS[component][0]}
        if not required <= files.keys():
            # Old release bundles lack the current build recipe; they cannot authorize a skipped build.
            continue
        selected = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()
                    if name in shared or name.startswith(PREFIXES[component])}
        values[component] = hashlib.sha256(json.dumps(
            {"version": 1, "platform": "linux/amd64", "files": selected}, sort_keys=True).encode()).hexdigest()
    return values


def verified_prior(folder, current):
    """Find the newest completed component verification with identical captured inputs; never trust tags alone."""
    for prior in sorted(folder.parent.iterdir(), reverse=True):
        if prior == folder or not prior.is_dir():
            continue
        try:
            identity = json.loads((prior / "identity.json").read_text())
            source = (prior / "source.zip").read_bytes()
            if hashlib.sha256(source).hexdigest() != identity["source_sha256"]:
                continue
            previous = fingerprints(source)
            images = json.loads((prior / "images.json").read_text())
            verification = json.loads((prior / "component-verification.json").read_text())
            for component, fingerprint in current.items():
                if previous.get(component) != fingerprint:
                    continue
                digest = images.get(OUTPUTS[component])
                scan = json.loads((prior / f"scan-{component}.json").read_text())
                proof = verification.get(component, {})
                if (isinstance(digest, str) and DIGEST.fullmatch(digest) and scan.get("status") == "passed"
                        and scan.get("component") == component and scan.get("digest") == digest
                        and proof.get("digest") == digest):
                    yield component, digest, prior.name, proof
        except (OSError, KeyError, ValueError, zipfile.BadZipFile):
            continue


def matching_manifest(response, digest):
    """Return one exact manifest when ECR's retained tag aliases all agree; failures never authorize reuse."""
    images = response.get("images", [])
    if response.get("failures") or not images:
        return None
    if any(image.get("imageId", {}).get("imageDigest") != digest for image in images):
        raise ValueError("Reusable image lookup returned a different digest")
    image = images[0]
    manifest, media_type = image.get("imageManifest"), image.get("imageManifestMediaType")
    if (not isinstance(manifest, str) or not manifest or media_type not in MANIFEST_TYPES
            or any(alias.get("imageManifest") != manifest or alias.get("imageManifestMediaType") != media_type
                   for alias in images)):
        return None
    if "sha256:" + hashlib.sha256(manifest.encode()).hexdigest() != digest:
        return None
    return image


def retag(ecr, repository, digest, tag):
    """Tag exactly the recorded manifest; never overwrite an immutable tag with another digest."""
    response = ecr.batch_get_image(repositoryName=repository, imageIds=[{"imageDigest": digest}])
    image = matching_manifest(response, digest)
    if image is None:
        return False
    try:
        result = ecr.put_image(repositoryName=repository, imageTag=tag, imageManifest=image["imageManifest"],
                               imageManifestMediaType=image["imageManifestMediaType"], imageDigest=digest)
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ImageAlreadyExistsException":
            raise
        existing = ecr.describe_images(repositoryName=repository, imageIds=[{"imageTag": tag}])["imageDetails"]
        if len(existing) != 1 or existing[0]["imageDigest"] != digest:
            raise ValueError("Release image tag already names another digest") from None
        return True
    if result.get("image", {}).get("imageId", {}).get("imageDigest") != digest:
        raise ValueError("Retagged image differs from its verified digest")
    return True


def build_recipe(folder):
    """Read the build recipe from the same captured cloud assembly used by the guarded foundation update."""
    template = json.loads((folder / "cdk.out" / "WildfireLabFoundation.template.json").read_text())
    projects = [resource["Properties"] for resource in template["Resources"].values()
                if resource["Type"] == "AWS::CodeBuild::Project"]
    if len(projects) != 1:
        raise ValueError("Captured foundation must contain exactly one service image builder")
    project = projects[0]
    return {"buildspec": project["Source"]["BuildSpec"], "environment": {
        key: project["Environment"][key] for key in ("Type", "Image", "ComputeType", "PrivilegedMode")}}


def build_verified(builder, ecr, foundation, component, proof, digest, recipe):
    """Re-identify the successful original build and its exact image, including across repeated reuse."""
    if (proof.get("project") != foundation["BuildProject"]
            or proof.get("source_bucket") != foundation["SourceBucket"] or proof.get("recipe") != recipe):
        return False
    builds = builder.batch_get_builds(ids=[proof.get("build_id", "")]).get("builds", [])
    if len(builds) != 1:
        return False
    build = builds[0]
    variables = {entry["name"]: entry["value"]
                 for entry in build.get("environment", {}).get("environmentVariables", [])}
    expected = {"IMAGE_TAG": proof.get("image_tag"), "BUILD_GROUP": proof.get("group"),
                "BUILD_COMPONENTS": ",".join(proof.get("components", []))}
    if (build.get("id") != proof.get("build_id") or build.get("buildStatus") != "SUCCEEDED"
            or build.get("projectName") != foundation["BuildProject"]
            or build.get("source", {}).get("location") != f"{foundation['SourceBucket']}/{proof.get('source_key')}"
            or build.get("sourceVersion") != proof.get("source_version")
            or build.get("source", {}).get("buildspec") != recipe["buildspec"]
            or any(build.get("environment", {}).get(actual) != recipe["environment"][expected]
                   for actual, expected in (("type", "Type"), ("image", "Image"), ("computeType", "ComputeType"),
                                            ("privilegedMode", "PrivilegedMode")))
            or component not in proof.get("components", []) or any(variables.get(k) != v for k, v in expected.items())):
        return False
    try:
        details = ecr.describe_images(repositoryName=foundation[OUTPUTS[component]].split("/", 1)[1],
                                      imageIds=[{"imageTag": proof["image_tag"]}])["imageDetails"]
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ImageNotFoundException":
            raise
        return False
    return len(details) == 1 and details[0]["imageDigest"] == digest


def plan_images(ecr, foundation, release, *, builder):
    """Retag unchanged verified images and record why each other component requires a build."""
    current = fingerprints(release.source)
    recipe = build_recipe(release.folder)
    reused = {}
    for component, digest, prior, proof in verified_prior(release.folder, current):
        if (component not in reused and build_verified(builder, ecr, foundation, component, proof, digest, recipe)
                and retag(ecr, foundation[OUTPUTS[component]].split("/", 1)[1], digest, release.id)):
            reused[component] = {"digest": digest, "verified_release": prior, "build_proof": proof}
    plan = {"source_sha256": release.digest, "fingerprints": current, "reused": reused,
            "build": [component for component in COMPONENTS if component not in reused]}
    (release.folder / "component-images.json").write_text(json.dumps(plan, indent=2))
    return plan
