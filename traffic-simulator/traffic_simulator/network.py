import hashlib
import json
import logging
import shutil
import subprocess  # nosec B404 - runs only server-built netconvert argument lists, never a shell
import time
from pathlib import Path

import sumolib
from shapely.geometry import LineString, Point, shape
from shapely.ops import transform, unary_union

from traffic_simulator import network_cache, osm_corridors, osm_extracts, settings
from traffic_simulator.demand import NoEligibleOrigins, demand_for
from traffic_simulator.models import PreparationScenario, Scenario
from traffic_simulator.road_planning import RoadCoverageUnavailable
from traffic_simulator.timings import Timings

# New automatic networks use exact dated source bytes and the complete-way extraction recipe.
NETWORK_CACHE_VERSION = "geofabrik-corridors-v3"
NETCONVERT_OPTIONS = ("--geometry.remove", "true", "--junctions.join", "true",
                      "--tls.guess", "true", "--tls.discard-simple", "true", "--ramps.guess", "true",
                      "--no-turnarounds", "true", "--keep-edges.by-vclass", "passenger",
                      "--output.original-names", "true")
logger = logging.getLogger(__name__)


class NetworkTooLarge(ValueError):
    """Only an explicit size rejection permits preparation to reduce the extent."""

    def __init__(self, message, *, edge_count=None, source_manifest=None):
        super().__init__(message)
        self.edge_count = edge_count
        self.source_manifest = source_manifest


def binary(name):
    """Return the path of a SUMO tool (sumo, netconvert): the one on PATH, else the installed SUMO package's."""
    found = shutil.which(name)
    if found:
        return found
    candidate = Path(sumolib.checkBinary(name))
    if candidate.exists():
        return str(candidate)
    raise RuntimeError(f"{name} is not installed; install the locked project dependencies")


def discover(area, progress=lambda *_: None, check_cancel=lambda: None, *, timings=None):
    """Return candidate corridors from the canonical source; expensive work belongs to the existing worker."""
    timings = timings if timings is not None else Timings()
    _, metadata = area_network(area, progress, check_cancel, timings=timings)
    return metadata["corridor_discovery"]


def corridor_discovery(raw, area, source_manifest=None):
    """Parse and retain the road snapshot's corridor identity and source provenance."""
    geometry = osm_corridors.geometry_from_xml(raw)
    return {
        "geometry": geometry,
        "highways": osm_corridors.rank_corridors(geometry, area.lat, area.lon, area.radius_km),
        "retrieved_at": time.time(), "source": "OpenStreetMap via Geofabrik",
        "source_manifest": source_manifest,
        "osm_sha256": hashlib.sha256(raw).hexdigest(), "attribution": osm_corridors.ATTRIBUTION,
        "study_bounds": osm_corridors.study_bounds(area.lat, area.lon, area.radius_km),
        "coverage": osm_corridors.coverage_from_xml(raw, geometry),
        "meaning": "Candidate road corridors; no inference of road safety, closures or emergency access",
    }


def command(args, check_cancel, cwd=None, timeout=None, max_output_bytes=None,
            size_failure="Extracted road XML exceeds the configured size limit"):
    """Run a server-built tool command such as netconvert, checking cancellation each second; return its output."""
    timeout = settings.road_preparation_timeout_seconds() if timeout is None else timeout
    check_cancel()
    output = (Path(args[args.index("--output")+1])
              if max_output_bytes is not None and "--output" in args else None)

    def check_output():
        if output is not None and output.exists() and output.stat().st_size > max_output_bytes:
            raise NetworkTooLarge(size_failure)
    # No shell invocation; all file names and switches are server generated.
    # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
    with subprocess.Popen(  # nosec B603 - server-built argument list, no shell
            args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) as proc:
        start = time.monotonic()
        while True:
            try:
                text, _ = proc.communicate(timeout=1)
                check_output()
                break
            except subprocess.TimeoutExpired:
                try:
                    check_cancel()
                    check_output()
                    if timeout and time.monotonic() - start > timeout:
                        raise TimeoutError("Road preparation exceeded its configured native deadline")
                except BaseException:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    raise
        if proc.returncode:
            # Tool output names server paths and inputs; it stays in the server log only.
            name = Path(args[0]).name
            logger.warning("%s exited with status %s: %s", name, proc.returncode, text[-2000:])
            raise RuntimeError(f"{name} could not build the road network. Retry later or choose another area.")
        return text


