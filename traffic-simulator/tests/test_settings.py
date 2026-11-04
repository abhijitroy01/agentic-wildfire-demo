"""Storage mode is required configuration; a missing or mistyped value never selects local mode."""
import os
import subprocess  # nosec B404 - runs only this interpreter with a fixed import check, never a shell
import sys
from pathlib import Path

import pytest

COMPONENT = Path(__file__).resolve().parents[1]
STORAGE = ("STORAGE_MODE", "AWS_REGION", "ARTIFACT_BUCKET", "JOBS_TABLE", "QUEUE_URL")
COMPLETE = {"AWS_REGION": "example-region-1", "ARTIFACT_BUCKET": "example-bucket",
            "JOBS_TABLE": "example-table", "QUEUE_URL": "https://example.invalid/queue"}
# The limits this package checks at start-up, as the stack sets them from deployment/settings.toml.
LIMITS = {"TOMTOM_CALLS_PER_DAY": "1000", "TOMTOM_REQUESTS_PER_RUN": "12", "TOMTOM_RESPONSE_MAX_AGE_SECONDS": "300",
          "TOMTOM_CACHE_MINUTES": "10", "DEMO_SESSION_DAYS": "7", "CALTRANS_RESPONSE_MAX_MB": "20",
          "MAX_STUDY_RADIUS_KM": "15", "ARTIFACT_WRITE_MAX_ATTEMPTS": "3"}


def import_settings(tmp_path, **values):
    """Import traffic_simulator.settings in a fresh interpreter with only the given storage and limit settings."""
    environment = {k: v for k, v in os.environ.items() if k not in STORAGE and k not in LIMITS}
    environment.update(PYTHONPATH=str(COMPONENT), DATA_DIR=str(tmp_path / "data"), **values)
    # This interpreter with a fixed code string; the test controls only environment values. No shell.
    # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
    return subprocess.run(  # nosec B603 - fixed argument list, no shell
        [sys.executable, "-c", "from traffic_simulator import settings; print(settings.CLOUD)"],
        env=environment, capture_output=True, text=True, timeout=60, check=False)


@pytest.mark.parametrize("mode", [None, "", "AWS", "Local", "cloud", " aws"])
def test_missing_or_unknown_storage_mode_refuses_to_start(tmp_path, mode):
    result = import_settings(tmp_path, **({} if mode is None else {"STORAGE_MODE": mode}))
    assert result.returncode != 0
    assert "RuntimeError: STORAGE_MODE must be 'local' or 'aws'" in result.stderr


def test_local_mode_starts_without_creating_scratch_data_at_import(tmp_path):
    result = import_settings(tmp_path, STORAGE_MODE="local")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False"
    assert not (tmp_path / "data").exists()


def test_aws_mode_requires_every_storage_location(tmp_path):
    result = import_settings(tmp_path, STORAGE_MODE="aws", **COMPLETE, **LIMITS)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True"
    result = import_settings(tmp_path, STORAGE_MODE="aws", **{**COMPLETE, "QUEUE_URL": ""}, **LIMITS)
    assert result.returncode != 0
    assert "STORAGE_MODE=aws requires AWS_REGION, ARTIFACT_BUCKET, JOBS_TABLE and QUEUE_URL" in result.stderr


@pytest.mark.parametrize("variable", sorted(LIMITS))
def test_aws_mode_refuses_to_start_without_each_operational_limit(tmp_path, variable):
    result = import_settings(tmp_path, STORAGE_MODE="aws", **COMPLETE,
                             **{k: v for k, v in LIMITS.items() if k != variable})
    assert result.returncode != 0
    assert f"STORAGE_MODE=aws requires {variable} (deployment/settings.toml {variable.lower()})" in result.stderr


@pytest.mark.parametrize("value,message", [
    ("100001", "from 0 to 100000 calls, not '100001'"), ("-1", "from 0 to 100000 calls"),
    ("12.5", "must be an integer"), ("many", "must be an integer"),
])
def test_out_of_range_or_malformed_limits_name_the_variable_and_range(tmp_path, value, message):
    result = import_settings(tmp_path, STORAGE_MODE="aws", **COMPLETE, **{**LIMITS, "TOMTOM_CALLS_PER_DAY": value})
    assert result.returncode != 0
    assert "TOMTOM_CALLS_PER_DAY must be an integer" in result.stderr and message in result.stderr


def test_local_mode_reads_the_deployment_settings_file_and_the_environment_overrides_it(monkeypatch):
    from traffic_simulator import settings
    monkeypatch.delenv("TOMTOM_CALLS_PER_DAY", raising=False)
    declared = settings.operational_settings()["TOMTOM_CALLS_PER_DAY"]
    assert settings.SETTINGS_FILE.name == "settings.toml" and settings.SETTINGS_FILE.parent.name == "deployment"
    assert settings.tomtom_calls_per_day() == declared["value"]
    monkeypatch.setenv("TOMTOM_CALLS_PER_DAY", "3")
    assert settings.tomtom_calls_per_day() == 3
    with pytest.raises(RuntimeError, match="NOT_A_SETTING is not declared in settings.toml"):
        settings.limit("NOT_A_SETTING")


def test_demo_areas_name_the_traffic_lab_examples_and_reject_other_names(monkeypatch):
    """cache.demo_areas_to_prebuild lists Traffic Lab example names: comma-separated, empty allowed, others refused."""
    from traffic_simulator import settings
    from traffic_simulator.examples import EXAMPLES
    spec = settings.operational_settings()["DEMO_AREAS_TO_PREBUILD"]
    assert set(spec["allowed"]) == set(EXAMPLES) and set(spec["value"]) <= set(EXAMPLES)
    monkeypatch.delenv("DEMO_AREAS_TO_PREBUILD", raising=False)
    assert settings.demo_areas_to_prebuild() == spec["value"]
    monkeypatch.setenv("DEMO_AREAS_TO_PREBUILD", "malibu, paradise")
    assert settings.demo_areas_to_prebuild() == ["malibu", "paradise"]
    monkeypatch.setenv("DEMO_AREAS_TO_PREBUILD", "")
    assert settings.demo_areas_to_prebuild() == []
    monkeypatch.setenv("DEMO_AREAS_TO_PREBUILD", "paradise,atlantis")
    with pytest.raises(RuntimeError, match=r"DEMO_AREAS_TO_PREBUILD must list only paradise, malibu .* not "
                                           r"'paradise,atlantis'"):
        settings.demo_areas_to_prebuild()
    monkeypatch.setattr(settings, "CLOUD", True)
    monkeypatch.delenv("DEMO_AREAS_TO_PREBUILD")
    with pytest.raises(RuntimeError, match=r"STORAGE_MODE=aws requires DEMO_AREAS_TO_PREBUILD"):
        settings.demo_areas_to_prebuild()


def test_scratch_folders_are_created_on_first_use(tmp_path, monkeypatch):
    from traffic_simulator import settings
    monkeypatch.setattr(settings, "DATA", tmp_path / "data")
    folder = settings.data_dir("networks", "abc")
    assert folder == tmp_path / "data" / "networks" / "abc" and folder.is_dir()
