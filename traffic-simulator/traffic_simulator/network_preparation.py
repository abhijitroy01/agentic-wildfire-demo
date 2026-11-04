"""Bounded background road validation; changing extent never starts a simulation."""
import math
from datetime import UTC, datetime

import sumolib
from shapely.geometry import Point

from traffic_simulator import network, osm_extracts, road_planning, settings
from traffic_simulator.errors import IntegrationError
from traffic_simulator.input_bundles import freeze_network, thaw_network, verify_sealed
from traffic_simulator.models import PreparationScenario, Scenario
from traffic_simulator.network import NetworkTooLarge, prepare_network
from traffic_simulator.persistence import digest
from traffic_simulator.timings import Timings

# The one public text of every refused prepared network, whichever check failed.
RESTORE_FAILURE = "Prepared road network failed its version, scenario or integrity check. Prepare again."


def prepare(scenario, progress, check, *, timings=None, extent_mode="fixed", max_radius_km=15):
    """Plan one road extent on one pinned source set, then seal a ready network or a coverage outcome.

    Automatic planning searches for mapped civilian and crew vehicle access around the unchanged incident. An
    explicit fixed extent is not expanded; existing size-driven reductions remain reviewable proposals.
    """
    if extent_mode not in ("auto", "fixed"):
        raise ValueError("Road extent mode must be auto or fixed")
    maximum = min(max_radius_km, settings.max_study_radius_km())
    if not math.isfinite(maximum) or not 2 <= maximum <= 15 or scenario.radius_km > maximum:
        raise IntegrationError("RADIUS_TOO_LARGE", "The requested road extent exceeds the configured study limit.")
    requested = scenario.radius_km
    minimum_hazard_radius = (scenario.hazard_radius_m
                             + scenario.hazard_growth_m_min * scenario.duration_minutes) / 900
    radii, attempted, attempts = [requested], set(), []
    timings = timings if timings is not None else Timings()
    source_manifest = None
    if extent_mode == "auto":
        # Select all required U.S. regions before geographic planning, including seams introduced by expansion.
        envelope = PreparationScenario.model_validate({**scenario.model_dump(), "radius_km": maximum})
        source_manifest = osm_extracts.plan(envelope, network.command, progress, check, timings)
    ceiling, size_refused, last_coverage = maximum, False, None
    while radii:
        radius = radii.pop(0)
        if radius in attempted or radius > ceiling:
            continue
        attempted.add(radius)
        check()
        if radius < minimum_hazard_radius:
            attempts.append({"radius_km": radius, "status": "ineligible", "reason_code": "NETWORK_LIMIT",
                             "reason": "This extent cannot contain the unchanged scenario hazard assumption."})
            continue
        model = PreparationScenario if isinstance(scenario, PreparationScenario) else Scenario
        candidate = model.model_validate({**scenario.model_dump(), "radius_km": radius})
        try:
            source_options = {"source_manifest": source_manifest} if source_manifest is not None else {}
            prepared = prepare_network(candidate, lambda phase, fraction, radius=radius: progress(
                f"Checking {radius:g} km extent: {phase}", fraction), check, timings=timings, **source_options)
        except NetworkTooLarge as exc:
            source_manifest = road_planning.pin_source(source_manifest, exc.source_manifest)
            size_refused, ceiling = True, min(ceiling, radius)
            attempts.append({"radius_km": radius, "status": "too_large",
                             "edge_count": exc.edge_count, "reason": str(exc), "reason_code": "NETWORK_LIMIT"})
            radii.extend(r for r in (6.0, 4.0, 3.0, 2.0) if r < radius and r not in attempted)
            continue
        except road_planning.RoadCoverageUnavailable as exc:
            source_manifest = road_planning.pin_source(source_manifest, exc.source_manifest)
            last_coverage = exc
            attempts.append({"radius_km": radius, "status": "unavailable", "reason_code": exc.code,
                             "reason": exc.message, "coverage": exc.details})
            if extent_mode == "auto":
                expanded = road_planning.next_radius(radius, ceiling, exc.details)
                if expanded > radius and expanded not in attempted:
                    radii.insert(0, expanded)
            continue
        attempts.append({"radius_km": radius, "status": "validated", "edge_count": len(prepared[2]),
                         "network_bounds": road_planning.network_bounds(prepared[6], scenario.lon)})
        # Automatic selection is resolved by the worker from the same snapshot it converted.
        # Never seal an unresolved preparation as a runnable road-network bundle.
        resolved = Scenario.model_validate(prepared[7].get("resolved_scenario", candidate.model_dump()))
        # Reuse the model's seeded plans to disclose the road-staging gap. This does not require every route to
        # exist: unresolved civilian and crew demand retains its existing ledger semantics.
        _, crews = network.demand_for(resolved, *prepared[1:6], check_cancel=check)
        staging_distances = [prepared[3][crew["stage"]].distance(Point(prepared[5])) / 1000
                             for crew in crews if crew["route"]]
        check()
        source_manifest = road_planning.pin_source(source_manifest, prepared[7].get("source_manifest"))
        validation = {
            "requested_radius_km": requested, "effective_radius_km": radius,
            "edge_count": len(prepared[2]), "highway_edge_count": prepared[7]["highway_edge_count"],
            "gate_count": len(prepared[4]), "network_sha256": prepared[7]["net_sha256"],
            "checked_at": datetime.now(UTC).isoformat(), "adjusted": radius != requested,
            "attempts": attempts, "gates": prepared[7]["gates"],
            "study_bounds": prepared[7].get("study_bounds", []),
            "corridor_label": prepared[7].get("corridor_label"),
            "extent_mode": extent_mode,
            "crew_staging_distance_km": max(staging_distances, default=0),
            "coverage": road_planning.coverage(
                scenario, extent_mode, attempts, source_manifest, "READY",
                "Mapped roads support modeled civilian exits and requested crew vehicle approaches."),
        }
        content = {
            "version": 1, "engine_version": sumolib.__version__, "scenario": resolved.model_dump(),
            **freeze_network(prepared), "validation": validation,
        }
        return {**content, "sha256": digest(content)}
    check()
    code = "NETWORK_LIMIT" if size_refused else last_coverage.code
    message = ("No usable road network fits the configured study-area and road-size limits." if size_refused
               else last_coverage.message)
    return {"outcome": "unavailable", "scenario": scenario.model_dump(),
            "coverage": road_planning.coverage(scenario, extent_mode, attempts, source_manifest, code, message)}


def restore(bundle, scenario):
    """Load a prepared road network after its version, scenario and hash checks pass."""
    try:
        verify_sealed(bundle, version=1)
    except IntegrationError:
        raise ValueError(RESTORE_FAILURE) from None
    if bundle.get("scenario") != scenario.model_dump():
        raise ValueError(RESTORE_FAILURE)
    return thaw_network(bundle, "prepared-network-")
