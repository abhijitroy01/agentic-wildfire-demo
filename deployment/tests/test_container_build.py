"""Native compilation reuse and component gates are verified before publication."""
import hashlib
import io
import json
import zipfile
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from deployment import component_images, container_build, deploy_workspace

ROOT = Path(__file__).resolve().parents[2]
DIGEST = "sha256:" + "a" * 64
RELEASE = "20261027000000000000"
URI = "123456789012.dkr.ecr.us-west-2.amazonaws.com/"
MANIFEST = '{"schemaVersion":2,"mediaType":"application/vnd.docker.distribution.manifest.v2+json","layers":[]}'
MANIFEST_DIGEST = "sha256:" + hashlib.sha256(MANIFEST.encode()).hexdigest()


def environment(group="platform", selected="worker,api"):
    return {"BUILD_GROUP": group, "BUILD_COMPONENTS": selected, "IMAGE_TAG": RELEASE,
            "NATIVE_REPOSITORY_URI": URI + "native", "CODEBUILD_SRC_DIR": "/source",
            "AWS_DEFAULT_REGION": "us-west-2",
            **{variable: URI + component for component, (_, _, variable) in container_build.COMPONENTS.items()}}


@pytest.mark.parametrize("group,selected", [("platform", "worker,api"),
                                          ("services", "fire-risk,firetruck,radio")])
def test_component_verification_precedes_runtime_and_all_group_pushes(monkeypatch, group, selected):
    calls = []
    monkeypatch.setattr(container_build, "run", lambda args, **_: calls.append(args))
    monkeypatch.setattr(container_build, "native_image", lambda *_: URI + "native@" + DIGEST)
    container_build.execute(environment(group, selected))
    push = next(i for i, args in enumerate(calls) if args[:2] == ["docker", "push"])
    assert all(args[:2] == ["docker", "push"] for args in calls[push:])
    for component in selected.split(","):
        recipe = container_build.COMPONENTS[component][0]
        builds = [(i, args) for i, args in enumerate(calls) if args[:2] == ["docker", "build"]
                  and recipe in args]
        if component in ("worker", "api", "fire-risk"):
            assert [args[args.index("--target")+1] for _, args in builds] == ["verification", "runtime"]
        else:
            gates = [(i, args) for i, args in enumerate(calls) if args[:2] == ["docker", "run"]
                     and URI + component + ":" + RELEASE in args]
            assert gates and all(builds[-1][0] < i < push for i, _ in gates)
            assert any("readonly" in " ".join(args) for _, args in gates)
    if group == "platform":
        assert next(i for i, a in enumerate(calls) if "wildfire-traffic-verification" in a) < next(
            i for i, a in enumerate(calls) if "wildfire-api-verification" in a)


def test_failed_gate_prevents_every_runtime_publication(monkeypatch):
    calls = []

    def run(args, **_):
        calls.append(args)
        if args[:2] == ["docker", "run"]:
            raise RuntimeError("failed test")
    monkeypatch.setattr(container_build, "run", run)
    with pytest.raises(RuntimeError, match="failed test"):
        container_build.execute(environment("services", "firetruck,radio"))
    assert not any(args[:2] == ["docker", "push"] for args in calls)


def test_radio_image_gate_mounts_and_runs_every_monitor_regression_before_publication(monkeypatch):
    calls = []
    monkeypatch.setattr(container_build, "run", lambda args, **_: calls.append(args))
    container_build.execute(environment("services", "radio"))
    push = next(i for i, args in enumerate(calls) if args[:2] == ["docker", "push"])
    suites = list((ROOT / "broadcastify/monitor").glob("test_*.py"))
    assert suites, "The radio image gate must find the monitor regression suites"
    for source in suites:
        target = f"/app/broadcastify/monitor/{source.name}"
        mount = f"type=bind,source=/source/broadcastify/monitor/{source.name},target={target},readonly"
        gates = [(i, args) for i, args in enumerate(calls)
                 if args[:2] == ["docker", "run"] and args[-1] == target]
        assert len(gates) == 1 and gates[0][0] < push, source.name
        assert mount in gates[0][1] and "--entrypoint" in gates[0][1], source.name


