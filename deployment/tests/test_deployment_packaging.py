"""Release source bundle: completeness, reproducibility, exclusions and Docker inputs."""
import ast
import io
import json
import os
import posixpath
import re
import shlex
import shutil
import subprocess  # nosec B404 - only fixed argument lists (git, generators, release checks), never a shell
import sys
import zipfile
from pathlib import Path

import pytest
from dockerfile_layout import (
    copy_sources,
    dockerignore_admits,
    dockerignore_rules,
    image_layout,
    instructions,
    stage,
    under,
)

from deployment.check_lockfiles import PYTHON_REQUIREMENTS
from deployment.container_build import COMPONENTS, GROUPS
from deployment.deploy_backend import runtime_source
from deployment.package_source import REQUIRED_FILES, collect_sources, package_source

REPOSITORY = Path(__file__).resolve().parents[2]
BUILDSPEC = REPOSITORY / "deployment/infra/lib/stacks.ts"


@pytest.fixture
def source_tree(tmp_path):
    root = tmp_path / "source"
    for name in REQUIRED_FILES + PYTHON_REQUIREMENTS:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# fixture\n" if name in PYTHON_REQUIREMENTS else "fixture\n")
    for folder in ("ui", "deployment/infra"):
        (root / folder / "package.json").write_text(json.dumps({"dependencies": {}}))
        (root / folder / "package-lock.json").write_text(json.dumps({"packages": {"": {"dependencies": {}}}}))
    (root / "requirements-lock.txt").write_text("fastapi==0.141.1\n")
    (root / "traffic-simulator/traffic_simulator").mkdir(parents=True)
    (root / "traffic-simulator/traffic_simulator/worker.py").write_text("print('source fixture')\n")
    return root


