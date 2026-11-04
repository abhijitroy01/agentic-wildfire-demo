"""Python and npm lock verification (parsing, image requirement coverage, fail-closed errors) and image bases."""
import json
import re
import shlex
from functools import cache
from importlib.metadata import PackageNotFoundError, distribution
from itertools import pairwise, takewhile
from pathlib import Path
from types import SimpleNamespace

import pytest
from dockerfile_layout import instructions
from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.version import Version

from deployment.check_lockfiles import (
    PYTHON_REQUIREMENTS,
    check,
    locked_versions,
    normalize,
    requirement_lines,
    requirements,
)
from deployment.package_source import git_files

REPOSITORY = Path(__file__).resolve().parents[2]
# Build context of every image that installs Python requirements (deployment/infra/lib/stacks.ts builds the
# first five; the backend Runtime build uses backend/runtime as its context).
IMAGE_CONTEXTS = {
    "traffic-simulator/Dockerfile": "", "backend/Dockerfile.api": "",
    "fire-risk-modeler/Dockerfile": "",
    "firetruck-simulator/Dockerfile": "firetruck-simulator", "broadcastify/monitor/Dockerfile": "broadcastify",
    "backend/runtime/Dockerfile": "backend/runtime",
}
# The images run Linux: the Runtime on arm64 (backend/runtime/Dockerfile), ECS tasks and Lambda packages on x86_64.
LINUX = [{**default_environment(), "os_name": "posix", "sys_platform": "linux", "platform_system": "Linux",
          "platform_machine": machine} for machine in ("aarch64", "x86_64")]
# An image build tool that the development environment does not install (scripts/bootstrap-dev.sh), so the closure
# cannot read its metadata, at the one version whose dependencies were checked by hand: uv's wheels declare none
# (PyPI requires_dist for 0.11.29, 2026-10-18). A lock bump fails the closure test until the new pin is checked too.
UNINSTALLED_TOOLS = {"uv": "0.11.29"}


def file_requirements(path, root=REPOSITORY):
    """The Requirements of a requirements file and its -r includes, read as check() reads them (N2)."""
    return [Requirement(line) for _, line in requirement_lines(path, root)]


def pip_installs(dockerfile):
    """The arguments of each pip install and uv pip install command in a Dockerfile, up to the next shell operator."""
    for line in instructions(dockerfile):
        words = shlex.split(line) if "pip install" in line else []
        for index, pair in enumerate(pairwise(words)):
            if pair == ("pip", "install"):
                yield list(takewhile(lambda word: word not in {"&&", "||", ";", "|"}, words[index + 2:]))


def by_name_requirements(dockerfile):
    """The packages a Dockerfile's pip installs name directly, not through a -r requirements or -c constraints file."""
    names = []
    for arguments in pip_installs(dockerfile):
        files = {index + 1 for index, word in enumerate(arguments)
                 if word in {"-r", "--requirement", "-c", "--constraint", "--find-links", "--wheel-dir"}}
        names += [word for index, word in enumerate(arguments) if index not in files and not word.startswith("-")]
    return names


def applies(requirement, extras=("",)):
    """Whether a requirement's marker holds on a Linux image, for the base install or any requested extra."""
    return requirement.marker is None or any(requirement.marker.evaluate({**environment, "extra": extra})
                                             for environment in LINUX for extra in extras)


@cache
def requires(name):
    """The Requires-Dist entries of an installed distribution, or None when it is not installed here."""
    try:
        return [Requirement(text) for text in distribution(name).requires or ()]
    except PackageNotFoundError:
        return None


def closure(declared):
    """Map each package the declared Requirements install on Linux, dependencies and extras followed, to the chain
    that pulls it in; also return the packages whose metadata is not installed here, so cannot be followed."""
    chains, walked, unreadable = {}, set(), set()
    pending = [(requirement, ()) for requirement in declared if applies(requirement)]
    while pending:
        requirement, chain = pending.pop()
        name, extras = normalize(requirement.name), sorted(map(normalize, requirement.extras))
        chains.setdefault(name, chain)
        new = [extra for extra in ("", *extras) if (name, extra) not in walked]
        if not new:
            continue
        walked.update((name, extra) for extra in new)
        if requires(name) is None:
            unreadable.add(name)
            continue
        link = f"{name}[{','.join(extras)}]" if extras else name
        pending += [(dependency, (*chain, link)) for dependency in requires(name) if applies(dependency, new)]
    return chains, unreadable


