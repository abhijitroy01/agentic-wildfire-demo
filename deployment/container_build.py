"""Bounded CodeBuild groups and immutable native artifacts; no application imports."""
import hashlib
import json
import os
import re
import subprocess  # nosec B404 - fixed Docker/AWS argument lists, never a shell
from pathlib import Path

COMPONENTS = {
    "worker": ("traffic-simulator/Dockerfile", ".", "TRAFFIC_REPOSITORY_URI"),
    "api": ("backend/Dockerfile.api", ".", "REPOSITORY_URI"),
    "fire-risk": ("fire-risk-modeler/Dockerfile", ".", "FIRE_RISK_REPOSITORY_URI"),
    "firetruck": ("firetruck-simulator/Dockerfile", "firetruck-simulator", "FIRETRUCK_REPOSITORY_URI"),
    "radio": ("broadcastify/monitor/Dockerfile", "broadcastify", "RADIO_REPOSITORY_URI"),
}
GROUPS = {"platform": ("worker", "api"), "services": ("fire-risk", "firetruck", "radio")}
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
IMAGE_URI = re.compile(r"[0-9]{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com/[a-z0-9/_-]+")


def run(args, *, capture=False):
    """Execute fixed tool arguments and propagate failures before any image publication."""
    result = subprocess.run(args, check=True, text=True, capture_output=capture)  # nosec B603
    return result.stdout if capture else None


def native_inputs(files, component):
    """Fingerprint the complete native recipe and its dependency pins, independent of application code."""
    dockerfile = COMPONENTS[component][0]
    source = files[dockerfile].decode()
    if component == "worker":
        recipe = source[source.index("FROM public."):source.index("FROM ${SUMO_NATIVE_IMAGE}")]
        dependencies = ("traffic-simulator/requirements-sumo.txt",)
    else:
        recipe = source[source.index("FROM public."):source.index("FROM ${PYRO_WHEELS_IMAGE}")]
        dependencies = ("requirements-lock.txt", "fire-risk-modeler/requirements.txt")
    payload = {"format": 1, "platform": "linux/amd64", "recipe": recipe,
               "dependencies": {name: hashlib.sha256(files[name]).hexdigest() for name in dependencies}}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def image_detail(repository, tag):
    """Read one artifact tag; only an absent image permits native compilation."""
    response = json.loads(run(["aws", "ecr", "batch-get-image", "--repository-name", repository,
                               "--image-ids", f"imageTag={tag}", "--output", "json"], capture=True))
    images, failures = response.get("images", []), response.get("failures", [])
    if images:
        digest = images[0]["imageId"]["imageDigest"]
        if not DIGEST.fullmatch(digest):
            raise ValueError("Native artifact has an invalid digest")
        return digest
    if failures and all(f.get("failureCode") == "ImageNotFound" for f in failures):
        return None
    raise RuntimeError("Native artifact lookup failed; no cache absence was established")


def native_image(component, environment, *, root=Path(".")):
    """Build or reuse one content-addressed native artifact, then verify its recipe label by digest."""
    uri = environment["NATIVE_REPOSITORY_URI"]
    if not IMAGE_URI.fullmatch(uri):
        raise ValueError("Native artifact repository must be the installation's private ECR repository")
    dockerfile = COMPONENTS[component][0]
    names = [dockerfile, "traffic-simulator/requirements-sumo.txt"] if component == "worker" else [
        dockerfile, "requirements-lock.txt", "fire-risk-modeler/requirements.txt"]
    identity = native_inputs({name: (root / name).read_bytes() for name in names}, component)
    tag = f"{component}-{identity}"
    repository = uri.split("/", 1)[1]
    digest = image_detail(repository, tag)
    if digest is None:
        target = "sumo-native-artifact" if component == "worker" else "pyro-native-artifact"
        run(["docker", "build", "--platform", "linux/amd64", "-f", dockerfile, "--target", target,
             "--label", f"org.fireguard.native-inputs={identity}", "-t", f"{uri}:{tag}", "."])
        run(["docker", "push", f"{uri}:{tag}"])
        digest = image_detail(repository, tag)
        if digest is None:
            raise RuntimeError("Published native artifact could not be identified")
    immutable = f"{uri}@{digest}"
    run(["docker", "pull", immutable])
    labels = json.loads(run(["docker", "image", "inspect", immutable, "--format",
                             "{{json .Config.Labels}}"], capture=True))
    if not isinstance(labels, dict) or labels.get("org.fireguard.native-inputs") != identity:
        raise ValueError("Native artifact recipe label does not match captured inputs")
    print(f"Native artifact {component}: {digest}", flush=True)
    return immutable