@pytest.fixture
def git_tree(source_tree, monkeypatch):
    """The fixture tree as an uncommitted git work tree, isolated from user and system git config."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    # Fixed git arguments with no shell; the path is this test's own temporary folder.
    subprocess.run(["git", "init", "-q", str(source_tree)], check=True, capture_output=True,  # nosec B603
                   timeout=60)
    return source_tree


def bundle_names(path):
    with zipfile.ZipFile(path) as archive:
        return set(archive.namelist())


def test_bundle_contains_contracts_and_excludes_runtime_credentials_and_dependencies(source_tree):
    for name in (".runtime/recording.json", ".env", "traffic-simulator/traffic_simulator/.env.local",
                 "traffic-simulator/traffic_simulator/.aws/credentials",
                 "traffic-simulator/traffic_simulator/__pycache__/worker.pyc",
                 "ui/scripts/node_modules/secret.txt", "traffic-simulator/traffic_simulator/credentials.json",
                 "broadcastify/recordings/private.py", "broadcastify/.env", "firetruck-simulator/.venv/x.py"):
        path = source_tree / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("DO_NOT_PACKAGE")
    bundle = package_source(source_tree, git=False)
    with zipfile.ZipFile(bundle) as archive:
        names = set(archive.namelist())
        assert set(REQUIRED_FILES) <= names
        assert "traffic-simulator/traffic_simulator/worker.py" in names
        assert all(b"DO_NOT_PACKAGE" not in archive.read(name) for name in names)


def test_bundle_is_reproducible_when_only_source_mtime_changes(source_tree):
    path = package_source(source_tree, git=False)
    before = path.read_bytes()
    for source in collect_sources(source_tree, git=False):
        os.utime(source, (1900000000, 1900000000))
    assert package_source(source_tree, git=False).read_bytes() == before
    (source_tree / "traffic-simulator/traffic_simulator/worker.py").write_text("print('changed source')\n")
    assert package_source(source_tree, git=False).read_bytes() != before


def test_missing_generated_contract_blocks_packaging_before_replacing_bundle(source_tree):
    existing = package_source(source_tree, git=False)
    before = existing.read_bytes()
    (source_tree / "schemas/simulation/openapi.json").unlink()
    with pytest.raises(ValueError, match="openapi.json"):
        package_source(source_tree, git=False)
    assert existing.read_bytes() == before


def test_source_symlink_cannot_package_a_file_outside_the_repository(source_tree, tmp_path):
    outside = tmp_path / "private.txt"
    outside.write_text("private")
    (source_tree / "traffic-simulator/traffic_simulator/link.py").symlink_to(outside)
    with pytest.raises(ValueError, match="symlink or external"):
        package_source(source_tree, git=False)


def test_unreviewed_lock_change_blocks_packaging(source_tree):
    (source_tree / "ui/package.json").write_text(json.dumps({"dependencies": {"unexpected": "1"}}))
    with pytest.raises(RuntimeError, match="does not match"):
        package_source(source_tree, git=False)


def test_python_pin_that_disagrees_with_the_repository_lock_blocks_packaging(source_tree):
    (source_tree / "backend/requirements-api.txt").write_text("fastapi==0.115.0\n")
    with pytest.raises(RuntimeError, match="fastapi==0.115.0"):
        package_source(source_tree, git=False)


def test_git_ignored_files_stay_out_of_the_bundle_and_untracked_source_stays_in(git_tree):
    (git_tree / ".gitignore").write_text("*.pem\nlocal_settings.py\n")
    for name in ("traffic-simulator/traffic_simulator/token.pem", "firetruck-simulator/api/local_settings.py"):
        (git_tree / name).parent.mkdir(parents=True, exist_ok=True)
        (git_tree / name).write_text("DO_NOT_PACKAGE")
    (git_tree / "traffic-simulator/traffic_simulator/new_module.py").write_text("print('untracked source')\n")
    names = bundle_names(package_source(git_tree, git_tree.parent / "release.zip"))
    assert {"traffic-simulator/traffic_simulator/worker.py", "traffic-simulator/traffic_simulator/new_module.py",
            *REQUIRED_FILES} <= names
    assert not {"traffic-simulator/traffic_simulator/token.pem", "firetruck-simulator/api/local_settings.py"} & names
    # The same files are bundled when a caller opts out of git (temporary fixture roots only).
    assert "traffic-simulator/traffic_simulator/token.pem" in bundle_names(package_source(git_tree, git=False))


def test_required_file_ignored_by_git_blocks_packaging(git_tree):
    (git_tree / ".gitignore").write_text("cdk.json\n")
    with pytest.raises(ValueError, match="ignored by git: deployment/infra/cdk.json"):
        package_source(git_tree)


def test_cdk_context_cache_naming_the_account_is_ignored_and_never_bundled(git_tree):
    """The CDK lookup cache names the AWS account (D11): git ignores it and the release bundle leaves it out."""
    assert "deployment/infra/cdk.context.json" in (REPOSITORY / ".gitignore").read_text().splitlines()
    assert not any("cdk.context" in name for name in REQUIRED_FILES)
    (git_tree / ".gitignore").write_text("deployment/infra/cdk.context.json\n")
    (git_tree / "deployment/infra/cdk.context.json").write_text('{"availability-zones:account=111111111111": []}')
    assert "deployment/infra/cdk.context.json" not in bundle_names(package_source(git_tree, git_tree.parent / "r.zip"))


def test_release_bundle_requires_a_git_work_tree(source_tree, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(source_tree.parent))
    with pytest.raises(ValueError, match="not a git work tree"):
        package_source(source_tree)
    with pytest.raises(ValueError, match="not a git work tree"):
        collect_sources(source_tree)


def quoted_commands(prefix):
    """Shell commands in the CodeBuild buildspec (deployment/infra/lib/stacks.ts) that start with prefix."""
    commands = []
    for line in BUILDSPEC.read_text().splitlines():
        line = line.strip().rstrip(",")
        if len(line) > 1 and line[0] in "'\"" and line[-1] == line[0] and line[1:].startswith(prefix):
            commands.append(shlex.split(line[1:-1]))
    return commands


def image_builds():
    """Map each Dockerfile the image build uses to its build context."""
    builds = {dockerfile: context for dockerfile, context, _ in COMPONENTS.values()}
    builds["deployment/infra/Dockerfile"] = "."
    assert {"traffic-simulator/Dockerfile", "backend/Dockerfile.api", "deployment/infra/Dockerfile",
            "firetruck-simulator/Dockerfile", "broadcastify/monitor/Dockerfile",
            "fire-risk-modeler/Dockerfile"} <= builds.keys(), builds
    return builds


def test_runtime_image_context_keeps_release_tests_out_and_its_own_checks_in():
    """DEPL-3: the buildspec mounts release-tests/ read-only, so the Runtime image never copies them."""
    with zipfile.ZipFile(io.BytesIO(runtime_source(REPOSITORY))) as source:
        names = source.namelist()
        rules = dockerignore_rules(source.read(".dockerignore").decode())
    tests = [name for name in names if name.startswith("release-tests/")]
    assert tests and not [name for name in tests if dockerignore_admits(rules, name)]
    # DEPL-10: the Runtime's release suites travel only in release-tests/, never in the image context.
    assert "release-tests/backend/runtime/tests/checks_support.py" in names
    assert not [name for name in names if name.startswith("tests/")]
    # W2.3: no Runtime check reads radio code; the radio suites run in the radio image.
    assert not [name for name in names if name.startswith("release-tests/broadcastify/")]
    # The image's own release gate (check_startup.py with schema_models.py) validates the built image itself.
    for name in ("check_startup.py", "schema_models.py", "wfm_agui_entrypoint.py", "requirements.txt",
                 "requirements-lock.txt", "schemas/api-schema.yaml"):
        assert name in names and dockerignore_admits(rules, name), name


def test_runtime_source_leaves_out_files_that_git_ignores(tmp_path, monkeypatch):
    """DEPL-3: a git-ignored local file, such as .bedrock_agentcore.yaml with account details, stays out."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    root = tmp_path / "checkout"
    for name in ("backend/runtime/wfm_agui_entrypoint.py", "backend/runtime/Dockerfile",
                 "backend/runtime/requirements.txt", "backend/runtime/.bedrock_agentcore.yaml",
                 "backend/gateway/geocode/handler.py", "backend/gateway/geocode/local_settings.py",
                 "schemas/api-schema.yaml", "requirements-lock.txt", "deployment/validate_infra_baseline.py"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture\n")
    (root / ".gitignore").write_text(".bedrock_agentcore.yaml\nlocal_settings.py\n")
    # Fixed git arguments with no shell; the path is this test's own temporary folder.
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True, timeout=60)  # nosec B603
    names = set(zipfile.ZipFile(io.BytesIO(runtime_source(root))).namelist())
    assert {"wfm_agui_entrypoint.py", "release-tests/backend/gateway/geocode/handler.py"} <= names
    assert not [name for name in names if name.endswith((".bedrock_agentcore.yaml", "local_settings.py"))]
    (root / ".gitignore").write_text("validate_infra_baseline.py\n")
    with pytest.raises(ValueError, match="ignored by git: deployment/validate_infra_baseline.py"):
        runtime_source(root)


