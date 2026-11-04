import re
import shutil
import subprocess  # nosec B404 - runs only fixed git argument lists on this test's own folder, never a shell
from pathlib import Path

import pytest

from deployment import deploy_workspace
from deployment.installation import (
    DOCS,
    EXAMPLE,
    REMOVED_OPTIONS,
    SETTINGS_FILE,
    Camera,
    InstallationError,
    access_parameters,
    camera_origin,
    camera_parameters,
    check_task_sizes,
    load_installation,
    load_settings,
    operator_parameters,
    parameter_name,
    parameter_value,
    removed_option_message,
    settings_table,
    stack_parameters,
    sync_docs,
)
from deployment.validate_infra_baseline import load_template

REPOSITORY = Path(__file__).resolve().parents[2]
# Environment variables that hold an operational limit (D23): each must be declared in deployment/settings.toml.
LIMIT_NAME = re.compile(r"(?:_PER_|LIMIT|MAX_|_MAX\b|TTL|CONCURRENCY|PARALLELISM|BUDGET|_BYTES|_SECONDS|_MINUTES"
                        r"|_HOURS|_DAYS|_VCPU|_GIB|_MIB|_KM\b)")
SETTINGS_MODULES = ("traffic-simulator/traffic_simulator/settings.py", "backend/api_settings.py",
                    "fire-risk-modeler/fire_risk_modeler/settings.py")
# The firetruck service's limits are pydantic-settings fields named after their settings.toml key (firetruck_*),
# which pydantic reads from the upper-case variable, so they appear as field names rather than getenv calls.
FIRETRUCK_SETTINGS = "firetruck-simulator/config/settings.py"


def test_example_installation_file_is_valid(tmp_path):
    copy = tmp_path / "installation.toml"
    shutil.copyfile(REPOSITORY / EXAMPLE, copy)
    installation = load_installation(copy, root=REPOSITORY)
    assert installation.target.name == "wildfire-demo" and installation.environment == "production"
    assert installation.access_mode == "public" and not installation.stage_only
    assert installation.public_guidance == "managed" and installation.recordings == ("/path/to/recording.mp3",)
    assert installation.settings == {key: s.value for key, s in load_settings(REPOSITORY).items()}
    # The documented [camera] section keeps the camera off until a device ID is set.
    assert installation.camera == Camera("https://wildfireai.com", "", "camera_a")
    assert not installation.camera.enabled


def test_installation_values_are_read_and_secret_free(make_installation):
    installation = make_installation({"access": {"mode": "staging", "staging_allowed_cidrs": ["198.51.100.0/24"]},
                                      "knowledge": {"public_guidance": "existing", "public_knowledge_base_id": "PUBLIC"}})
    assert installation.stage_only and installation.staging_allowed_cidrs == ("198.51.100.0/24",)
    assert installation.public_knowledge_base_id == "PUBLIC"
    identity = installation.identity()
    assert identity["account"] == "123456789012" and identity["access"]["mode"] == "staging"
    assert set(identity) >= {"file", "region", "name", "environment", "models", "knowledge", "provider_secret_arn"}


def test_staging_ranges_become_waf_allowlist_parameters_by_ip_version(make_installation):
    ranges = ["203.0.113.7", "2001:DB8::/32", "198.51.100.0/24", "203.0.113.7/32", "2001:db8:0::/32"]
    staging = make_installation({"access": {"mode": "staging", "staging_allowed_cidrs": ranges}})
    # CIDR notation as WAF IP sets require (a single address is /32), each range once, in the file's order.
    assert staging.staging_allowed_cidrs == ("203.0.113.7/32", "2001:db8::/32", "198.51.100.0/24")
    assert access_parameters(staging) == {"StagingAllowedIpv4Cidrs": "203.0.113.7/32,198.51.100.0/24",
                                          "StagingAllowedIpv6Cidrs": "2001:db8::/32"}
    only_ipv6 = make_installation({"access": {"mode": "staging", "staging_allowed_cidrs": ["2001:db8::/32"]}})
    assert access_parameters(only_ipv6) == {"StagingAllowedIpv4Cidrs": "", "StagingAllowedIpv6Cidrs": "2001:db8::/32"}
    # A public installation has no allowlist, even when the file still lists ranges.
    public = make_installation({"access": {"mode": "public", "staging_allowed_cidrs": ["198.51.100.0/24"]}})
    assert access_parameters(public) == {"StagingAllowedIpv4Cidrs": "", "StagingAllowedIpv6Cidrs": ""}


