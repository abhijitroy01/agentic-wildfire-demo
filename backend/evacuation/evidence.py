"""Deterministic, bounded evidence from retained artifacts; no model or provider calls."""
import base64
import hashlib
import json
import math
from itertools import pairwise, product
from uuid import UUID, uuid5

from traffic_simulator.errors import IntegrationError

from backend.evacuation.contracts import (
    ArtifactReference,
    EvidenceComparison,
    EvidenceFact,
    EvidencePage,
    EvidenceRequest,
    EvidenceResponse,
    FinalTime,
    ReplayTime,
)

NAMESPACE = UUID("98c8e2d2-f442-4f1f-bcbd-df7eddd8f15e")
BYTE_LIMIT = 32 * 1024
READABLE_ARTIFACT_VERSIONS = (1, 2, 3, 4)
OCCUPANCY_NOTE = "People counts use assumed vehicle occupancy, not measured population."
RESULT_NOTE = "Model exits are not verified safety; staging is not suppression; exposure is not injury."
QUEUE_NOTE = "Slow traffic includes crew service stops and excludes demand waiting to enter the network."


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def count(value):
    return finite(value) and value >= 0 and value == int(value)


def mapping(value):
    return value if isinstance(value, dict) else {}


def horizon_for(result):
    """Return a result's simulated horizon in seconds, or None when the duration is invalid."""
    duration = mapping(result.get("scenario")).get("duration_minutes")
    return duration * 60 if finite(duration) and 0 < duration <= 60 else None


def validity(policy, scenario):
    """Port of the UI's mission ledger checks; missing fields never become zero."""
    summary = mapping(policy.get("summary"))
    required = ("exited_vehicles", "active_vehicles", "not_departed_vehicles", "exposed_vehicles",
                "crews_staged", "crews_returned", "exposed_crews", "crews_not_departed")
    if any(k not in summary for k in required + ("valid_engine_run", "valid_accounting",
                                               "simulated_seconds")):
        return "unavailable", "ACCOUNTING_FIELDS_MISSING"
    if summary["valid_engine_run"] is not True or summary["valid_accounting"] is not True:
        return "invalid", "INVALID_ENGINE_OR_ACCOUNTING"
    duration = scenario.get("duration_minutes")
    if not finite(duration) or not 0 < duration <= 60 or summary["simulated_seconds"] != duration * 60:
        return "invalid", "INCOMPLETE_HORIZON"
    if not all(count(scenario.get(k)) for k in ("vehicles", "people_per_vehicle", "crews")):
        return "invalid", "INVALID_DEMAND"
    vehicles, occupancy, crews = (scenario[k] for k in ("vehicles", "people_per_vehicle", "crews"))
    if vehicles <= 0 or occupancy <= 0 or not all(count(summary[k]) for k in required):
        return "invalid", "INVALID_COUNTS"
    unresolved = summary["active_vehicles"] + summary["not_departed_vehicles"]
    if (summary["exited_vehicles"] + unresolved != vehicles
            or ("unresolved_vehicles" in summary and summary["unresolved_vehicles"] != unresolved)
            or summary["exposed_vehicles"] > vehicles
            or summary["crews_returned"] > summary["crews_staged"]
            or summary["crews_staged"] + summary["crews_not_departed"] > crews
            or any(summary[k] > crews for k in required[4:])):
        return "invalid", "LEDGER_CONTRADICTION"
    return "available", None


def policies_for(result):
    """Index a result's policy recordings by policy id as (position, recording) pairs."""
    policies = result.get("policies")
    if not isinstance(policies, list):
        return {}
    indexed = {}
    for index, policy in enumerate(policies):
        if not isinstance(policy, dict) or policy.get("policy") not in ("baseline", "coordinated", "phased"):
            continue
        name = policy["policy"]
        # Duplicate policy identities are ambiguous rather than last-write-wins.
        indexed[name] = (index, policy) if name not in indexed else (-1, {})
    return indexed


