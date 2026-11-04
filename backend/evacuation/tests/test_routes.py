"""One offline walk through the v1 routes the deployed workspace UI calls, through the real router and owner check.

The geocoder and highway discovery are patched, and the worker's network validation is completed as the
`prepared` builder does. GET /api/v1/agent/turns/{turn_id} and POST /api/v1/variants are published but have no
caller, so the walk leaves them out.
"""
from uuid import uuid4

import pytest

from backend.evacuation import locations
from backend.tests.builders import HIGHWAYS, client, complete_preparation, resolution

NETWORK = {"type": "FeatureCollection", "features": []}


@pytest.fixture
def walk(demo, monkeypatch):
    """Return the demo app's store and two signed-in browsers, with no geocoder or Caltrans call possible."""
    app, store = demo
    monkeypatch.setattr(locations, "resolve", resolution)
    monkeypatch.setattr("traffic_simulator.network.discover", lambda area: HIGHWAYS)
    monkeypatch.setattr("traffic_simulator.traffic.configured", lambda: True)
    with client(app) as alice, client(app) as bob:
        for browser in (alice, bob):
            assert browser.post("/api/v1/demo/session").status_code == 200
        yield store, alice, bob


def ok(response, status=200):
    """Return a response's JSON after checking its status code."""
    assert response.status_code == status, response.text
    return response.json()


def reasons(registry):
    """Return the set of reason codes across a capability registry's operations (None means enabled)."""
    return {operation["reason_code"] for operation in registry["operations"]}


def test_the_ui_route_sequence_from_session_to_run_capabilities(walk):
    store, alice, bob = walk
    session = ok(alice.post("/api/v1/sessions", json={"request_id": str(uuid4()), "entry_mode": "lab"}))
    path = f"/api/v1/sessions/{session['session_id']}"
    assert ok(alice.get(path)) == session
    patched = ok(alice.patch(path, json={"request_id": str(uuid4()), "expected_revision": 1,
                                         "policy_id": "coordinated", "replay_time_s": 90}))
    assert (patched["revision"], patched["policy_id"], patched["replay_time_s"]) == (2, "coordinated", 90)

    incident = ok(alice.post("/api/v1/incidents", json={
        "request_id": str(uuid4()), "session_id": session["session_id"], "preset_id": "paradise-2018"}))
    assert incident["location"]["matched_address"] == "Paradise, California"
    assert ok(alice.get(f"/api/v1/incidents/{incident['incident_id']}")) == incident
    assert {preset["preset_id"] for preset in ok(alice.get("/api/v1/presets"))} == {"paradise-2018",
                                                                                   "palisades-2025"}

    draft = ok(alice.post("/api/v1/scenario-drafts", json={
        "request_id": str(uuid4()), "session_id": session["session_id"],
        "incident_id": incident["incident_id"], "location_revision": incident["location_revision"]}))
    assert draft["status"] == "preparing" and draft["preparation_id"]
    network = f"/api/v1/scenario-drafts/{draft['draft_id']}/network"
    assert ok(alice.get(network), 409)["error"]["code"] == "NETWORK_NOT_READY"
    store.artifact(draft["preparation_id"], "network", NETWORK)
    ready = complete_preparation(store, draft)
    assert ok(alice.get(network)) == NETWORK
    # Cancelling after validation finished leaves the ready draft as it is: cancellation never overwrites completion.
    cancelled = ok(alice.post(f"/api/v1/scenario-drafts/{draft['draft_id']}/cancel",
                              json={"request_id": str(uuid4())}))
    assert cancelled == ready and cancelled["status"] == "ready"

    run = ok(alice.post("/api/v1/runs", json={
        "request_id": str(uuid4()), "draft_id": ready["draft_id"], "draft_revision": ready["draft_revision"],
        "capability_version": "1.0.0"}), 202)
    assert run["status"] == "queued" and run["draft_id"] == ready["draft_id"]
    assert ok(alice.get(f"/api/v1/runs/{run['run_id']}")) == run

    capabilities = f"/api/v1/runs/{run['run_id']}/capabilities"
    # Without a run, operations are enabled unless the simulator lacks them; a queued run's reads wait for it.
    assert None in reasons(ok(alice.get("/api/v1/capabilities")))
    queued = reasons(ok(alice.get(capabilities)))
    assert "RESULT_NOT_READY" in queued and "ARTIFACT_UNAVAILABLE" not in queued
    # A completed run whose retained result is gone (410) still answers, with its reads unavailable.
    store.update(run["run_id"], status="completed")
    completed = reasons(ok(alice.get(capabilities)))
    assert "ARTIFACT_UNAVAILABLE" in completed and "RESULT_NOT_READY" not in completed

    # Every route above checks the owner: another browser finds none of it.
    for other in (path, f"/api/v1/incidents/{incident['incident_id']}", network,
                  f"/api/v1/runs/{run['run_id']}", capabilities):
        assert bob.get(other).status_code == 404, other


def test_an_unknown_v1_route_answers_in_the_error_envelope(walk):
    _, alice, _ = walk
    response = alice.get("/api/v1/not-a-route")
    value = ok(response, 404)
    assert value["error"]["request_id"] == response.headers["X-Request-ID"]
    assert {name: item for name, item in value["error"].items() if name != "request_id"} == {
        "code": "REQUEST_FAILED", "message": "The request could not be completed.", "retryable": False,
        "details": {}}
