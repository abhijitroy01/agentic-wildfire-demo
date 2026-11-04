"""The UI and model consume the same implemented capability inventory."""
from backend.evacuation.contracts import (
    CapabilityOperation,
    CapabilityRegistry,
    SimulationScenario,
    VariantChanges,
)
from backend.evacuation.evidence import (
    READABLE_ARTIFACT_VERSIONS,
    horizon_for,
    mapping,
    policies_for,
    samples_for,
    valid_crew_records,
    valid_replay_sample,
    validity,
)


def capabilities(result=None, *, job_status=None):
    """List the simulator operations a run supports, each enabled or disabled with a reason code."""
    operations = []

    def add(name, *, view=None, evidence=(), question=None, reason=None, parameter=None, effect="read"):
        enabled = reason is None
        if result is not None or job_status is not None:
            if job_status != "completed":
                enabled, reason = False, "RESULT_NOT_READY"
            elif result is None:
                enabled, reason = False, "ARTIFACT_UNAVAILABLE"
            elif (type(result.get("schema_version")) is not int
                  or result["schema_version"] not in READABLE_ARTIFACT_VERSIONS):
                enabled, reason = False, "ARTIFACT_VERSION_UNSUPPORTED"
            elif enabled and view in ("outcomes", "crew", "replay"):
                policies = policies_for(result)
                scenario = mapping(result.get("scenario"))
                valid = policies and all(validity(p, scenario)[0] == "available" for _, p in policies.values())
                if not valid:
                    enabled, reason = False, "VALID_OUTCOME_EVIDENCE_UNAVAILABLE"
                elif view == "crew" and not all(valid_crew_records(p, scenario) is not None
                                                for _, p in policies.values()):
                    enabled, reason = False, "CREW_TIMELINE_UNAVAILABLE"
                elif view == "replay" and not all(
                    series is not None and all(valid_replay_sample(s, scenario) for s in series)
                    for _, p in policies.values() for series in [samples_for(p, horizon_for(result))]
                ):
                    enabled, reason = False, "REPLAY_SERIES_UNAVAILABLE"
            elif enabled and view == "sources" and not result.get("sources") and not result.get("network"):
                enabled, reason = False, "SOURCE_SNAPSHOT_NOT_RETAINED"
            elif enabled and view == "assumptions" and not result.get("scenario"):
                enabled, reason = False, "ASSUMPTIONS_NOT_RETAINED"
        operations.append(CapabilityOperation(
            id=name, enabled=enabled, reason_code=reason, effect=effect,
            required_job_states=["completed"] if view else [],
            required_evidence=list(evidence),
            supported_time_scopes=["replay"] if view == "replay" else ["final"] if view else [],
            tool="get_simulation_evidence" if view else None,
            parameter=parameter,
            parameter_schema=VariantChanges.model_json_schema()["properties"][parameter] if parameter else None,
            suggested_question=question if enabled else None,
        ))

    operations.append(CapabilityOperation(id="run_status", enabled=True, effect="read",
                                          required_job_states=[], tool="get_simulation_status"))
    add("scenario_assumptions", view="assumptions", question="What assumptions does this simulation use?")
    add("source_evidence", view="sources", question="Which observations actually affected the simulation?")
    add("compare_outcomes", view="outcomes",
        evidence=("valid_accounting", "valid_engine_run", "complete_horizon"),
        question="Compare resident evacuation and crew access across the policies.")
    add("crew_details", view="crew", evidence=("complete_crew_timeline",),
        question="Which crews did not reach staging, and how did waiting and travel times change?")
    add("inspect_replay", view="replay", evidence=("conserved_replay_ledger",),
        question="How many people are still waiting or unresolved at this time?")
    frozen_reason = None if result is None and job_status is None or (
        result and result.get("input_bundle_id") and result.get("input_bundle_sha256")
    ) else "FROZEN_INPUTS_UNAVAILABLE"
    add("variant_departure_window", parameter="departure_minutes", effect="create_run",
        question="What if departure were spread over 20 minutes?", reason=frozen_reason)
    add("variant_vehicle_count", parameter="vehicles", effect="create_run",
        question="Prepare a comparison with 600 vehicles.", reason=frozen_reason)
    operations.append(CapabilityOperation(
        id="cancel_run", enabled=job_status is None or job_status in ("queued", "running"),
        reason_code=None if job_status is None or job_status in ("queued", "running") else "RUN_NOT_ACTIVE",
        effect="cancel", required_job_states=["queued", "running"]))
    for operation in ("wind_driven_spread", "scheduled_road_closure", "contraflow"):
        add(operation, effect="create_run", reason="SIMULATOR_CONTROL_NOT_IMPLEMENTED")
    return CapabilityRegistry(
        artifact_versions_readable=list(READABLE_ARTIFACT_VERSIONS), operations=operations,
        scenario_schema=SimulationScenario.model_json_schema(),
        cross_field_constraints=[
            "departure_minutes <= duration_minutes",
            "hazard_radius_m + hazard_growth_m_min * duration_minutes <= radius_km * 900",
            "policies are unique",
            "Source locations, selected-road eligibility and network gates require runtime checks.",
            "Synthetic traffic uses explicit demand assumptions; optional provider traffic requires coverage.",
        ],
    )