def version_mismatches(chains, locked, distribution=distribution):
    """Name each walked package installed at another version than its lock pin (F2): the walk reads the installed
    metadata, so a stale .venv would follow an older release's dependencies and miss a new one."""
    stale = []
    for package in sorted(chains.keys() & locked.keys()):
        try:
            installed = distribution(package).version
        except PackageNotFoundError:
            continue  # closure() reports a package that is not installed here
        if Version(installed) != Version(locked[package]):
            stale.append(f"{package}: installed {installed}, requirements-lock.txt pins {locked[package]}; "
                         "run scripts/bootstrap-dev.sh to install the lock")
    return stale


@pytest.fixture
def lock_tree(tmp_path):
    """A minimal checkout: every listed requirements file, a lock pinning fastapi, and matching npm locks."""
    for name in PYTHON_REQUIREMENTS:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text("# fixture\n")
    (tmp_path / "requirements-lock.txt").write_text("fastapi==0.141.1\nPyJWT==2.14.0\n")
    for folder in ("ui", "deployment/infra"):
        (tmp_path / folder).mkdir(parents=True, exist_ok=True)
        (tmp_path / folder / "package.json").write_text(json.dumps({"dependencies": {}}))
        (tmp_path / folder / "package-lock.json").write_text(json.dumps({"packages": {"": {"dependencies": {}}}}))
    return tmp_path


def parse(tmp_path, text):
    path = tmp_path / "requirements.txt"
    path.write_text(text)
    return [(name, pin) for _, name, pin in requirements(path, tmp_path)]


def test_repository_requirements_are_all_pinned_by_the_lock():
    check(REPOSITORY)


def test_the_lock_pins_everything_each_requirements_file_and_image_installs_on_linux():
    """WAPI-14 (review H1): a constraints file binds only the packages it names, so the lock must pin each file's
    whole Linux closure, read from the installed metadata with extras (mcp's pyjwt[crypto] needs cryptography).
    F2: the walk also starts from the packages a Dockerfile installs by name (the Runtime models stage's generator),
    and that metadata must be the locked version's, so a stale .venv cannot hide a new dependency."""
    locked = locked_versions(REPOSITORY / "requirements-lock.txt", REPOSITORY)
    sources = {name: file_requirements(REPOSITORY / name) for name in PYTHON_REQUIREMENTS}
    sources |= {name: [Requirement(word) for word in by_name_requirements((REPOSITORY / name).read_text())]
                for name in IMAGE_CONTEXTS}
    unpinned, unreadable, stale = [], set(), set()
    for name, declared in sources.items():
        chains, missing = closure(declared)
        unpinned += [f"{name}: {' -> '.join((*chain, package))}" for package, chain in sorted(chains.items())
                     if package not in locked]
        unreadable |= missing
        stale.update(version_mismatches(chains, locked))
    exempt = {name for name, version in UNINSTALLED_TOOLS.items() if locked.get(name) == version}
    assert not stale, "\n".join(sorted(stale))
    assert not unpinned, "requirements-lock.txt does not pin:\n" + "\n".join(unpinned)
    assert unreadable == exempt, (f"not installed here, so their dependencies were not checked: "
                                  f"{sorted(unreadable - exempt)}; drop the UNINSTALLED_TOOLS entries now installed "
                                  f"here or installed by no image: {sorted(exempt - unreadable)}")


def test_the_closure_reads_continued_and_hashed_requirements_as_check_does(tmp_path):
    """N2: the closure reads a requirements file through check_lockfiles, which joins continuations and drops
    --hash options and comments, so a file that check() accepts never breaks the closure test."""
    (tmp_path / "requirements.txt").write_text(
        "# fixture\npackaging==26.3 \\\n    --hash=sha256:abc \\\n    --hash=sha256:def  # reviewed\n")
    chains, unreadable = closure(file_requirements(tmp_path / "requirements.txt", tmp_path))
    assert "packaging" in chains and not unreadable


