"""Check, plan and deploy one FireGUard installation from its installation file. Default: check and plan only."""
import argparse
import fcntl
import hashlib
import io
import json
import os
import re
import shutil
import subprocess  # nosec B404 - command() runs only this module's own tool argument lists, never a shell
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import boto3
from botocore.exceptions import ClientError, CredentialRetrievalError, NoCredentialsError, ProfileNotFound

from deployment import NPM_CI, ROOT
from deployment.acceptance.visitor import error_code
from deployment.change_sets import deploy_guarded, parse_acknowledgment
from deployment.component_images import OUTPUTS, build_recipe, plan_images
from deployment.container_build import GROUPS
from deployment.deploy_backend import (
    capture_backend,
    deploy_backend,
    prepare_backend,
    put_version,
    require_environment,
    restore_backend,
    runtime_source,
    verify_contracts,
    wait_build,
)
from deployment.deployment_target import inspect_stack, parameter_values, safe_outputs, validate_arn
from deployment.image_scans import POLICY, exceptions_apply, load_policy, refresh_image_scan, verify_image
from deployment.installation import (
    CAMERA_KEY_MAX_LENGTH,
    CAMERA_KEY_MIN_LENGTH,
    DEFAULT_INSTALLATION,
    access_parameters,
    camera_key_valid,
    camera_parameters,
    changed_settings,
    load_installation,
    operator_parameters,
    removed_option_message,
    stack_parameters,
)
from deployment.package_source import archive, git_commit, package_source, source_bundle
from deployment.publish_workspace import build_environment, publish, secret_values, verify_assets
from deployment.release_checks import CutoverAcceptance, browser_check, candidate_check, live_checks
from deployment.retention import LOG_RETENTION_DAYS, require_retained_images, retain_installation_logs
from deployment.runtime_binding import initial_parameters, require_api_binding, verify_endpoint
from deployment.runtime_build import resumable_early_build, resume_runtime_build, start_runtime_build

IMAGE_OUTPUTS = ("RepositoryUri", "TrafficSimulatorRepositoryUri", "FiretruckRepositoryUri", "RadioRepositoryUri", "FireRiskRepositoryUri")
# The application build may run 60 minutes after queueing 30 (the ImageBuilder project's timeout and queuedTimeout,
# deployment/infra/lib/stacks.ts); waiting less fails a valid release while CodeBuild keeps building (B-2).
APPLICATION_BUILD_SECONDS = (60 + 30) * 60
# The standard CDK bootstrap stack; --apply creates it when an account and Region do not have it yet.
BOOTSTRAP_STACK = "CDKToolkit"
RELEASE_ID = re.compile(r"\d{20}")
# Error code of the WAF staging allowlist's 403 (deployment/infra/lib/stacks.ts, "staging-network-only" body).
STAGING_BLOCK_CODE = "NETWORK_NOT_ALLOWED"
# What the release record says about anonymous access for each [access] mode.
ACCESS = {"public": "enabled", "staging": "staging allowlist only"}
# Application provider-secret field <- provider-input secret field. The API task receives the application secret
# whole as PROVIDER_CREDENTIALS (D43), so every field copied here reaches the API; a test pins
# this list.
PROVIDER_FIELDS = (("TOMTOM_API_KEY", "TOMTOM_API_KEY"), ("FIRMS_MAP_KEY", "NASA_FIRMS_API_KEY"),
                   ("DELPHIRE_API_KEY", "DELPHIRE_API_KEY"))
# The largest demo recording the deployer copies: an input bound, not an operational setting (D38).
RECORDING_MAX_MIB = 50
RECORDING_MAX_BYTES = RECORDING_MAX_MIB * 1024 * 1024
# What an installation older than the removed one-time migrations (DEPL-4) must do instead.
UPGRADE_FIRST = ('This deployer cannot upgrade it: deploy a new installation instead, as "Installations from '
                 'before October 7, 2026" in architecture/29-workspace-operations.md describes. '
                 "No AWS changes made.")


@dataclass(frozen=True)
class Preflight:
    """Read-only preflight results: existing stacks, provider keys, captured audio, bootstrap need, source digest."""
    stacks: dict
    provider: dict
    audio: dict
    bootstrap: bool
    digest: str


@dataclass(frozen=True)
class Release:
    """One release's captured inputs: ID, records folder, source bundle and digest, verified UI and backend assets."""
    id: str
    folder: Path
    source: bytes
    digest: str
    files: list
    backend: dict
    resumed: bool

    @property
    def assembly(self):
        """The captured CDK cloud assembly; every stack update of the release deploys from it."""
        return self.folder / "cdk.out"


@dataclass(frozen=True)
class Candidate:
    """The application after its candidate pass: next stack parameters and the prior and candidate bindings."""
    parameters: dict
    previous: dict
    lab: dict
    backend: dict
    prior_arn: str
    prior: dict
    binding: dict
    providers_changed: bool


def arguments(argv=None):
    """Parse installation, apply and bounded recovery of a captured release."""
    parser = argparse.ArgumentParser(
        prog="deployment/deploy-all.sh", allow_abbrev=False, description=__doc__,
        epilog="Every other value lives in the installation file and deployment/settings.toml; "
               "see deployment/README.md.")
    parser.add_argument("installation", nargs="?",
                        help=f"installation file, relative to the repository root (default: {DEFAULT_INSTALLATION})")
    parser.add_argument("--apply", action="store_true",
                        help="deploy or update the installation; without it nothing in AWS changes")
    parser.add_argument("--resume-release", metavar="RELEASE",
                        help="resume captured assets before the Runtime build; application code and settings must match")
    parser.add_argument("--acknowledge", action="append", default=[], metavar="STACK/RESOURCE=TYPE:SCOPE",
                        help="with --apply, approve one change that the guard stopped for review, as its stop message "
                             "prints it; repeat for each. It never overrides a stateful replacement or deletion")
    options, unexpected = parser.parse_known_args(argv)
    if unexpected:
        raise SystemExit(removed_option_message(unexpected[0]))
    return options


def command(args, cwd, env=None, *, timeout=None):
    """Run one deployer tool (npm, node or the checkout's cdk) from an argument list with no shell; fail on error.

    timeout, in seconds, kills the tool at the deadline (subprocess.TimeoutExpired); None waits for it.
    """
    # Every caller builds a fixed program and arguments here; the only installation values in them are the validated
    # name, account and Region (deployment_target.Target) and paths the deployer resolved.
    # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
    subprocess.run([str(a) for a in args], cwd=cwd, env=env, check=True,  # nosec B603 - argument list, no shell
                   timeout=timeout)