def network_key(area, source_manifest=None):
    """Exact geographic inputs, immutable source bytes, extraction/conversion recipes and engine select a network."""
    identity = [area.lat, area.lon, area.radius_km, NETCONVERT_OPTIONS, NETWORK_CACHE_VERSION, sumolib.__version__,
                osm_extracts.identity(source_manifest) if source_manifest is not None else None]
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:24]


def build_network(area, key, folder, progress, check_cancel, timings, source_manifest):
    """Extract the selected canonical snapshot, convert it with netconvert and atomically publish its cache entry."""
    # Per-call scratch files: concurrent preparations of one area never share partial output.
    with network_cache.scratch(folder) as scratch:
        with timings.measure("osm_extract"):
            try:
                raw, _ = osm_extracts.extract(area, source_manifest, scratch, command, progress, check_cancel)
            except ValueError as exc:
                if str(exc) == "Extracted road XML exceeds the configured size limit":
                    raise NetworkTooLarge(str(exc)) from None
                raise
        discovery = corridor_discovery(raw, area, source_manifest)
        osm = scratch / "roads.osm.xml"
        osm.write_bytes(raw)
        progress("Converting road directions, junctions, and lanes for SUMO", 0.09)
        built = scratch / network_cache.NETWORK
        with timings.measure("netconvert"):
            log = command([binary("netconvert"), "--osm-files", str(osm), "--output-file", str(built),
                           "--proj", osm_corridors.projection(area.lat, area.lon),
                           *NETCONVERT_OPTIONS], check_cancel)
        metadata = {"source": "OpenStreetMap via Geofabrik", "retrieved_at": time.time(),
                    "source_manifest": source_manifest,
                    "osm_sha256": hashlib.sha256(raw).hexdigest(),
                    "net_sha256": hashlib.sha256(built.read_bytes()).hexdigest(),
                    "attribution": osm_extracts.ATTRIBUTION,
                    "conversion_log_tail": log[-2500:], "cache_key": key,
                    "corridor_discovery": discovery, "study_bounds": discovery["study_bounds"],
                    "projection": "Local azimuthal equidistant"}
        network_cache.install(folder, scratch, metadata)
    return metadata


def area_network(area, progress, check_cancel, *, timings, source_manifest=None):
    """Return the SUMO road network file and metadata for an area from the network cache, building it on a full miss.

    Reuses this worker's local copy, else (AWS mode) the shared copy in the artifacts bucket, each only after its
    SHA-256 check; otherwise downloads and converts the roads, keeps them locally and shares them. Adding a network
    evicts the least recently used others past the worker's cache bounds. timings receives network_cache_hit,
    network_cache_download, or osm_extract, netconvert and (AWS mode) network_cache_upload.
    """
    if source_manifest is None:
        source_manifest = osm_extracts.plan(area, command, progress, check_cancel, timings)
    else:
        source_manifest = {**source_manifest, "study_bounds":
                           osm_corridors.study_bounds(area.lat, area.lon, area.radius_km)}
    key = network_key(area, source_manifest)
    folder = settings.data_dir("networks", key)
    started = time.monotonic()
    metadata = network_cache.reuse(folder)
    if metadata is not None and not matches_source(metadata, source_manifest):
        metadata = None
    if metadata is not None:
        timings.add("network_cache_hit", time.monotonic() - started)
        return folder / network_cache.NETWORK, metadata
    metadata = network_cache.fetch_shared(key, folder)
    if metadata is not None and not matches_source(metadata, source_manifest):
        metadata = None
    if metadata is not None:
        timings.add("network_cache_download", time.monotonic() - started)
    else:
        try:
            metadata = build_network(area, key, folder, progress, check_cancel, timings, source_manifest)
        except (NetworkTooLarge, RoadCoverageUnavailable) as exc:
            exc.source_manifest = source_manifest
            raise
        sharing = time.monotonic()
        if network_cache.share(key, folder, metadata):
            timings.add("network_cache_upload", time.monotonic() - sharing)
    network_cache.evict(keep=key)
    return folder / network_cache.NETWORK, metadata


def matches_source(metadata, selected):
    """Only a network explicitly retaining this exact source identity can satisfy a new automatic preparation."""
    try:
        return osm_extracts.identity(metadata["source_manifest"]) == osm_extracts.identity(selected)
    except (KeyError, TypeError):
        return False