def test_by_name_requirements_read_the_models_stage():
    """F2: uv and the model generator reach the Runtime build stage by name; options and -r/-c files are not names."""
    dockerfile = (REPOSITORY / "backend/runtime/Dockerfile").read_text()
    assert [Requirement(word).name for word in by_name_requirements(dockerfile)] == [
        "uv", "datamodel-code-generator", "uv"]
    fixture = ("# RUN pip install commented==1\nRUN pip install --no-cache-dir -r a.txt --constraint b.txt \\\n"
               "    httpx==0.28.1 && uv pip install --requirement c.txt -c b.txt boto3 && echo pip\n")
    assert by_name_requirements(fixture) == ["httpx==0.28.1", "boto3"]


def test_version_mismatches_name_the_package_both_versions_and_bootstrap():
    """F2: a package installed at another version than its pin fails the closure test, naming both versions."""
    installed = {"fastapi": "0.141.1", "pyjwt": "2.13.0"}

    def fake_distribution(name):
        if name not in installed:
            raise PackageNotFoundError(name)
        return SimpleNamespace(version=installed[name])

    chains = {"fastapi": (), "pyjwt": ("mcp",), "uv": (), "unlocked": ()}
    locked = {"fastapi": "0.141.1", "pyjwt": "2.14.0", "uv": "0.11.29"}
    assert version_mismatches(chains, locked, fake_distribution) == [
        "pyjwt: installed 2.13.0, requirements-lock.txt pins 2.14.0; run scripts/bootstrap-dev.sh to install the lock"]


def test_every_requirements_file_an_image_installs_is_checked():
    installed = set()
    for dockerfile, context in IMAGE_CONTEXTS.items():
        for arguments in pip_installs((REPOSITORY / dockerfile).read_text()):
            for flag, value in pairwise(arguments):
                if flag in {"-r", "--requirement"}:
                    installed.add(str(Path(context, value).as_posix()).removeprefix("./"))
    assert installed and installed <= set(PYTHON_REQUIREMENTS), installed - set(PYTHON_REQUIREMENTS)


def test_every_dockerfile_builds_from_ecr_public_or_the_verified_worker():
    """External bases use ECR Public or validated native inputs; scratch only packages compiler outputs."""
    dockerfiles = sorted(path for path in git_files(REPOSITORY)
                         if path.name in {"Dockerfile", "Dockerfile.api"} and (REPOSITORY / path).is_file())
    assert set(IMAGE_CONTEXTS) <= {path.as_posix() for path in dockerfiles}
    for dockerfile in dockerfiles:
        stages = set()
        text = (REPOSITORY / dockerfile).read_text()
        for line in text.splitlines():
            words = line.split()
            if not words or words[0].upper() != "FROM":
                continue
            image, *alias = [word for word in words[1:] if not word.startswith("--")]
            verified_worker = (dockerfile.as_posix() == "backend/Dockerfile.api"
                               and image == "${SUMO_VERIFY_IMAGE}"
                               and "ARG SUMO_VERIFY_IMAGE=wildfire-traffic-verification" in text)
            native = {
                "traffic-simulator/Dockerfile": ("SUMO_NATIVE_IMAGE", "sumo-native-artifact"),
                "fire-risk-modeler/Dockerfile": ("PYRO_WHEELS_IMAGE", "pyro-native-artifact"),
            }.get(dockerfile.as_posix())
            verified_native = native and (
                image == "${" + native[0] + "}" and f"ARG {native[0]}={native[1]}" in text
                or image == "scratch" and alias == ["AS", native[1]])
            assert image.lower() in stages or image.startswith("public.ecr.aws/") or verified_worker or verified_native, (
                dockerfile.as_posix(), image)
            if len(alias) == 2 and alias[0].upper() == "AS":
                stages.add(alias[1].lower())


def test_runtime_image_generates_models_in_a_build_stage_with_the_locked_generator():
    """RTGW-13: the model generator runs only in a build stage; the runtime stage copies its output."""
    text = (REPOSITORY / "backend/runtime/Dockerfile").read_text()
    locked = {name: version for _, name, version in requirements(REPOSITORY / "requirements-lock.txt", REPOSITORY)}
    assert re.findall(r"\bdatamodel-code-generator==(\S+)", text) == [locked["datamodel-code-generator"]]
    assert "COPY --from=models /build/schemas/generated_models.py schemas/generated_models.py" in text
    runtime = {name for _, name, _ in requirements(REPOSITORY / "backend/runtime/requirements.txt", REPOSITORY)}
    assert "datamodel-code-generator" not in runtime