def cdk_environment(target, profile):
    """Return the environment for the checkout's npm and cdk: the installation's profile, account and Region."""
    env = {**os.environ, "AWS_PROFILE": profile, "AWS_REGION": target.region,
           "AWS_DEFAULT_REGION": target.region, "CDK_DEFAULT_ACCOUNT": target.account,
           "CDK_DEFAULT_REGION": target.region}
    # Never carry offline synth into a release; build_environment blanks a caller's VITE settings.
    env.pop("LAB_SYNTH_OFFLINE", None)
    # The CDK CLI prefers key variables in the caller's shell (it reads both spellings) to AWS_PROFILE, so drop them:
    # cdk then uses the installation's profile, as the deployer's boto3 session does.
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
                "AMAZON_ACCESS_KEY_ID", "AMAZON_SECRET_ACCESS_KEY", "AMAZON_SESSION_TOKEN"):
        env.pop(key, None)
    return env


def read_audio(session, source):
    """Capture bounded recordings before deployment; never mutate the originals."""
    if source.startswith("s3://"):
        parsed = urlsplit(source)
        if parsed.query or parsed.fragment or not parsed.netloc or not parsed.path[1:]:
            raise ValueError("Radio audio must be a concrete S3 object, without query or fragment")
        s3 = session.client("s3")
        head = s3.head_object(Bucket=parsed.netloc, Key=parsed.path[1:])
        if head["ContentLength"] > RECORDING_MAX_BYTES:
            raise ValueError(f"Each demo recording must be at most {RECORDING_MAX_MIB} MiB")
        options = {"VersionId": head["VersionId"]} if head.get("VersionId") else {"IfMatch": head["ETag"]}
        body = s3.get_object(Bucket=parsed.netloc, Key=parsed.path[1:], **options)["Body"]
        try:
            data = body.read(RECORDING_MAX_BYTES + 1)
        finally:
            body.close()
        suffix = Path(parsed.path).suffix.lower()
    else:
        path = Path(source).expanduser().resolve(strict=True)
        if path.stat().st_size > RECORDING_MAX_BYTES:
            raise ValueError(f"Each demo recording must be at most {RECORDING_MAX_MIB} MiB")
        data, suffix = path.read_bytes(), path.suffix.lower()
    if suffix not in {".mp3", ".wav", ".m4a", ".ogg", ".flac"} or not data or len(data) > RECORDING_MAX_BYTES:
        raise ValueError(f"Recording must be a nonempty supported audio file of at most {RECORDING_MAX_MIB} MiB")
    return "input/"+hashlib.sha256(data).hexdigest()+suffix, data


def require_unexpired_image_exceptions(root):
    """Fail before any build when root's image-scan policy lists exceptions on or after their expiry date."""
    policy, expires = load_policy(root / POLICY)
    if policy["exceptions"] and not exceptions_apply(expires):
        raise ValueError(
            f"{POLICY} exceptions expired on {expires.isoformat()}; after the build the image scan "
            "gate would block every finding they cover. Re-review them per the Exception re-review "
            f"step in {policy['review']} (drop fixed or unmatched exceptions, re-justify the rest, set "
            "a new expiry), then retry. No AWS changes made.")


def require_migrated(stacks):
    """Refuse stacks older than the one-time migrations W2.11 removed (DEPL-4), from their DescribeStacks parameters.

    A backend without GeocodeServiceS3Version still has the OpenAPI geocode and wildfire Gateway targets that Lambda
    targets replaced on 2026-09-29. A bound application without both endpoint slots predates the Runtime endpoint
    slots of 2026-10-07: it still invokes DEFAULT, and its job outboxes may need the pending-index backfill.
    This repository cannot upgrade either installation; deploy a new one instead.
    """
    backend, application = stacks.get("backend"), stacks.get("application")
    if backend and "GeocodeServiceS3Version" not in parameter_values(backend):
        raise ValueError(f"{backend.get('StackName', 'The backend stack')} has no GeocodeServiceS3Version parameter: "
                         "its geocode and wildfire Gateway targets predate the Lambda targets of 2026-09-29, and "
                         f"this deployer no longer migrates them. {UPGRADE_FIRST}")
    bound = parameter_values(application)
    if bound.get("SimulationRuntimeArn") and not (bound.get("RuntimeEndpointAVersion")
                                                  and bound.get("RuntimeEndpointBVersion")):
        raise ValueError(f"{application.get('StackName', 'The application stack')} is bound to a Runtime but has no "
                         "RuntimeEndpointAVersion and RuntimeEndpointBVersion: it predates the Runtime endpoint slots "
                         "of 2026-10-07, and this deployer no longer moves it off DEFAULT or backfills its "
                         f"pending-outbox index. {UPGRADE_FIRST}")


def require_operator_roles(iam, installation, caller_arn):
    """Require the deploying role in [aws] operator_role_arns and each listed role to exist with that exact ARN.

    The bucket policies refuse object access to every role outside the installation and this list (D52), so a
    deployer missing from it would be refused its own uploads, and an ARN whose path differs from the role's would
    refuse that operator. Read-only: no STS call of its own and one iam:GetRole per listed role.
    """
    session = re.fullmatch(r"arn:aws:sts::\d{12}:assumed-role/([^/]+)/[^/]+", caller_arn)
    if not session:
        kind = ("the root user" if caller_arn.endswith(":root") else
                "a federated user" if ":federated-user/" in caller_arn else "an IAM user")
        raise ValueError(f"The deployer must run as an IAM role session, not {kind}; [aws] operator_role_arns lists "
                         "roles only. No AWS changes made")
    role, account = session[1], installation.target.account
    if role not in {arn.rsplit("/", 1)[1] for arn in installation.operator_role_arns}:
        raise ValueError(f"[aws] operator_role_arns does not list the deploying role {role}; add "
                         f"arn:aws:iam::{account}:role/... for it (aws iam get-role --role-name {role} shows its ARN), "
                         "or the new bucket policies would refuse this deployer's own uploads. No AWS changes made")
    for arn in installation.operator_role_arns:
        try:
            actual = iam.get_role(RoleName=arn.rsplit("/", 1)[1])["Role"]["Arn"]
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "NoSuchEntity":
                raise
            raise ValueError(f"[aws] operator_role_arns names {arn}, which IAM does not have; fix or remove it. "
                             "No AWS changes made") from None
        if actual != arn:
            raise ValueError(f"[aws] operator_role_arns names {arn}, but that role's ARN is {actual} (its path "
                             "differs); use the exact ARN. No AWS changes made")


