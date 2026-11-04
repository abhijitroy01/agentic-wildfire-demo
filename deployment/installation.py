"""Load and validate an installation file and deployment/settings.toml before any AWS call (decisions D23, D24).

deployment/installation.toml (git-ignored; copy deployment/installation.example.toml) holds one installation's
identity and choices. deployment/settings.toml (checked in) holds the shared operational settings; an
installation may override them in its optional [settings] table. Running this module rewrites the generated
settings table in architecture/29-workspace-operations.md; --check fails when that table is stale.
"""
import argparse
import ipaddress
import os
import re
import subprocess  # nosec B404 - refuse_tracked() runs only one fixed git argument list, never a shell
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from deployment import ROOT
from deployment.deploy_backend import ENVIRONMENTS
from deployment.deployment_target import Target, validate_arn
from deployment.package_source import GIT_LOCATION

SETTINGS_FILE = Path("deployment/settings.toml")
DEFAULT_INSTALLATION = Path("deployment/installation.toml")
EXAMPLE = Path("deployment/installation.example.toml")
DOCS = Path("architecture/29-workspace-operations.md")
README = "deployment/README.md"
TABLE_START = ("<!-- settings-table:start (generated from deployment/settings.toml by "
               ".venv/bin/python -m deployment.installation; do not edit) -->")
TABLE_END = "<!-- settings-table:end -->"
# Allowed keys of each installation-file section; True marks a required key. operator_role_arns is required too,
# with its own message (load_installation).
SCHEMA = {
    "aws": {"profile": True, "account_id": True, "region": True, "name": True, "environment": True,
            "operator_role_arns": False},
    "access": {"mode": True, "staging_allowed_cidrs": False},
    "models": {"agent_model_id": True, "report_model_id": True},
    "radio": {"recordings": True},
    "knowledge": {"public_guidance": True, "public_knowledge_base_id": False},
    "providers": {"secret_arn": True},
}
# Sections an installation file may leave out; when one is present, its keys follow the same rule as SCHEMA.
OPTIONAL_SCHEMA = {"camera": {"api_base_url": True, "device_id": True, "camera_alias": True}}
# The optional Delphire camera, one rule set for three validators: the same rule text is in
# backend/api_settings.py and deployment/infra/lib/stacks.ts, and
# deployment/tests/test_installation.py keeps the three equal. Protocol bounds, not operational settings (D38).
# Lower-case DNS labels; the last label starts with a letter, so an IPv4 host never matches.
CAMERA_ORIGIN = r"https://([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]([a-z0-9-]{0,61}[a-z0-9])?"
# "https://" (8 characters) plus a host of at most 253.
CAMERA_ORIGIN_MAX_LENGTH = 261
CAMERA_DEVICE_ID = r"[A-Za-z0-9_-]{1,64}"
# The camera aliases of Delphire's latest-records answer (owner brief 2026-10-14).
CAMERA_ALIASES = ("camera_a", "camera_b", "infrared_camera_a", "infrared_camera_b", "test_camera_a")
# A key is visible ASCII (0x21-0x7E) only, so it is always a valid Authorization header value.
CAMERA_KEY_MIN_LENGTH = 16
CAMERA_KEY_MAX_LENGTH = 8192
# [aws] operator_role_arns (D52): the same pattern text is OPERATOR_ROLE_ARN in deployment/infra/lib/bucket-access.ts
# and the OperatorRoleArns AllowedPattern of the backend templates; deployment/tests/test_installation.py keeps the
# three equal. Whole path segments, then a 1-64 character role name (IAM's RoleName limit), so a value that passes
# can never fail iam:GetRole's parameter validation. The deployer supports only the aws partition.
OPERATOR_ROLE_ARN = r"arn:aws:iam::[0-9]{12}:role/(?:[A-Za-z0-9+=.@_-]+/)*[A-Za-z0-9+=.@_-]{1,64}"
# A protocol bound (D38): each bucket policy lists every operator, far below S3's 20 KB policy limit.
MAX_OPERATOR_ROLES = 10
ACCESS_MODES = ("public", "staging")
PUBLIC_GUIDANCE = ("managed", "existing", "none")
MAX_RECORDINGS = 10
# Each option the deployer used to take, and what replaces it; architecture/29 lists the same mapping. Pairs, not
# a dict literal, so the deployment-value scanner does not read "--account": "..." as a hardcoded account.
REMOVED_OPTION_PAIRS = (
    ("--profile", "set [aws] profile in the installation file"),
    ("--account", "set [aws] account_id in the installation file"),
    ("--region", "set [aws] region in the installation file"),
    ("--name", "set [aws] name in the installation file"),
    ("--environment", "set [aws] environment in the installation file"),
    ("--stage-only", 'set [access] mode = "staging" in the installation file'),
    ("--model-id", "set [models] agent_model_id in the installation file"),
    ("--report-model-id", "set [models] report_model_id in the installation file"),
    ("--radio-audio", "list the files in [radio] recordings in the installation file"),
    ("--knowledge-base-id", "nothing replaces it; the Runtime has no private knowledge-base retrieval (W2.11)"),
    ("--with-public-sop", 'set [knowledge] public_guidance = "managed" in the installation file'),
    ("--public-knowledge-base-id",
     'set [knowledge] public_guidance = "existing" and public_knowledge_base_id in the installation file'),
    ("--provider-secret-arn", "set [providers] secret_arn in the installation file"),
    ("--bootstrap", "--apply bootstraps CDK automatically when the Region's bootstrap stack is missing"),
)
REMOVED_OPTIONS = dict(REMOVED_OPTION_PAIRS)
SETTING_FIELDS = {"value", "unit", "min", "max", "allowed", "description"}
NAME = re.compile(r"[a-z][a-z0-9_]*[a-z0-9]")
# Fargate (Linux) task sizes: vCPU -> allowed memory in GiB. Checked for every <task>_vcpu/<task>_memory_gib pair.
FARGATE_MEMORY_GIB = {0.25: (0.5, 1, 2), 0.5: range(1, 5), 1: range(2, 9), 2: range(4, 17), 4: range(8, 31),
                      8: range(16, 61, 4), 16: range(32, 121, 8)}


