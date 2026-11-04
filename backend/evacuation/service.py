"""Owned evacuation sessions, locations, drafts and runs; calculations remain in SUMO."""
import logging
import time
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import ValidationError
from traffic_simulator.errors import IntegrationError
from traffic_simulator.persistence import digest, expires_at, mutate, owned, record

from backend import simulation_jobs
from backend.api_contracts import CAPABILITY_VERSION
from backend.evacuation import geocoding, locations
from backend.evacuation.contracts import (
    DraftResponse,
    IncidentResponse,
    PreparationScenario,
    RunResponse,
    SessionResponse,
    SimulationScenario,
)

logger = logging.getLogger(__name__)

def session_view(value):
    return SessionResponse(**{k: value[k] for k in SessionResponse.model_fields if k in value}).model_dump(mode="json")


def run_view(job):
    return RunResponse(
        run_id=job["id"], session_id=job["session_id"], incident_id=job["incident_id"],
        location_revision=job["location_revision"], status=job["status"],
        progress=job["progress"], phase=job["phase"], scenario=job["scenario"],
        created_at=datetime.fromtimestamp(job["created"], UTC).isoformat(),
        error=job.get("error"), error_code=job.get("error_code"),
        parent_run_id=job.get("parent_run_id"), draft_id=job["draft_id"],
    ).model_dump(mode="json")


def retain_inputs(store, owner, job, until, *, pending_ids=()):
    """Keep referenced metadata and frozen inputs for at least the new draft/run's lifetime.

    TTL extensions are monotonic and safe if a later transaction conflicts or fails.
    Traverse ancestry without coupling its length to DynamoDB's transaction size.
    """
    def extend(identifier):
        try:
            store.retain(identifier, until)
        except FileNotFoundError:
            raise IntegrationError("FROZEN_INPUTS_UNAVAILABLE",
                                   "Referenced simulation inputs expired. Prepare a new scenario.", 409) from None

    seen = set()
    while job and job["id"] not in seen:
        seen.add(job["id"])
        if job["id"] not in pending_ids:
            extend(job["id"])
        for field, prefix in (("session_id", "session:"), ("incident_id", "incident:"),
                              ("draft_id", "draft:"), ("prepared_network_id", "")):
            if job.get(field):
                identifier = f"{prefix}{job[field]}"
                value = store.get(identifier)
                if identifier not in pending_ids and value and value.get("subject", value.get("owner")) == owner:
                    extend(identifier)
        job = simulation_jobs.owned_job(store, job["parent_run_id"], owner, "run") if job.get("parent_run_id") else None


def create_session(store, owner, request):
    """Start a visitor's workspace session with baseline selection and no run chosen."""
    def build():
        value = record("session", uuid4(), owner, session_id=str(uuid4()), entry_mode=request.entry_mode,
                       selected_run_id=None, policy_id="baseline", replay_time_s=0.0)
        value["id"] = f"session:{value['session_id']}"
        return session_view(value), [(value, None)]
    return mutate(store, owner, "create_session", request.request_id, request.model_dump(mode="json"), build)


def patch_session(store, owner, session_id, request):
    """Change a session's selected run, policy or replay time at its expected revision."""
    def build():
        value = owned(store, "session", session_id, owner)
        if value["revision"] != request.expected_revision:
            raise IntegrationError("SESSION_REVISION_CONFLICT", "Refresh the session before changing selection.", 409)
        if request.selected_run_id:
            job = simulation_jobs.owned_job(store, request.selected_run_id, owner, "run")
            if job["session_id"] != str(session_id):
                raise IntegrationError("CONTEXT_MISMATCH", "Run belongs to another session.", 409)
        previous = value["revision"]
        value.update(revision=previous+1, selected_run_id=str(request.selected_run_id) if request.selected_run_id else None,
                     policy_id=request.policy_id, replay_time_s=request.replay_time_s)
        return session_view(value), [(value, previous)]
    return mutate(store, owner, "patch_session", request.request_id,
                  {"session_id": str(session_id), **request.model_dump(mode="json")}, build)


def create_incident(store, owner, request, *, resolver=None):
    """Record one owned location with its address, area, explicit-point or reported-fire provenance."""
    session = owned(store, "session", request.session_id, owner)
    preset = locations.PRESETS.get(request.preset_id)
    receipt = None

    def build():
        nonlocal receipt
        # On an idempotent replay mutate() returns before a second provider lookup.
        if receipt is None:
            if request.coordinates is not None:
                receipt = locations.coordinate_receipt(request.coordinates)
            elif request.fire_assessment_id is not None:
                receipt = locations.reported_fire_receipt(store, owner, request.fire_assessment_id)
            elif request.fire_observation_id is not None:
                receipt = locations.reported_fire_observation_receipt(store, owner, request.fire_observation_id)
            else:
                options = {"preset": bool(preset)}
                if request.reported_place is not None:
                    options["place"] = True
                address = preset["address"] if preset else request.reported_address or request.reported_place
                receipt = geocoding.lookup(
                    store, owner, request.request_id, body,
                    lambda: (resolver or locations.resolve)(address, **options))
        value = IncidentResponse(
            incident_id=uuid4(), session_id=request.session_id, location_revision=1,
            preset_id=request.preset_id, location=receipt).model_dump(mode="json")
        saved = record("incident", value["incident_id"], owner, **value)
        # Serialize active location changes against draft/submit operations.
        current = owned(store, "session", request.session_id, owner)
        revision = current["revision"]
        current.update(incident_id=value["incident_id"], latest_draft_id=None, revision=revision+1)
        return value, [(saved, None), (current, revision)]
    if (session["entry_mode"] == "lab") != bool(preset):
        raise IntegrationError("ENTRY_MODE_MISMATCH", "Choose a location for the incident or a preset for Simulation Lab.")
    body = request.model_dump(mode="json")
    # Preserve retained request hashes from before the optional observation reference was introduced.
    if request.fire_observation_id is None:
        body.pop("fire_observation_id")
    return mutate(store, owner, "create_incident", request.request_id, body, build)