def preflight(session, installation):
    """Complete identity, ownership, provider, model, bootstrap and input checks before any AWS write."""
    target = installation.target
    caller = session.client("sts").get_caller_identity()
    if caller["Account"] != target.account:
        raise ValueError("Profile account does not match [aws] account_id; no AWS changes made")
    if session.get_partition_for_region(target.region) != "aws":
        raise ValueError("This deployment currently supports the commercial AWS partition")
    require_operator_roles(session.client("iam"), installation, caller["Arn"])
    # SDK endpoint metadata is advisory. Actual API reads establish availability/access.
    session.client("bedrock-agentcore-control").list_agent_runtimes(maxResults=1)
    zones = session.client("ec2").describe_availability_zones(
        Filters=[{"Name": "state", "Values": ["available"]}])["AvailabilityZones"]
    if len([z for z in zones if z.get("ZoneType", "availability-zone") == "availability-zone"]) < 2:
        raise ValueError("Deployment requires two available standard availability zones")
    cfn = session.client("cloudformation")
    for template in sorted((ROOT / "backend/deployment/templates").rglob("*.yaml")):
        cfn.validate_template(TemplateBody=template.read_text())
    stacks = {component: inspect_stack(cfn, target.stack(component), target)
              for component in ("foundation", "application", "backend")}
    # Only "managed" deploys the installation's own public-guidance stack, so D52 changes would miss it otherwise.
    guidance = target.stack("public-guidance")
    if installation.public_guidance != "managed" and inspect_stack(cfn, guidance, target):
        raise ValueError(f"{guidance} is this installation's public-guidance stack, but [knowledge] "
                         f'public_guidance = "{installation.public_guidance}" never updates it. '
                         'Set [knowledge] public_guidance = '
                         f'"managed" and public_knowledge_base_id = "" in {installation.path}, so each '
                         "release updates it (D52). No AWS changes made")
    require_environment(stacks["backend"], installation.environment)
    require_migrated(stacks)
    # Each repository keeps its newest images (D53); a push must not expire one a release still runs or rolls back to.
    require_retained_images(session, installation, stacks)
    if stacks["backend"]:
        existing = safe_outputs(stacks["backend"])
        if existing.get("RuntimeId"):
            session.client("bedrock-agentcore-control").get_agent_runtime(agentRuntimeId=existing["RuntimeId"])
    bootstrap = inspect_stack(cfn, BOOTSTRAP_STACK, target, allow_unmanaged=True) is None
    validate_arn(installation.provider_secret_arn, target, "secretsmanager")
    provider = json.loads(session.client("secretsmanager").get_secret_value(
        SecretId=installation.provider_secret_arn)["SecretString"])
    required = ("DTN_CLIENT_ID", "DTN_CLIENT_SECRET", "TOMTOM_API_KEY")
    if any(not isinstance(provider.get(key), str) or not provider[key].strip() for key in required):
        raise ValueError("Provider secret needs nonempty fields: "+", ".join(required))
    check_camera_key(installation, provider)
    bedrock = session.client("bedrock")
    for model in {installation.agent_model_id, installation.report_model_id}:
        if model.startswith(("global.", "us.", "eu.", "apac.")) or "inference-profile/" in model:
            bedrock.get_inference_profile(inferenceProfileIdentifier=model)
        else:
            bedrock.get_foundation_model(modelIdentifier=model)
    if installation.public_knowledge_base_id:
        session.client("bedrock-agent").get_knowledge_base(knowledgeBaseId=installation.public_knowledge_base_id)
    if installation.public_guidance == "managed":
        from deployment.deploy_public_sop import preflight as public_sop_preflight
        stacks["public-guidance"] = public_sop_preflight(session, target, root=ROOT)
    audio = dict(read_audio(session, source) for source in installation.recordings)
    for tool in ("node", "npm", "aws", "uv", "git"):
        if not shutil.which(tool):
            raise ValueError(f"Install {tool} before deployment")
    # The image scan gate runs only after the build, so its expired exceptions fail here instead.
    require_unexpired_image_exceptions(ROOT)
    # The offline checks packaging repeats later, so a partial, unlocked or non-git checkout fails here.
    digest = hashlib.sha256(source_bundle(ROOT)).hexdigest()
    runtime_source(ROOT)
    verify_contracts(ROOT)
    return Preflight(stacks, provider, audio, bootstrap, digest)


def check_camera_key(installation, provider):
    """Require [camera] device_id and the provider secret's DELPHIRE_API_KEY together; name the missing one.

    A key is absent when the field is missing, null or ""; any other value must be a usable key. Neither set is a
    release without a camera. No message contains a value.
    """
    key = provider.get("DELPHIRE_API_KEY")
    present = key not in (None, "")
    if installation.camera.enabled and not present:
        raise ValueError(f"[camera] device_id is set in {installation.path}, but the provider secret has no "
                         "DELPHIRE_API_KEY. Add DELPHIRE_API_KEY to the secret named by [providers] secret_arn, or "
                         'set [camera] device_id = "" to keep the camera off.')
    if present and not installation.camera.enabled:
        raise ValueError(f"The provider secret has DELPHIRE_API_KEY, but [camera] device_id is empty or missing in "
                         f"{installation.path}. Set [camera] device_id and camera_alias, or remove DELPHIRE_API_KEY "
                         "from the secret to keep the camera off.")
    if present and not camera_key_valid(key):
        raise ValueError(f"The provider secret's DELPHIRE_API_KEY must be {CAMERA_KEY_MIN_LENGTH}-"
                         f"{CAMERA_KEY_MAX_LENGTH} visible ASCII characters with no spaces.")


def interrupted_release(state, digest):
    """Return the newest unfinished release ID that can resume (built from source digest), else None."""
    folder = state / "releases"
    latest = json.loads((state / "latest.json").read_text())["release"] if (state / "latest.json").is_file() else ""
    candidates = sorted((p for p in folder.iterdir() if p.is_dir() and not p.is_symlink()
                         and RELEASE_ID.fullmatch(p.name) and p.name > latest), reverse=True) if folder.is_dir() else []
    if not candidates:
        return None
    newest = candidates[0]
    # A staged Runtime ZIP is not a build request. Preparation may be replayed on a stable stack. A release without
    # its captured backend (older deployers captured it only after the service rollout) cannot resume.
    if ((newest / "deployment.json").exists()
            or (newest / "backend-build-request.json").exists() and not resumable_early_build(newest)
            or not ((newest / "application-build-request.json").is_file() or resumable_early_build(newest))
            or not (newest / "identity.json").is_file()
            or not (newest / "backend-assets.zip").is_file()):
        return None
    return newest.name if json.loads((newest / "identity.json").read_text()).get("source_sha256") == digest else None


def configure_providers(session, lab, provider):
    """Copy the TomTom, FIRMS and Delphire camera keys into the application's provider secret; True if it changed.

    An absent optional key and "" are the same, so an installation without the camera writes nothing for it.
    """
    client = session.client("secretsmanager")
    arn = lab["ProviderSecretArn"]
    fields = {field: provider.get(source) or "" for field, source in PROVIDER_FIELDS}
    current = json.loads(client.get_secret_value(SecretId=arn)["SecretString"])
    if all(current.get(k, "") == v for k, v in fields.items()):
        return False
    client.put_secret_value(SecretId=arn, SecretString=json.dumps({**current, **fields}))
    return True


