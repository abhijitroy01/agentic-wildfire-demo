import importlib.util
import json
import os
import subprocess  # nosec B404 - runs only the repository's contract generator with fixed arguments, never a shell
import sys
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[2]
GENERATOR = REPOSITORY / "scripts/export_integration_contracts.py"


def load_generator():
    """Load the contract generator by file path; a 'scripts' package import depends on sys.path order."""
    spec = importlib.util.spec_from_file_location("export_integration_contracts", GENERATOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


outputs = load_generator().outputs


def test_committed_contracts_match_canonical_models():
    for path, expected in outputs().items():
        assert path.read_text() == expected, f"Run scripts/export_integration_contracts.py ({path.name})"


def test_export_is_offline_and_does_not_initialize_production_storage(tmp_path):
    # Empty workspace settings make any settings import fail, so the export proves it loads none.
    environment = {
        **os.environ, "STORAGE_MODE": "aws", "DATA_DIR": str(tmp_path / "must-not-be-created"),
        "ARTIFACT_BUCKET": "", "JOBS_TABLE": "", "QUEUE_URL": "", "FABRIC_PUBLIC_URL": "",
        "PUBLIC_DEMO_MODE": "", "AWS_EC2_METADATA_DISABLED": "true",
    }
    # This interpreter runs the repository's generator with fixed arguments and no shell.
    # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
    completed = subprocess.run(  # nosec B603 - fixed argument list, no shell
        [sys.executable, "scripts/export_integration_contracts.py", "--check"],
        cwd=REPOSITORY, env=environment, capture_output=True, text=True, timeout=30, check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert not (tmp_path / "must-not-be-created").exists()


def test_published_contract_has_no_user_sign_in():
    """Owner-scoped operations name only the demo-session cookie; no bearer scheme or header remains."""
    specification = json.loads(next(text for path, text in outputs().items() if path.name == "openapi.json"))
    assert set(specification["components"]["securitySchemes"]) == {"demoSession"}
    for route, operations in specification["paths"].items():
        for operation in operations.values():
            assert not [p for p in operation.get("parameters", [])
                        if p["in"] == "header" and p["name"].lower() == "authorization"], route
            assert operation.get("security", [{"demoSession": []}]) == [{"demoSession": []}], route


def test_offline_openapi_uses_actual_production_request_schemas():
    from backend.app import app
    exported = outputs()
    specification = json.loads(next(text for path, text in exported.items() if path.name == "openapi.json"))
    production = app.openapi()
    route = "/api/v1/evidence"
    assert specification["paths"][route]["post"]["requestBody"] == production["paths"][route]["post"]["requestBody"]
    for name, schema in specification["components"]["schemas"].items():
        if name in production["components"]["schemas"]:
            assert schema == production["components"]["schemas"][name], name