def test_settings_overrides_take_precedence_and_name_their_source(make_installation):
    installation = make_installation({"settings": {"rate_limits": {"requests_per_ip_per_5_minutes": 1200},
                                                   "simulation": {"max_study_radius_km": 10}}})
    assert installation.settings["requests_per_ip_per_5_minutes"] == 1200
    assert installation.setting_sources["requests_per_ip_per_5_minutes"].endswith("installation.toml [settings]")
    parameters = stack_parameters(installation)
    # Every setting reaches the stack on every deploy; a number setting given as an integer stays a number.
    assert set(parameters) == {setting.parameter for setting in load_settings(REPOSITORY).values()}
    assert (parameters["RequestsPerIpPer5Minutes"], parameters["WorkRequestsPerIpPer5Minutes"]) == ("1200", "20")
    assert (parameters["MaxStudyRadiusKm"], parameters["TomtomCallsPerDay"]) == ("10.0", "1000")
    default = make_installation()
    assert default.settings["requests_per_ip_per_5_minutes"] == 600
    assert default.setting_sources["requests_per_ip_per_5_minutes"] == str(SETTINGS_FILE)


@pytest.mark.parametrize("override,message", [
    ({"rate_limits": {"requests_per_ip_per_5_minutes": 99}}, "outside the allowed range 100-100000 requests"),
    # WAF rate-based rules accept limits from 10, so a lower value fails before any AWS call.
    ({"rate_limits": {"work_requests_per_ip_per_5_minutes": 9}}, "outside the allowed range 10-100000 requests"),
    ({"rate_limits": {"requests_per_ip_per_5_minutes": "600"}}, "must be an integer"),
    ({"rate_limits": {"requests_per_ip_per_5_minutes": True}}, "must be an integer"),
    ({"rate_limits": {"requests_per_ip_per_typo": 1}}, r"\[settings.rate_limits\] requests_per_ip_per_typo is not a setting"),
    ({"budgets": {"requests_per_ip_per_5_minutes": 700}}, r"\[settings.budgets\] requests_per_ip_per_5_minutes is not"),
])
def test_settings_overrides_are_validated_by_settings_toml(make_installation, override, message):
    with pytest.raises(InstallationError, match=message):
        make_installation({"settings": override})


@pytest.mark.parametrize("changes,message", [
    ({"aws": {"profiles": "typo"}}, r"unknown key \[aws\] profiles"),
    ({"extra": {"key": "value"}}, r"unknown section \[extra\]"),
    ({"aws": {"region": None}}, r"missing \[aws\] region; see deployment/installation.example.toml"),
    ({"providers": None}, r"missing section \[providers\]"),
    ({"aws": {"account_id": "12345"}}, "twelve digits"),
    ({"aws": {"name": "Bad Name"}}, "Deployment name"),
    ({"aws": {"environment": "prod"}}, r"\[aws\] environment must be one of development, staging, production"),
    ({"aws": {"profile": ""}}, r"\[aws\] profile must be a nonempty string"),
    ({"access": {"mode": "private"}}, r"\[access\] mode must be one of public, staging"),
    # Anonymous access is an explicit choice: a file without [access] mode is refused, never treated as public.
    ({"access": {"mode": None}}, r"missing \[access\] mode; see deployment/installation.example.toml"),
    ({"access": {"mode": ""}}, r"\[access\] mode must be a nonempty string"),
    ({"access": {"mode": "staging"}}, "at least one CIDR range"),
    ({"access": {"mode": "staging", "staging_allowed_cidrs": ["10.0.0.300/8"]}}, "not a CIDR range"),
    ({"access": {"mode": "staging", "staging_allowed_cidrs": ["198.51.100.1/24"]}}, "not a CIDR range"),
    # WAF IP sets reject /0, and an allowlist of every address is not a staging gate.
    ({"access": {"mode": "staging", "staging_allowed_cidrs": ["0.0.0.0/0"]}}, "allows every address"),
    ({"access": {"mode": "staging", "staging_allowed_cidrs": ["198.51.100.0/24", "::/0"]}}, "allows every address"),
    ({"radio": {"recordings": []}}, r"\[radio\] recordings must list 1-10 files"),
    ({"radio": {"recordings": [f"r{i}.mp3" for i in range(11)]}}, "1-10 files"),
    ({"knowledge": {"public_guidance": "existing"}}, "public_knowledge_base_id is required"),
    ({"knowledge": {"public_guidance": "managed", "public_knowledge_base_id": "KB"}}, 'must be "" otherwise'),
    # W2.11 removed private knowledge-base retrieval; the key now stops the check with what to do.
    ({"knowledge": {"private_knowledge_base_id": "KB"}},
     (r"\[knowledge\] private_knowledge_base_id was removed: the Runtime has no private knowledge-base retrieval "
      r"since W2\.11\. Delete the line\.")),
    ({"knowledge": {"public_guidance": "sometimes"}}, "public_guidance must be one of managed, existing, none"),
    ({"providers": {"secret_arn": "arn:aws:secretsmanager:us-east-1:123456789012:secret:provider"}},
     "secret_arn must be a Secrets Manager ARN in account 123456789012 and Region us-west-2"),
])
def test_installation_file_errors_name_the_field(make_installation, changes, message):
    with pytest.raises(InstallationError, match=message):
        make_installation(changes)