def fetch(url):
    """GET an HTTPS URL anonymously and return (status, body of at most 1 MiB) for any HTTP status."""
    if not url.startswith("https://"):
        raise ValueError("Workspace smoke test requires HTTPS")
    request = urllib.request.Request(url, headers={"User-Agent": "FireGUardDeployment/1"})
    try:
        # The stack's https:// workspace URL, scheme-checked above; no caller chooses a scheme or host.
        # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected
        with urllib.request.urlopen(request, timeout=30) as response:  # nosec B310 - https:// checked above
            return response.status, response.read(1024 * 1024)
    except urllib.error.HTTPError as error:
        try:
            return error.code, error.read(1024 * 1024)
        finally:
            error.close()


def smoke(url, staging=False, camera=False):
    """Check the UI and broker anonymously: demo sessions are served, and staging WAF may block this machine.

    camera is whether [camera] configures a device: the workspace must report the same camera_configured.
    """
    base = url.rstrip("/")
    status, page = fetch(base + "/")
    if status != 200:
        raise RuntimeError("Workspace check failed: /")
    if b"<html" not in page.lower():
        raise RuntimeError("Workspace did not return the UI")
    checks = {"https_ui": "passed", "anonymous_access": ACCESS["staging" if staging else "public"]}
    status, data = fetch(base + "/simulation-api/v1/workspace-config")
    # The static UI comes from S3; the API is behind WAF, which in staging blocks every network outside the
    # allowlist. Being blocked proves the gate from outside; only an allowlisted machine can check the broker.
    if staging and status == 403 and error_code(data) == STAGING_BLOCK_CODE:
        return {**checks, "broker_configuration": "not checked: WAF blocked this machine, which is outside "
                                                  "[access] staging_allowed_cidrs"}
    if status != 200:
        raise RuntimeError("Workspace check failed: /simulation-api/v1/workspace-config")
    config = json.loads(data)
    if (config.get("access_mode") != "public_demo" or not config.get("agent_configured")
            or not config.get("geocoder_configured")):
        raise RuntimeError("Workspace broker/agent binding is not ready")
    if bool(config.get("camera_configured")) is not camera:
        raise RuntimeError("Workspace camera status does not match [camera] device_id")
    return {**checks, "broker_configuration": "passed", "camera": "configured" if camera else "not configured"}


def resume_sources(state, target, release, provider):
    """Load an interrupted release's captured source, UI and backend after checking identity and integrity."""
    if not RELEASE_ID.fullmatch(release):
        raise ValueError("Resume requires the exact 20-digit release ID")
    folder = state / "releases" / release
    if folder.is_symlink() or not folder.resolve().is_relative_to((state / "releases").resolve()):
        raise ValueError("Release directory must belong to this installation")
    if ((folder / "backend-build-request.json").exists() and not resumable_early_build(folder)
            or (folder / "deployment.json").exists()):
        raise ValueError("This resume mode ends when the Runtime build starts")
    identity = json.loads((folder / "identity.json").read_text())
    if any(identity.get(k) != v for k, v in {
        "account": target.account, "region": target.region, "name": target.name, "image_tag": release,
    }.items()):
        raise ValueError("Captured release belongs to another installation")
    source = (folder / "source.zip").read_bytes()
    if hashlib.sha256(source).hexdigest() != identity["source_sha256"]:
        raise ValueError("Captured application source failed its integrity check")
    manifest = json.loads((folder / "cdk.out/manifest.json").read_text())
    application = manifest["artifacts"]["WildfireLabApplication"]
    if (application["environment"] != f"aws://{target.account}/{target.region}"
            or application["properties"]["stackName"] != target.stack("application")):
        raise ValueError("Captured cloud assembly belongs to another installation")
    with tempfile.TemporaryDirectory(prefix="wildfire-captured-ui-") as temporary:
        root = Path(temporary).resolve()
        with zipfile.ZipFile(folder / "ui-assets.zip") as bundle:
            for name in bundle.namelist():
                path = root / name
                if not path.resolve().is_relative_to(root) or name.endswith("/"):
                    raise ValueError("Invalid path in captured UI assets")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(bundle.read(name))
        secrets = {v for v in provider.values() if isinstance(v, str) and len(v) >= 12}
        files = verify_assets(root, secrets)
    backend = restore_backend((folder / "backend-assets.zip").read_bytes())
    staged = folder / "runtime-source.zip"
    if staged.exists() and staged.read_bytes() != backend["payload"]:
        raise ValueError("Staged Runtime source differs from captured backend")
    (folder / "resume-request.json").write_text(json.dumps({
        "requested_at": datetime.now(UTC).isoformat(), "release": release,
        "source_sha256": identity["source_sha256"], "rebuild_application": False,
        "prior_preparation": json.loads((folder / "backend-prepare-changes.json").read_text())
        if (folder / "backend-prepare-changes.json").exists() else None,
    }, indent=2))
    return folder, source, files, backend


def explicit_resume(state, installation, release):
    """Allow a coordinator fix without rebuilding images; reject application or configuration drift."""
    if not RELEASE_ID.fullmatch(release):
        raise ValueError("Resume requires the exact 20-digit release ID")
    latest = json.loads((state / "latest.json").read_text())["release"] if (state / "latest.json").exists() else ""
    if release <= latest:
        raise ValueError("Only an unfinished release newer than the accepted release can resume")
    folder = state / "releases" / release
    if ((folder / "backend-build-request.json").exists() and not resumable_early_build(folder)
            or (folder / "deployment.json").exists()):
        raise ValueError("This resume mode ends when the Runtime build starts")
    if not (folder / "backend-assets.zip").is_file():
        raise ValueError(f"Release {release} has no captured backend (backend-assets.zip): an older deployer captured "
                         "it only after the service rollout, so it cannot resume (W2.11, DEPL-4). Run bash "
                         "deployment/deploy-all.sh --apply without --resume-release to start a new release.")
    identity = json.loads((folder / "identity.json").read_text())
    if (identity["installation"] != installation.identity()
            or identity["settings"]["values"] != installation.settings):
        raise ValueError("Installation or settings changed since capture; use a new release")
    with zipfile.ZipFile(folder / "source.zip") as saved, zipfile.ZipFile(
            io.BytesIO(source_bundle(ROOT))) as current:
        coordinator = {"deployment/deploy_workspace.py", "deployment/deploy_backend.py"}
        if set(saved.namelist()) != set(current.namelist()) or any(
                saved.read(name) != current.read(name) for name in saved.namelist() if name not in coordinator):
            raise ValueError("Application source changed since capture; use a new release")
    if restore_backend((folder / "backend-assets.zip").read_bytes())["payload"] != runtime_source(ROOT):
        raise ValueError("Runtime source changed since capture; use a new release")
    return release