def crew_records(policy, horizon):
    """Read crew timing records from a policy's final frame at the horizon, else None."""
    if horizon is None:
        return None
    frames = policy.get("frames")
    if not isinstance(frames, list) or not frames or not isinstance(frames[-1], dict):
        return None
    frame = frames[-1]
    records = frame.get("crews")
    if frame.get("t") != horizon or not isinstance(records, list):
        return None
    ids = set()
    for record in records:
        if not isinstance(record, dict):
            return None
        crew_id, scheduled = record.get("id"), record.get("scheduled_departure_s")
        if not isinstance(crew_id, str) or not crew_id or crew_id in ids or not finite(scheduled) or scheduled < 0:
            return None
        ids.add(crew_id)
        if any(k not in record for k in ("departed_s", "reached_staging_s", "returned_s")):
            return None
        departed, staged, returned = (record[k] for k in ("departed_s", "reached_staging_s", "returned_s"))
        if any(v is not None and (not finite(v) or not 0 <= v <= horizon)
               for v in (departed, staged, returned)):
            return None
        if (departed is not None and departed < scheduled
                or staged is not None and (departed is None or staged < departed)
                or returned is not None and (staged is None or returned < staged)):
            return None
    return records


def valid_crew_records(policy, scenario):
    """Return crew records only when the policy is valid and they match the crew counts."""
    if validity(policy, scenario)[0] != "available":
        return None
    records = crew_records(policy, scenario["duration_minutes"] * 60)
    if (records is None or len(records) != scenario["crews"]
            or sum(c["reached_staging_s"] is not None for c in records) != policy["summary"]["crews_staged"]
            or sum(c["returned_s"] is not None for c in records) != policy["summary"]["crews_returned"]
            or sum(c["departed_s"] is None for c in records) != policy["summary"]["crews_not_departed"]):
        return None
    return records


def samples_for(policy, horizon):
    """Return a policy's replay samples when they are ordered and cover the horizon, else None."""
    series = policy.get("series")
    if not isinstance(series, list) or len(series) < 2 or horizon is None:
        return None
    previous = -1
    for sample in series:
        if not isinstance(sample, dict) or not finite(sample.get("t")) or not previous < sample["t"] <= horizon:
            return None
        previous = sample["t"]
    if series[0]["t"] != 0 or series[-1]["t"] != horizon:
        return None
    return series


def valid_replay_sample(sample, scenario):
    """Check that a replay sample's counts are non-negative and conserve the vehicle total."""
    fields = ("exited", "active", "not_departed", "exposed", "crews_staged", "queue")
    return (
        bool(sample) and all(count(sample.get(k)) for k in fields)
        and sample["exited"] + sample["active"] + sample["not_departed"] == scenario["vehicles"]
        and sample["exposed"] <= scenario["vehicles"]
        and sample["crews_staged"] <= scenario["crews"]
    )


class Facts:
    def __init__(self, run_id, policy_id=None, prefix="", scope=None):
        self.run_id = UUID(str(run_id))
        self.policy_id = policy_id
        self.prefix = prefix
        self.scope = scope
        self.items = []

    def add(self, metric, value, unit=None, *, fields=(), derivation="retained artifact field",
            availability="available", reason=None, notes=(), crew_id=None):
        """Record one evidence fact with a stable identity, its source fields and availability."""
        identity = f"{self.run_id}.{self.policy_id or 'run'}.{crew_id or 'all'}.{metric}"
        if self.scope:
            identity += f".{self.scope.kind}.{self.scope.effective_time_s}"
        fact = EvidenceFact(
            fact_id=identity, run_id=self.run_id, metric=metric,
            value=value if availability == "available" else None, unit=unit,
            availability=availability, reason_code=reason, time_scope=self.scope,
            policy_id=self.policy_id, crew_id=crew_id, derivation=derivation,
            source_fields=[path for field in fields
                           if "/-1/" not in (path := field if field.startswith("/") else self.prefix + field)],
            notes=list(notes),
        )
        self.items.append(fact)
        return fact


OUTCOME_METRICS = {
    "people_exited": ("people", ("exited_vehicles",)),
    "people_unresolved": ("people", ("active_vehicles", "not_departed_vehicles")),
    "people_waiting": ("people", ("not_departed_vehicles",)),
    "people_exposed": ("people", ("exposed_vehicles",)),
    "crews_assigned": ("crew_vehicles", ()),
    "crews_staged": ("crew_vehicles", ("crews_staged",)),
    "crews_not_staged": ("crew_vehicles", ("crews_staged",)),
    "crews_exposed": ("crew_vehicles", ("exposed_crews",)),
    "crews_waiting": ("crew_vehicles", ("crews_not_departed",)),
    "crews_returned": ("crew_vehicles", ("crews_returned",)),
}