def test_dockerignore_rules_follow_last_match_and_parent_directories():
    rules = ["*", "!traffic-simulator/requirements.txt", "!schemas/", "**/__pycache__"]
    assert dockerignore_admits(rules, "traffic-simulator/requirements.txt")
    assert dockerignore_admits(rules, "schemas/simulation/openapi.json")
    assert not dockerignore_admits(rules, "schemas/__pycache__/x.pyc")
    assert not dockerignore_admits(rules, "traffic-simulator/requirements-dev.txt")
    assert not dockerignore_admits(rules, ".env")

def test_docker_copy_globs_include_only_matching_files_and_preserve_the_image_folder():
    members = {"backend/app.py", "backend/api_settings.py", "backend/Dockerfile.api",
               "backend/evacuation/routes.py"}
    assert under(members, "backend/*.py") == ["backend/api_settings.py", "backend/app.py"]
    _, layout = image_layout(["WORKDIR /opt/app", "COPY backend/*.py ./backend/"], [], members)
    assert layout == {"backend/api_settings.py": "/opt/app/backend/api_settings.py",
                      "backend/app.py": "/opt/app/backend/app.py"}


@pytest.fixture(scope="module")
def release_bundle(tmp_path_factory):
    """The real repository release archive, built once for the Docker input checks."""
    path = package_source(REPOSITORY, tmp_path_factory.mktemp("release") / "release.zip")
    with zipfile.ZipFile(path) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def test_real_archive_contains_every_docker_copy_input(release_bundle):
    """Check the distributable archive, not just files present in the developer checkout."""
    # Folder contexts receive files the buildspec copies in before docker build (the repository lock).
    supplied = {destination: source for _, source, destination in quoted_commands("cp ")}
    for dockerfile, context in image_builds().items():
        for source in copy_sources(release_bundle[dockerfile].decode()):
            path = posixpath.normpath(posixpath.join(context, source))
            assert under(release_bundle, supplied.get(path, path)), (dockerfile, source)