def capture_release(installation, stacks, provider, state, env, cdk):
    """Run the local release gates, then capture a new release: source, verified UI, backend and cloud assembly."""
    target = installation.target
    infra, ui = ROOT / "deployment/infra", ROOT / "ui"
    command(NPM_CI, ui, env)
    command(["npm", "audit", "--omit=dev", "--audit-level=high"], ui, env)
    command([sys.executable, "scripts/check_quality.py", "all"], ROOT, env)
    if stacks.get("backend") and installation.public_guidance != "none":
        command([sys.executable, "-m", "deployment.acceptance.check_guidance_model",
                 "--installation", installation.path,
                 "--output", state / "guidance-model-check.json"], ROOT, env)
    ui_env, known_secrets = build_environment(ui, env)
    # env lacks the caller's key variables (cdk_environment), so search the bundle for the caller's own values too.
    known_secrets |= secret_values(os.environ)
    known_secrets.update(v for v in provider.values() if isinstance(v, str) and len(v) >= 12)
    command(["npm", "run", "build", "--", "--mode", "public-demo"], ui, ui_env)
    files = verify_assets(ui / "dist", known_secrets)
    release = datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")
    release_dir = state / "releases" / release
    release_dir.mkdir(parents=True, exist_ok=False)
    browser_check(command, ui_env, release_dir)
    package_source(ROOT, destination=release_dir / "source.zip")
    source = (release_dir / "source.zip").read_bytes()
    digest = hashlib.sha256(source).hexdigest()
    # The release record: identity (no secrets), git commit and the effective settings with their sources.
    (release_dir / "identity.json").write_text(json.dumps({
        "account": target.account, "region": target.region, "name": target.name,
        "source_sha256": digest, "image_tag": release, "git": git_commit(ROOT),
        "installation": installation.identity(),
        "settings": {"values": installation.settings, "sources": installation.setting_sources},
        "stacks": {key: target.stack(key) for key in stacks}}, indent=2))
    (release_dir / "ui-assets.zip").write_bytes(archive(dict(files)))
    backend_sources = prepare_backend(ROOT)
    (release_dir / "backend-assets.zip").write_bytes(capture_backend(backend_sources))
    captured = Release(release, release_dir, source, digest, files, backend_sources, resumed=False)
    command([*cdk, "-c", f"imageTag={release}", "synth", "--quiet", "--output", captured.assembly], infra, env)
    return captured


def resume_release(state, target, release, provider):
    """Load an interrupted release's verified captured source, UI and backend to continue it without a rebuild."""
    print(f"Resuming interrupted release {release} from its captured verified assets.", flush=True)
    folder, source, files, backend = resume_sources(state, target, release, provider)
    return Release(folder.name, folder, source, hashlib.sha256(source).hexdigest(), files, backend, resumed=True)


def build_images(session, foundation, release):
    """Start the release's CodeBuild image build, or re-identify a resumed one; wait, then gate every image scan."""
    prior_request = release.folder / "application-build-request.json"
    legacy_resume = release.resumed and prior_request.is_file() and "build_id" in json.loads(prior_request.read_text())
    if "NativeBuildRepositoryUri" in foundation and not legacy_resume:
        return build_component_images(session, foundation, release)
    source_key = f"sources/{release.digest}.zip"
    builder = session.client("codebuild")
    if release.resumed and prior_request.is_file():
        request = json.loads((release.folder / "application-build-request.json").read_text())
        build_id = request["build_id"]
        build = builder.batch_get_builds(ids=[build_id])["builds"][0]
        if (request["source_key"] != source_key or build["projectName"] != foundation["BuildProject"]
                or build["source"]["location"] != f"{foundation['SourceBucket']}/{source_key}"
                or build["sourceVersion"] != request["source_version"]
                or not any(v["name"] == "IMAGE_TAG" and v["value"] == release.id
                           for v in build["environment"]["environmentVariables"])):
            raise ValueError("Captured build identity does not match this release")
    else:
        version = put_version(session.client("s3"), foundation["SourceBucket"], source_key, release.source)
        build_id = builder.start_build(
            projectName=foundation["BuildProject"], sourceVersion=version,
            sourceLocationOverride=f"{foundation['SourceBucket']}/{source_key}",
            environmentVariablesOverride=[{"name": "IMAGE_TAG", "value": release.id, "type": "PLAINTEXT"}],
        )["build"]["id"]
        (release.folder / "application-build-request.json").write_text(json.dumps({
            "build_id": build_id, "source_key": source_key, "source_version": version}, indent=2))
    print("Release records:", release.folder, flush=True)
    wait_build(builder, build_id, "Application build", stopped="application deployment stopped",
               timeout=APPLICATION_BUILD_SECONDS)
    images = {}
    for key, component in zip(IMAGE_OUTPUTS, ("api", "worker", "firetruck", "radio", "fire-risk"), strict=True):
        repo = foundation[key].split("/", 1)[1]
        images[key] = session.client("ecr").describe_images(repositoryName=repo,
            imageIds=[{"imageTag": release.id}])["imageDetails"][0]["imageDigest"]
        verify_image(session.client("ecr"), repo, images[key], component,
                     release.folder / f"scan-{component}.json", policy_path=release.folder / "image-policy.json")
    (release.folder / "images.json").write_text(json.dumps(images, indent=2))