def context(store, owner, session_id, incident_id, location_revision):
    """Load an owned session and incident, refusing a stale location revision with 409."""
    session = owned(store, "session", session_id, owner)
    incident = owned(store, "incident", incident_id, owner)
    if (incident["session_id"] != str(session_id) or session.get("incident_id") != str(incident_id)
            or incident["location_revision"] != location_revision):
        raise IntegrationError("STALE_LOCATION", "The selected location changed. Refresh before continuing.", 409)
    return session, incident


def draft_view(store, draft):
    """Render a scenario draft with its road-network preparation status for the API."""
    value = {k: draft[k] for k in DraftResponse.model_fields if k in draft}
    if draft.get("preparation_id"):
        job = store.get(draft["preparation_id"])
        if not job or job.get("owner") != draft["subject"]:
            raise IntegrationError("PREPARATION_UNAVAILABLE", "Prepare the road network again.", 410)
        value.update(status="preparing", phase=job["phase"], error=None, error_code=None,
                     road_coverage=job.get("road_coverage"))
        if job["status"] == "completed" and job.get("preparation_outcome") == "unavailable":
            if not job.get("road_coverage"):
                raise IntegrationError("PREPARATION_INVALID", "Road preparation returned no coverage evidence.", 503)
            value.update(status="unavailable")
        elif job["status"] == "completed":
            try:
                scenario = SimulationScenario.model_validate(job["prepared_scenario"]).model_dump(mode="json")
            except (ValidationError, KeyError, TypeError):
                raise IntegrationError("PREPARATION_INVALID", "Road preparation did not return a resolved scenario. "
                                       "Prepare again.", 503) from None
            value.update(status="ready", phase="Road network validated; review before running",
                         scenario=scenario,
                         scenario_hash=digest(scenario), draft_revision=2,
                         network_validation=job["network_validation"])
        elif job["status"] in ("failed", "cancelled"):
            value.update(status="failed", error=job.get("error", "Preparation was cancelled."),
                         error_code=job.get("error_code"))
    return DraftResponse.model_validate(value).model_dump(mode="json")


def prepare(store, owner, request, *, discoverer=None):
    """Queue road discovery and validation; the API never performs a blocking road-provider request."""

    def build():
        from backend import api_settings as settings

        session, incident = context(store, owner, request.session_id, request.incident_id,
                                    request.location_revision)
        preparation_id = str(uuid4())
        point = incident["location"]
        preset = locations.PRESETS.get(incident.get("preset_id"), {})
        extent_mode = "fixed" if request.radius_km is not None or preset else "auto"
        maximum_radius = settings.max_study_radius_km()
        initial_radius = (request.radius_km if request.radius_km is not None else
                          preset.get("radius_km", min(4.0, maximum_radius)))
        scenario = PreparationScenario(
            lat=point["lat"], lon=point["lon"], radius_km=initial_radius,
            vehicles=request.vehicles, departure_minutes=request.departure_minutes,
            traffic_mode=request.traffic_mode)
        simulation_jobs.check_radius(scenario.radius_km)
        # The admission and session revisions serialize the queued intent with active location changes.
        admission = simulation_jobs.admit(store, owner, preparation_id)
        draft = DraftResponse(
            draft_id=uuid4(), draft_revision=1, session_id=request.session_id,
            incident_id=request.incident_id, location_revision=request.location_revision,
            scenario=scenario, scenario_hash=digest(scenario.model_dump()), input_mode="current",
            status="preparing", preparation_id=preparation_id, phase="Queued for road discovery and validation",
        ).model_dump(mode="json")
        # Publish the latest draft with the session revision that locks its incident context.
        revision = session["revision"]
        session["revision"] += 1
        session["latest_draft_id"] = draft["draft_id"]
        session["selected_run_id"] = None
        now = time.time()
        job = {"id": preparation_id, "owner": owner, "kind": "network_preparation",
               "status": "queued", "created": now, "updated": now, "progress": 0.0,
               "phase": draft["phase"], "scenario": draft["scenario"], "cancel": False,
               "extent_mode": extent_mode,
               "requested_radius_km": scenario.radius_km if extent_mode == "fixed" else None,
               "max_radius_km": maximum_radius,
               "outbox_pending": True, "expires": expires_at(now)}
        return draft, [(record("draft", draft["draft_id"], owner, **draft), None),
                       (session, revision), (job, None), *admission]
    value = mutate(store, owner, "prepare_simulation", request.request_id, request.model_dump(mode="json"), build)
    simulation_jobs.dispatch(store, value["preparation_id"])
    return draft_view(store, owned(store, "draft", value["draft_id"], owner))