def outcomes(run_id, result, policy_id):
    """Build the outcome evidence facts (exits, waiting, exposure) for one policy of a run."""
    index, policy = policies_for(result).get(policy_id, (-1, {}))
    scenario = mapping(result.get("scenario"))
    status, reason = validity(policy, scenario) if policy else ("unavailable", "POLICY_NOT_AVAILABLE")
    facts = Facts(run_id, policy_id, f"/policies/{index}/summary/", FinalTime(effective_time_s=horizon_for(result)))
    summary = mapping(policy.get("summary"))
    for metric, (unit, keys) in OUTCOME_METRICS.items():
        value = None
        fields = list(keys)
        if unit == "people":
            fields.append("/scenario/people_per_vehicle")
            derivation = f"({' + '.join(keys)}) * scenario.people_per_vehicle"
            if status == "available":
                value = sum(summary[k] for k in keys) * scenario["people_per_vehicle"]
        elif metric in ("crews_assigned", "crews_not_staged"):
            fields.append("/scenario/crews")
            derivation = "scenario.crews" + (" - crews_staged" if metric == "crews_not_staged" else "")
            if status == "available":
                value = scenario["crews"] - (summary["crews_staged"] if metric == "crews_not_staged" else 0)
        else:
            derivation = keys[0]
            if status == "available":
                value = summary[keys[0]]
        facts.add(metric, value, unit, fields=fields, derivation=derivation,
                  availability=status, reason=reason, notes=[OCCUPANCY_NOTE] if unit == "people" else [])
    t95, t95_status, t95_reason = None, status, reason
    if status == "available":
        reached = summary["exited_vehicles"] >= math.ceil(scenario["vehicles"] * 0.95)
        candidate = summary.get("t95_seconds")
        if "t95_seconds" not in summary:
            t95_status, t95_reason = "unavailable", "THRESHOLD_TIME_MISSING"
        elif candidate is None and not reached:
            t95_status, t95_reason = "not_reached", "EXIT_THRESHOLD_NOT_REACHED"
        elif reached and finite(candidate) and 0 <= candidate <= horizon_for(result):
            t95 = candidate
        else:
            t95_status, t95_reason = "invalid", "THRESHOLD_CONTRADICTION"
    facts.add("t95_seconds", t95, "s", fields=["t95_seconds", "exited_vehicles", "/scenario/vehicles"],
              availability=t95_status, reason=t95_reason)
    series = samples_for(policy, horizon_for(result))
    queue_valid = series and all(count(s.get("queue")) for s in series)
    queue_status = status if status != "available" else "available" if queue_valid else "unavailable"
    queue_reason = reason if status != "available" else None if queue_valid else "QUEUE_SERIES_UNAVAILABLE"
    integral = None
    if queue_status == "available":
        integral = sum((a["queue"] + b["queue"]) / 2 * (b["t"] - a["t"])
                       for a, b in pairwise(series))
    facts.add("slow_vehicle_seconds", integral, "vehicle_s",
              fields=[f"/policies/{index}/series"], derivation="Trapezoidal integral of sampled queue",
              availability=queue_status, reason=queue_reason, notes=[QUEUE_NOTE])
    return facts.items


def crews(run_id, result, policy_id, crew_id=None):
    """Build crew staging and timing evidence facts for one policy, optionally one crew."""
    items = outcomes(run_id, result, policy_id)
    index, policy = policies_for(result).get(policy_id, (-1, {}))
    scenario = mapping(result.get("scenario"))
    records = valid_crew_records(policy, scenario) if policy else None
    facts = Facts(run_id, policy_id, f"/policies/{index}/frames/",
                  FinalTime(effective_time_s=horizon_for(result)))
    if records is None:
        facts.add("crew_timeline", None, availability="unavailable", reason="CREW_TIMELINE_UNAVAILABLE",
                  fields=[f"/policies/{index}/frames"], crew_id=crew_id)
        return items + facts.items
    selected = [c for c in records if crew_id is None or c["id"] == crew_id]
    if crew_id is not None and not selected:
        facts.add("crew_timeline", None, availability="unavailable", reason="CREW_NOT_FOUND", crew_id=crew_id)
    for record in selected:
        position = records.index(record)
        prefix = f"{len(policy['frames']) - 1}/crews/{position}/"
        for name in ("scheduled_departure_s", "departed_s", "reached_staging_s", "returned_s"):
            value = record[name]
            facts.add(name, value, "s", fields=[prefix + name], crew_id=record["id"],
                      availability="available" if value is not None else "not_reached",
                      reason=None if value is not None else "EVENT_NOT_REACHED")
        for name, first, last in (
            ("dispatch_to_staging_s", "scheduled_departure_s", "reached_staging_s"),
            ("departure_wait_s", "scheduled_departure_s", "departed_s"),
            ("travel_to_staging_s", "departed_s", "reached_staging_s"),
        ):
            available = record[last] is not None and record[first] is not None
            facts.add(name, record[last] - record[first] if available else None, "s",
                      fields=[prefix + first, prefix + last], derivation=f"{last} - {first}",
                      crew_id=record["id"], availability="available" if available else "not_reached",
                      reason=None if available else "EVENT_NOT_REACHED")
    return items + facts.items


