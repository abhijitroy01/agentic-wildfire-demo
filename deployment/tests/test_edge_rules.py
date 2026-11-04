"""The WAF rules in deployment/infra/lib/stacks.ts match the workspace API routes and the deployer's smoke check."""
import json
import os
import re
import subprocess  # nosec B404 - runs only this interpreter with the fixed ROUTES code, never a shell
import sys
import uuid
from pathlib import Path

from deployment import deploy_workspace

REPOSITORY = Path(__file__).resolve().parents[2]
STACKS = REPOSITORY / "deployment/infra/lib/stacks.ts"
# POST routes that start work, which WAF limits per viewer IP (rate_limits.work_requests_per_ip_per_5_minutes):
# runs, variants and draft preparation, Traffic Lab runs and lookups, agent and investigation turns, radio
# streams, and firetruck runs and reports.
WORK = {
    "/api/v1/runs", "/api/v1/variants", "/api/v1/scenario-drafts", "/api/v1/agent/turns",
    "/api/v1/investigation/turns", "/api/v1/radio/stream", "/api/v1/research/jobs", "/api/v1/research/discover",
    "/api/v1/research/sources", "/api/v1/firetruck/runs", "/api/v1/firetruck/runs/{run_id}/reports",
    "/api/v1/incidents/{incident_id}/investigation",
    "/api/v1/fire-exposure/jobs/{job_id}/retry",
}
# POST routes that start no work: records, geocoding (a daily budget of its own), evidence reads, cancellations,
# the demo session (its own per-IP limit) and the Gateway adapter's internal route (blocked at CloudFront).
OTHER = {
    "/api/v1/sessions", "/api/v1/incidents", "/api/v1/evidence", "/api/v1/demo/session",
    "/api/v1/runs/{run_id}/cancel", "/api/v1/scenario-drafts/{draft_id}/cancel",
    "/api/v1/research/jobs/{job_id}/cancel", "/api/internal/v1/simulations/{operation}",
    "/api/v1/research/discover/{job_id}/cancel", "/api/v1/fire-exposure/jobs/{job_id}/cancel",
    "/api/v1/incidents/{incident_id}/investigation/cancel",
}
# Enumerate the configured routers, including routes excluded from public OpenAPI.
ROUTES = """
import json
from fastapi.routing import APIRoute
from backend.app import app
def routes(router):
    for route in router.routes:
        if isinstance(route, APIRoute):
            yield route
        elif hasattr(route, "original_router"):
            yield from routes(route.original_router)
print(json.dumps(sorted({route.path for route in routes(app) if "POST" in route.methods})))
"""


def post_routes(data_dir):
    """Import the workspace API in local mode in a child process and return its POST route paths."""
    env = {"PATH": os.environ.get("PATH", ""), "STORAGE_MODE": "local", "DATA_DIR": str(data_dir),
           "PYTHONDONTWRITEBYTECODE": "1",
           "PYTHONPATH": os.pathsep.join(str(REPOSITORY / part) for part in ("traffic-simulator", "fire-risk-modeler",
                                                                           "."))}
    # This interpreter with the fixed ROUTES code above; no shell.
    # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
    completed = subprocess.run([sys.executable, "-c", ROUTES], cwd=REPOSITORY, env=env,  # nosec B603
                               capture_output=True, text=True, timeout=120, check=False)
    assert completed.returncode == 0, completed.stderr[-2000:]
    return set(json.loads(completed.stdout.strip().splitlines()[-1]))


def test_every_post_route_is_classified_for_the_waf_work_rate_rule(tmp_path):
    pattern = re.compile(re.search(r'workStartingPaths = "([^"]+)";', STACKS.read_text())[1])
    routes = post_routes(tmp_path)
    assert routes == WORK | OTHER, "Classify each new POST route in WORK or OTHER, and in workStartingPaths."
    # WAF matches the URL-decoded, lower-cased path; route parameters are UUIDs or operation names.
    misclassified = [path for path in sorted(routes)
                     if bool(pattern.search(re.sub(r"\{[^}]+\}", str(uuid.uuid4()), path).lower())) != (path in WORK)]
    assert misclassified == []


def test_the_smoke_check_recognizes_the_staging_block_that_waf_sends():
    assert f'"{deploy_workspace.STAGING_BLOCK_CODE}"' in STACKS.read_text()