@pytest.mark.parametrize("selected", ["api,api", "radio", "unknown"])
def test_invalid_selection_never_executes_tools(monkeypatch, selected):
    monkeypatch.setattr(container_build, "run", lambda *_: pytest.fail("must reject before tools"))
    with pytest.raises(ValueError):
        container_build.execute(environment(selected=selected))


def test_api_only_rebuild_uses_immutable_worker_and_refuses_a_mutable_tag(monkeypatch):
    calls = []
    monkeypatch.setattr(container_build, "run", lambda args, **_: calls.append(args))
    env = {**environment(selected="api"), "SUMO_VERIFY_IMAGE": URI + "worker@" + DIGEST}
    container_build.execute(env)
    assert ["docker", "pull", env["SUMO_VERIFY_IMAGE"]] in calls
    api = [args for args in calls if "backend/Dockerfile.api" in args]
    assert all("SUMO_VERIFY_IMAGE=" + env["SUMO_VERIFY_IMAGE"] in args for args in api)
    with pytest.raises(ValueError, match="exact digest"):
        container_build.execute({**env, "SUMO_VERIFY_IMAGE": URI + "worker:latest"})


@pytest.mark.parametrize("component", ["worker", "fire-risk"])
@pytest.mark.parametrize("warm", [False, True])
def test_native_artifact_on_fresh_builder_reuses_digest_only_after_recipe_verification(monkeypatch, component, warm):
    names = [container_build.COMPONENTS[component][0], "requirements-lock.txt",
             "traffic-simulator/requirements-sumo.txt", "fire-risk-modeler/requirements.txt"]
    identity = container_build.native_inputs({name: (ROOT / name).read_bytes() for name in names}, component)
    lookups, calls = iter([DIGEST] if warm else [None, DIGEST]), []
    monkeypatch.setattr(container_build, "image_detail", lambda *_: next(lookups))

    def run(args, **_):
        calls.append(args)
        return json.dumps({"org.fireguard.native-inputs": identity}) if args[:3] == [
            "docker", "image", "inspect"] else None
    monkeypatch.setattr(container_build, "run", run)
    image = container_build.native_image(component, environment(), root=ROOT)
    assert image == URI + "native@" + DIGEST
    assert any(args[:2] == ["docker", "build"] for args in calls) is not warm
    assert ["docker", "pull", image] in calls
    if not warm:
        assert any(args[:2] == ["docker", "push"] for args in calls)
    monkeypatch.setattr(container_build, "image_detail", lambda *_: DIGEST)
    monkeypatch.setattr(container_build, "run", lambda *_args, **_: "{}")
    with pytest.raises(ValueError, match="recipe label"):
        container_build.native_image(component, environment(), root=ROOT)


def source_files():
    names = component_images.COMMON | component_images.API_FILES | component_images.MODEL_CONTRACTS
    names |= {recipe for recipe, _, _ in container_build.COMPONENTS.values()}
    names |= {"requirements-dev.txt", "pyproject.toml", "deployment/settings.toml",
              "backend/app.py", "traffic-simulator/traffic_simulator/worker.py",
              "traffic-simulator/requirements-sumo.txt", "fire-risk-modeler/requirements.txt",
              "fire-risk-modeler/fire_risk_modeler/surface_fire.py"}
    return {name: (ROOT / name).read_bytes() for name in names}