def test_runtime_dockerfile_uv_pin_equals_the_lock():
    """The Runtime image's uv build tool pin matches requirements-lock.txt, so the two cannot drift."""
    pins = re.findall(r"\buv==(\S+)", (REPOSITORY / "backend/runtime/Dockerfile").read_text())
    locked = {name: version for _, name, version in requirements(REPOSITORY / "requirements-lock.txt", REPOSITORY)}
    assert pins and set(pins) == {locked["uv"]}, (pins, locked.get("uv"))


@pytest.mark.parametrize("text,expected", [
    ("fastapi == 1.0\n", [("fastapi", "1.0")]),
    ("   indented==2.0   \n", [("indented", "2.0")]),
    ("PyJWT[crypto]==2.14.0\n", [("pyjwt", "2.14.0")]),
    ("uvicorn[standard] >= 0.32.0\n", [("uvicorn", None)]),
    ("mcp>=1.28.1,<2\n", [("mcp", None)]),
    ("typing_extensions>=4.7 ; python_version < '3.13'\n", [("typing-extensions", None)]),
    ("Foo.Bar==1.0; sys_platform == 'linux'  # reviewed\n", [("foo-bar", "1.0")]),
    ("numpy==2.*\n", [("numpy", None)]),
    ("httpx\n# comment only\n\n", [("httpx", None)]),
    ("pkg==1.0 \\\n    --hash=sha256:abc \\\n    --hash=sha256:def\n", [("pkg", "1.0")]),
])
def test_requirement_lines_parse_to_normalized_names_and_exact_pins(tmp_path, text, expected):
    assert parse(tmp_path, text) == expected


@pytest.mark.parametrize("line", [
    "-c other.txt", "--constraint=other.txt", "--index-url https://example.invalid/simple",
    "--extra-index-url https://example.invalid/simple", "-e .", "pkg @ https://example.invalid/pkg.whl",
    "https://example.invalid/pkg.whl", "./local-package", "pkg==1.0 --global-option=x",
])
def test_unsupported_pip_lines_fail_closed(tmp_path, line):
    with pytest.raises(RuntimeError, match="unsupported pip option|cannot be checked"):
        parse(tmp_path, line + "\n")


def test_requirement_includes_are_followed_relative_to_the_including_file(tmp_path):
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested/base.txt").write_text("boto3>=1.39\n")
    assert parse(tmp_path, "-r nested/base.txt\nhttpx==0.28.1\n") == [("boto3", None), ("httpx", "0.28.1")]
    (tmp_path / "nested/base.txt").write_text("-r ../requirements.txt\n")
    with pytest.raises(RuntimeError, match="includes itself"):
        parse(tmp_path, "--requirement=nested/base.txt\n")


def test_lock_check_passes_for_pinned_and_ranged_requirements(lock_tree):
    (lock_tree / "backend/runtime/requirements.txt").write_text("fastapi>=0.115.0\nPyJWT[crypto]==2.14.0\n")
    check(lock_tree)


@pytest.mark.parametrize("name,text,message", [
    ("backend/runtime/requirements.txt", "unlocked-package>=1.0\n", "requires unlocked-package but"),
    ("firetruck-simulator/requirements.txt", "python-multipart>=0.0.21\n", "requires python-multipart but"),
    ("broadcastify/monitor/requirements.txt", "fastapi==0.115.0\n", "pins fastapi==0.115.0"),
    ("requirements-lock.txt", "fastapi>=0.141\n", "must pin fastapi with =="),
    ("requirements-lock.txt", "fastapi==0.141.1\nFastAPI==0.115.0\n", "pins fastapi twice"),
])
def test_unlocked_or_mismatched_requirements_block_release(lock_tree, name, text, message):
    (lock_tree / name).write_text(text)
    with pytest.raises(RuntimeError, match=re.escape(message)):
        check(lock_tree)


def test_missing_listed_requirements_file_blocks_release(lock_tree):
    (lock_tree / "broadcastify/monitor/requirements.txt").unlink()
    with pytest.raises(RuntimeError, match="broadcastify/monitor/requirements.txt is missing"):
        check(lock_tree)
