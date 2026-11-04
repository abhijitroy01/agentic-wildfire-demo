"""The synthetic browser fixture stays labeled, small, usable by the harness and out of every release bundle."""
import gzip
import importlib.util
import json

from deployment import ROOT
from deployment.package_source import SERVICE_FOLDERS, SOURCE_FOLDERS

GENERATOR = ROOT / "traffic-simulator/scripts/build_browser_fixture.py"
FIXTURE = ROOT / "ui/test-fixtures/browser-synthetic-grid"
FILES = ("result.json.gz", "network.json.gz", "fixture.json")


def generator():
    """Load the fixture generator by path; its module level imports only the standard library."""
    spec = importlib.util.spec_from_file_location("build_browser_fixture", GENERATOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def artifact(name):
    """Read one of the fixture's gzip JSON artifacts."""
    return json.loads(gzip.decompress((FIXTURE / f"{name}.json.gz").read_bytes()))


def test_the_fixture_is_labeled_synthetic_by_its_generator():
    label = json.loads((FIXTURE / "fixture.json").read_text())
    assert label == generator().FIXTURE_LABEL
    assert label["synthetic"] is True
    assert label["label"].startswith("Synthetic ") and "not real roads, traffic or incidents" in label["label"]
    assert (ROOT / label["generator"]) == GENERATOR and artifact("result")["scenario"]["seed"] == label["seed"]


def test_each_fixture_file_is_under_one_mebibyte():
    sizes = {name: (FIXTURE / name).stat().st_size for name in FILES}
    assert all(0 < size < 1024 * 1024 for size in sizes.values()), sizes


def test_the_fixture_meets_the_browser_harness_requirements():
    result, network = artifact("result"), artifact("network")
    assert generator().harness_problems(result) == []
    assert result["traffic"]["status"] == "not_requested"
    assert network["type"] == "FeatureCollection" and len(network["features"]) == result["network"]["edge_count"]


def test_the_fixture_is_outside_every_release_bundle_folder():
    inside = [folder for folder in SOURCE_FOLDERS + SERVICE_FOLDERS if FIXTURE.is_relative_to(ROOT / folder)]
    assert FIXTURE.is_dir() and not inside