CAMERA = {"api_base_url": "https://wildfireai.com", "device_id": "fixture-device", "camera_alias": "camera_b"}


def test_an_installation_without_camera_loads_with_the_camera_off(make_installation):
    default = make_installation()
    assert default.camera == Camera() and not default.camera.enabled
    assert camera_parameters(default) == {"CameraApiBaseUrl": "", "CameraDeviceId": "", "CameraAlias": ""}
    assert default.identity()["camera"] == {"api_base_url": "", "device_id": "", "camera_alias": ""}


def test_a_camera_section_is_normalized_and_reaches_the_stack(make_installation):
    configured = make_installation({"camera": {**CAMERA, "api_base_url": "  HTTPS://WildfireAI.com/ "}})
    assert configured.camera == Camera("https://wildfireai.com", "fixture-device", "camera_b")
    assert configured.camera.enabled
    assert camera_parameters(configured) == {"CameraApiBaseUrl": "https://wildfireai.com",
                                             "CameraDeviceId": "fixture-device", "CameraAlias": "camera_b"}
    # The installation identity names the camera; it holds no secret, because the key is never in the file.
    assert configured.identity()["camera"] == {"api_base_url": "https://wildfireai.com",
                                               "device_id": "fixture-device", "camera_alias": "camera_b"}
    assert not make_installation({"camera": {**CAMERA, "device_id": ""}}).camera.enabled


ORIGIN_ERROR = r"\[camera\] api_base_url must be an https:// origin with only a host name"


@pytest.mark.parametrize("changes,message", [
    ({"api_base_url": "http://wildfireai.com"}, ORIGIN_ERROR),
    ({"api_base_url": "https://user:secret@wildfireai.com"}, ORIGIN_ERROR),
    ({"api_base_url": "https://wildfireai.com/api"}, ORIGIN_ERROR),
    ({"api_base_url": "https://wildfireai.com?key=1"}, ORIGIN_ERROR),
    ({"api_base_url": "https://wildfireai.com#x"}, ORIGIN_ERROR),
    ({"api_base_url": "https://wildfireai.com:8443"}, ORIGIN_ERROR),
    ({"api_base_url": "http://203.0.113.10:2361"}, ORIGIN_ERROR),
    ({"api_base_url": ""}, r"\[camera\] api_base_url must be a nonempty string"),
    ({"api_base_url": None}, r"missing \[camera\] api_base_url"),
    ({"device_id": "../other"}, r'\[camera\] device_id must be "" \(no camera\) or 1-64 letters'),
    ({"device_id": "x" * 65}, r"\[camera\] device_id must be"),
    ({"device_id": None}, r"missing \[camera\] device_id"),
    ({"camera_alias": "camera_c"}, r"\[camera\] camera_alias must be one of camera_a, camera_b, infrared_camera_a"),
    ({"camera_alias": None}, r"missing \[camera\] camera_alias"),
    ({"api_key": "x"}, r"unknown key \[camera\] api_key; the keys are api_base_url, device_id, camera_alias"),
])
def test_camera_section_errors_name_the_key(make_installation, changes, message):
    with pytest.raises(InstallationError, match=message):
        make_installation({"camera": {**CAMERA, **changes}})


def test_a_camera_value_that_is_not_a_table_is_refused(tmp_path, installation_file):
    path = installation_file()
    path.write_text('camera = "https://wildfireai.com"\n' + path.read_text())
    with pytest.raises(InstallationError, match=r"\[camera\] must be a table"):
        load_installation(path, root=REPOSITORY)


# The [camera] api_base_url rule, run through the deployer and through the stack's CloudFormation pattern on the
# value the deployer passes. backend/tests/test_workspace_settings.py runs the same rows through the API.
HOST_253 = ".".join(("a" * 63, "b" * 63, "c" * 63, "d" * 61))
ORIGINS = [
    ("https://wildfireai.com", "https://wildfireai.com"),
    ("https://wildfireai.com/", "https://wildfireai.com"),
    ("https://WildfireAI.com", "https://wildfireai.com"),
    ("https://203.0.113.10", None),
    ("https://wildfireai.com.", None),
    ("https://-x.example", None),
    ("https://wildfireai.com:443", None),
    ("https://u@wildfireai.com", None),
    ("http://wildfireai.com", None),
    ("https://wildfireai.com/x", None),
    ("https://wildfireai.com?q=1", None),
    ("https://localhost", None),
    ("https://[::1]", None),
    ("https://" + HOST_253, "https://" + HOST_253),
    ("https://" + HOST_253 + "d", None),
]
DEPLOYER = REPOSITORY / "deployment/installation.py"
API_SETTINGS = REPOSITORY / "backend/api_settings.py"
STACKS = REPOSITORY / "deployment/infra/lib/stacks.ts"