def bundle(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return output.getvalue()


@pytest.mark.parametrize("path,affected", [
    ("backend/app.py", {"api"}),
    ("traffic-simulator/traffic_simulator/worker.py", {"worker", "api"}),
    ("deployment/settings.toml", {"worker", "api", "fire-risk"}),
    ("fire-risk-modeler/fire_risk_modeler/surface_fire.py", {"fire-risk"}),
    ("requirements-lock.txt", set(container_build.COMPONENTS)),
])
def test_component_fingerprints_follow_captured_dependencies(path, affected):
    files = source_files()
    before = component_images.fingerprints(bundle(files))
    files[path] += b"\n# changed\n"
    after = component_images.fingerprints(bundle(files))
    assert {name for name in before if before[name] != after[name]} == affected


@pytest.mark.parametrize("component,pin", [("worker", "traffic-simulator/requirements-sumo.txt"),
                                        ("fire-risk", "fire-risk-modeler/requirements.txt")])
def test_native_fingerprint_ignores_application_code_but_requires_exact_pins(component, pin):
    files = source_files()
    before = container_build.native_inputs(files, component)
    files["backend/app.py"] += b"app change"
    assert container_build.native_inputs(files, component) == before
    files[pin] += b"pin change"
    assert container_build.native_inputs(files, component) != before


@pytest.mark.parametrize("changed", [None, "status", "source-version", "tag", "missing", "buildspec", "architecture"])
def test_reuse_requires_original_successful_build_and_exact_image(changed):
    foundation = {"BuildProject": "builder", "SourceBucket": "source", "RepositoryUri": URI + "api"}
    proof = {"build_id": "builder:build", "project": "builder", "source_bucket": "source",
             "source_key": "sources/exact.zip", "source_version": "version", "image_tag": RELEASE,
             "group": "platform", "components": ["api"], "digest": DIGEST}
    recipe = {"buildspec": "reviewed-buildspec", "environment": {
        "Type": "LINUX_CONTAINER", "Image": "aws/codebuild/standard:7.0", "ComputeType": "BUILD_GENERAL1_LARGE",
        "PrivilegedMode": True}}
    proof["recipe"] = recipe
    built = {"id": proof["build_id"], "buildStatus": "SUCCEEDED", "projectName": "builder",
             "source": {"location": "source/sources/exact.zip", "buildspec": recipe["buildspec"]},
             "sourceVersion": "version",
             "environment": {"type": "LINUX_CONTAINER", "image": "aws/codebuild/standard:7.0",
                             "computeType": "BUILD_GENERAL1_LARGE", "privilegedMode": True,
                             "environmentVariables": [{"name": k, "value": v} for k, v in {
                 "IMAGE_TAG": RELEASE, "BUILD_GROUP": "platform", "BUILD_COMPONENTS": "api"}.items()]}}
    if changed == "status":
        built["buildStatus"] = "FAILED"
    if changed == "source-version":
        built["sourceVersion"] = "other"
    if changed == "buildspec":
        built["source"]["buildspec"] = "unreviewed"
    if changed == "architecture":
        built["environment"]["type"] = "ARM_CONTAINER"
    builder, ecr = MagicMock(), MagicMock()
    builder.batch_get_builds.return_value = {"builds": [] if changed == "missing" else [built]}
    ecr.describe_images.return_value = {"imageDetails": [{"imageDigest": "sha256:" + "b" * 64
                                                         if changed == "tag" else DIGEST}]}
    assert component_images.build_verified(builder, ecr, foundation, "api", proof, DIGEST, recipe) is (changed is None)


def test_build_groups_start_before_waiting_and_failure_never_records_deployable_images(tmp_path, monkeypatch):
    release = SimpleNamespace(source=b"captured", digest="captured-hash", folder=tmp_path, id=RELEASE, resumed=False)
    foundation = {"BuildProject": "builder", "SourceBucket": "source",
                  **{key: URI + component for component, key in component_images.OUTPUTS.items()}}
    builder, ecr, session, events = MagicMock(), MagicMock(), MagicMock(), []
    session.client.side_effect = lambda name: {"codebuild": builder, "ecr": ecr, "s3": MagicMock()}[name]
    builder.start_build.side_effect = lambda **options: (
        events.append(("start", options["environmentVariablesOverride"])) or
        {"build": {"id": "build" + str(len(events))}})
    monkeypatch.setattr(deploy_workspace, "plan_images", lambda *_args, **_: {
        "build": list(container_build.COMPONENTS), "reused": {}})
    monkeypatch.setattr(deploy_workspace, "put_version", lambda *_: "version")

    def wait(*_args, **_):
        assert len([event for event in events if event[0] == "start"]) == 2
        events.append(("wait", None))
        raise RuntimeError("build failed")
    monkeypatch.setattr(deploy_workspace, "wait_build", wait)
    with pytest.raises(RuntimeError, match="build failed"):
        deploy_workspace.build_component_images(session, foundation, release)
    request = json.loads((tmp_path / "application-build-request.json").read_text())
    assert set(request["builds"]) == {"platform", "services"}
    assert not (tmp_path / "images.json").exists()
    ecr.describe_images.assert_not_called()


def test_tampered_source_cannot_authorize_component_reuse(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    payload = bundle(source_files())
    (old / "source.zip").write_bytes(payload)
    (old / "identity.json").write_text(json.dumps({"source_sha256": hashlib.sha256(payload).hexdigest()}))
    (old / "source.zip").write_bytes(payload + b"changed")
    assert list(component_images.verified_prior(new, component_images.fingerprints(payload))) == []


def test_partial_concurrent_start_resumes_only_missing_job(tmp_path, monkeypatch):
    payload = bundle(source_files())
    release = SimpleNamespace(source=payload, digest=hashlib.sha256(payload).hexdigest(), folder=tmp_path,
                              id=RELEASE, resumed=False)
    foundation = {"BuildProject": "builder", "SourceBucket": "source",
                  **{key: URI + component for component, key in component_images.OUTPUTS.items()}}
    plan = {"build": list(container_build.COMPONENTS), "reused": {}, "source_sha256": release.digest}
    (tmp_path / "component-images.json").write_text(json.dumps(plan))
    builder, ecr, session = MagicMock(), MagicMock(), MagicMock()
    session.client.side_effect = lambda name: {"codebuild": builder, "ecr": ecr, "s3": MagicMock()}[name]
    builder.start_build.side_effect = [{"build": {"id": "first"}}, RuntimeError("capacity")]
    monkeypatch.setattr(deploy_workspace, "plan_images", lambda *_args, **_: plan)
    monkeypatch.setattr(deploy_workspace, "put_version", lambda *_: "version")
    with pytest.raises(RuntimeError, match="capacity"):
        deploy_workspace.build_component_images(session, foundation, release)
    assert set(json.loads((tmp_path / "application-build-request.json").read_text())["builds"]) == {"platform"}
    builder.batch_get_builds.return_value = {"builds": [{
        "projectName": "builder", "source": {"location": f"source/sources/{release.digest}.zip"},
        "sourceVersion": "version", "environment": {"environmentVariables": [
            {"name": key, "value": value} for key, value in {
                "IMAGE_TAG": RELEASE, "BUILD_GROUP": "platform", "BUILD_COMPONENTS": "worker,api"}.items()]}}]}
    builder.start_build.side_effect = [{"build": {"id": "second"}}]
    release.resumed = True
    monkeypatch.setattr(deploy_workspace, "wait_build", lambda *_args, **_: None)
    monkeypatch.setattr(deploy_workspace, "verify_image", lambda *_args, **_: None)
    monkeypatch.setattr(deploy_workspace, "build_recipe", lambda *_: {"fixture": "recipe"})
    ecr.describe_images.return_value = {"imageDetails": [{"imageDigest": DIGEST}]}
    deploy_workspace.build_component_images(session, foundation, release)
    request = json.loads((tmp_path / "application-build-request.json").read_text())
    assert request["builds"]["platform"]["build_id"] == "first"
    assert request["builds"]["services"]["build_id"] == "second"
    assert builder.start_build.call_count == 3
    last = builder.start_build.call_args.kwargs
    assert last["sourceVersion"] == "version"
    assert {v["name"]: v["value"] for v in last["environmentVariablesOverride"]}["BUILD_GROUP"] == "services"


def test_retag_requires_the_recorded_digest_before_any_write():
    ecr = MagicMock()
    ecr.batch_get_image.return_value = {"images": [{"imageId": {"imageDigest": "sha256:" + "b" * 64}}]}
    with pytest.raises(ValueError, match="different digest"):
        component_images.retag(ecr, "repo", DIGEST, RELEASE)
    ecr.put_image.assert_not_called()
    ecr.batch_get_image.return_value = {"failures": [{"failureCode": "ImageNotFound"}]}
    assert component_images.retag(ecr, "repo", DIGEST, RELEASE) is False
    ecr.put_image.assert_not_called()


def manifest_alias(tag):
    return {"imageId": {"imageDigest": MANIFEST_DIGEST, "imageTag": tag}, "imageManifest": MANIFEST,
            "imageManifestMediaType": "application/vnd.docker.distribution.manifest.v2+json"}


@pytest.mark.parametrize("media_type", sorted(component_images.MANIFEST_TYPES))
def test_retag_accepts_only_identical_verified_aliases(media_type):
    ecr = MagicMock()
    images = [manifest_alias("release-first"), manifest_alias("release-second")]
    for image in images:
        image["imageManifestMediaType"] = media_type
    ecr.batch_get_image.return_value = {"images": images, "failures": []}
    ecr.put_image.return_value = {"image": {"imageId": {"imageDigest": MANIFEST_DIGEST}}}
    assert component_images.retag(ecr, "repo", MANIFEST_DIGEST, RELEASE) is True
    ecr.put_image.assert_called_once_with(repositoryName="repo", imageTag=RELEASE, imageManifest=MANIFEST,
                                         imageManifestMediaType=media_type, imageDigest=MANIFEST_DIGEST)


@pytest.mark.parametrize("change", ["digest", "manifest", "media-type", "unsupported-type", "missing-manifest",
                                  "empty-manifest", "tampered-manifests", "failures", "missing-images"])
def test_retag_rejects_inconsistent_or_unverified_aliases_before_any_write(change):
    ecr = MagicMock()
    response = {"images": [manifest_alias("release-first"), manifest_alias("release-second")]}
    if change == "digest":
        response["images"][1]["imageId"]["imageDigest"] = DIGEST
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
    ecr.batch_get_image.return_value = response
    if change == "digest":
        with pytest.raises(ValueError, match="different digest"):
            component_images.retag(ecr, "repo", MANIFEST_DIGEST, RELEASE)
    else:
        assert component_images.retag(ecr, "repo", MANIFEST_DIGEST, RELEASE) is False
    ecr.put_image.assert_not_called()


@pytest.mark.parametrize("same_digest", [True, False])
def test_matching_aliases_never_replace_a_conflicting_existing_release_tag(same_digest):
    ecr = MagicMock()
    ecr.batch_get_image.return_value = {"images": [
        manifest_alias("release-first"), manifest_alias("release-second")]}
    ecr.put_image.side_effect = ClientError({"Error": {"Code": "ImageAlreadyExistsException"}}, "PutImage")
    ecr.describe_images.return_value = {"imageDetails": [{"imageDigest": MANIFEST_DIGEST if same_digest else DIGEST}]}
    if same_digest:
        assert component_images.retag(ecr, "repo", MANIFEST_DIGEST, RELEASE) is True
    else:
        with pytest.raises(ValueError, match="another digest"):
            component_images.retag(ecr, "repo", MANIFEST_DIGEST, RELEASE)
    ecr.put_image.assert_called_once()
    ecr.describe_images.assert_called_once_with(repositoryName="repo", imageIds=[{"imageTag": RELEASE}])


def test_third_unchanged_release_reuses_aliases_and_preserves_original_build_proofs(tmp_path, monkeypatch):
    payload = bundle(source_files())
    source_sha = hashlib.sha256(payload).hexdigest()
    first, second, third = (tmp_path / str(int(RELEASE) + offset) for offset in (1, 2, 3))
    recipe = {"buildspec": "reviewed-buildspec", "environment": {
        "Type": "LINUX_CONTAINER", "Image": "aws/codebuild/standard:7.0", "ComputeType": "BUILD_GENERAL1_LARGE",
        "PrivilegedMode": True}}
    foundation = {"BuildProject": "builder", "SourceBucket": "source",
                  **{key: URI + component for component, key in component_images.OUTPUTS.items()}}
    proofs, builds = {}, {}
    for group, components in {"platform": ["worker", "api"], "services": ["fire-risk", "firetruck", "radio"]}.items():
        proof = {"build_id": "builder:" + group, "project": "builder", "source_bucket": "source",
                 "source_key": f"sources/{source_sha}.zip", "source_version": "original-version",
                 "image_tag": first.name, "group": group, "components": components,
                 "digest": MANIFEST_DIGEST, "recipe": recipe}
        for component in components:
            proofs[component] = deepcopy(proof)
        builds[proof["build_id"]] = {
            "id": proof["build_id"], "buildStatus": "SUCCEEDED", "projectName": "builder",
            "source": {"location": "source/" + proof["source_key"], "buildspec": recipe["buildspec"]},
            "sourceVersion": proof["source_version"],
            "environment": {"type": "LINUX_CONTAINER", "image": "aws/codebuild/standard:7.0",
                            "computeType": "BUILD_GENERAL1_LARGE", "privilegedMode": True,
                            "environmentVariables": [{"name": key, "value": value} for key, value in {
                                "IMAGE_TAG": first.name, "BUILD_GROUP": group,
                                "BUILD_COMPONENTS": ",".join(components)}.items()]}}
    for folder in (first, second):
        folder.mkdir()
        (folder / "source.zip").write_bytes(payload)
        (folder / "identity.json").write_text(json.dumps({"source_sha256": source_sha}))
        (folder / "images.json").write_text(json.dumps({
            key: MANIFEST_DIGEST for key in component_images.OUTPUTS.values()}))
        (folder / "component-verification.json").write_text(json.dumps(proofs))
        for component in proofs:
            (folder / f"scan-{component}.json").write_text(json.dumps({
                "status": "passed", "component": component, "digest": MANIFEST_DIGEST}))
    third.mkdir()
    ecr, builder = MagicMock(), MagicMock()
    ecr.describe_images.return_value = {"imageDetails": [{"imageDigest": MANIFEST_DIGEST}]}
    ecr.batch_get_image.return_value = {"images": [manifest_alias(first.name), manifest_alias(second.name)]}
    ecr.put_image.return_value = {"image": {"imageId": {"imageDigest": MANIFEST_DIGEST}}}
    builder.batch_get_builds.side_effect = lambda **options: {"builds": [builds[options["ids"][0]]]}
    monkeypatch.setattr(component_images, "build_recipe", lambda _folder: recipe)
    release = SimpleNamespace(source=payload, digest=source_sha, folder=third, id=third.name)
    plan = component_images.plan_images(ecr, foundation, release, builder=builder)
    assert plan["build"] == []
    assert set(plan["reused"]) == set(container_build.COMPONENTS)
    for component, reused in plan["reused"].items():
        assert reused == {"digest": MANIFEST_DIGEST, "verified_release": second.name, "build_proof": proofs[component]}
    assert ecr.put_image.call_count == len(container_build.COMPONENTS)
    assert all(call.kwargs["imageTag"] == third.name for call in ecr.put_image.call_args_list)
    assert all(call.kwargs["imageDigest"] == MANIFEST_DIGEST for call in ecr.put_image.call_args_list)
    assert {call.kwargs["ids"][0] for call in builder.batch_get_builds.call_args_list} == set(builds)
    builder.start_build.assert_not_called()