class InstallationError(ValueError):
    """An installation file or settings.toml value is missing, unknown or invalid; nothing was sent to AWS."""


@dataclass(frozen=True)
class Setting:
    """One operational setting from deployment/settings.toml: default, unit, allowed range or values, description."""
    section: str
    key: str
    value: object
    unit: str
    description: str
    minimum: float | None = None
    maximum: float | None = None
    allowed: tuple = ()

    @property
    def name(self):
        """Full setting name as written in settings.toml: section.key."""
        return f"{self.section}.{self.key}"

    @property
    def parameter(self):
        """CloudFormation parameter name of this setting: the key in CamelCase."""
        return parameter_name(self.key)

    @property
    def environment_variable(self):
        """Container environment variable of this setting: the key in upper case."""
        return self.key.upper()


@dataclass(frozen=True)
class Camera:
    """The optional [camera] section: the Delphire API origin, device and camera alias; no device ID means off."""
    api_base_url: str = ""
    device_id: str = ""
    alias: str = ""

    @property
    def enabled(self):
        """True when [camera] device_id names a device: the broker serves it once the provider key is copied."""
        return bool(self.device_id)


@dataclass(frozen=True)
class Installation:
    """One installation's identity and choices from its installation file, with its effective settings."""
    path: Path
    target: Target
    profile: str
    environment: str
    access_mode: str
    staging_allowed_cidrs: tuple[str, ...]
    agent_model_id: str
    report_model_id: str
    recordings: tuple[str, ...]
    public_guidance: str
    public_knowledge_base_id: str
    provider_secret_arn: str
    settings: dict = field(default_factory=dict)
    setting_sources: dict = field(default_factory=dict)
    camera: Camera = field(default_factory=Camera)
    # [aws] operator_role_arns: the only principals besides the installation's own roles and AWS services that the
    # bucket policies let read or write objects (D52). load_installation always sets them; preflight refuses none.
    operator_role_arns: tuple[str, ...] = ()

    @property
    def stage_only(self):
        """True when [access] mode is staging: WAF admits only the staging_allowed_cidrs networks."""
        return self.access_mode == "staging"

    def identity(self):
        """Describe the installation for the plan and the release record; the files hold no secrets."""
        return {"file": str(self.path), "account": self.target.account, "region": self.target.region,
                "name": self.target.name, "environment": self.environment, "profile": self.profile,
                "operator_role_arns": list(self.operator_role_arns),
                "access": {"mode": self.access_mode, "staging_allowed_cidrs": list(self.staging_allowed_cidrs)},
                "models": {"agent": self.agent_model_id, "report": self.report_model_id},
                "knowledge": {"public_guidance": self.public_guidance,
                              "public_knowledge_base_id": self.public_knowledge_base_id},
                "recordings": len(self.recordings), "provider_secret_arn": self.provider_secret_arn,
                "camera": {"api_base_url": self.camera.api_base_url, "device_id": self.camera.device_id,
                           "camera_alias": self.camera.alias}}