def source_way_ids(edge):
    """Read netconvert's original OSM way IDs from its lane parameters, including merged ways."""
    return sorted({int(value) for lane in edge.getLanes()
                   for value in lane.getParam("origId", "").split() if value.isdecimal()})


def exit_gates(edges, edge_lines, fire_xy, scenario, direction):
    """Existing model gate rules, applied to one candidate corridor and direction."""
    axis, sign = {"north": (1, 1), "south": (1, -1), "east": (0, 1), "west": (0, -1)}[direction]
    candidates = [edge for edge in edges if edge.getID() in edge_lines
                  and (edge.getShape()[-1][axis] - edge.getShape()[0][axis]) * sign > 0
                  and edge_lines[edge.getID()].distance(Point(fire_xy)) > scenario.hazard_radius_m + 500]
    candidates.sort(key=lambda edge: (edge.getShape()[-1][axis] - fire_xy[axis]) * sign, reverse=True)
    gates = []
    for edge in candidates:
        if all(edge_lines[edge.getID()].distance(edge_lines[gate.getID()]) > 400 for gate in gates):
            gates.append(edge)
        if len(gates) == 3:
            break
    return gates


def prepare_network(scenario, progress, check_cancel, *, timings=None, route_selected=None, source_manifest=None):
    """Validate a concrete or automatic corridor against the exact converted OSM road snapshot.

    The shortlist and SUMO input come from the same download/cache entry. Original
    OSM way IDs identify new corridors; the legacy California selection retains its
    documented geometric matching for older direct scenarios. Neither establishes safety.
    """
    timings = timings if timings is not None else Timings()
    source_options = {"source_manifest": source_manifest} if source_manifest is not None else {}
    netfile, metadata = area_network(scenario, progress, check_cancel, timings=timings, **source_options)
    discovery = metadata["corridor_discovery"]
    with timings.measure("discovery"):
        roads = discovery["highways"]
        coverage = {**discovery.get("coverage", {}),
                    "nearest_corridor_distance_km": min((road["distance_km"] for road in roads), default=None)}
        automatic = isinstance(scenario, PreparationScenario)
        if automatic:
            roads = [road for road in roads if road["distance_km"] <= scenario.radius_km * 0.8]
        else:
            roads = [road for road in roads if road["corridor_id"] == scenario.highway
                     or osm_corridors.legacy_highway(road["route_ref"]) == scenario.highway]
            if not roads:
                raise ValueError("Selected highway does not intersect this area. Discover highways before running.")
            if roads[0]["distance_km"] > scenario.radius_km * 0.8:
                raise ValueError("Highway is too close to the area boundary. Increase the area radius.")
    progress("Validating road corridors and assumed model exits", 0.1)
    check_cancel()
    loading = time.monotonic()
    net = sumolib.net.readNet(str(netfile), withInternal=False)
    edges = [e for e in net.getEdges() if e.allows("passenger") and e.getLength() > 15]
    largest = settings.max_network_edges()
    if len(edges) > largest:
        raise NetworkTooLarge(
            f"Network has {len(edges):,} eligible edges, exceeding the {largest:,}-edge limit.",
            edge_count=len(edges), source_manifest=metadata.get("source_manifest"))
    if len(edges) < 10:
        raise RoadCoverageUnavailable("NO_DRIVABLE_ROADS",
                                      "Too few connected passenger roads in this study extent.",
                                      details={**coverage, "edge_count": len(edges)},
                                      source_manifest=metadata.get("source_manifest"))
    fire_xy = net.convertLonLat2XY(scenario.lon, scenario.lat)
    edge_lines = {e.getID(): LineString(e.getShape()) for e in edges if len(e.getShape()) >= 2}
    by_way = {}
    for edge in edges:
        for identifier in source_way_ids(edge):
            by_way.setdefault(identifier, []).append(edge)
    selected_route, highway_edges, gates, direction = None, [], [], None
    for road in roads:
        check_cancel()
        matching = {edge.getID(): edge for identifier in road["source_way_ids"]
                    for edge in by_way.get(identifier, ()) if edge.getID() in edge_lines}
        if not matching and not automatic and not scenario.highway.startswith("osm:"):
            selected = [feature for feature in discovery["geometry"]["features"]
                        if feature["properties"]["corridor_id"] == road["corridor_id"]]
            geometry = unary_union([shape(feature["geometry"]) for feature in selected])
            projected = transform(lambda x, y, z=None: net.convertLonLat2XY(x, y), geometry)
            matching = {edge.getID(): edge for edge in edges if edge.getID() in edge_lines
                        and edge_lines[edge.getID()].interpolate(0.5, normalized=True).distance(projected) < 45}
        candidate_edges = list(matching.values())
        directions = ("south", "north", "east", "west") if automatic else (scenario.exit_direction,)
        choices = [(exit_gates(candidate_edges, edge_lines, fire_xy, scenario, value), value)
                   for value in directions]
        # An isolated highway can have geometrically plausible gates while no actual civilian/crew plan can use
        # them. Automatic selection validates the existing seeded demand on this same authentic graph. Individual
        # unresolved trips remain in the demand ledger; only a wholly unusable corridor/direction is rejected.
        for candidate_gates, candidate_direction in sorted(choices, key=lambda choice: len(choice[0]), reverse=True):
            if not candidate_gates:
                continue
            if automatic:
                progress(f"Checking civilian trips and crew approaches on {road['label']}", 0.1)
                candidate_scenario = Scenario.model_validate({
                    **scenario.model_dump(), "highway": road["corridor_id"], "exit_direction": candidate_direction,
                })
                try:
                    civilians, crews = demand_for(candidate_scenario, net, edges, edge_lines, candidate_gates,
                                                 fire_xy, check_cancel=check_cancel)
                except NoEligibleOrigins:
                    continue
                if not any(person["route"] for person in civilians):
                    continue
                if scenario.crews and not any(crew["route"] for crew in crews):
                    continue
            selected_route, highway_edges, gates, direction = (
                road, candidate_edges, candidate_gates, candidate_direction)
            break
        if selected_route is not None:
            break
    if selected_route is None:
        if automatic:
            raise RoadCoverageUnavailable(
                "NO_ROUTABLE_CORRIDOR",
                "No corridor in this study extent supports the requested civilian and crew routes.",
                details={**coverage, "edge_count": len(edges), "interior_corridors": len(roads)},
                source_manifest=metadata.get("source_manifest"))
        raise ValueError("No suitable model exit gate on this highway. Choose another exit direction or area.")
    resolved = Scenario.model_validate({
        **scenario.model_dump(),
        "highway": selected_route["corridor_id"] if automatic else scenario.highway,
        "exit_direction": direction,
    })
    if route_selected is not None:
        route_selected(selected_route)
    features = []
    selected_ids = {e.getID() for e in highway_edges}
    for edge in edges:
        coords = [list(net.convertXY2LonLat(x, y)) for x, y in edge.getShape()]
        features.append({"type": "Feature", "geometry": {"type": "LineString", "coordinates": coords},
                         "properties": {"id": edge.getID(), "highway": edge.getID() in selected_ids,
                                        "lanes": edge.getLaneNumber(), "speed_m_s": edge.getSpeed(),
                                        "source_way_ids": source_way_ids(edge)}})
    public = {"type": "FeatureCollection", "features": features}
    metadata.update(highway=resolved.highway, corridor_label=selected_route["label"], edge_count=len(edges),
                    highway_edge_count=len(highway_edges), selected_route=selected_route,
                    resolved_scenario=resolved.model_dump(),
                    gates=[{"edge": g.getID(), "coordinate": list(net.convertXY2LonLat(*g.getShape()[-1])),
                            "label": "Assumed model exit; safety unverified"} for g in gates],
                    limitations=[
                        "Current OSM geometry and inferred missing lanes/speeds; not a historical reconstruction.",
                        "Signal programs inferred from OSM; not field-verified traffic-controller plans.",
                        ("Legacy corridor matching may use geometry within 45 m; it is not a lane crosswalk."
                         if not resolved.highway.startswith("osm:")
                         else "OSM source way IDs identify the corridor; access attributes remain unverified."),
                        "Exit gates model network departure, not arrival at a verified safe refuge.",
                        "The bounded network omits downstream bottlenecks outside its extent.",
                    ])
    timings.add("network_load", time.monotonic() - loading)
    return netfile, net, edges, edge_lines, gates, fire_xy, public, metadata