def test_repository_root_dockerignore_allowlists_admit_every_copy_input(release_bundle):
    checked = []
    for dockerfile, context in image_builds().items():
        ignore = dockerfile + ".dockerignore"
        if context != "." or ignore not in release_bundle:
            continue
        rules = dockerignore_rules(release_bundle[ignore].decode())
        for source in copy_sources(release_bundle[dockerfile].decode()):
            files = under(release_bundle, posixpath.normpath(source))
            rejected = [name for name in files if not dockerignore_admits(rules, name)]
            assert files and not rejected, (dockerfile, source, rejected)
        checked.append(dockerfile)
    assert {"traffic-simulator/Dockerfile", "backend/Dockerfile.api", "fire-risk-modeler/Dockerfile",
            "deployment/infra/Dockerfile"} <= set(
        checked)


# How CDK code (deployment/infra/{bin,lib,test}/*.ts) reaches a repository file (F1): an fs read, existence check or
# folder listing, a synth-time asset or template include, a computed module path, or a JSON module. A path counts
# only in a form a check can derive from the source: join(__dirname, ...) or resolve(__dirname, ...), with or without
# "path.", of string literals only and as the whole argument (F4); or a JSON module's relative specifier, which Node
# resolves from the importing file's folder.
LITERAL = r""""[^"\\]*"|'[^'\\]*'"""
FILE_CALL = re.compile(rf"\b(readFileSync|existsSync|readdirSync|readFile|\w*Asset\w*|CfnInclude)\s*\(|"
                       rf"\b(require|import)\s*\((?!\s*(?:{LITERAL})\s*\))")
DIRNAME_PATH = re.compile(
    rf"\s*(?:path\.)?(?:join|resolve)\(\s*__dirname((?:\s*,\s*(?:{LITERAL}))+)\s*,?\s*\)(?=\s*[,\])])")
JSON_MODULE = re.compile(rf"(?:\bfrom|\b(?:require|import)\s*\()\s*({LITERAL})")
# An fs import that renames, defaults or namespaces the module, or loads it with require, would let a call escape
# FILE_CALL, so it is a problem in itself.
FS_MODULE = r"""["'](?:node:)?fs(?:/promises)?["']"""
FS_IMPORT = re.compile(rf"""\bimport\s+([^;"']+?)\s+from\s*{FS_MODULE}|\b(?:require|import)\s*\(\s*{FS_MODULE}\s*\)""")
# Calls whose paths their source cannot show, with the repository paths each reaches. stacks.test.ts joins these to
# the repository root, so the test checks each where it resolves from that file in the image (F2: /build/<path>).
LISTED_ACCESSES = {}


def repository_file_accesses(name, text):
    """Find the repository paths a CDK source file reaches, as ([(file:line, call, path)], [problem]).

    A call whose path the source cannot show is a problem with file and line, unless LISTED_ACCESSES names its paths.
    """
    folder, accesses, problems = posixpath.dirname(name), [], []

    def where(match):
        line = text.count("\n", 0, match.start()) + 1
        return f"{name}:{line}"

    for found in FS_IMPORT.finditer(text):
        if found[1] is None or not re.fullmatch(r"\{[^}]*\}", found[1]) or re.search(r"\bas\b", found[1]):
            problems.append(f"{where(found)}: import fs functions by their own names, so this check sees each call")
    for found in FILE_CALL.finditer(text):
        call, path = found[1] or found[2], DIRNAME_PATH.match(text, found.end())
        if path:
            parts = [literal[1:-1] for literal in re.findall(LITERAL, path[1])]
            accesses.append((where(found), call, posixpath.normpath(posixpath.join(folder, *parts))))
        elif (name, call) in LISTED_ACCESSES:
            accesses += [(where(found), call, listed) for listed in LISTED_ACCESSES[name, call]]
        else:
            problems.append(f"{where(found)}: {call}'s path is not join(__dirname, string literals); derive it or "
                            "list it in LISTED_ACCESSES")
    # A Python subprocess helper is another repository dependency, not a local .venv executable.
    for found in re.finditer(r"\bexecFileSync\s*\(", text):
        invocation = re.match(r'\s*["\']python3["\']\s*,\s*\[\s*', text[found.end():])
        path = DIRNAME_PATH.match(text, found.end() + invocation.end()) if invocation else None
        if path:
            parts = [literal[1:-1] for literal in re.findall(LITERAL, path[1])]
            accesses.append((where(found), "Python helper", posixpath.normpath(posixpath.join(folder, *parts))))
        else:
            problems.append(f"{where(found)}: execFileSync must use python3 and a literal __dirname helper path "
                            "available in the verification image")
    for found in JSON_MODULE.finditer(text):
        specifier = found[1][1:-1]
        if specifier.startswith(".") and specifier.endswith(".json"):
            accesses.append((where(found), "a JSON module", posixpath.normpath(posixpath.join(folder, specifier))))
    return accesses, problems