def parameter_name(key):
    """Turn a settings.toml key into its CloudFormation parameter name (requests_per_ip -> RequestsPerIp)."""
    return "".join(part[:1].upper() + part[1:] for part in key.split("_"))


def parameter_value(value):
    """Render a setting value as a CloudFormation parameter string (lists become comma-separated)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return ",".join(value)
    return str(value)


def shown(path, root):
    """Show path relative to root when it is inside the checkout."""
    try:
        return str(path.resolve().relative_to(root))
    except ValueError:
        return str(path)


def read_toml(path, root):
    """Parse one TOML file, naming it in any syntax error."""
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise InstallationError(f"{shown(path, root)} is not valid TOML: {exc}") from None


def kind(value):
    """Name the settings type of a TOML value, or None when settings.toml does not support it."""
    if isinstance(value, bool):
        return "a boolean"
    if isinstance(value, int):
        return "an integer"
    if isinstance(value, float):
        return "a number"
    if isinstance(value, str):
        return "a string"
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return "a list of strings"
    return None


def check_value(setting, value, where):
    """Validate one setting value against its type and range from settings.toml; return it as the stored type."""
    expected, actual = kind(setting.value), kind(value)
    if actual != expected and not (expected == "a number" and actual == "an integer"):
        raise InstallationError(f"{where}: {setting.name} must be {expected}, not {value!r}")
    if setting.minimum is not None and not setting.minimum <= value <= setting.maximum:
        raise InstallationError(f"{where}: {setting.name} = {value!r} is outside the allowed range "
                                f"{setting.minimum}-{setting.maximum} {setting.unit}")
    items = value if isinstance(value, list) else [value]
    if setting.allowed and any(item not in setting.allowed for item in items):
        raise InstallationError(f"{where}: {setting.name} = {value!r} is not one of {', '.join(setting.allowed)}")
    return float(value) if expected == "a number" else value


def load_settings(root=ROOT):
    """Read deployment/settings.toml into {key: Setting}; reject malformed entries, duplicate keys and bad defaults."""
    where = str(SETTINGS_FILE)
    settings = {}
    for section, entries in read_toml(root / SETTINGS_FILE, root).items():
        if not isinstance(entries, dict) or not NAME.fullmatch(section):
            raise InstallationError(f"{where}: [{section}] must be a lower_snake_case section of settings")
        for key, spec in entries.items():
            name = f"{section}.{key}"
            if not isinstance(spec, dict) or not NAME.fullmatch(key):
                raise InstallationError(f"{where}: {name} must be a lower_snake_case table with value, unit and "
                                        "description")
            unknown = sorted(set(spec) - SETTING_FIELDS)
            missing = sorted({"value", "unit", "description"} - set(spec))
            if unknown or missing:
                raise InstallationError(f"{where}: {name} " + (f"has unknown fields {', '.join(unknown)}" if unknown
                                        else f"is missing {', '.join(missing)}"))
            if key in settings:
                raise InstallationError(f"{where}: {key} appears in [{settings[key].section}] and [{section}]; "
                                        "keys must be unique because they name parameters and variables")
            value_kind = kind(spec["value"])
            numeric = value_kind in ("an integer", "a number")
            if value_kind is None:
                raise InstallationError(f"{where}: {name} value must be a number, string, boolean or list of strings")
            if numeric != ("min" in spec and "max" in spec) or (numeric and "allowed" in spec):
                raise InstallationError(f"{where}: {name} needs min and max when it is a number, and only then")
            if value_kind == "a string" and not spec.get("allowed"):
                raise InstallationError(f"{where}: {name} needs allowed values")
            if not all(isinstance(spec.get(k, 0), int | float) and not isinstance(spec.get(k), bool)
                       for k in ("min", "max")) or (numeric and spec["min"] > spec["max"]):
                raise InstallationError(f"{where}: {name} min and max must be numbers with min <= max")
            if not isinstance(spec.get("allowed", []), list) or not all(
                    isinstance(item, str) for item in spec.get("allowed", [])):
                raise InstallationError(f"{where}: {name} allowed must be a list of strings")
            if not all(isinstance(spec[k], str) and spec[k].strip() for k in ("unit", "description")):
                raise InstallationError(f"{where}: {name} unit and description must be nonempty strings")
            setting = Setting(section, key, spec["value"], spec["unit"], spec["description"],
                              spec.get("min"), spec.get("max"), tuple(spec.get("allowed", ())))
            check_value(setting, spec["value"], where)
            settings[key] = setting
    return settings


def fargate_memory_choices(vcpu):
    """Describe the memory sizes Fargate allows with a vCPU count, for example "8-30 GiB"; None if no such size."""
    allowed = FARGATE_MEMORY_GIB.get(vcpu)
    if allowed is None:
        return None
    if not isinstance(allowed, range):
        return ", ".join(f"{size:g}" for size in allowed) + " GiB"
    steps = f" in {allowed.step} GiB steps" if allowed.step > 1 else ""
    return f"{allowed.start}-{allowed[-1]} GiB{steps}"


def check_task_sizes(values, where):
    """Reject a <task>_vcpu and <task>_memory_gib pair that Fargate cannot run, naming the sizes it can."""
    for key, vcpu in values.items():
        memory_key = key.removesuffix("_vcpu") + "_memory_gib"
        if key.endswith("_vcpu") and memory_key in values and values[memory_key] not in FARGATE_MEMORY_GIB.get(
                vcpu, ()):
            choices = fargate_memory_choices(vcpu)
            hint = (f"{vcpu:g} vCPU takes {choices}" if choices else
                    "Fargate tasks have " + ", ".join(f"{size:g}" for size in FARGATE_MEMORY_GIB) + " vCPU")
            raise InstallationError(f"{where}: {key} = {vcpu} with {memory_key} = {values[memory_key]} is not a "
                                    f"Fargate task size ({hint}); see the AWS Fargate task CPU and memory table")


def check_policy_parallelism(values, where):
    """Reject a policy_parallelism above worker_vcpu: every policy the worker simulates at once needs a vCPU."""
    parallelism, vcpu = values.get("policy_parallelism"), values.get("worker_vcpu")
    if parallelism is not None and vcpu is not None and parallelism > vcpu:
        raise InstallationError(f"{where}: policy_parallelism = {parallelism} is more than worker_vcpu = {vcpu}; "
                                "each policy simulated at once needs its own worker vCPU, so lower "
                                "policy_parallelism or raise worker_vcpu")


def effective_settings(settings, overrides, where):
    """Apply an installation's [settings] overrides to the settings.toml defaults; return values and sources."""
    values = {key: setting.value for key, setting in settings.items()}
    sources = dict.fromkeys(settings, str(SETTINGS_FILE))
    if not isinstance(overrides, dict):
        raise InstallationError(f"{where}: [settings] must be a table of settings.toml sections")
    for section, entries in overrides.items():
        if not isinstance(entries, dict):
            raise InstallationError(f"{where}: [settings.{section}] must be a table; use the settings.toml sections")
        for key, value in entries.items():
            setting = settings.get(key)
            if setting is None or setting.section != section:
                raise InstallationError(f"{where}: [settings.{section}] {key} is not a setting in {SETTINGS_FILE}")
            values[key] = check_value(setting, value, f"{where} [settings]")
            sources[key] = f"{where} [settings]"
    check_task_sizes(values, where)
    check_policy_parallelism(values, where)
    return values, sources


