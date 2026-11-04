"""Shared deployer test fixtures: tracked files, and installation files written and loaded without AWS calls."""
import copy
import json

import pytest

from deployment import ROOT
from deployment.installation import load_installation
from deployment.package_source import git_files

# A valid installation; tests change one field at a time.
BASE = {
    "aws": {"profile": "fixture", "account_id": "123456789012", "region": "us-west-2", "name": "training",
            "environment": "production", "operator_role_arns": ["arn:aws:iam::123456789012:role/fixture-deployer"]},
    "access": {"mode": "public", "staging_allowed_cidrs": []},
    "models": {"agent_model_id": "model", "report_model_id": "report-model"},
    "radio": {"recordings": ["recording.mp3"]},
    "knowledge": {"public_guidance": "none", "public_knowledge_base_id": ""},
    "providers": {"secret_arn": "arn:aws:secretsmanager:us-west-2:123456789012:secret:provider"},
}


def toml_value(value):
    """Write one TOML value (strings use JSON escapes, which TOML basic strings share)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(item) for item in value) + "]"
    return json.dumps(value)


@pytest.fixture
def installation_file(tmp_path):
    """Write an installation file outside the checkout: changes map section -> {key: value} (None drops it)."""
    def write(changes=None, name="installation.toml"):
        data = copy.deepcopy(BASE)
        for section, entries in (changes or {}).items():
            if entries is None:
                data.pop(section, None)
                continue
            table = data.setdefault(section, {})
            for key, value in entries.items():
                if value is None:
                    table.pop(key, None)
                else:
                    table[key] = value
        lines = []
        for section, table in data.items():
            nested = section == "settings"
            for header, entries in (table.items() if nested else [(section, table)]):
                lines.append(f"[settings.{header}]" if nested else f"[{header}]")
                lines += [f"{key} = {toml_value(value)}" for key, value in entries.items()]
        path = tmp_path / name
        path.write_text("\n".join(lines) + "\n")
        return path
    return write


@pytest.fixture
def make_installation(installation_file):
    """Load a validated Installation from BASE plus changes, with the checkout's deployment/settings.toml."""
    return lambda changes=None: load_installation(installation_file(changes), root=ROOT)


@pytest.fixture(scope="session")
def tracked_files():
    """List existing tracked and unignored source files, as the release builder sees the current checkout."""
    return sorted(path.as_posix() for path in git_files(ROOT) if (ROOT / path).is_file())