def replay(run_id, result, policy_id, requested, common_time=None, require_common=False):
    """Build evidence facts for one policy at a requested or shared replay time."""
    index, policy = policies_for(result).get(policy_id, (-1, {}))
    scenario = mapping(result.get("scenario"))
    status, reason = validity(policy, scenario) if policy else ("unavailable", "POLICY_NOT_AVAILABLE")
    horizon = horizon_for(result)
    series = samples_for(policy, horizon)
    effective, sample, sample_index = None, {}, -1
    if status == "available":
        if requested > horizon:
            status, reason = "unavailable", "REPLAY_OUTSIDE_HORIZON"
        elif series is None:
            status, reason = "unavailable", "REPLAY_SERIES_UNAVAILABLE"
        elif require_common and common_time is None:
            status, reason = "unavailable", "NO_COMMON_REPLAY_TIMESTAMP"
        else:
            eligible = [(i, s) for i, s in enumerate(series)
                        if s["t"] <= requested and (not require_common or s["t"] == common_time)]
            if eligible:
                sample_index, sample = eligible[-1]
                effective = sample["t"]
            if not valid_replay_sample(sample, scenario):
                status, reason = "invalid", "REPLAY_LEDGER_INVALID"
    facts = Facts(run_id, policy_id, f"/policies/{index}/series/{sample_index}/",
                  ReplayTime(requested_time_s=requested, effective_time_s=effective))
    for metric, keys in (
        ("people_exited", ["exited"]), ("people_unresolved", ["active", "not_departed"]),
        ("people_waiting", ["not_departed"]), ("people_exposed", ["exposed"]),
        ("crews_staged", ["crews_staged"]), ("crews_not_staged", ["crews_staged"]),
        ("crews_assigned", []), ("slow_vehicles", ["queue"]),
    ):
        value = None
        fields = list(keys)
        unit = "people" if metric.startswith("people") else "vehicles" if metric == "slow_vehicles" else "crew_vehicles"
        derivation = " + ".join(keys)
        if status == "available":
            value = sum(sample[k] for k in keys)
        if unit == "people":
            fields.append("/scenario/people_per_vehicle")
            derivation = f"({derivation}) * scenario.people_per_vehicle"
            if value is not None:
                value *= scenario["people_per_vehicle"]
        if metric in ("crews_not_staged", "crews_assigned"):
            fields.append("/scenario/crews")
            derivation = "scenario.crews" + (" - crews_staged" if metric == "crews_not_staged" else "")
            if status == "available":
                value = scenario["crews"] - (sample["crews_staged"] if metric == "crews_not_staged" else 0)
        facts.add(metric, value, unit, fields=fields, derivation=derivation,
                  availability=status, reason=reason, notes=[OCCUPANCY_NOTE] if unit == "people" else [])
    # Old frames do not record exposure by crew. Never substitute the end-of-run count.
    facts.add("crews_exposed", None, "crew_vehicles", availability="unavailable",
              reason="CREW_EXPOSURE_NOT_RECORDED_AT_REPLAY_TIME")
    return facts.items