@pytest.mark.parametrize("text,accesses,problems", [
    ('readFileSync(join(__dirname, "../cdk.json"), "utf8");', [(1, "readFileSync", "deployment/infra/cdk.json")], []),
    ('readdirSync(path.join(__dirname, "fixtures"));', [(1, "readdirSync", "deployment/infra/test/fixtures")], []),
    ('await readFile(resolve(__dirname, "x.json"));', [(1, "readFile", "deployment/infra/test/x.json")], []),
    ('lambda.Code.fromAsset(join(__dirname, "../lambda"));', [(1, "fromAsset", "deployment/infra/lambda")], []),
    # F4: a multi-line join may end with a comma, and it must be the whole argument.
    ('\nreadFileSync(join(\n  __dirname,\n  "../../change_sets.py",\n), "utf8");',
     [(2, "readFileSync", "deployment/change_sets.py")], []),
    ('readFileSync(join(__dirname, "a") + ".bak");', [], [1]),
    ('import contract from "../../../schemas/x.json";\nconst data = require("./data.json");',
     [(1, "a JSON module", "schemas/x.json"), (2, "a JSON module", "deployment/infra/test/data.json")], []),
    ('existsSync(join(repository, source));', [], [1]),
    ('new CfnInclude(this, "Template", { templateFile: "template.yaml" });', [], [1]),
    ('new DockerImageAsset(this, "Image", { directory: join(__dirname, "..") });', [], [1]),
    ("const loaded = require(modulePath);", [], [1]),
    ('import { readFileSync as read } from "node:fs";\nread(join(__dirname, "x"));', [], [1]),
    ('import * as fs from "fs";\nimport fs2 from "fs/promises";\nconst { readFileSync: r } = require("node:fs");',
     [], [1, 2, 3]),
    (('import { existsSync, readFileSync } from "node:fs";\nimport { App } from "aws-cdk-lib";\n'
      'const cdk = require("aws-cdk-lib");'), [], []),
    ('execFileSync("python3", [join(__dirname, "worker_settings.py")], { encoding: "utf8" });',
     [(1, "Python helper", "deployment/infra/test/worker_settings.py")], []),
    ('execFileSync(join(__dirname, "../../../.venv/bin/python"), [join(__dirname, "worker_settings.py")]);',
     [], [1]),
    ('execFileSync("python3", [join(__dirname, helper)]);', [], [1]),
])
def test_repository_file_accesses_derive_each_path_or_report_file_and_line(text, accesses, problems):
    """F1, F4: every way CDK code reaches a repository file is found; an underivable path fails closed."""
    name = "deployment/infra/test/example.test.ts"
    found, reported = repository_file_accesses(name, text)
    assert [(int(place.rsplit(":", 1)[1]), call, path) for place, call, path in found] == accesses
    assert [problem.split(": ", 1)[0] for problem in reported] == [f"{name}:{line}" for line in problems]


def test_a_listed_call_reaches_exactly_its_listed_paths(monkeypatch):
    """F2: the call LISTED_ACCESSES names reaches its listed paths, which the image check then follows."""
    name = "deployment/infra/test/stacks.test.ts"
    paths = ("firetruck-simulator/tests", "broadcastify/monitor/test_private.py")
    monkeypatch.setitem(LISTED_ACCESSES, (name, "existsSync"), paths)
    assert repository_file_accesses(name, "\n    assert.ok(existsSync(join(repository, source)));") == (
        [(f"{name}:2", "existsSync", path) for path in LISTED_ACCESSES[name, "existsSync"]], [])