def refuse_tracked(path, root):
    """Refuse an installation file that git tracks: it holds one installation's identity and stays local."""
    try:
        relative = path.resolve().relative_to(root)
    except ValueError:
        return
    env = {key: value for key, value in os.environ.items() if key not in GIT_LOCATION}
    try:
        # Fixed git arguments with no shell; root and the file are paths the deployer resolved inside the checkout.
        completed = subprocess.run(  # nosec B603 B607 - fixed argument list, no shell; git is found on PATH
            ["git", "-C", str(root), "ls-files", "--error-unmatch", "--", relative.as_posix()],
            capture_output=True, env=env, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InstallationError(f"Could not ask git whether {relative} is tracked ({exc}); install git and deploy "
                                "from a git checkout.") from None
    if completed.returncode == 0:
        raise InstallationError(f"{relative} is tracked by git, but installation files stay local. Run "
                                f"git rm --cached {relative} (the file stays on disk), keep it git-ignored and retry.")


def text(table, section, key, where, *, required=True):
    """Read one string field of the installation file; a required one must be nonempty."""
    value = table.get(key, "")
    if not isinstance(value, str) or (required and not value.strip()):
        raise InstallationError(f"{where}: [{section}] {key} must be a nonempty string; see {EXAMPLE}")
    return value.strip()


def strings(table, section, key, where):
    """Read one list-of-strings field of the installation file."""
    value = table.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise InstallationError(f"{where}: [{section}] {key} must be a list of nonempty strings; see {EXAMPLE}")
    return tuple(item.strip() for item in value)


def check_keys(table, section, keys, where):
    """Refuse an unknown key in a section, or a required key it lacks."""
    extra = sorted(set(table) - set(keys))
    if extra:
        raise InstallationError(f"{where}: unknown key [{section}] {extra[0]}; the keys are {', '.join(keys)}")
    absent = [key for key, required in keys.items() if required and key not in table]
    if absent:
        raise InstallationError(f"{where}: missing [{section}] {absent[0]}; see {EXAMPLE}")


def camera_origin(value):
    """Return the Delphire API origin normalized (lower case, no trailing slash), or None if it is not one."""
    origin = value.strip().lower().removesuffix("/")
    return origin if len(origin) <= CAMERA_ORIGIN_MAX_LENGTH and re.fullmatch(CAMERA_ORIGIN, origin) else None


def camera_key_valid(value):
    """True for a usable Delphire key: a string of CAMERA_KEY_MIN_LENGTH-CAMERA_KEY_MAX_LENGTH visible ASCII characters."""
    return (isinstance(value, str) and CAMERA_KEY_MIN_LENGTH <= len(value) <= CAMERA_KEY_MAX_LENGTH
            and all("\x21" <= character <= "\x7e" for character in value))


def load_camera(table, where):
    """Validate [camera]: an https:// origin with only a host name, a device ID or "" (off), and a known alias."""
    origin = camera_origin(text(table, "camera", "api_base_url", where))
    if origin is None:
        raise InstallationError(f"{where}: [camera] api_base_url must be an https:// origin with only a host name "
                                "(no user name, port, path, query or fragment, and not an IP address), for example "
                                "https://wildfireai.com")
    device = text(table, "camera", "device_id", where, required=False)
    if device and not re.fullmatch(CAMERA_DEVICE_ID, device):
        raise InstallationError(f'{where}: [camera] device_id must be "" (no camera) or 1-64 letters, digits, "_" '
                                'or "-"')
    alias = text(table, "camera", "camera_alias", where)
    if alias not in CAMERA_ALIASES:
        raise InstallationError(f"{where}: [camera] camera_alias must be one of {', '.join(CAMERA_ALIASES)}")
    return Camera(origin, device, alias)


def load_operator_roles(aws, account, where):
    """Validate [aws] operator_role_arns: 1-10 IAM role ARNs in the installation's account, each kept once in order."""
    why = (f"list the IAM role ARNs that deploy or operate this installation, including the role of [aws] profile "
           f"(aws sts get-caller-identity --profile {aws.get('profile', '<profile>')} names it). Every bucket policy "
           "refuses object access to principals outside the installation (architecture/36).")
    if "operator_role_arns" not in aws:
        raise InstallationError(f"{where}: missing [aws] operator_role_arns: {why}")
    roles = tuple(dict.fromkeys(strings(aws, "aws", "operator_role_arns", where)))
    if not 1 <= len(roles) <= MAX_OPERATOR_ROLES:
        raise InstallationError(f"{where}: [aws] operator_role_arns lists {len(roles)} roles; it takes "
                                f"1-{MAX_OPERATOR_ROLES}: {why}")
    for arn in roles:
        if not re.fullmatch(OPERATOR_ROLE_ARN, arn) or arn.split(":")[4] != account:
            raise InstallationError(f"{where}: [aws] operator_role_arns has {arn!r}, which is not an IAM role ARN in "
                                    f"account {account} (arn:aws:iam::{account}:role/NAME or role/PATH/NAME; no "
                                    "wildcards, commas or empty segments)")
    return roles


def load_installation(path=None, *, root=ROOT):
    """Validate an installation file and settings.toml before any AWS call; return the Installation."""
    root = root.resolve()
    path = root / DEFAULT_INSTALLATION if path is None else Path(path).expanduser()
    path = path if path.is_absolute() else root / path
    where = shown(path, root)
    if not path.is_file():
        raise InstallationError(f"Installation file {where} not found. Copy {EXAMPLE} to {DEFAULT_INSTALLATION}, "
                                f"fill it in, and run bash deployment/deploy-all.sh (see {README}).")
    refuse_tracked(path, root)
    data = read_toml(path, root)
    unknown = sorted(set(data) - set(SCHEMA) - set(OPTIONAL_SCHEMA) - {"settings"})
    if unknown:
        raise InstallationError(f"{where}: unknown section [{unknown[0]}]; the sections are "
                                f"{', '.join(f'[{name}]' for name in SCHEMA)}, an optional [camera] and an "
                                "optional [settings]")
    # Older installation files may still hold this key, so its error says what changed instead of "unknown key".
    if isinstance(data.get("knowledge"), dict) and "private_knowledge_base_id" in data["knowledge"]:
        raise InstallationError(f"{where}: [knowledge] private_knowledge_base_id was removed: the Runtime has no "
                                "private knowledge-base retrieval since W2.11. Delete the line.")
    for section, keys in SCHEMA.items():
        table = data.get(section)
        if not isinstance(table, dict):
            raise InstallationError(f"{where}: missing section [{section}]; see {EXAMPLE}")
        check_keys(table, section, keys, where)
    for section, keys in OPTIONAL_SCHEMA.items():
        if section in data:
            if not isinstance(data[section], dict):
                raise InstallationError(f"{where}: [{section}] must be a table; see {EXAMPLE}")
            check_keys(data[section], section, keys, where)
    aws, access, knowledge = data["aws"], data["access"], data["knowledge"]
    try:
        target = Target(text(aws, "aws", "account_id", where), text(aws, "aws", "region", where),
                        text(aws, "aws", "name", where))
    except InstallationError:
        raise
    except ValueError as exc:
        raise InstallationError(f"{where} [aws]: {exc}") from None
    environment = text(aws, "aws", "environment", where)
    if environment not in ENVIRONMENTS:
        raise InstallationError(f"{where}: [aws] environment must be one of {', '.join(ENVIRONMENTS)}")
    operators = load_operator_roles(aws, target.account, where)
    mode = text(access, "access", "mode", where)
    if mode not in ACCESS_MODES:
        raise InstallationError(f"{where}: [access] mode must be one of {', '.join(ACCESS_MODES)}")
    networks = []
    for cidr in strings(access, "access", "staging_allowed_cidrs", where):
        try:
            network = ipaddress.ip_network(cidr)
        except ValueError:
            raise InstallationError(f"{where}: [access] staging_allowed_cidrs has {cidr!r}, which is not a CIDR "
                                    "range such as 198.51.100.0/24") from None
        if not network.prefixlen:
            raise InstallationError(f"{where}: [access] staging_allowed_cidrs has {cidr!r}, which allows every "
                                    "address; list only the networks that may use the staging installation")
        networks.append(str(network))
    # WAF IP sets take CIDR notation (a single address is /32 or /128); each range is listed once.
    cidrs = tuple(dict.fromkeys(networks))
    if mode == "staging" and not cidrs:
        raise InstallationError(f'{where}: [access] staging_allowed_cidrs must list at least one CIDR range when '
                                'mode = "staging"')
    recordings = strings(data["radio"], "radio", "recordings", where)
    if not 1 <= len(recordings) <= MAX_RECORDINGS:
        raise InstallationError(f"{where}: [radio] recordings must list 1-{MAX_RECORDINGS} files")
    guidance = text(knowledge, "knowledge", "public_guidance", where)
    if guidance not in PUBLIC_GUIDANCE:
        raise InstallationError(f"{where}: [knowledge] public_guidance must be one of {', '.join(PUBLIC_GUIDANCE)}")
    public = text(knowledge, "knowledge", "public_knowledge_base_id", where, required=False)
    if (guidance == "existing") != bool(public):
        raise InstallationError(f'{where}: [knowledge] public_knowledge_base_id is required when public_guidance = '
                                '"existing", and must be "" otherwise')
    secret = text(data["providers"], "providers", "secret_arn", where)
    try:
        validate_arn(secret, target, "secretsmanager")
    except ValueError:
        raise InstallationError(f"{where}: [providers] secret_arn must be a Secrets Manager ARN in account "
                                f"{target.account} and Region {target.region}") from None
    values, sources = effective_settings(load_settings(root), data.get("settings", {}), where)
    return Installation(
        path=path, target=target, profile=text(aws, "aws", "profile", where), environment=environment,
        access_mode=mode, staging_allowed_cidrs=cidrs,
        agent_model_id=text(data["models"], "models", "agent_model_id", where),
        report_model_id=text(data["models"], "models", "report_model_id", where), recordings=recordings,
        public_guidance=guidance, public_knowledge_base_id=public, provider_secret_arn=secret, settings=values,
        setting_sources=sources, camera=load_camera(data["camera"], where) if "camera" in data else Camera(),
        operator_role_arns=operators)


def changed_settings(installation, root=ROOT):
    """List the effective settings that differ from settings.toml, with the file each value came from."""
    defaults = load_settings(root)
    return {defaults[key].name: {"value": value, "settings.toml": defaults[key].value,
                                 "source": installation.setting_sources[key]}
            for key, value in installation.settings.items() if value != defaults[key].value}


def stack_parameters(installation):
    """Map every effective setting to its CloudFormation parameter name and string value."""
    return {parameter_name(key): parameter_value(value) for key, value in installation.settings.items()}


def camera_parameters(installation):
    """Map [camera] to the stack's CameraApiBaseUrl, CameraDeviceId and CameraAlias parameters ("" without it)."""
    camera = installation.camera
    return {"CameraApiBaseUrl": camera.api_base_url, "CameraDeviceId": camera.device_id, "CameraAlias": camera.alias}


def operator_parameters(installation):
    """Map [aws] operator_role_arns to every stack's OperatorRoleArns list parameter (comma-separated, D52)."""
    return {"OperatorRoleArns": ",".join(installation.operator_role_arns)}


def access_parameters(installation):
    """Map [access] to the WAF staging allowlist parameters: IPv4 and IPv6 CIDR lists, both "" when public."""
    cidrs = installation.staging_allowed_cidrs if installation.stage_only else ()
    versions = [ipaddress.ip_network(cidr).version for cidr in cidrs]
    return {f"StagingAllowedIpv{version}Cidrs": ",".join(c for c, v in zip(cidrs, versions) if v == version)
            for version in (4, 6)}


def removed_option_message(argument):
    """Explain in one line that a deploy-all option was removed, and what replaces it."""
    option = argument.split("=", 1)[0]
    if option in REMOVED_OPTIONS:
        return f"{option} was removed: {REMOVED_OPTIONS[option]} (see {README})."
    return (f"Unexpected argument {argument!r}: deploy-all.sh takes only an optional installation file path "
            f"with --apply and --resume-release RELEASE (see {README}).")


def settings_table(settings):
    """Render settings.toml as the Markdown table in architecture/29-workspace-operations.md."""
    rows = ["| Setting | Default | Unit | Allowed | CloudFormation parameter | Description |",
            "|---|---|---|---|---|---|"]
    for setting in sorted(settings.values(), key=lambda item: item.name):
        allowed = (f"{setting.minimum}–{setting.maximum}" if setting.minimum is not None
                   else ", ".join(setting.allowed) or "any")
        rows.append(f"| `{setting.name}` | {parameter_value(setting.value)} | {setting.unit} | {allowed} | "
                    f"`{setting.parameter}` | {setting.description} |")
    return "\n".join(rows)


def sync_docs(root=ROOT, *, check=False):
    """Rewrite the generated settings table in architecture/29; with check=True, fail when it is stale."""
    path = root / DOCS
    current = path.read_text()
    start, end = current.find(TABLE_START), current.find(TABLE_END)
    if start < 0 or end < start:
        raise InstallationError(f"{DOCS} has no generated settings table markers")
    expected = (current[:start + len(TABLE_START)] + "\n\n" + settings_table(load_settings(root)) + "\n\n"
                + current[end:])
    if expected == current:
        return False
    if check:
        raise InstallationError(f"{DOCS} settings table is stale: run .venv/bin/python -m deployment.installation")
    path.write_text(expected)
    return True


def main(argv=None):
    """Regenerate (or --check) the settings table in architecture/29 from deployment/settings.toml."""
    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument("--check", action="store_true", help="fail when the generated table is stale")
    options = parser.parse_args(argv)
    changed = sync_docs(ROOT, check=options.check)
    print(f"{DOCS}: settings table " + ("updated" if changed else "is current"))


if __name__ == "__main__":
    try:
        main()
    except InstallationError as exc:
        raise SystemExit(str(exc)) from None