def assumptions(run_id, result):
    """Build evidence facts for a run's scenario inputs and model assumptions, with units."""
    facts = Facts(run_id)
    scenario = mapping(result.get("scenario"))
    units = {"lat": "degrees", "lon": "degrees", "radius_km": "km", "vehicles": "vehicles",
             "people_per_vehicle": "people/vehicle", "crews": "crew_vehicles", "duration_minutes": "min",
             "departure_minutes": "min", "hazard_radius_m": "m", "hazard_growth_m_min": "m/min",
             "hazard_start_minutes": "min", "background_speed_factor": "ratio"}
    allowed = [*units, "seed", "highway", "exit_direction", "traffic_mode", "policies"]
    for key in allowed:
        value = scenario.get(key)
        facts.add(f"assumption.{key}", value, units.get(key), fields=[f"/scenario/{key}"],
                  availability="available" if value is not None else "unavailable",
                  reason=None if value is not None else "ASSUMPTION_NOT_RETAINED",
                  notes=["Scenario input; not an observed population, ignition, or dispatch plan."])
    notes = result.get("assumptions")
    if isinstance(notes, list):
        for i, note in enumerate(notes):
            good = isinstance(note, str) and 0 < len(note) <= 4096
            facts.add(f"assumption.note.{i}", note if good else None, fields=[f"/assumptions/{i}"],
                      availability="available" if good else "unavailable",
                      reason=None if good else "ASSUMPTION_TEXT_UNAVAILABLE")
    return facts.items


def sources(run_id, result):
    """Only allowlisted metadata enters tool context; no URLs, records, or provider error text."""
    facts = Facts(run_id)
    network = mapping(result.get("network"))
    for key in ("source", "retrieved_at", "osm_sha256", "net_sha256", "attribution"):
        value = network.get(key)
        facts.add(f"network.{key}", value, fields=[f"/network/{key}"],
                  availability="available" if value is not None else "unavailable",
                  reason=None if value is not None else "SOURCE_FIELD_NOT_RETAINED",
                  notes=["Road network used by the simulator."])
    snapshots = mapping(result.get("sources")).get("sources")
    if not isinstance(snapshots, list):
        facts.add("source_context", None, availability="unavailable", reason="SOURCE_SNAPSHOT_NOT_RETAINED")
    else:
        for index, source in enumerate(snapshots):
            source = mapping(source)
            for key in ("name", "status", "retrieved_at", "incomplete"):
                value = source.get(key)
                facts.add(f"source.{index}.{key}", value, fields=[f"/sources/sources/{index}/{key}"],
                          availability="available" if value is not None else "unavailable",
                          reason=None if value is not None else "SOURCE_FIELD_NOT_RETAINED",
                          notes=["Observed context only; not applied as fire spread, traffic demand, or closures."])
            records = source.get("records")
            n = len(records) if isinstance(records, list) else records if count(records) else None
            facts.add(f"source.{index}.record_count", n, "records", fields=[f"/sources/sources/{index}/records"],
                      availability="available" if n is not None else "unavailable",
                      reason=None if n is not None else "RECORD_COUNT_UNAVAILABLE",
                      notes=["A zero record count does not verify absence of a hazard or closure."])
    traffic = mapping(result.get("traffic"))
    mode = mapping(result.get("scenario")).get("traffic_mode")
    matches = traffic.get("matches")
    applied = (mode == "provider_speed_limits" and traffic.get("status") == "applied"
               and isinstance(matches, list) and len(matches) > 0)
    # A snapshot marked unavailable (the daily TomTom budget was used up) records that no speeds were applied.
    withheld = traffic.get("status") == "unavailable" and matches == []
    known_application = applied or withheld or mode == "synthetic"
    facts.add("traffic.application",
              "matched_edge_speed_limits" if applied else "not_applied" if known_application else None,
              fields=["/scenario/traffic_mode", "/traffic/matches"],
              availability="available" if known_application else "unavailable",
              reason=None if known_application else "TRAFFIC_APPLICATION_UNVERIFIED",
              notes=["Frozen speed snapshot; does not initialize demand, queues, closures, or traffic signals."])
    for key in ("provider", "mode", "status", "retrieved_at", "matched_edges", "highway_edges"):
        value = traffic.get(key)
        facts.add(f"traffic.{key}", value, fields=[f"/traffic/{key}"],
                  availability="available" if value is not None else "unavailable",
                  reason=None if value is not None else "TRAFFIC_FIELD_NOT_RETAINED")
    facts.add("traffic.observed_at", None, availability="unavailable",
              reason="PROVIDER_OBSERVATION_TIMESTAMP_UNAVAILABLE",
              notes=["Capture time is retained separately; it is not the provider's observation time."])
    return facts.items


