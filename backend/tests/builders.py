"""Shared workspace API test builders: the demo browser, prepared drafts and runs, and signed contexts.

A plain module, not a test module, so no test file imports another test file.
"""
import copy
import time
from uuid import uuid4

import jwt
from fastapi.testclient import TestClient

from backend import demo_access, trusted_context
from backend.evacuation import service
from backend.evacuation.contracts import IncidentRequest, PrepareRequest, SessionRequest, SubmitRequest
from backend.evacuation.locations import select_receipt

ORIGIN = "https://workspace.cloudfront.net"
HEADERS = {"origin": ORIGIN, "sec-fetch-site": "same-origin", "x-lab-workspace": "public-demo"}
SECRET = "fixture-secret-long-enough-not-a-real-credential"
# A claim value that claims_for leaves out of the claim set.
MISSING = object()
HIGHWAYS = {"highways": [{"route_type": "State", "route_number": 191, "approx_distance_km": 1.0}]}


def client(app):
    """Open a same-origin browser test client for the public demo workspace app."""
    return TestClient(app, base_url=ORIGIN, headers=HEADERS)


def owner_of(browser):
    """Return the workspace owner bound to a test browser's demo-session cookie."""
    return "demo-" + demo_access.digest(browser.cookies.get(demo_access.COOKIE))


def resolution(address, *, preset):
    """Resolve any address to Paradise, California, as Amazon Location would, without a geocoder call."""
    return select_receipt(address, {"provider": "Amazon Location", "resolver_version": "amazon-location-v1",
                                  "candidates": [{
        "matched_address": "Paradise, California", "score": 100,
        "address_type": "Locality", "region": "CA", "country": "USA",
        "lon": -121.6219, "lat": 39.7596,
    }]}, preset=preset)


def located(store, owner="alice"):
    """Create a session and a resolved Paradise incident; return both and the request that prepares a draft."""
    session = service.create_session(store, owner, SessionRequest(request_id=uuid4(), entry_mode="lab"))
    incident = service.create_incident(store, owner, IncidentRequest(
        request_id=uuid4(), session_id=session["session_id"], preset_id="paradise-2018"), resolver=resolution)
    return session, incident, PrepareRequest(request_id=uuid4(), session_id=session["session_id"],
                                             incident_id=incident["incident_id"], location_revision=1)


def complete_preparation(store, draft):
    """Complete a draft's background network validation as the worker would; return the ready draft view."""
    scenario = {**draft["scenario"], "highway": "State 191", "exit_direction": "south"}
    store.update(draft["preparation_id"], status="completed", prepared_scenario=scenario,
                 network_validation={"requested_radius_km":8.0,"effective_radius_km":8.0,
                    "edge_count":100,"highway_edge_count":10,"gate_count":1,
                    "network_sha256":"a"*64,"checked_at":"2026-09-29T00:00:00Z","adjusted":False,"attempts":[]})
    return service.draft_view(store, store.get("draft:"+draft["draft_id"]))


def prepared(store, owner="alice"):
    """Return a session, incident and ready draft whose network validation has completed."""
    session, incident, request = located(store, owner)
    draft = service.prepare(store, owner, request, discoverer=lambda area: HIGHWAYS)
    return session, incident, complete_preparation(store, draft)


def submission(draft):
    """Return the request that submits a ready draft as a run."""
    return SubmitRequest(request_id=uuid4(), draft_id=draft["draft_id"], draft_revision=draft["draft_revision"],
                         capability_version="1.0.0")


