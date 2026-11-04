"""Seeded civilian trips and crew plans shared by preparation and simulation, with unchanged route policy."""
import random

from shapely.geometry import Point


class NoEligibleOrigins(ValueError):
    """A valid graph has no civilian origin under the declared demand-area rules."""


def demand_for(scenario, net, edges, lines, gates, fire_xy, *, check_cancel=lambda: None):
    """Sample seeded civilian trips and crew plans inside the scenario's demand circle."""
    rng = random.Random(scenario.seed)
    # Sample only inside the declared circular demand area; OSM download uses an envelope.
    origins = [e for e in edges if e.getID() in lines and e not in gates
               and lines[e.getID()].distance(Point(fire_xy)) <= scenario.radius_km * 850
               and e.getLength() > 40 and e.getOutgoing()]
    if not origins:
        raise NoEligibleOrigins("No eligible vehicle origins in this area")
    weights = [min(e.getLength(), 500) / (1 if e.getSpeed() < 20 else 3) for e in origins]
    route_cache = {}
    civilians = []
    for index in range(scenario.vehicles):
        check_cancel()
        edge = rng.choices(origins, weights=weights)[0]
        if edge.getID() not in route_cache:
            options = []
            for gate in gates:
                route, cost = net.getFastestPath(edge, gate, vClass="passenger")
                if route:
                    options.append((cost, [e.getID() for e in route]))
            route_cache[edge.getID()] = min(options, default=(None, None), key=lambda v: v[0])[1]
        coordinate = lines[edge.getID()].interpolate(0.1, normalized=True)
        distance = coordinate.distance(Point(fire_xy))
        departure_fraction = rng.random()
        civilians.append({
            "id": f"evac-{index}", "origin": edge.getID(), "route": route_cache[edge.getID()],
            "depart": round(departure_fraction * scenario.departure_minutes * 60, 1),
            "departure_fraction": departure_fraction,
            "distance_m": distance, "people": scenario.people_per_vehicle,
            "origin_xy": [coordinate.x, coordinate.y],
        })
    # Synthetic crew bases at the outer area and staging points outside initial exclusion.
    staging = sorted([e for e in origins if lines[e.getID()].distance(Point(fire_xy))
                      > scenario.hazard_radius_m + 750],
                     key=lambda e: lines[e.getID()].distance(Point(fire_xy)))
    bases = sorted(origins, key=lambda e: lines[e.getID()].distance(Point(fire_xy)), reverse=True)
    crews = []
    for index in range(scenario.crews):
        chosen = None
        for base in bases[index:index + 30]:
            check_cancel()
            for stage in staging[:30]:
                incoming, _ = net.getFastestPath(base, stage, vClass="passenger")
                outgoing, _ = net.getFastestPath(stage, gates[index % len(gates)], vClass="passenger")
                if incoming and outgoing and len(incoming) >= 3:
                    chosen = {
                        "id": f"crew-{index}", "origin": base.getID(), "stage": stage.getID(),
                        "route": [e.getID() for e in incoming] + [e.getID() for e in outgoing][1:],
                        "stage_index": len(incoming) - 1, "depart": 120 + index * 45,
                        "people": 0, "origin_xy": list(base.getShape()[0]),
                    }
                    break
            if chosen:
                break
        crews.append(chosen or {"id": f"crew-{index}", "route": None, "people": 0,
                                "depart": 120 + index * 45})
    return civilians, crews

