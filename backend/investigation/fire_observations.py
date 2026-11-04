"""Immutable owned fire-source points captured from the trusted Runtime snapshot stream."""
from uuid import NAMESPACE_URL, uuid5

from fire_risk_modeler.models import FireObservation
from pydantic import ValidationError
from traffic_simulator.errors import IntegrationError
from traffic_simulator.persistence import digest, record

from backend.investigation.contracts import ReportedFireObservation


def source_points(snapshot):
    """Validate actual fire records, never the query location or model/weather assumptions."""
    if not isinstance(snapshot, dict) or snapshot.get("type") != "structuredResponse":
        return []
    content = snapshot.get("content")
    data = content.get("mapData") if isinstance(content, dict) else None
    locations = data.get("locations") if isinstance(data, dict) else None
    if not isinstance(locations, list):
        return []
    points = []
    for location in locations:
        if not isinstance(location, dict) or not isinstance(location.get("fire"), dict):
            continue
        source = location["fire"]
        try:
            fire = FireObservation.model_validate({
                "latitude": location.get("latitude"), "longitude": location.get("longitude"),
                "name": source.get("name"), "source_id": source.get("sourceId"),
                "source_timestamp": source.get("lastUpdated"),
            })
            if not fire.source_id or not fire.source_id.strip():
                continue
        except (ValidationError, ValueError, TypeError):
            continue
        points.append(fire)
    return points


def capture_writes(store, owner, task_id, attempt_id, snapshot):
    """Build inserts for the same transaction that publishes the fenced investigation update.

    Stable identities deduplicate repeated snapshots in one attempt. Later attempts cannot overwrite
    an earlier source receipt, even when the mutable investigation task is retried.
    """
    observations, writes, seen = [], [], set()
    for fire in source_points(snapshot):
        source = fire.model_dump(mode="json")
        identifier = str(uuid5(NAMESPACE_URL, digest([
            "reported-fire-observation-v1", owner, task_id, attempt_id, fire.source_id, source,
        ])))
        if identifier in seen:
            continue
        seen.add(identifier)
        saved = store.get(f"fire_observation:{identifier}")
        if saved:
            if (saved.get("subject") != owner or saved.get("fire") != source
                    or saved.get("source_task_id") != task_id or saved.get("source_attempt_id") != attempt_id):
                raise IntegrationError("SOURCE_INVALID", "The saved fire observation failed its identity check.", 409)
        else:
            writes.append((record(
                "fire_observation", identifier, owner, fire=source,
                source_task_id=task_id, source_attempt_id=attempt_id,
            ), None))
        observations.append(ReportedFireObservation(
            observation_id=identifier, source_id=fire.source_id, name=fire.name,
            latitude=fire.latitude, longitude=fire.longitude, source_timestamp=fire.source_timestamp,
        ).model_dump(mode="json"))
    return observations, writes