def ledger_result():
    """Return a two-policy simulation result: one crew never departs, three stage, only one returns."""
    timeline = [
        {"id": f"crew-{i}", "scheduled_departure_s": schedule, "departed_s": departed,
         "reached_staging_s": staged, "returned_s": returned}
        for i, (schedule, departed, staged, returned) in enumerate(
            [(120, 121, 994, 1500), (165, 166, 1201, None), (210, None, None, None),
             (255, 256, 1349, None)])
    ]
    baseline = {
        "policy": "baseline",
        "summary": {"valid_engine_run": True, "valid_accounting": True, "simulated_seconds": 1800,
                    "exited_vehicles": 351, "active_vehicles": 37, "not_departed_vehicles": 12,
                    "exposed_vehicles": 13, "crews_staged": 3, "crews_returned": 1,
                    "exposed_crews": 0, "crews_not_departed": 1, "t95_seconds": None},
        "series": [
            {"t": 0, "exited": 0, "active": 0, "not_departed": 400, "exposed": 0, "crews_staged": 0, "queue": 0},
            {"t": 900, "exited": 180, "active": 180, "not_departed": 40, "exposed": 8,
             "crews_staged": 0, "queue": 20},
            {"t": 1800, "exited": 351, "active": 37, "not_departed": 12, "exposed": 13,
             "crews_staged": 3, "queue": 10},
        ],
        "frames": [{"t": 1800, "crews": timeline}],
    }
    coordinated = copy.deepcopy(baseline)
    coordinated["policy"] = "coordinated"
    coordinated["summary"].update(exited_vehicles=367, active_vehicles=21, exposed_vehicles=4,
                                  crews_returned=0)
    coordinated["series"][-1].update(exited=367, active=21, exposed=4)
    for index, arrival in [(0, 1004), (1, 1202), (3, 1353)]:
        coordinated["frames"][-1]["crews"][index].update(reached_staging_s=arrival, returned_s=None)
    return {"schema_version": 3,
            "scenario": {"duration_minutes": 30, "vehicles": 400, "people_per_vehicle": 2, "crews": 4,
                         "traffic_mode": "synthetic", "departure_minutes": 10},
            "engine": "Eclipse SUMO", "engine_version": "fixture",
            "policies": [baseline, coordinated], "assumptions": ["Synthetic demand"],
            "network": {"source": "OpenStreetMap", "retrieved_at": 1789592278.0},
            "sources": {"sources": [{"name": "Caltrans closures", "status": "available",
                                    "records": [], "retrieved_at": 1789598880.0, "incomplete": False}]}}


def canonical(purpose):
    """Return a context with every claim its purpose requires, before the signer adds the purpose and times."""
    if purpose == "simulation":
        return {"sub": "alice", "session_id": str(uuid4()), "incident_id": str(uuid4()), "location_revision": 1,
                "turn_id": str(uuid4()), "attempt_id": str(uuid4()),
                "allowed_operations": ["get_simulation_status"], "draft_id": None}
    report = {"context_hash": "a"*64} if purpose == "firetruck_report" else {}
    return {"sub": "b"*64, "session_id": str(uuid4()), "allowed_operations": [], **report}


def claims_for(purpose, /, **changes):
    """Return a purpose's full claim set with changes applied: MISSING removes a claim, a callable gets now."""
    now = int(time.time())
    claims = {**canonical(purpose), "purpose": purpose, "iss": trusted_context.ISSUER,
              "aud": trusted_context.AUDIENCE, "iat": now, "nbf": now, "exp": now+240, **changes}
    return {name: value(now) if callable(value) else value
            for name, value in claims.items() if value is not MISSING}


def forged(claims, purpose="simulation"):
    """Sign raw claims with a purpose's derived key, bypassing the signer's own claims."""
    return jwt.encode(claims, trusted_context.purpose_key(SECRET, purpose), algorithm="HS256")


def investigation_readers():
    """Use the same explicit owned evidence dependencies as the API host."""
    from backend import simulation_jobs
    from backend.dependencies import InvestigationReaders
    from backend.evacuation import evidence, service

    return InvestigationReaders(context=service.context, owned_job=simulation_jobs.owned_job,
                                build_evidence=evidence.build_evidence, planning_summary=evidence.planning_summary)