def test_infra_verification_image_has_every_repository_file_the_cdk_code_reaches(release_bundle):
    """The CDK checks reach repository files that the verification image must carry where the checks look.

    Each file a path reaches (F1) must ship in the bundle and be copied to where that path resolves from the reaching
    file in the image (phase 6 release attempt 1: logical-ids.test.ts read deployment/change_sets.py, which neither
    the bundle nor the image had). That includes the service tests whose paths the release gate inspects, though
    those tests run in other images (F2).
    """
    _, layout = image_layout(stage(instructions(release_bundle["deployment/infra/Dockerfile"].decode())),
                             dockerignore_rules(release_bundle["deployment/infra/Dockerfile.dockerignore"].decode()),
                             release_bundle)
    reached, problems = [], []
    for name in sorted(member for member in release_bundle
                       if member.startswith("deployment/infra/") and member.endswith(".ts")):
        accesses, found = repository_file_accesses(name, release_bundle[name].decode())
        problems += found
        home = layout.get(name)
        for place, call, path in accesses:
            files = under(release_bundle, path)
            if not files:
                problems.append(f"{place}: {call} reaches {path}, which the release bundle lacks")
            for file in files:
                # Where the same path resolves in the image, from the reaching file's own place there.
                relative = posixpath.relpath(file, posixpath.dirname(name))
                image = posixpath.normpath(posixpath.join(posixpath.dirname(home), relative)) if home else None
                if image is None or layout.get(file) != image:
                    problems.append(f"{place}: {call} reaches {file}, which the image lacks at {image or 'any path'}")
            reached.append((name, call, path))
    assert not problems, problems
    assert reached, "No repository file access found in the CDK code"
    listed = {(name, call) for name, call, path in reached if path in LISTED_ACCESSES.get((name, call), ())}
    assert listed == set(LISTED_ACCESSES), f"LISTED_ACCESSES entries no call uses: {set(LISTED_ACCESSES) - listed}"