def build(component, environment, native=None):
    """Verify the selected component before building its runtime; images are pushed only after all group gates."""
    dockerfile, context, variable = COMPONENTS[component]
    image = f"{environment[variable]}:{environment['IMAGE_TAG']}"
    arguments = ["docker", "build", "-f", dockerfile]
    if native:
        argument = "SUMO_NATIVE_IMAGE" if component == "worker" else "PYRO_WHEELS_IMAGE"
        arguments += ["--build-arg", f"{argument}={native}"]
    if component == "api" and environment.get("SUMO_VERIFY_IMAGE"):
        arguments += ["--build-arg", f"SUMO_VERIFY_IMAGE={environment['SUMO_VERIFY_IMAGE']}"]
    if component in ("worker", "api", "fire-risk"):
        verification = "wildfire-traffic-verification" if component == "worker" else f"wildfire-{component}-verification"
        run([*arguments, "--target", "verification", "-t", verification, context])
        run([*arguments, "--target", "runtime", "-t", image, context])
    else:
        run([*arguments, "-t", image, context])
    if component == "firetruck":
        run(["docker", "run", "--rm", "--mount",
             f"type=bind,source={environment['CODEBUILD_SRC_DIR']}/firetruck-simulator/tests,target=/app/tests,readonly",
             "--entrypoint", "python", image, "-m", "unittest", "discover", "-s", "tests"])
    if component == "radio":
        for name in ("test_private.py", "test_radio_pipeline.py", "test_audio_resilience.py"):
            target = f"/app/broadcastify/monitor/{name}"
            run(["docker", "run", "--rm", "--mount",
                 f"type=bind,source={environment['CODEBUILD_SRC_DIR']}/broadcastify/monitor/{name},target={target},readonly",
                 "--entrypoint", "python", image, target])
        run(["docker", "run", "--rm", "-e", "AWS_EC2_METADATA_DISABLED=true",
             "-e", f"AWS_REGION={environment['AWS_DEFAULT_REGION']}", "-e", "S3_BUCKET=fixture",
             "-e", "MONITOR_BEDROCK_MODEL_ID=fixture", "--entrypoint", "python", image,
             "-c", "import broadcastify.monitor.private"])
    return image


def execute(environment):
    """Run one of at most two build groups; shared SUMO verification stays before API verification."""
    group = environment["BUILD_GROUP"]
    selected = environment["BUILD_COMPONENTS"].split(",") if environment["BUILD_COMPONENTS"] else []
    if group not in GROUPS or len(selected) != len(set(selected)) or not set(selected) <= set(GROUPS[group]):
        raise ValueError("Build selection must be a distinct subset of one fixed group")
    if not re.fullmatch(r"\d{20}", environment["IMAGE_TAG"]):
        raise ValueError("Build tag must be the captured release ID")
    for variable in ("NATIVE_REPOSITORY_URI", *[COMPONENTS[c][2] for c in selected]):
        if not IMAGE_URI.fullmatch(environment[variable]):
            raise ValueError("Build repositories must be private ECR URIs")
    if group == "platform":
        run(["docker", "build", "-f", "deployment/infra/Dockerfile", "-t", "wildfire-infra-verification", "."])
        if "api" in selected and "worker" not in selected:
            source = environment.get("SUMO_VERIFY_IMAGE", "")
            if not re.fullmatch(IMAGE_URI.pattern + r"@sha256:[0-9a-f]{64}", source):
                raise ValueError("API verification requires the unchanged worker's exact digest")
            run(["docker", "pull", source])
    built = []
    for component in GROUPS[group]:
        if component in selected:
            native = native_image(component, environment) if component in ("worker", "fire-risk") else None
            built.append(build(component, environment, native))
    # A failed test or Docker build never reaches publication of this group's runtime images.
    for image in built:
        run(["docker", "push", image])


if __name__ == "__main__":
    execute(dict(os.environ))