def python_constant(text, name):
    """Read a raw-string, integer or tuple constant from Python source text."""
    return re.search(rf'^{name} = (r"[^"\n]*"|\d+|\([^)]*\))$', text, re.MULTILINE)[1]


def typescript_constant(text, name):
    """Read a String.raw, number or array constant from TypeScript source text."""
    return re.search(rf"^export const {name} = (String\.raw`[^`]*`|\d+|\[[^\]]*\]);$", text, re.MULTILINE)[1]


def stack_rules():
    """The stack's CameraApiBaseUrl rule: its pattern text and length limit."""
    stacks = STACKS.read_text()
    return (typescript_constant(stacks, "CAMERA_ORIGIN").removeprefix("String.raw`").removesuffix("`"),
            int(typescript_constant(stacks, "CAMERA_ORIGIN_MAX_LENGTH")))


def test_camera_rules_are_identical_in_the_deployer_the_stack_and_the_api():
    deployer, api, stacks = DEPLOYER.read_text(), API_SETTINGS.read_text(), STACKS.read_text()
    for name in ("CAMERA_ORIGIN", "CAMERA_DEVICE_ID"):
        python = python_constant(deployer, name)
        assert python == python_constant(api, name), name
        typescript = typescript_constant(stacks, name)
        assert typescript == "String.raw`" + python.removeprefix('r"').removesuffix('"') + "`", name
    for name in ("CAMERA_ORIGIN_MAX_LENGTH", "CAMERA_KEY_MIN_LENGTH", "CAMERA_KEY_MAX_LENGTH"):
        assert python_constant(deployer, name) == python_constant(api, name), name
    assert python_constant(deployer, "CAMERA_ORIGIN_MAX_LENGTH") == typescript_constant(stacks, "CAMERA_ORIGIN_MAX_LENGTH")
    aliases = python_constant(deployer, "CAMERA_ALIASES")
    assert aliases == python_constant(api, "CAMERA_ALIASES")
    assert typescript_constant(stacks, "CAMERA_ALIASES") == "[" + aliases[1:-1] + "]"
    # The parameters use exactly these rules, so CloudFormation refuses what the deployer refuses.
    assert "allowedPattern: `(${CAMERA_ORIGIN})?`, maxLength: CAMERA_ORIGIN_MAX_LENGTH" in stacks
    assert "allowedPattern: `(${CAMERA_DEVICE_ID})?`" in stacks
    assert 'allowedPattern: `(${CAMERA_ALIASES.join("|")})?`' in stacks


OPERATOR = "arn:aws:iam::123456789012:role/fixture-deployer"
NOT_A_ROLE = "which is not an IAM role ARN in account 123456789012"


@pytest.mark.parametrize("roles,message", [
    (None, (r"missing \[aws\] operator_role_arns: list the IAM role ARNs that deploy or operate this installation, "
            r"including the role of \[aws\] profile \(aws sts get-caller-identity --profile fixture names it\)\. "
            r"Every bucket policy refuses object access to principals outside the installation \(architecture/36\)\.")),
    (OPERATOR, r"\[aws\] operator_role_arns must be a list of nonempty strings"),
    ([], r"\[aws\] operator_role_arns lists 0 roles; it takes 1-10: list the IAM role ARNs"),
    ([f"arn:aws:iam::123456789012:role/r{i}" for i in range(11)],
     r"\[aws\] operator_role_arns lists 11 roles; it takes 1-10"),
    (["arn:aws:iam::999999999999:role/fixture-deployer"], NOT_A_ROLE),
    (["arn:aws:iam::123456789012:role/*"], r"has 'arn:aws:iam::123456789012:role/\*', " + NOT_A_ROLE),
    (["arn:aws:iam::123456789012:role/one,two"], NOT_A_ROLE),
    (["arn:aws:iam::123456789012:user/fixture-deployer"], NOT_A_ROLE),
    (["arn:aws:iam::123456789012:role/ops/"], NOT_A_ROLE),
    (["arn:aws:iam::123456789012:role//fixture"], NOT_A_ROLE),
    ([f"arn:aws:iam::123456789012:role/{'r' * 65}"], NOT_A_ROLE),
])
def test_operator_roles_are_required_and_validated(make_installation, roles, message):
    """L1, D52: the bucket policies admit only these roles besides the installation's own, so the file names them."""
    with pytest.raises(InstallationError, match=message):
        make_installation({"aws": {"operator_role_arns": roles}})