def test_worker_settings_helper_runs_with_only_the_captured_infra_image_files(release_bundle, tmp_path):
    """The AST helper's nested worker sources and interpreter must exist outside the developer checkout."""
    dockerfile = instructions(release_bundle["deployment/infra/Dockerfile"].decode())
    install = next(index for index, line in enumerate(dockerfile)
                   if line.startswith("RUN ") and any(step.strip().startswith("apt-get install ")
                                                     and "python3" in shlex.split(step)
                                                     for step in line[4:].split("&&")))
    assert install < dockerfile.index("USER node"), "The verification image must install its Python interpreter."
    _, layout = image_layout(stage(dockerfile),
                             dockerignore_rules(release_bundle["deployment/infra/Dockerfile.dockerignore"].decode()),
                             release_bundle)
    worker_files = [name for name in release_bundle
                    if name.startswith("traffic-simulator/traffic_simulator/") and name.endswith(".py")]
    assert worker_files, "The captured release must contain the worker Python source."
    for name in worker_files:
        assert layout.get(name) == f"/build/{name}", f"The AST helper's nested source is missing: {name}"
        # Bookworm's Python 3.11 parses source only; the worker still runs in its own Python 3.14 image.
        ast.parse(release_bundle[name], filename=name, feature_version=(3, 11))
    # Reconstruct only the image's Python files. There is no .venv or third-party worker dependency.
    for name, destination in layout.items():
        if not name.endswith(".py"):
            continue
        path = tmp_path / destination.lstrip("/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(release_bundle[name])
    helper = tmp_path / layout["deployment/infra/test/worker_settings.py"].lstrip("/")
    # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
    done = subprocess.run([sys.executable, "-I", str(helper)], cwd=tmp_path,  # nosec B603 - fixed stdlib helper
                          capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    names = json.loads(done.stdout)
    assert names["required"] == names["startup"], "All consumed settings must be validated before queue polling."
    assert "MAX_STUDY_RADIUS_KM" in names["required"], "The gate must find the worker's newly shared radius limit."


def test_api_verification_uses_the_workers_source_built_engine(release_bundle):
    """API integration tests must exercise the worker's verified native engine, not a wheel."""
    dockerfile = release_bundle["backend/Dockerfile.api"].decode()
    assert "ARG SUMO_VERIFY_IMAGE=wildfire-traffic-verification" in dockerfile
    assert "COPY --from=sumo-test-tools /opt/sumo/ /opt/sumo/" in dockerfile
    assert "--requirement traffic-simulator/requirements-sumo.txt" not in dockerfile
    assert GROUPS["platform"] == ("worker", "api")


def test_platform_images_have_health_checks_non_root_users_and_reasoned_scanner_skips():
    """CKV_DOCKER_2/3/4 (G74): a health check or a reasoned skip, a non-root user, and ADD only with a checksum."""
    names = ("backend/Dockerfile.api", "traffic-simulator/Dockerfile", "deployment/infra/Dockerfile",
             "fire-risk-modeler/Dockerfile")
    texts = {name: (REPOSITORY / name).read_text() for name in names}
    skips = {name: dict(re.findall(r"#\s*checkov:skip=(CKV_DOCKER_\d+):\s*(\S.*)", text)) for name, text in texts.items()}
    assert {name: sorted(found) for name, found in skips.items()} == {
        "backend/Dockerfile.api": [], "traffic-simulator/Dockerfile": ["CKV_DOCKER_2", "CKV_DOCKER_4"],
        "deployment/infra/Dockerfile": ["CKV_DOCKER_2"], "fire-risk-modeler/Dockerfile": []}
    assert all(len(reason) > 40 and reason.endswith("(D13)") for found in skips.values() for reason in found.values())
    api = instructions(texts["backend/Dockerfile.api"])
    assert any(line.startswith("HEALTHCHECK ") and "http://127.0.0.1:8000/health" in line for line in api)
    modeler = instructions(texts["fire-risk-modeler/Dockerfile"])
    assert any(line.startswith("HEALTHCHECK ") and "http://127.0.0.1:8085/health" in line for line in modeler)
    for name, text in texts.items():
        lines = instructions(text)
        users = [line.split()[1] for line in lines if line.startswith("USER ")]
        assert users and users[-1].split(":")[0] not in {"root", "0"}, name
        assert all("--checksum=sha256:" in line for line in lines if line.startswith("ADD ")), name
    # The CDK verification build runs as the image's node user in folders it owns.
    infra = instructions(texts["deployment/infra/Dockerfile"])
    assert infra.index("USER node") < min(i for i, line in enumerate(infra) if line.startswith("RUN npm"))
    assert all("--chown=node:node" in line for line in infra if line.startswith("COPY "))


def test_release_archive_contains_the_service_tests_the_image_gates_mount(release_bundle):
    """The firetruck and radio images exclude tests, so the bundle carries the files their gates bind-mount."""
    gates = (REPOSITORY / "deployment/container_build.py").read_text()
    sources = ["firetruck-simulator/tests", *(
        path.relative_to(REPOSITORY).as_posix() for path in (REPOSITORY / "broadcastify/monitor").glob("test_*.py"))]
    for source in sources:
        assert any(name.endswith(".py") for name in under(release_bundle, source)), source
        assert source.rsplit("/", 1)[-1] in gates, source


def test_release_archive_exports_contracts_without_source_checkout(tmp_path):
    """The cloud receives only this archive; contract verification must work after extraction."""
    bundle = package_source(REPOSITORY, tmp_path / "release.zip")
    extracted = tmp_path / "extracted"
    with zipfile.ZipFile(bundle) as archive:
        archive.extractall(extracted)
    # This interpreter runs the bundled generator with fixed arguments and no shell.
    # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
    completed = subprocess.run(  # nosec B603 - fixed argument list, no shell
        [sys.executable, "scripts/export_integration_contracts.py", "--check"], cwd=extracted,
        capture_output=True, text=True, check=False, timeout=60,
        env={**os.environ, "STORAGE_MODE": "aws", "AWS_EC2_METADATA_DISABLED": "true", "PYTHONPATH": ""},
    )
    assert completed.returncode == 0, completed.stderr
    assert not (extracted / ".runtime").exists()


# Runs a release check as `python <script>` does (sys.argv, sys.path[0], __main__) without the schema model
# generator: only the Dockerfile's build stage installs it (RTGW-13), so the image's checks cannot import it.
# Runs `python [options] -m module ...` or `python [options] script ...` with the generator hidden. The options
# go to the interpreter itself, so -P leaves the working directory off sys.path as it does in the image.
WITHOUT_GENERATOR = """import runpy, sys
from pathlib import Path
sys.modules["datamodel_code_generator"] = None
sys.argv = sys.argv[1:]
if sys.argv[0] == "-m":
    sys.argv = sys.argv[1:]
    runpy.run_module(sys.argv[0], run_name="__main__", alter_sys=True)
else:
    if not sys.flags.safe_path:
        sys.path[0] = str(Path(sys.argv[0]).resolve().parent)
    runpy.run_path(sys.argv[0], run_name="__main__")
"""


def test_runtime_release_checks_pass_in_the_image_layout(tmp_path):
    """Run each Runtime image release check in that image's file layout, rebuilt offline from its source.

    A checkout keeps schemas/ at the repository root, so only the image layout shows a check that needs a
    file the image lacks (phase 2 release attempt 7). The layout follows the Dockerfile (the models that the
    build stage generates and the image copies, the context through its .dockerignore, WORKDIR /app) and the
    buildspec's mounts, and the checks run without the generator, as in the image. Each check runs in full,
    as the buildspec runs it; a -h run would hide that failure, because datamodel-code-generator exits 0 on
    -h as soon as a check imports it.
    """
    with zipfile.ZipFile(io.BytesIO(runtime_source(REPOSITORY))) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    rules = dockerignore_rules(files[".dockerignore"].decode())
    dockerfile = instructions(files["Dockerfile"].decode())
    # The image is the last stage; the stages before it build the files that it copies --from them.
    last = max(index for index, line in enumerate(dockerfile) if line.startswith("FROM "))
    build, image = dockerfile[:last], dockerfile[last:]
    assert {"WORKDIR /app", "COPY . ."} <= set(image)
    source, root = tmp_path / "source", tmp_path / "image"
    app = root / "app"
    for name, data in files.items():
        for folder in (source, app) if dockerignore_admits(rules, name) else (source,):
            (folder / name).parent.mkdir(parents=True, exist_ok=True)
            (folder / name).write_bytes(data)
    # The build stage's own generator command in its WORKDIR, reading the schema that it stages in /tmp.
    staged = {line.split()[-1]: str(source / line.split()[1]) for line in build
              if line.startswith("COPY ") and line.split()[-1].startswith("/tmp/")}
    generator = next(step for line in build if line.startswith("RUN ")
                     for step in line[4:].split("&&") if "datamodel_code_generator" in step)
    workdir = root / next(line.split()[1] for line in build if line.startswith("WORKDIR ")).lstrip("/")
    (workdir / "schemas").mkdir(parents=True)
    generated = subprocess.run(  # nosec B603 - the Dockerfile's fixed argument list, no shell
        [sys.executable, *(staged.get(arg, arg) for arg in shlex.split(generator)[1:])], cwd=workdir,
        capture_output=True, text=True, timeout=120, check=False)
    assert generated.returncode == 0, generated.stderr
    # The image receives only the build-stage files that it copies.
    for line in image:
        if line.startswith("COPY --from="):
            origin, target = shlex.split(line)[-2:]
            (app / target).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(root / origin.lstrip("/"), app / target)
    # The image's environment: its ENV lines and no AWS settings or PYTHONPATH.
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "TMPDIR": str(tmp_path), **dict(
        pair.split("=", 1) for line in image if line.startswith("ENV ") for pair in shlex.split(line[4:]))}
    template = (REPOSITORY / "backend/deployment/templates/nested/4-agentcore-resources.yaml").read_text()
    runs = [shlex.split(line) for line in re.findall(r"^ +- (docker run .+)$", template, re.MULTILINE)]
    # The arguments after the image go to the entrypoint, python.
    commands = [args[args.index("--entrypoint") + 3:] for args in runs]
    tests = "/release-tests/backend/runtime/tests"
    assert [args[args.index("--entrypoint") + 1] for args in runs] == ["python", "python"]
    assert commands == [["-P", "-m", "unittest", "discover", "-s", tests, "-t", tests, "-v"],
                        ["/app/check_startup.py"]]
    for args, command in zip(runs, commands, strict=True):
        # A bind mount that the run declares is copied into place.
        for mount in (args[index + 1] for index, option in enumerate(args) if option == "--mount"):
            fields = dict(part.partition("=")[::2] for part in mount.split(","))
            shutil.copytree(fields["source"].replace("$CODEBUILD_SRC_DIR", str(source)),
                            root / fields["target"].lstrip("/"), dirs_exist_ok=True)
        start = next(index for index, arg in enumerate(command) if arg == "-m" or not arg.startswith("-"))
        rest = [str(root / arg.lstrip("/")) if arg.startswith("/") else arg for arg in command[start:]]
        done = subprocess.run(  # nosec B603 - interpreter arguments from the buildspec, no shell
            [sys.executable, *command[:start], "-c", WITHOUT_GENERATOR, *rest], cwd=app, env=env,
            capture_output=True, text=True, timeout=120, check=False)
        assert done.returncode == 0, (command, done.stderr[-4000:])


def test_every_runtime_release_check_runs_under_unittest_discover():
    """S-F1: the Runtime image runs backend/runtime/tests with unittest discover, which collects test*.py files and
    TestCase methods only, while check_quality.py runs the folder with pytest. A module-level test function would
    pass locally and never run in the image gate."""
    checks = sorted(path for path in (REPOSITORY / "backend/runtime/tests").glob("*.py")
                    if path.name != "checks_support.py")
    unnamed = [path.name for path in checks if not path.match("test*.py")]
    functions = [f"{path.name}:{node.lineno} {node.name}"
                 for path in checks for node in ast.parse(path.read_text()).body
                 if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith("test")]
    assert checks and not unnamed and not functions, (
        f"unittest discover skips these; name each check file test*.py and make each test a TestCase method: "
        f"{unnamed + functions}")