def comparison_kind(reference_id, alternative_id, results):
    """Classify two runs as a policy, controlled-variant, changed-conditions or incompatible comparison."""
    if reference_id == alternative_id:
        return "policy"
    reference, alternative = results[reference_id], results[alternative_id]
    bundle = reference.get("input_bundle_id")
    if not bundle or bundle != alternative.get("input_bundle_id"):
        return "changed_conditions"
    # A bundle identifier alone is not proof of controlled execution.
    input_hash = reference.get("input_bundle_sha256")
    if not input_hash or input_hash != alternative.get("input_bundle_sha256"):
        return "incompatible"
    if any(reference.get(k) != alternative.get(k) for k in ("engine", "engine_version")):
        return "incompatible"
    a, b = mapping(reference.get("scenario")), mapping(alternative.get("scenario"))
    fixed = (set(a) | set(b)) - {"vehicles", "departure_minutes", "policies"}
    if any(a.get(k) != b.get(k) for k in fixed):
        return "incompatible"
    return "controlled_variant"


def compare_facts(reference, alternative, kind):
    """Pair facts by metric and compute deltas where both sides are available and comparable."""
    comparisons = []
    by_metric = {f.metric: f for f in reference}
    for fact in alternative:
        other = by_metric.get(fact.metric)
        if other is None:
            continue
        reason = None
        if kind in ("changed_conditions", "incompatible"):
            reason = "INPUTS_NOT_CONTROLLED"
        elif not fact.time_scope or not other.time_scope or fact.time_scope != other.time_scope:
            reason = "TIME_SCOPES_DIFFER"
        elif (fact.availability != "available" or other.availability != "available"
              or not finite(fact.value) or not finite(other.value)):
            reason = "COMPARISON_EVIDENCE_UNAVAILABLE"
        comparisons.append(EvidenceComparison(
            comparison_id=f"{other.fact_id}__{fact.fact_id}",
            reference_fact_id=other.fact_id, alternative_fact_id=fact.fact_id,
            metric=fact.metric, delta=None if reason else fact.value - other.value,
            unit=fact.unit, availability="unavailable" if reason else "available", reason_code=reason,
            comparison_kind=kind, notes=["Delta is alternative minus reference.",
                                         "Evaluate resident and crew outcomes together; there is no combined score."],
        ))
    return comparisons


def paired_crew_facts(run_id, result, reference_id, alternative_id):
    """Compare the same crews' response timing between two policies of one run."""
    indexed = policies_for(result)
    reference = indexed.get(reference_id, (-1, {}))[1]
    alternative = indexed.get(alternative_id, (-1, {}))[1]
    scenario = mapping(result.get("scenario"))
    a, b = valid_crew_records(reference, scenario), valid_crew_records(alternative, scenario)
    facts = Facts(run_id, alternative_id, scope=FinalTime(effective_time_s=horizon_for(result)))
    reason = None
    if a is None or b is None:
        reason = "PAIRED_CREW_TIMELINE_UNAVAILABLE"
    elif ({c["id"]: c["scheduled_departure_s"] for c in a}
          != {c["id"]: c["scheduled_departure_s"] for c in b}):
        reason = "CREW_COHORTS_DIFFER"
    status = "unavailable" if reason else "available"
    by_id = {c["id"]: c for c in a or []}
    pairs = [(by_id[c["id"]], c) for c in b or [] if not reason
             and c["reached_staging_s"] is not None and by_id[c["id"]]["reached_staging_s"] is not None]
    fields = [f"/policies/{indexed.get(p, (-1,))[0]}/frames" for p in (reference_id, alternative_id)]
    notes = [f"Compared with {reference_id}; only crews arriving in both policies are paired.",
             "Assignment and nonarrival counts remain in the accompanying outcomes."]
    facts.add(f"paired.{reference_id}.count", len(pairs) if not reason else None, "crew_vehicles",
              fields=fields, availability=status, reason=reason, notes=notes)
    for name, start, end in (("response_delta_s", "scheduled_departure_s", "reached_staging_s"),
                             ("wait_delta_s", "scheduled_departure_s", "departed_s"),
                             ("travel_delta_s", "departed_s", "reached_staging_s")):
        differences = [(y[end] - y[start]) - (x[end] - x[start]) for x, y in pairs]
        facts.add(f"paired.{reference_id}.{name}", sum(differences) / len(pairs) if pairs else None, "s",
                  fields=fields, derivation="Mean of paired per-crew duration deltas; alternative minus reference",
                  availability="available" if pairs else "unavailable",
                  reason=reason or ("NO_PAIRED_ARRIVALS" if not pairs else None), notes=notes)
    return facts.items