def test_operator_roles_are_kept_once_in_order_and_reach_every_stack(make_installation):
    path, longest = "arn:aws:iam::123456789012:role/ops/fixture-operator", f"arn:aws:iam::123456789012:role/{'r' * 64}"
    installation = make_installation({"aws": {"operator_role_arns": [OPERATOR, path, longest, OPERATOR]}})
    assert installation.operator_role_arns == (OPERATOR, path, longest)
    assert installation.identity()["operator_role_arns"] == [OPERATOR, path, longest]
    assert operator_parameters(installation) == {"OperatorRoleArns": f"{OPERATOR},{path},{longest}"}
    # One pattern for the deployer, the CDK stacks' parameter and the backend templates' parameters, so
    # CloudFormation refuses only what the deployer already refused.
    python = python_constant(DEPLOYER.read_text(), "OPERATOR_ROLE_ARN").removeprefix('r"').removesuffix('"')
    bucket_access = (REPOSITORY / "deployment/infra/lib/bucket-access.ts").read_text()
    assert typescript_constant(bucket_access, "OPERATOR_ROLE_ARN") == f"String.raw`{python}`"
    for template in ("wildfire-backend-stack.yaml", "nested/1-infrastructure.yaml"):
        parsed = load_template((REPOSITORY / "backend/deployment/templates" / template).read_text())
        assert parsed["Parameters"]["OperatorRoleArns"]["AllowedPattern"] == python, template


@pytest.mark.parametrize("value,origin", ORIGINS)
def test_the_deployer_and_the_stack_apply_one_camera_origin_rule(value, origin):
    assert camera_origin(value) == origin
    pattern, longest = stack_rules()
    passed = value.strip().lower().removesuffix("/")
    stack_accepts = len(passed) <= longest and re.fullmatch(f"({pattern})?", passed) is not None
    assert stack_accepts is (origin is not None), "CloudFormation accepts exactly what the deployer accepts"


def test_missing_and_malformed_installation_files_point_to_the_example(tmp_path):
    with pytest.raises(InstallationError, match="not found. Copy deployment/installation.example.toml"):
        load_installation(tmp_path / "absent.toml", root=REPOSITORY)
    broken = tmp_path / "broken.toml"
    broken.write_text("[aws\nprofile = 1\n")
    with pytest.raises(InstallationError, match="broken.toml is not valid TOML"):
        load_installation(broken, root=REPOSITORY)


def test_installation_file_tracked_by_git_is_refused(tmp_path, installation_file):
    root = tmp_path / "checkout"
    (root / "deployment").mkdir(parents=True)
    shutil.copyfile(REPOSITORY / SETTINGS_FILE, root / SETTINGS_FILE)
    path = root / "deployment/installation.toml"
    shutil.copyfile(installation_file(), path)
    # Fixed git arguments on this test's own temporary checkout; no shell.
    # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
    subprocess.run(["git", "init", "-q", str(root)], check=True, timeout=60)  # nosec B603 B607
    assert load_installation(path, root=root).target.name == "training"
    # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
    subprocess.run(["git", "-C", str(root), "add", "deployment/installation.toml"],  # nosec B603 B607
                   check=True, timeout=60)
    with pytest.raises(InstallationError, match="deployment/installation.toml is tracked by git"):
        load_installation(None, root=root)


def test_repository_git_ignores_installation_files():
    lines = (REPOSITORY / ".gitignore").read_text().splitlines()
    assert "deployment/installation.toml" in lines and "deployment/installations/" in lines
    assert lines[-1] == "!deployment/infra/lib/", "The CDK lib/ exception must stay the last line."


@pytest.mark.parametrize("option", sorted(REMOVED_OPTIONS))
def test_removed_options_print_one_line_pointing_to_the_readme(option):
    with pytest.raises(SystemExit) as stopped:
        deploy_workspace.arguments([option, "value"])
    message = str(stopped.value)
    assert message.startswith(f"{option} was removed: ") and "deployment/README.md" in message
    assert "\n" not in message
    assert removed_option_message(f"{option}=value") == message


def test_deploy_all_takes_installation_apply_and_bounded_recovery():
    assert vars(deploy_workspace.arguments([])) == {"installation": None, "apply": False, "resume_release": None,
                                                    "acknowledge": []}
    assert vars(deploy_workspace.arguments(["deployment/installations/x.toml", "--apply"])) == {
        "installation": "deployment/installations/x.toml", "apply": True, "resume_release": None, "acknowledge": []}
    assert vars(deploy_workspace.arguments(["--apply", "--resume-release", "20261001000000000002"])) == {
        "installation": None, "apply": True, "resume_release": "20261001000000000002", "acknowledge": []}
    assert deploy_workspace.arguments(["--apply", "--acknowledge", "a", "--acknowledge", "b"]).acknowledge == ["a", "b"]
    for unexpected in (["--app"], ["a.toml", "b.toml"], ["--setting", "x=1"]):
        with pytest.raises(SystemExit, match="deploy-all.sh takes only an optional installation file path"):
            deploy_workspace.arguments(unexpected)