def build_component_images(session, foundation, release):
    """Build at most two independent groups from pinned inputs; retain fresh scans for every reused digest."""
    source_key = f"sources/{release.digest}.zip"
    builder, ecr = session.client("codebuild"), session.client("ecr")
    request_path = release.folder / "application-build-request.json"
    if release.resumed and request_path.is_file():
        request = json.loads(request_path.read_text())
        plan = json.loads((release.folder / "component-images.json").read_text())
        if request["source_key"] != source_key or plan["source_sha256"] != release.digest:
            raise ValueError("Captured component build source differs from this release")
        selections = {group: [c for c in components if c in plan["build"]]
                      for group, components in GROUPS.items()}
        expected_groups = {g for g, selected in selections.items() if g == "platform" or selected}
        if not set(request["builds"]) <= expected_groups:
            raise ValueError("Captured component build groups differ from this release")
        for group, entry in request["builds"].items():
            if entry["components"] != selections[group]:
                raise ValueError("Captured component selection differs from this release")
            build = builder.batch_get_builds(ids=[entry["build_id"]])["builds"][0]
            actual = {v["name"]: v["value"] for v in build["environment"]["environmentVariables"]}
            expected = {"IMAGE_TAG": release.id, "BUILD_GROUP": group,
                        "BUILD_COMPONENTS": ",".join(entry["components"])}
            if (build["projectName"] != foundation["BuildProject"]
                    or build["source"]["location"] != f"{foundation['SourceBucket']}/{source_key}"
                    or build["sourceVersion"] != request["source_version"]
                    or any(actual.get(k) != v for k, v in expected.items())
                    or actual.get("SUMO_VERIFY_IMAGE", "") != entry.get("sumo_verify_image", "")):
                raise ValueError("Captured component build identity differs from this release")
    else:
        plan = plan_images(ecr, foundation, release, builder=builder)
        version = put_version(session.client("s3"), foundation["SourceBucket"], source_key, release.source)
        request = {"source_key": source_key, "source_version": version, "builds": {}}
        request_path.write_text(json.dumps(request, indent=2))
    for group, components in GROUPS.items():
        selected = [c for c in components if c in plan["build"]]
        # Infrastructure verification always runs. Resume starts only a group whose original start failed.
        if group in request["builds"] or group == "services" and not selected:
            continue
        environment = {"IMAGE_TAG": release.id, "BUILD_GROUP": group, "BUILD_COMPONENTS": ",".join(selected)}
        if "api" in selected and "worker" not in selected:
            environment["SUMO_VERIFY_IMAGE"] = (
                foundation[OUTPUTS["worker"]] + "@" + plan["reused"]["worker"]["digest"])
        build = builder.start_build(
            projectName=foundation["BuildProject"], sourceVersion=request["source_version"],
            sourceLocationOverride=f"{foundation['SourceBucket']}/{source_key}",
            environmentVariablesOverride=[{"name": k, "value": v, "type": "PLAINTEXT"}
                                          for k, v in environment.items()])["build"]
        request["builds"][group] = {"build_id": build["id"], "components": selected,
                                  "sumo_verify_image": environment.get("SUMO_VERIFY_IMAGE", "")}
        request_path.write_text(json.dumps(request, indent=2))
    print("Release records:", release.folder, flush=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        waits = [pool.submit(wait_build, builder, entry["build_id"], f"Application {group} build",
                             stopped="application deployment stopped", timeout=APPLICATION_BUILD_SECONDS)
                 for group, entry in request["builds"].items()]
        for future in waits:
            future.result()
    images = {}
    verification = {}
    recipe = build_recipe(release.folder)
    for component, key in OUTPUTS.items():
        repository = foundation[key].split("/", 1)[1]
        digest = ecr.describe_images(repositoryName=repository,
                                     imageIds=[{"imageTag": release.id}])["imageDetails"][0]["imageDigest"]
        scan_not_before = None
        if component in plan["reused"]:
            if digest != plan["reused"][component]["digest"]:
                raise ValueError("Reused release tag no longer names its verified digest")
            scan_not_before = refresh_image_scan(ecr, repository, digest)
            verification[component] = plan["reused"][component]["build_proof"]
        else:
            group = next(g for g, components in GROUPS.items() if component in components)
            verification[component] = {
                "build_id": request["builds"][group]["build_id"], "project": foundation["BuildProject"],
                "source_key": source_key, "source_version": request["source_version"],
                "source_bucket": foundation["SourceBucket"], "image_tag": release.id,
                "group": group, "components": request["builds"][group]["components"], "digest": digest,
                "recipe": recipe,
            }
        verify_image(ecr, repository, digest, component, release.folder / f"scan-{component}.json",
                     policy_path=release.folder / "image-policy.json", scan_not_before=scan_not_before)
        images[key] = digest
    (release.folder / "images.json").write_text(json.dumps(images, indent=2))
    (release.folder / "component-verification.json").write_text(json.dumps(verification, indent=2))


def foundation_parameters(installation, stacks):
    """Return the foundation stack's parameters: the operator roles its bucket policies admit (D52) and the site bucket
    whose object writes its trail records (D53), from the application stack's output; "" before that stack exists."""
    return {**operator_parameters(installation),
            "WorkspaceAssetsBucketName": safe_outputs(stacks.get("application") or {}).get("WorkspaceAssetsBucket", "")}


def application_parameters(installation, foundation, audio, previous):
    """Return the application stack's services-pass parameters, keeping the prior Runtime binding and public mode."""
    return {
        "ImageRepositoryArn": foundation["RepositoryArn"],
        "TrafficSimulatorRepositoryArn": foundation["TrafficSimulatorRepositoryArn"],
        "FiretruckRepositoryArn": foundation["FiretruckRepositoryArn"],
        "RadioRepositoryArn": foundation["RadioRepositoryArn"],
        "FireRiskRepositoryArn": foundation["FireRiskRepositoryArn"],
        "RadioModelId": installation.agent_model_id, "RadioAudioKeys": ",".join(audio),
        "PublicDemoMode": previous.get("PublicDemoMode", "false"),
        "SimulationRuntimeArn": previous.get("SimulationRuntimeArn", ""),
        "GeocoderFunctionArn": previous.get("GeocoderFunctionArn", ""),
        "WeatherFunctionArn": previous.get("WeatherFunctionArn", ""),
        # Both passes apply the staging allowlist, so staging never opens beyond its networks (D20).
        **access_parameters(installation),
        # Every deploy applies exactly the effective deployment/settings.toml values (D23).
        **stack_parameters(installation),
        # The optional [camera], "" without it. In a release that turns the camera on, the services pass's API tasks
        # have the device ID before configure_providers copies the key, so they report it as not configured.
        **camera_parameters(installation),
        # Every pass keeps the operator roles in the bucket policies (D52).
        **operator_parameters(installation),
    }


def deploy_candidate(session, installation, release, deploy_stack, *, application, foundation, provider, audio,
                     acknowledged=(), early_runtime_build=None):
    """Roll out the services, the backend, then the application with the new Runtime on its inactive endpoint.

    application is the existing application stack or None. Public mode and the API's Runtime binding keep their
    prior values; promote() changes them. acknowledged reaches the backend's change sets.
    """
    # Keep the prior Runtime/public mode on upgrades; initial deployment stays closed until the bind pass.
    previous = parameter_values(application)
    parameters = application_parameters(installation, foundation, audio, previous)
    control = session.client("bedrock-agentcore-control")
    parameters.update(initial_parameters(control, parameters["SimulationRuntimeArn"], previous))
    lab = deploy_stack("application", parameters, "services")
    prior_arn = parameters["SimulationRuntimeArn"]
    prior_endpoint = parameters["SimulationRuntimeEndpoint"]
    prior_version = parameters[f"RuntimeEndpoint{prior_endpoint[-1]}Version"]
    if prior_arn:
        verify_endpoint(control, prior_arn, prior_endpoint, prior_version)
        require_api_binding(session.client("ecs"), lab, prior_endpoint, prior_version)
    for key, data in audio.items():
        put_version(session.client("s3"), lab["RadioAudioBucket"], key, data)
    providers_changed = configure_providers(session, lab, provider)
    backend = deploy_backend(session, installation.target, ROOT, foundation["SourceBucket"],
                             lab, provider, installation, release.folder, captured=release.backend,
                             acknowledged=acknowledged, early_runtime_build=early_runtime_build)
    endpoint = ("RELEASE_B" if prior_endpoint == "RELEASE_A" else "RELEASE_A") if prior_arn else "RELEASE_A"
    if prior_arn and backend["RuntimeArn"] != prior_arn:
        raise RuntimeError("Runtime replacement requires a deliberate endpoint migration")
    # The active endpoint/version stays fixed while the inactive native resource is updated.
    parameters.update(SimulationRuntimeArn=backend["RuntimeArn"], GeocoderFunctionArn=backend["GeocodeServiceArn"],
                      WeatherFunctionArn=backend["WeatherDTNServiceArn"])
    parameters[f"RuntimeEndpoint{endpoint[-1]}Version"] = backend["RuntimeVersion"]
    if not prior_arn:
        parameters["RuntimeEndpointBVersion"] = backend["RuntimeVersion"]
    lab = deploy_stack("application", parameters, "candidate")
    binding = verify_endpoint(control, backend["RuntimeArn"], endpoint, backend["RuntimeVersion"])
    if prior_arn:
        verify_endpoint(control, prior_arn, prior_endpoint, prior_version)
        require_api_binding(session.client("ecs"), lab, prior_endpoint, prior_version)
    candidate_check(session, backend["RuntimeArn"], endpoint, backend["RuntimeVersion"],
                    release.id, release.folder)
    return Candidate(parameters, previous, lab, backend, prior_arn,
                     {"endpoint": prior_endpoint, "version": prior_version}, binding, providers_changed)


def promote(session, candidate, release, deploy_stack, env, *, camera):
    """Open public mode on the candidate Runtime binding and accept it live; on failure restore the prior binding.

    camera is whether [camera] configures a device: the live checks expect the same camera state.
    """
    parameters, prior, binding = candidate.parameters, candidate.prior, candidate.binding
    lab = candidate.lab
    cutover = CutoverAcceptance(lab["IntegratedWorkspaceUrl"], release.folder) if candidate.prior_arn else None
    functional = {}
    try:
        # Create the old execution session immediately before promotion, so natural VM expiry cannot
        # make the continuity gate pass accidentally after the long container build.
        if cutover:
            cutover.seed(prior)
        parameters.update(PublicDemoMode="true", SimulationRuntimeEndpoint=binding["endpoint"])
        lab = deploy_stack("application", parameters, "bind")
        require_api_binding(session.client("ecs"), lab, binding["endpoint"], binding["version"])
        if cutover:
            functional["cutover"] = cutover.finish(binding, release.id)
        functional.update(live_checks(command, env, release.folder, lab["IntegratedWorkspaceUrl"], camera=camera))
    except Exception:
        if cutover:
            cutover.report["status"] = "failed"
            cutover.save()
        if candidate.prior_arn:
            # Runtime rollback changes the binding only. Candidate assets remain available for inspection;
            # the prior endpoint, source receipts and Memory remain intact.
            parameters.update(SimulationRuntimeEndpoint=prior["endpoint"],
                              PublicDemoMode=candidate.previous.get("PublicDemoMode", "false"))
            lab = deploy_stack("application", parameters, "rollback-binding")
            restored = require_api_binding(session.client("ecs"), lab, prior["endpoint"], prior["version"])
            verify_endpoint(session.client("bedrock-agentcore-control"), candidate.prior_arn,
                            prior["endpoint"], prior["version"])
            (release.folder / "runtime-binding-rollback.json").write_text(json.dumps({
                "status": "passed", **restored, "scope": "Runtime binding only; other services were not rolled back"},
                indent=2))
        raise
    finally:
        if cutover:
            cutover.close()
    return lab, functional


def publish_release(session, installation, state, release, lab, functional, *, candidate, public_guidance,
                    change_sets):
    """Restart services on changed provider keys, publish the UI, then record and smoke check the release."""
    target = installation.target
    if candidate.providers_changed:
        ecs = session.client("ecs")
        services = [lab["ApiServiceName"], lab["WorkerServiceName"]]
        for service in services:
            ecs.update_service(cluster=lab["ClusterName"], service=service, forceNewDeployment=True)
        ecs.get_waiter("services_stable").wait(cluster=lab["ClusterName"], services=services,
            WaiterConfig={"Delay": 15, "MaxAttempts": 80})
    publication = publish(session, lab, release.files)
    # A failed smoke check leaves an explicit unfinished receipt, not a false success.
    deployed = {"account": target.account, "region": target.region, "name": target.name,
                "image_tag": release.id, "source_sha256": release.digest, "ui_sha256": publication,
                "url": lab["IntegratedWorkspaceUrl"], "lab": lab, "backend": candidate.backend,
                "public_guidance": public_guidance, "environment": installation.environment,
                "settings": {"values": installation.settings, "sources": installation.setting_sources},
                "change_sets": [{key: summary[key] for key in ("stack", "change_set", "result")}
                                for summary in change_sets],
                "anonymous_access": ACCESS[installation.access_mode]}
    (release.folder / "deployment.json").write_text(json.dumps(deployed, indent=2))
    checks = {**smoke(deployed["url"], installation.stage_only, camera=installation.camera.enabled),
              "functional_acceptance": "passed", "workflows": functional}
    (release.folder / "acceptance.json").write_text(json.dumps(checks, indent=2))
    (state / "latest.json").write_text(json.dumps({"release": release.id, **deployed}, indent=2))
    print("Deployed:", deployed["url"])
    if installation.stage_only:
        print("Staging checks passed. Only the networks in [access] staging_allowed_cidrs can use the workspace; "
              "WAF blocks every other address. After review, set [access] mode = \"public\" in the installation "
              "file and rerun with --apply to open it to everyone.")
    else:
        print("Local quality/browser, image scan policy and live radio, investigation/SUMO, response/report checks passed.")
    print("Manual post-deploy checks: deployment/acceptance/check_public_sop_browser.cjs (architecture/28), "
          "check_prepared_simulation_browser.cjs (architecture/32), and check_gateway_secret verify (architecture/29).")


def deploy(session, installation, stacks, provider, audio, state, *, bootstrap=False, resume=None, acknowledged=()):
    """Build or resume a release, deploy every stack through guarded change sets, bind, publish and smoke check.

    acknowledged holds the operator's change_sets.Acknowledgments; every guarded change set receives them.
    """
    target = installation.target
    env = cdk_environment(target, installation.profile)
    infra = ROOT / "deployment/infra"
    command(NPM_CI, infra, env)
    cdk = [infra / "node_modules/.bin/cdk", "-c", f"deploymentName={target.name}"]
    release = (resume_release(state, target, resume, provider) if resume
               else capture_release(installation, stacks, provider, state, env, cdk))
    # Use the policy captured with these sources, including when resuming an interrupted build.
    with zipfile.ZipFile(io.BytesIO(release.source)) as bundle:
        (release.folder / "image-policy.json").write_bytes(bundle.read(POLICY))
    if bootstrap:
        print(f"The CDK bootstrap stack {BOOTSTRAP_STACK} is missing in {target.account}/{target.region}; "
              "bootstrapping it now.", flush=True)
        command([*cdk, "bootstrap", f"aws://{target.account}/{target.region}"], infra, env)
    # All deployment phases use this captured cloud assembly, not later checkout edits.
    captured_cdk = [infra / "node_modules/.bin/cdk", "--app", release.assembly]
    cfn = session.client("cloudformation")
    change_sets = []

    def run(args):
        # The checkout's npm and cdk, in its CDK app, with the release environment.
        command(args, infra, env)

    def deploy_stack(component, parameters, phase):
        # Recheck ownership and status immediately before every update, including the second application pass.
        created = inspect_stack(cfn, target.stack(component), target) is None
        name = f"release-{release.id}-{component}-{phase}-{datetime.now(UTC).strftime('%H%M%S')}"
        change_sets.append(deploy_guarded(
            cfn, target.stack(component), name, run=run, cdk=captured_cdk, construct="WildfireLab"+component.title(),
            created=created, receipt=release.folder / f"{component}-{phase}-changes.json", parameters=parameters,
            acknowledged=acknowledged))
        return safe_outputs(cfn.describe_stacks(StackName=target.stack(component))["Stacks"][0])

    foundation = (safe_outputs(stacks["foundation"]) if release.resumed else
                  deploy_stack("foundation", foundation_parameters(installation, stacks), "release"))
    public_guidance = None
    if installation.public_guidance == "managed":
        from deployment.deploy_public_sop import deploy_public_sop
        public_guidance = (safe_outputs(stacks["public-guidance"]) if release.resumed else
                           deploy_public_sop(session, target, release.folder, root=ROOT, run=run,
                                             parameters=operator_parameters(installation), install=False,
                                             acknowledged=acknowledged))
        installation = replace(installation, public_knowledge_base_id=public_guidance["PublicKnowledgeBaseId"])
    early_runtime = ((resume_runtime_build if release.resumed else start_runtime_build)(
        session, target, foundation["SourceBucket"], installation, release.folder, release.backend))
    build_images(session, foundation, release)
    candidate = deploy_candidate(session, installation, release, deploy_stack, application=stacks["application"],
                                 foundation=foundation, provider=provider, audio=audio, acknowledged=acknowledged,
                                 early_runtime_build=early_runtime)
    lab, functional = promote(session, candidate, release, deploy_stack, env, camera=installation.camera.enabled)
    publish_release(session, installation, state, release, lab, functional, candidate=candidate,
                    public_guidance=public_guidance, change_sets=change_sets)
    # After publication (D53): the release is accepted and recorded, the active endpoint's newest group exists, and
    # a failed retention call is recorded for the next release instead of stopping this one. deploy_backend returns
    # its outputs only after reading the Runtime, so RuntimeId is always there.
    components = ("foundation", "application", "backend") + (
        ("public-guidance",) if installation.public_guidance == "managed" else ())
    retain_installation_logs(session, [target.stack(c) for c in components], candidate.backend["RuntimeId"],
                             release.folder)


def plan(installation, checked, resume, acknowledged=()):
    """Describe the installation, its non-default settings and what --apply will do; no AWS writes."""
    target, stacks = installation.target, checked.stacks
    guidance = {"managed": "provision, ingest, verify and bind the reviewed public guidance collection",
                "existing": f"bind the existing public knowledge base {installation.public_knowledge_base_id}",
                "none": "leave public guidance retrieval unavailable"}[installation.public_guidance]
    steps = [f"bootstrap CDK in {target.account}/{target.region} ({BOOTSTRAP_STACK} is missing)"] if checked.bootstrap else []
    steps += [f"resume interrupted release {resume}, built from this exact source" if resume else
              f"build a new release from source sha256 {checked.digest[:16]} and its service images in CodeBuild",
              ("deploy the foundation, application and backend stacks through CloudFormation change sets, stopping "
               "before any change set that would replace or delete a stateful resource"),
              *([f"accept these reviewed changes: {', '.join(map(str, acknowledged))}"] if acknowledged else []),
              guidance,
              f"copy {len(checked.audio)} radio recording(s), bind the Runtime, publish the UI and run the smoke check",
              ("admit only the [access] staging_allowed_cidrs networks through the WAF allowlist (staging)"
               if installation.stage_only else "enable anonymous public access"),
              (f"after publication, set a {LOG_RETENTION_DAYS}-day retention on the installation's log groups that "
               "keep logs forever")]
    camera = installation.camera
    return {"installation": installation.identity(),
            "settings_changed": changed_settings(installation, ROOT) or "none: every value is the settings.toml value",
            "stacks": {k: {"name": target.stack(k), "action": "update" if v else "create"} for k, v in stacks.items()},
            "camera": (f"Delphire device {camera.device_id}, {camera.alias}, through the broker" if camera.enabled
                       else "off: [camera] device_id is empty"),
            "audio_objects": list(checked.audio), "apply_will": steps, "cleanup": "never performed by deployment"}


def main(argv=None):
    """Check and plan one installation from its installation file; with --apply, deploy or update it."""
    options = arguments(argv)
    acknowledged = tuple(parse_acknowledgment(text) for text in options.acknowledge)
    if acknowledged and not options.apply:
        raise SystemExit("--acknowledge approves reviewed changes for a deployment; use it with --apply")
    installation = load_installation(options.installation, root=ROOT)
    target = installation.target
    session = boto3.Session(profile_name=installation.profile, region_name=target.region)
    state = ROOT / ".runtime/deployments" / target.account / target.region / target.name
    state.mkdir(parents=True, exist_ok=True)
    with (state / "deployment.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Another local deployment owns this account/region/name") from None
        checked = preflight(session, installation)
        resume = (explicit_resume(state, installation, options.resume_release) if options.resume_release
                  else interrupted_release(state, checked.digest))
        planned = plan(installation, checked, resume, acknowledged)
        (state / "plan.json").write_text(json.dumps(planned, indent=2))
        print(json.dumps(planned, indent=2))
        if not options.apply:
            again = f" {options.installation}" if options.installation else ""
            chosen = f"--installation {options.installation} " if options.installation else ""
            print(f"Check passed. Run bash deployment/deploy-all.sh{again} --apply to carry out this plan.")
            print("Before --apply on a deployed backend, capture the Gateway client secret baseline (architecture/29): "
                  f".venv/bin/python -m deployment.acceptance.check_gateway_secret {chosen}capture "
                  "--output .runtime/gateway-secret-baseline.json")
            return
        deploy(session, installation, checked.stacks, checked.provider, checked.audio, state,
               bootstrap=checked.bootstrap, resume=resume, acknowledged=acknowledged)


if __name__ == "__main__":
    try:
        main()
    except (CredentialRetrievalError, NoCredentialsError, ProfileNotFound):
        raise SystemExit("AWS profile credentials are unavailable. Refresh the selected profile's login/device "
                         "verification, confirm aws sts get-caller-identity, then retry. No keys belong in the UI.") from None
    except Exception as exc:  # noqa: BLE001 — sanitize SDK failures at the CLI boundary
        # SDK exceptions can include request parameters. Never echo secret-bearing payloads.
        if isinstance(exc, (ValueError, RuntimeError, TimeoutError)):
            raise SystemExit(str(exc)) from None
        raise SystemExit(f"Deployment stopped ({type(exc).__name__}); inspect the scoped release records and AWS events.") from None