def submit(store, owner, request):
    """Queue a simulation run from a validated draft under the active-job limits."""
    if request.capability_version != CAPABILITY_VERSION:
        raise IntegrationError("CAPABILITY_VERSION_MISMATCH", "Refresh the simulator capabilities.", 409)

    def build():
        from traffic_simulator.traffic import configured
        saved_draft = owned(store, "draft", request.draft_id, owner)
        draft = {**saved_draft, **draft_view(store, saved_draft)}
        session, _ = context(store, owner, draft["session_id"], draft["incident_id"], draft["location_revision"])
        if draft["draft_revision"] != request.draft_revision:
            raise IntegrationError("DRAFT_REVISION_CONFLICT", "Review the current draft before submission.", 409)
        # A draft submits only once, even if another browser generates a new request ID.
        if draft.get("run_id"):
            return run_view(simulation_jobs.owned_job(store, draft["run_id"], owner, "run")), []
        if draft["status"] != "ready":
            raise IntegrationError("NETWORK_NOT_READY", "Road-network preparation must finish before submission.", 409)
        if not draft.get("parent_run_id") and not draft.get("network_validation"):
            raise IntegrationError("NETWORK_NOT_VALIDATED", "Prepare and review this scenario again before running.", 409)
        if (not draft.get("parent_run_id") and draft["scenario"]["traffic_mode"] == "provider_speed_limits"
                and not configured()):
            raise IntegrationError("TRAFFIC_NOT_CONFIGURED", "Configure TomTom before requesting current traffic.")
        now = time.time()
        job = {"id": str(uuid4()), "owner": owner, "kind": "run", "revision": 1, "status": "queued",
               "created": now, "updated": now, "progress": 0.0, "phase": "Queued",
               "scenario": draft["scenario"], "cancel": False, "outbox_pending": True, "expires": expires_at(now),
               **{k: draft[k] for k in ("session_id", "incident_id", "location_revision",
                                        "draft_id", "parent_run_id", "input_mode", "changes")}}
        if draft.get("preparation_id"):
            job["prepared_network_id"] = draft["preparation_id"]
        admission = simulation_jobs.admit(store, owner, job["id"])
        retain_inputs(store, owner, {**job, "id": saved_draft["id"]}, job["expires"],
                      pending_ids=(saved_draft["id"], session["id"]))
        draft["expires"] = max(draft["expires"], job["expires"])
        draft_revision = draft["revision"]
        draft.update(run_id=job["id"], revision=draft_revision+1)
        session_revision = session["revision"]
        session.update(revision=session_revision+1, selected_run_id=job["id"],
                       expires=max(session["expires"], job["expires"]))
        return run_view(job), [(job, None), *admission, (draft, draft_revision),
                               (session, session_revision)]
    response = mutate(store, owner, "submit_simulation", request.request_id, request.model_dump(mode="json"), build)
    simulation_jobs.dispatch(store, response["run_id"])
    return response


def variant(store, owner, request):
    """Queue a controlled variant of a completed run that reuses its frozen inputs."""
    def build():
        parent = simulation_jobs.owned_job(store, request.parent_run_id, owner, "run")
        session, _ = context(store, owner, parent["session_id"], parent["incident_id"], parent["location_revision"])
        if parent["status"] != "completed":
            raise IntegrationError("RESULT_NOT_READY", "Complete the parent run before creating a variant.", 409)
        from backend.simulation_artifacts import retained_artifact
        bundle = retained_artifact(store, parent["id"], "bundle")
        from traffic_simulator.input_bundles import validate_bundle
        validate_bundle(bundle)
        changes = request.changes.model_dump(exclude_unset=True)
        scenario = SimulationScenario.model_validate({**parent["scenario"], **changes})
        value = DraftResponse(
            draft_id=uuid4(), draft_revision=1, session_id=parent["session_id"],
            incident_id=parent["incident_id"], location_revision=parent["location_revision"],
            scenario=scenario, scenario_hash=digest(scenario.model_dump()),
            parent_run_id=request.parent_run_id, input_mode="frozen_parent", changes=changes,
        ).model_dump(mode="json")
        revision = session["revision"]
        session["revision"] += 1
        saved = record("draft", value["draft_id"], owner, **value)
        retain_inputs(store, owner, parent, saved["expires"], pending_ids=(session["id"],))
        session["expires"] = max(session["expires"], saved["expires"])
        return value, [(saved, None), (session, revision)]
    if request.capability_version != CAPABILITY_VERSION:
        raise IntegrationError("CAPABILITY_VERSION_MISMATCH", "Refresh simulator capabilities.", 409)
    return mutate(store, owner, "create_simulation_variant", request.request_id, request.model_dump(mode="json"), build)