def write_settings(root, text):
    (root / "deployment").mkdir(parents=True, exist_ok=True)
    (root / SETTINGS_FILE).write_text(text)
    return root


@pytest.mark.parametrize("text,message", [
    ('[alpha.size]\nvalue = 1\nunit = "u"\nmin = 0\nmax = 2\n', "is missing description"),
    ('[alpha.size]\nvalue = 1\nunit = "u"\nmin = 0\nmax = 2\ndescription = "d"\ncolour = "red"\n',
     "unknown fields colour"),
    ('[alpha.size]\nvalue = 5\nunit = "u"\nmin = 0\nmax = 2\ndescription = "d"\n', "outside the allowed range 0-2"),
    ('[alpha.size]\nvalue = 1\nunit = "u"\ndescription = "d"\n', "needs min and max"),
    ('[alpha.size]\nvalue = "s"\nunit = "u"\ndescription = "d"\n', "needs allowed values"),
    ('[alpha.size]\nvalue = 1\nunit = "u"\nmin = 3\nmax = 2\ndescription = "d"\n', "min <= max"),
    (('[alpha.size]\nvalue = 1\nunit = "u"\nmin = 0\nmax = 2\ndescription = "d"\n'
      '[beta.size]\nvalue = 1\nunit = "u"\nmin = 0\nmax = 2\ndescription = "d"\n'),
     r"size appears in \[alpha\] and \[beta\]"),
    ('[Alpha.size]\nvalue = 1\nunit = "u"\nmin = 0\nmax = 2\ndescription = "d"\n', "lower_snake_case"),
])
def test_malformed_settings_file_is_rejected(tmp_path, text, message):
    with pytest.raises(InstallationError, match=message):
        load_settings(write_settings(tmp_path, text))


def test_settings_types_ranges_and_allowed_values(tmp_path):
    settings = load_settings(write_settings(tmp_path, (
        '[alpha.ratio]\nvalue = 0.5\nunit = "fraction"\nmin = 0\nmax = 1\ndescription = "d"\n'
        '[alpha.mode]\nvalue = "fast"\nunit = "mode"\nallowed = ["fast", "slow"]\ndescription = "d"\n'
        '[alpha.areas]\nvalue = ["paradise"]\nunit = "areas"\nallowed = ["paradise", "palisades"]\n'
        'description = "d"\n')))
    assert settings["ratio"].parameter == "Ratio" and settings["areas"].environment_variable == "AREAS"
    assert "| `alpha.mode` | fast | mode | fast, slow | `Mode` | d |" in settings_table(settings)
    assert "| `alpha.ratio` | 0.5 | fraction | 0–1 | `Ratio` | d |" in settings_table(settings)


def test_parameter_names_and_values_follow_the_naming_rule():
    assert parameter_name("requests_per_ip_per_5_minutes") == "RequestsPerIpPer5Minutes"
    assert parameter_name("worker_memory_gib") == "WorkerMemoryGib"
    assert [parameter_value(v) for v in (True, False, 4, 0.5, ["a", "b"], "x")] == [
        "true", "false", "4", "0.5", "a,b", "x"]


@pytest.mark.parametrize("values,valid", [
    ({"worker_vcpu": 4, "worker_memory_gib": 8}, True), ({"worker_vcpu": 0.25, "worker_memory_gib": 0.5}, True),
    ({"worker_vcpu": 8, "worker_memory_gib": 20}, True), ({"api_vcpu": 1, "api_memory_gib": 2}, True),
    ({"worker_vcpu": 4, "worker_memory_gib": 4}, False), ({"worker_vcpu": 8, "worker_memory_gib": 18}, False),
    ({"worker_vcpu": 3, "worker_memory_gib": 8}, False),
    ({"fire_risk_vcpu": 0.5, "fire_risk_memory_gib": 1}, True),
    ({"fire_risk_vcpu": 0.5, "fire_risk_memory_gib": 0.5}, False),
])
def test_fargate_cpu_and_memory_combinations_are_checked(values, valid):
    if valid:
        check_task_sizes(values, "fixture")
    else:
        with pytest.raises(InstallationError, match="is not a Fargate task size"):
            check_task_sizes(values, "fixture")


def test_task_sizes_and_policy_parallelism_are_deploy_time_settings(make_installation):
    """The ECS task vCPU and memory and the worker's policy parallelism reach the stack from settings.toml."""
    default = make_installation()
    assert {key: default.settings[key] for key in ("api_vcpu", "api_memory_gib", "worker_vcpu", "worker_memory_gib",
                                                     "policy_parallelism", "worker_tasks")} == {
        "api_vcpu": 1, "api_memory_gib": 4, "worker_vcpu": 4, "worker_memory_gib": 8,
        "policy_parallelism": 3, "worker_tasks": 2}
    smaller = make_installation({"settings": {"capacity": {"worker_vcpu": 2, "worker_memory_gib": 4},
                                              "simulation": {"policy_parallelism": 2}}})
    parameters = stack_parameters(smaller)
    assert (parameters["WorkerVcpu"], parameters["WorkerMemoryGib"], parameters["PolicyParallelism"]) == ("2", "4", "2")
    assert parameters["WorkerTasks"] == "2"
    for task in ("api", "worker", "firetruck", "radio", "fire_risk"):
        assert {f"{task}_vcpu", f"{task}_memory_gib"} <= default.settings.keys(), task