def all_evidence(request: EvidenceRequest, results):
    """Answer an evidence request: facts per run and policy plus their comparisons."""
    facts, comparisons = [], []
    scope = getattr(request, "time_scope", None)
    runs = [str(r) for r in request.run_ids]
    common = None
    is_replay = scope is not None and scope.kind == "replay"
    if request.view == "comparison" and is_replay:
        times = []
        for run, policy_id in product(runs, request.policy_ids):
            policy = policies_for(results[run]).get(policy_id, (-1, {}))[1]
            series = samples_for(policy, horizon_for(results[run]))
            times.append({s["t"] for s in series if s["t"] <= scope.requested_time_s} if series else set())
        common = max(set.intersection(*times), default=None) if times else None
    grouped = {}
    for run in runs:
        result = results[run]
        if request.view == "sources":
            facts.extend(sources(run, result))
        elif request.view == "assumptions":
            facts.extend(assumptions(run, result))
        else:
            for policy_id in request.policy_ids:
                if is_replay:
                    items = replay(run, result, policy_id, scope.requested_time_s, common,
                                   require_common=request.view == "comparison")
                elif request.view == "crew":
                    items = crews(run, result, policy_id, request.crew_id)
                else:
                    items = outcomes(run, result, policy_id)
                grouped[(run, policy_id)] = items
                facts.extend(items)
    if request.view == "comparison":
        reference_key = (runs[0], request.policy_ids[0])
        for key, items in grouped.items():
            if key == reference_key:
                continue
            kind = comparison_kind(reference_key[0], key[0], results)
            comparisons.extend(compare_facts(grouped[reference_key], items, kind))
            if key[0] == reference_key[0] and not is_replay:
                facts.extend(paired_crew_facts(key[0], results[key[0]], reference_key[1], key[1]))
    return facts, comparisons


def planning_summary(pages):
    """Render retained facts for a saved plan; models never recalculate these values."""
    facts = {(f.get("policy_id"), f["metric"]): f for page in pages for f in page["facts"]}

    def number(policy, metric, *, delta=False):
        fact = facts.get((policy, metric), {})
        value = fact.get("value")
        if fact.get("availability") != "available" or not finite(value):
            return "not reached" if fact.get("availability") == "not_reached" else "unavailable"
        return f"{value:+.1f}" if delta else f"{value:g}"

    lines = []
    for policy in ("baseline", "coordinated", "phased"):
        if (policy, "people_exited") not in facts:
            continue
        lines.append(
            f"{policy.title()}: {number(policy, 'people_exited')} people exited; "
            f"{number(policy, 'people_unresolved')} unresolved "
            f"({number(policy, 'people_waiting')} not departed); "
            f"{number(policy, 'people_exposed')} exposed. Crew vehicles: "
            f"{number(policy, 'crews_staged')}/{number(policy, 'crews_assigned')} staged; "
            f"{number(policy, 'crews_not_staged')} not staged; "
            f"{number(policy, 'crews_waiting')} waiting; {number(policy, 'crews_exposed')} exposed. "
            f"Time to 95% of vehicle exits: {number(policy, 't95_seconds')}"
            f"{' seconds' if facts.get((policy, 't95_seconds'), {}).get('availability') == 'available' else ''}."
        )
        if policy != "baseline":
            lines.append(
                f"Paired mean changes vs baseline, among {number(policy, 'paired.baseline.count')} "
                f"crew vehicles arriving in both policies: dispatch-to-staging "
                f"{number(policy, 'paired.baseline.response_delta_s', delta=True)} seconds; "
                f"waiting {number(policy, 'paired.baseline.wait_delta_s', delta=True)} seconds; "
                f"travel {number(policy, 'paired.baseline.travel_delta_s', delta=True)} seconds. "
                "Excluded nonarrivals remain in the counts above."
            )
    gaps = []
    for (policy, metric), fact in facts.items():
        if policy is not None or not metric.startswith("source.") or not metric.endswith(".status"):
            continue
        prefix = metric.removesuffix(".status")
        status = fact["value"] if fact["availability"] == "available" else "unavailable"
        if status == "available":
            continue
        name = facts.get((None, prefix+".name"), {}).get("value") or "Source"
        count = number(None, prefix+".record_count")
        gaps.append(f"{name}: {str(status).replace('_', ' ')}; record count {count}.")
    if not lines:
        return ""
    return "\n\n".join([
        "Retained simulation evidence; people counts use assumed scenario vehicle occupancy.",
        *lines,
        *gaps,
        ("These are modeled exits and staging events, not verified safe destinations or suppression. "
         "Exposure is not an injury estimate. Missed staging does not establish lost positions or "
         "communications; its cause requires separate evidence."),
    ])


