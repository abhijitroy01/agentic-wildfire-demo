# Firetruck Response Tracker

## Responsibility and boundaries

The deployed image serves `api.private:app` from `firetruck-simulator/`. Its authenticated `PUT`, `GET` and `DELETE /internal/runs/{id}` routes are reached only through `backend/firetruck_broker.py`. The broker assigns run IDs and sends the owner digest in `X-Firetruck-Owner`; another owner receives 404. Status is polled; the private service has no streaming endpoint.

| Module | Responsibility |
|---|---|
| `api/private.py` | Owner checks, attempt/lease fencing, update tasks and durable evidence |
| `simulator/engine.py` | Synthetic unit movement, water/crew telemetry and elapsed-time suppression phases |
| `simulator/truck_manager.py`, `behaviors.py`, `movement.py` and `api/models/` | Simulation state and typed request/telemetry models |
| `config/settings.py` | Limits consumed from deployment settings |
| `backend/firetruck_broker.py` | Visitor admission, private calls, polling and owned report persistence |
| `backend/runtime/agents/firetruck_report_agent.py` | No-tool report grounded in signed saved evidence |

`api.private:app` is the service's only application; the local REST/WebSocket API was removed on 2026-10-19. SUMO crew staging and synthetic firetruck suppression are separate models; neither is live dispatch or validated containment.

## Exercise behavior

The standalone exercise stays a labeled synthetic exercise, separate from the numbered SUMO stages. It opens from the “Standalone firetruck exercise” button in 3 · Response Tracker, which stays the current step while it is open. “Return to Response Tracker”, or Escape while its map is docked, closes it and returns to the stage-3 replay. It is not linked to the selected run or policy.

- Arrival by role: engines, tankers and brush units start suppression. Command and rescue units stage at their assigned point with the pump off, so they draw no water and never resupply. `simulator/constants.py:PUMPING_UNIT_TYPES` is the one declaration of which unit types pump.
- Flow limit: suppression pressures give 450–500 GPM (`config/settings.py:get_flow_rate_for_pressure`), within the 1,500 GPM limit of `TruckWater`. The values are illustrative, not a fleet specification.
- Alert rule: a water recommendation names only a unit in suppression with 20–40% of its tank left. Idle, en-route, staged and resupplying units get none.
- Limitations label: each snapshot's `limitations` states that phases follow a fixed elapsed-time schedule, crew rotation is not modeled, and fire growth, weather and safety values are illustrative. Runs saved earlier keep their earlier label.

## Durability and extension rules

Each tick renews a DynamoDB lease and publishes an S3 snapshot. Expired live runs read as interrupted. Every write requires the attempt's unexpired lease. Stop retains evidence. Start failure is explicit. Running snapshots cap timeline events with `omitted_events`; final snapshots retain all events. Replaced snapshots are deleted, and lifecycle handles abandoned objects.

Run lifetime, per-task capacity, per-session runs/streams, stream duration and event bounds come from `deployment/settings.toml`. Starts reserve a slot before awaited persistence/engine work; in-progress reservations count toward capacity. Duplicate receipts are checked before rejecting capacity, and reservations release on failure or cancellation.

Keep engine behavior independent of HTTP and persistence. New report fields must come from the typed saved snapshot and retain provenance. Extend the existing engine rather than cloning policy or report logic into the broker.

## Verification

The canonical quality gate runs this suite in its own process. To run it alone, use `(cd firetruck-simulator && ../.venv/bin/python -m pytest tests -q)`. It covers engine/lifecycle, the seeded MODEL-02 exercise and its limitations label, private ownership, interruption and durable evidence. MODEL-02: in a seeded 10-minute standalone run, command and rescue units stage with the pump off, published water telemetry validates against its model, water alerts name only pumping units, and staged units are never in the danger zone. `deployment/acceptance/accept_firetruck.py` verifies the deployed broker/report workflow. Local tests do not replace AWS acceptance.