@pytest.mark.parametrize("override,message", [
    # Checked before any AWS call, naming both settings and the memory Fargate allows with that vCPU count.
    ({"capacity": {"worker_vcpu": 2, "worker_memory_gib": 30}},
     r"worker_vcpu = 2 with worker_memory_gib = 30 is not a Fargate task size \(2 vCPU takes 4-16 GiB\)"),
    ({"capacity": {"api_vcpu": 8, "api_memory_gib": 18}}, r"\(8 vCPU takes 16-60 GiB in 4 GiB steps\)"),
    ({"capacity": {"radio_vcpu": 3}}, r"radio_vcpu = 3 .*\(Fargate tasks have 0.25, 0.5, 1, 2, 4, 8, 16 vCPU\)"),
    ({"capacity": {"fire_risk_vcpu": 2, "fire_risk_memory_gib": 30}},
     r"fire_risk_vcpu = 2 with fire_risk_memory_gib = 30 is not a Fargate task size"),
    ({"capacity": {"worker_vcpu": 2.5}}, "worker_vcpu must be an integer"),
    # Each policy simulated at once needs its own vCPU, so a smaller worker needs a lower parallelism too.
    ({"capacity": {"worker_vcpu": 2}}, "policy_parallelism = 3 is more than worker_vcpu = 2"),
    ({"simulation": {"policy_parallelism": 4}}, "outside the allowed range 1-3 policies"),
])
def test_task_size_and_parallelism_errors_name_the_setting(make_installation, override, message):
    with pytest.raises(InstallationError, match=message):
        make_installation({"settings": override})


def test_network_cache_and_demo_area_settings_are_validated_and_reach_the_stack(make_installation):
    """The network cache bounds, its S3 lifetime and the demo areas to pre-build are deploy-time settings."""
    default = make_installation()
    parameters = stack_parameters(default)
    assert (parameters["NetworkCacheDays"], parameters["CachedNetworksPerWorker"],
            parameters["CachedNetworkMibPerWorker"], parameters["DemoAreasToPrebuild"],
            parameters["MaxNetworkEdges"], parameters["OverpassResponseMaxMb"]) == (
        "30", "50", "4096", "paradise", "14000", "50")
    # An empty list turns the warm-up off and reaches the worker as "".
    quiet = make_installation({"settings": {"cache": {"demo_areas_to_prebuild": [], "network_cache_days": 7}}})
    assert stack_parameters(quiet)["DemoAreasToPrebuild"] == "" and stack_parameters(quiet)["NetworkCacheDays"] == "7"
    both = make_installation({"settings": {"cache": {"demo_areas_to_prebuild": ["paradise", "malibu"]}}})
    assert stack_parameters(both)["DemoAreasToPrebuild"] == "paradise,malibu"
    with pytest.raises(InstallationError, match=r"demo_areas_to_prebuild = \['atlantis'\] is not one of paradise, malibu"):
        make_installation({"settings": {"cache": {"demo_areas_to_prebuild": ["atlantis"]}}})
    # The published contract allows at most 14,000 edges, so the setting can only lower it.
    with pytest.raises(InstallationError, match="outside the allowed range 1000-14000 edges"):
        make_installation({"settings": {"simulation": {"max_network_edges": 20000}}})


def test_provider_request_concurrency_and_response_cap_are_validated_and_reach_the_stack(make_installation):
    """The TomTom requests a run sends at once and the Caltrans response cap are deploy-time settings."""
    parameters = stack_parameters(make_installation())
    assert (parameters["TomtomRequestConcurrency"], parameters["CaltransResponseMaxMb"]) == ("4", "20")
    changed = stack_parameters(make_installation({"settings": {"simulation": {
        "tomtom_request_concurrency": 1, "caltrans_response_max_mb": 5}}}))
    assert (changed["TomtomRequestConcurrency"], changed["CaltransResponseMaxMb"]) == ("1", "5")
    with pytest.raises(InstallationError, match="outside the allowed range 1-12 requests"):
        make_installation({"settings": {"simulation": {"tomtom_request_concurrency": 20}}})
    with pytest.raises(InstallationError, match="outside the allowed range 1-100 MB"):
        make_installation({"settings": {"simulation": {"caltrans_response_max_mb": 0}}})