def build_evidence(request: EvidenceRequest, results: dict, *, byte_limit=BYTE_LIMIT):
    """Results must be owner-authorized by the caller before entering this pure adapter."""
    results = {str(k): v for k, v in results.items()}
    refs = []
    for run_id in request.run_ids:
        result = results.get(str(run_id))
        if not isinstance(result, dict):
            raise IntegrationError("ARTIFACT_UNAVAILABLE", "A retained result is unavailable.", 410)
        version = result.get("schema_version")
        if type(version) is not int or version not in READABLE_ARTIFACT_VERSIONS:
            raise IntegrationError("ARTIFACT_VERSION_UNSUPPORTED", "This result version cannot be read.", 409)
        try:
            artifact_hash = digest(result)
        except (ValueError, TypeError):
            raise IntegrationError("ARTIFACT_INVALID", "The result contains invalid JSON values.", 409) from None
        refs.append(ArtifactReference(run_id=run_id, sha256=artifact_hash, artifact_schema_version=version))
    facts, comparisons = all_evidence(request, results)
    content_key = digest({"request": request.model_dump(mode="json", exclude={"cursor"}),
                          "artifacts": [ref.model_dump(mode="json") for ref in refs]})
    evidence_id = uuid5(NAMESPACE, content_key)
    items = [("facts", item) for item in facts] + [("comparisons", item) for item in comparisons]
    offset = 0
    if request.cursor:
        try:
            cursor = json.loads(base64.urlsafe_b64decode(request.cursor.encode()))
            offset = cursor["offset"]
            if (set(cursor) != {"key", "offset"} or cursor["key"] != content_key
                    or type(offset) is not int or not 0 < offset < len(items)):
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            raise IntegrationError("CURSOR_INVALID", "The cursor does not match this request and artifact.", 409)
    reasons = sorted({item.reason_code for item in [*facts, *comparisons] if item.reason_code})
    states = {item.availability for item in [*facts, *comparisons]}
    availability = ("invalid" if "invalid" in states else
                    "unavailable" if not states or "unavailable" in states else
                    "not_reached" if states == {"not_reached"} else "available")

    def page(end):
        has_more = end < len(items)
        next_cursor = (base64.urlsafe_b64encode(canonical({"key": content_key, "offset": end}).encode()).decode()
                       if has_more else None)
        return EvidenceResponse(
            evidence_id=evidence_id, view=request.view, artifact_refs=refs, availability=availability,
            facts=[item for kind, item in items[offset:end] if kind == "facts"],
            comparisons=[item for kind, item in items[offset:end] if kind == "comparisons"],
            missing_reasons=reasons,
            pagination=EvidencePage(next_cursor=next_cursor, has_more=has_more, total_items=len(items),
                                    returned_items=end - offset, offset=offset, byte_limit=byte_limit),
            notes=[RESULT_NOTE, "Availability describes the whole evidence set; inspect each fact.",
                   "Read every page before drawing a comparison."],
        )
    # Binary search bounds the actual UTF-8 response, including metadata and cursors.
    low, high = offset, len(items)
    while low < high:
        middle = (low + high + 1) // 2
        if len(page(middle).model_dump_json().encode()) <= byte_limit:
            low = middle
        else:
            high = middle - 1
    if (low == offset and items) or len(page(low).model_dump_json().encode()) > byte_limit:
        raise IntegrationError("EVIDENCE_ITEM_TOO_LARGE", "An evidence item exceeds the response budget.", 422)
    return page(low)