def test_fire_risk_limits_and_explicit_demo_assumptions_reach_the_stack(make_installation):
    installation = make_installation({"settings": {
        "budgets": {"fire_risk_assessments_per_day": 50, "fire_risk_weather_calls_per_day": 20},
        "capacity": {"fire_risk_vcpu": 2, "fire_risk_memory_gib": 4, "fire_risk_max_fires_per_turn": 2},
        "simulation": {"fire_risk_inner_km": 2.0, "fire_risk_middle_km": 6.0, "fire_risk_outer_km": 10.0,
                       "fire_risk_surface_fuel_model": 4, "fire_risk_surface_moisture_profile": "high"},
    }})
    parameters = stack_parameters(installation)
    assert {name: parameters[name] for name in (
        "FireRiskAssessmentsPerDay", "FireRiskWeatherCallsPerDay", "FireRiskVcpu", "FireRiskMemoryGib",
        "FireRiskMaxFiresPerTurn", "FireRiskInnerKm", "FireRiskMiddleKm", "FireRiskOuterKm",
        "FireRiskSurfaceFuelModel", "FireRiskSurfaceMoistureProfile",
    )} == {
        "FireRiskAssessmentsPerDay": "50", "FireRiskWeatherCallsPerDay": "20",
        "FireRiskVcpu": "2", "FireRiskMemoryGib": "4", "FireRiskMaxFiresPerTurn": "2",
        "FireRiskInnerKm": "2.0", "FireRiskMiddleKm": "6.0", "FireRiskOuterKm": "10.0",
        "FireRiskSurfaceFuelModel": "4", "FireRiskSurfaceMoistureProfile": "high",
    }
    with pytest.raises(InstallationError, match="fire_risk_surface_moisture_profile"):
        make_installation({"settings": {"simulation": {"fire_risk_surface_moisture_profile": "unmeasured"}}})


def test_every_setting_reaches_the_stack_and_every_limit_variable_is_declared():
    """D23 consistency: settings.toml <-> CloudFormation parameters <-> limit environment variables in code."""
    settings = load_settings(REPOSITORY)
    assert settings, "deployment/settings.toml declares the operational settings"
    stacks = (REPOSITORY / "deployment/infra/lib/stacks.ts").read_text()
    for setting in settings.values():
        assert f'new CfnParameter(this, "{setting.parameter}"' in stacks, setting.name
    declared = {setting.environment_variable for setting in settings.values()}
    read, limits = set(), set()
    for module in SETTINGS_MODULES:
        text = (REPOSITORY / module).read_text()
        read |= set(re.findall(r"""(?:environ(?:\.get)?|getenv)\(\s*["']([A-Z0-9_]+)["']""", text))
        read |= set(re.findall(r"""environ\[\s*["']([A-Z0-9_]+)["']\s*\]""", text))
        # Operational settings go through traffic_simulator.settings.limit("VARIABLE") or names("VARIABLE").
        limits |= set(re.findall(r"""(?:limit|names)\(\s*["']([A-Z0-9_]+)["']\s*\)""", text))
    fields = re.findall(r"^    (firetruck_[a-z0-9_]+)\s*:", (REPOSITORY / FIRETRUCK_SETTINGS).read_text(), re.MULTILINE)
    assert "firetruck_evidence_events_max" in fields, f"{FIRETRUCK_SETTINGS} declares its limits as firetruck_* fields"
    limits |= {field.upper() for field in fields}
    read |= set(re.findall(r"\b([A-Z][A-Z0-9_]+):\s", stacks.split("environment", 1)[-1]))
    assert limits and sorted(limits - declared) == []
    assert sorted(name for name in read if LIMIT_NAME.search(name) and name not in declared) == []


def test_generated_settings_table_in_the_operations_guide_is_current():
    assert sync_docs(REPOSITORY, check=True) is False


def test_stale_settings_table_is_detected_and_rewritten(tmp_path):
    shutil.copyfile(REPOSITORY / SETTINGS_FILE, write_settings(tmp_path, "") / SETTINGS_FILE)
    docs = tmp_path / DOCS
    docs.parent.mkdir(parents=True)
    from deployment.installation import TABLE_END, TABLE_START
    docs.write_text(f"intro\n{TABLE_START}\n| stale |\n{TABLE_END}\noutro\n")
    with pytest.raises(InstallationError, match="settings table is stale"):
        sync_docs(tmp_path, check=True)
    assert sync_docs(tmp_path) is True and sync_docs(tmp_path, check=True) is False
    assert "`RequestsPerIpPer5Minutes`" in docs.read_text() and docs.read_text().endswith("outro\n")


def test_operations_guide_maps_every_removed_option_to_its_file_key():
    guide = (REPOSITORY / DOCS).read_text()
    assert [option for option in REMOVED_OPTIONS if f"`{option}`" not in guide] == []
