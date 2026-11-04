"""One canonical road source: dated public Geofabrik PBF snapshots, extracted locally with Osmium.

Publisher polygons select coverage for the actual study bounds. Source and derived-network caches never substitute
different regions/dates for an unavailable selection. Complete ways and restriction members remain OSM objects.
"""
import hashlib
import json
import logging
import random
import re
import shutil
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree

import httpx
from shapely.geometry import Point, box, shape
from shapely.ops import unary_union

from traffic_simulator import osm_corridors, osm_source_cache, settings
from traffic_simulator.errors import IntegrationError
from traffic_simulator.road_planning import RoadCoverageUnavailable

INDEX_URL = "https://download.geofabrik.de/index-v1.json"
PUBLISHER = "download.geofabrik.de"
RECIPE_VERSION = "geofabrik-roads-smart-restrictions-v1"
OSMIUM_VERSION = "1.19.1"
INDEX_MAX_BYTES = 16 * 1024 * 1024  # A metadata protocol envelope, separate from regional/road-byte limits.
ATTRIBUTION = "© OpenStreetMap contributors, ODbL 1.0; extracts by Geofabrik"
logger = logging.getLogger(__name__)


def invalid(message="The canonical road publisher returned an unusable snapshot."):
    return IntegrationError("OSM_SOURCE_INVALID", message, 502)


def publisher_url(value):
    """Accept public publisher files only, including publisher redirects; no credentials, query or alternate host."""
    if not isinstance(value, str):
        raise invalid()
    parsed = urlparse(value)
    try:
        valid = (parsed.scheme == "https" and parsed.hostname == PUBLISHER and parsed.port in (None, 443)
                 and parsed.username is None and parsed.password is None and not parsed.query and not parsed.fragment
                 and ".." not in parsed.path.split("/") and parsed.path.startswith("/"))
    except ValueError:
        valid = False
    if not valid:
        raise invalid("The road-source URL failed the public publisher boundary check.")
    return value


@contextmanager
def client(check):
    """HTTP lifetime inside an owned transport child; the parent can terminate an idle socket read."""
    seconds = settings.osm_source_http_timeout_seconds()
    http = httpx.Client(timeout=None if seconds == 0 else seconds, follow_redirects=False, trust_env=False)
    try:
        check()
        yield http
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        retryable = status in (429, 500, 502, 503, 504)
        try:
            delay = min(settings.osm_source_retry_max_seconds(),
                        max(0, int(exc.response.headers.get("retry-after", "0"))))
        except ValueError:
            delay = 0
        logger.warning("osm_source_http_failure status=%s retryable=%s", status, retryable)
        raise IntegrationError("OSM_SOURCE_UNAVAILABLE", "The canonical OSM road source rejected the request.",
                               503 if retryable else 502, retryable=retryable,
                               retry_after=delay or None, reason=f"HTTP_{status}") from None
    except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
        raise IntegrationError("OSM_SOURCE_UNAVAILABLE", "The canonical OSM road source is unavailable.", 503,
                               retryable=True, reason=type(exc).__name__) from None
    finally:
        http.close()


def transport(operation, payload, check, progress=None, maximum_bytes=INDEX_MAX_BYTES):
    """Fixed road operations cross the worker's bounded owned-process protocol; byte files stay on owned scratch."""
    from traffic_simulator.transport_work import run_transport

    if operation not in ("index", "resolve", "download"):
        raise invalid("The road transport operation is invalid.")
    attempts = settings.osm_source_max_attempts()
    for attempt in range(attempts):
        check()
        try:
            return run_transport("traffic_simulator.osm_transport", operation, payload, maximum_bytes, check,
                                 progress=progress)
        except IntegrationError as exc:
            # Public read operations only. Malformed data, capacity, coverage, local process and 4xx
            # failures do not improve by replaying the request. A download retains its selected dated URL.
            if exc.code != "OSM_SOURCE_UNAVAILABLE" or not exc.retryable or attempt + 1 == attempts:
                raise
            delay = min(settings.osm_source_retry_max_seconds(),
                        max(exc.retry_after or 0, random.uniform(0.5, 1.0) * 2 ** attempt))
            if progress:
                progress(f"Road publisher temporarily unavailable; retrying request {attempt + 2}/{attempts}",
                         0.04)
            pause(delay, check)
    raise AssertionError("Road-source attempts must be positive")


def pause(seconds, check):
    """Backoff retains parent cancellation and the source/job lease fence; it is not an operation deadline."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        check()
        time.sleep(min(0.1, max(0, deadline - time.monotonic())))
    check()


def read_bytes(http, url, maximum, check):
    with http.stream("GET", publisher_url(url)) as response:
        response.raise_for_status()
        data = bytearray()
        for chunk in response.iter_bytes():
            check()
            data += chunk
            if len(data) > maximum:
                raise invalid("Road-source metadata exceeds its supported envelope.")
    return bytes(data)


def fetch_index(check):
    """Read bounded canonical publisher metadata inside its cancellable owned process."""
    with client(check) as http:
        try:
            value = json.loads(read_bytes(http, INDEX_URL, INDEX_MAX_BYTES, check))
        except ValueError:
            raise invalid() from None
    if not isinstance(value, dict) or value.get("type") != "FeatureCollection":
        raise invalid("The publisher region index is invalid.")
    return value


def index(check):
    """Daily publisher geometry metadata is reusable; every new plan still resolves its current dated files."""
    path = settings.data_dir("osm-index") / "index-v1.json"
    if path.exists() and time.time() - path.stat().st_mtime < settings.osm_index_cache_seconds():
        try:
            value = json.loads(path.read_bytes())
        except (OSError, ValueError):
            value = None
        if isinstance(value, dict) and value.get("type") == "FeatureCollection":
            return value
    value = transport("index", {}, check)
    if not isinstance(value, dict) or value.get("type") != "FeatureCollection":
        raise invalid("The publisher region index is invalid.")
    check()
    osm_source_cache.install_json(path, value)
    return value


def region_coverage(collection, bounds, *, require_country=False, origin=None):
    """Choose complete U.S. regional coverage dynamically, including state seams and split Aleutian bounds.

    Prefer one smallest covering U.S. state extract. Otherwise choose intersecting state extracts only if their union
    covers the requested bounds; a covering publisher parent is an upfront selection, never an outage replacement.
    Production planning requires the country geometry. Smaller caller-provided catalogs can use the original
    full-envelope coverage contract, which never clips absent country metadata.
    """
    requested = unary_union([box(*extent) for extent in bounds])
    regions, states, country = [], [], None
    try:
        for feature in collection["features"]:
            properties = feature["properties"]
            if not properties.get("urls", {}).get("pbf"):
                continue
            codes = properties.get("iso3166-2") or []
            countries = properties.get("iso3166-1:alpha2") or []
            is_state = any(isinstance(code, str) and re.fullmatch(r"US-[A-Z]{2}", code) for code in codes)
            identifier = properties.get("id", "")
            if not (is_state or "US" in countries or identifier in ("us", "us-west", "us-midwest",
                                                                  "us-northeast", "us-south")):
                continue
            geometry = shape(feature["geometry"])
            if geometry.is_empty or not geometry.is_valid or geometry.geom_type not in ("Polygon", "MultiPolygon"):
                raise invalid("The publisher U.S. coverage geometry is invalid.")
            value = (geometry, properties)
            regions.append(value)
            if is_state:
                states.append(value)
            if identifier == "us":
                country = geometry
        if require_country and country is None:
            raise invalid("The publisher region index has no usable U.S. country coverage geometry.")
        if origin is not None and country is not None and not country.covers(origin):
            # The two date-line longitudes denote one exact point, even if the publisher only stores one side.
            on_date_line = abs(origin.x) == 180 and country.covers(Point(-origin.x, origin.y))
            if not on_date_line:
                raise IntegrationError("OUT_OF_COVERAGE",
                                       "The incident coordinate is outside supported U.S. road coverage.", 422)
        # The incident remains unchanged. Offshore and non-U.S. parts of its query envelope cannot require a
        # foreign road file. Clipping is authorized only by the publisher's validated U.S. coverage geometry.
        required = requested.intersection(country) if country is not None else requested
        if required.is_empty:
            raise IntegrationError("OSM_COVERAGE_UNAVAILABLE",
                                   "No canonical U.S. road extract covers this study extent.", 422)
        covering = [item for item in states if item[0].covers(required)]
        if covering:
            selected = [min(covering, key=lambda item: (item[0].area, item[1]["id"]))[1]]
        else:
            intersecting = [item for item in states if item[0].intersects(required)]
            if intersecting and unary_union([item[0] for item in intersecting]).covers(required):
                selected = [item[1] for item in sorted(intersecting, key=lambda item: item[1]["id"])]
            else:
                covering = [item for item in regions if item[0].covers(required)]
                if not covering:
                    raise IntegrationError("OSM_COVERAGE_UNAVAILABLE",
                                           "No canonical U.S. road extract covers this study extent.", 422)
                selected = [min(covering, key=lambda item: (item[0].area, item[1]["id"]))[1]]
    except (KeyError, TypeError, ValueError):
        raise invalid("The publisher region geometry is invalid.") from None
    parts = list(required.geoms) if hasattr(required, "geoms") else [required]
    return selected, {"required_us_bounds": [list(part.bounds) for part in parts if not part.is_empty],
                      "includes_unsupported_area": not required.equals(requested)}


def select_regions(collection, bounds):
    """Return the canonical regions selected by the validated geographic coverage decision."""
    return region_coverage(collection, bounds)[0]


def resolve(region, http, check):
    """Resolve the mutable convenience URL once into a dated immutable publisher file and declared checksum."""
    url = publisher_url(region["urls"]["pbf"])
    check()
    response = http.head(url)
    if response.is_redirect:
        url = publisher_url(urljoin(url, response.headers.get("location", "")))
        check()
        response = http.head(url)
    response.raise_for_status()
    if not re.search(r"-[0-9]{6}\.osm\.pbf$", urlparse(url).path) or response.is_redirect:
        raise invalid("The publisher did not resolve an immutable dated road snapshot.")
    try:
        size = int(response.headers["content-length"])
    except (KeyError, ValueError):
        raise invalid("The regional road file has no declared size.") from None
    if not 0 < size <= settings.osm_source_max_mib() * osm_source_cache.MIB:
        raise IntegrationError("OSM_SOURCE_CAPACITY", "The regional road file exceeds the configured source capacity.",
                               503)
    try:
        checksum = read_bytes(http, url + ".md5", 4096, check).decode("ascii", errors="strict").split()
    except UnicodeDecodeError:
        raise invalid("The publisher road-file checksum is invalid.") from None
    if not checksum or not re.fullmatch(r"[a-fA-F0-9]{32}", checksum[0]):
        raise invalid("The publisher road-file checksum is invalid.")
    if len(checksum) > 1 and Path(checksum[1].lstrip("*")).name != Path(urlparse(url).path).name:
        raise invalid("The publisher checksum names a different road file.")
    return {"region_id": region["id"], "region_name": region.get("name", region["id"]), "url": url,
            "bytes": size, "md5": checksum[0].lower(), "etag": response.headers.get("etag"),
            "last_modified": response.headers.get("last-modified")}


def download(source, target, check, progress):
    """Own the blocking download process; the cache parent keeps the lease, verification and publication fence."""
    selected = {name: source.get(name) for name in ("url", "bytes", "etag", "region_id")}
    result = transport("download", {"source": selected, "target": str(target.resolve()),
                                   "scratch_root": str(settings.DATA.resolve())}, check, progress,
                       maximum_bytes=16384)
    if not isinstance(result, dict) or result.get("bytes") != source["bytes"]:
        raise invalid("The regional road download is incomplete.")


def download_direct(source, target, check, progress):
    """Stream one selected source file; exact bytes/checksums are checked before any cache publication."""
    received, last_report = 0, time.monotonic()
    with client(check) as http, http.stream("GET", publisher_url(source["url"]),
                                           headers={"If-Match": source["etag"]} if source["etag"] else {}) as response:
        response.raise_for_status()
        with target.open("wb") as handle:
            for chunk in response.iter_bytes(osm_source_cache.MIB):
                check()
                received += len(chunk)
                if received > source["bytes"]:
                    raise invalid("The regional road download exceeds its declared size.")
                handle.write(chunk)
                if time.monotonic() - last_report >= 1:
                    progress(f"Downloading {source['region_id']}: {received:,} / {source['bytes']:,} bytes", 0.04)
                    last_report = time.monotonic()
    if received != source["bytes"]:
        raise invalid("The regional road download is incomplete.")
    return received


def tool():
    found = shutil.which("osmium")
    if not found:
        raise IntegrationError("OSM_TOOL_UNAVAILABLE", "The verified road extraction tool is unavailable.", 503)
    return found


def inspect(path, run, check):
    try:
        return json.loads(run([tool(), "fileinfo", "--json", str(path)], check))
    except ValueError:
        raise invalid("The road extraction tool returned invalid source metadata.") from None


def verify_tool(run, check):
    """Do not label a cache recipe with a native version that this worker did not actually execute."""
    version = run([tool(), "--version"], check)
    if (f"osmium version {OSMIUM_VERSION}" not in version.splitlines()
            or "libosmium version 2.23.1" not in version.splitlines()):
        raise IntegrationError("OSM_TOOL_UNAVAILABLE",
                               "The installed road extraction tool does not match the verified version pins.", 503)


def plan(area, run, progress, check, timings):
    """Resolve source identities before looking up a derived network; no cached older source can be selected."""
    bounds = osm_corridors.study_bounds(area.lat, area.lon, area.radius_km)
    progress("Resolving canonical OpenStreetMap regional snapshots", 0.02)
    with timings.measure("osm_source_metadata"):
        verify_tool(run, check)
        regions, coverage = region_coverage(index(check), bounds, require_country=True,
                                            origin=Point(area.lon, area.lat))
        sources = [transport("resolve", {"region": {name: region[name] for name in ("id", "name", "urls")
                                                  if name in region}}, check, progress, maximum_bytes=16384)
                   for region in regions]
    metadata = []
    for source in sources:
        value = osm_source_cache.manifest(source)
        if value is None:
            with timings.measure("osm_source_download"):
                _, value = osm_source_cache.obtain(source, download, lambda path: inspect(path, run, check),
                                                   check, progress)
        # Transport validators come from this resolution, while the source byte/hash identity remains immutable.
        metadata.append({**value, "etag": source.get("etag"), "last_modified": source.get("last_modified")})
    timestamps = {value["snapshot_timestamp"] for value in metadata}
    if len(timestamps) != 1:
        raise invalid("Intersecting regional road files do not share one publisher snapshot timestamp.")
    return {"publisher": "Geofabrik", "snapshot_timestamp": metadata[0]["snapshot_timestamp"],
            "recipe": RECIPE_VERSION, "osmium_version": OSMIUM_VERSION, "sources": metadata, "study_bounds": bounds,
            **coverage}


def identity(value):
    """Stable source bytes/recipe identity, excluding cache access times and local file names."""
    return {"recipe": value["recipe"], "osmium_version": value["osmium_version"],
            "snapshot_timestamp": value["snapshot_timestamp"],
            "sources": [{key: source[key] for key in ("region_id", "url", "bytes", "md5", "sha256")}
                        for source in value["sources"]]}


def merge_xml(paths, maximum):
    """Merge bounded same-snapshot subgraphs, refusing conflicting duplicate objects rather than choosing one."""
    objects, size = {}, 0
    for path in paths:
        size += path.stat().st_size
        if size > maximum:
            raise ValueError("Extracted road XML exceeds the configured size limit")
        root = osm_corridors.parse_xml(path.read_bytes())
        for item in root:
            if item.tag not in ("node", "way", "relation"):
                continue
            try:
                identifier = int(item.attrib["id"])
            except (KeyError, ValueError):
                raise invalid("The road snapshot has an invalid OSM object identity.") from None
            # Attribute/tag order carries no meaning; nd/member order does.
            canonical = (tuple(sorted(item.attrib.items())),
                         tuple((child.tag, tuple(sorted(child.attrib.items()))) for child in item
                               if child.tag != "tag"),
                         tuple(sorted(tuple(sorted(child.attrib.items())) for child in item if child.tag == "tag")))
            key = item.tag, identifier
            if key in objects and objects[key][0] != canonical:
                raise invalid("Regional road snapshots contain conflicting versions of an OSM object.")
            objects[key] = canonical, item
    root = ElementTree.Element("osm", version="0.6", generator=RECIPE_VERSION)
    order = {"node": 0, "way": 1, "relation": 2}
    for key in sorted(objects, key=lambda value: (order[value[0]], value[1])):
        root.append(objects[key][1])
    raw = ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)
    if len(raw) > maximum:
        raise ValueError("Extracted road XML exceeds the configured size limit")
    return raw


def extract(area, value, directory, run, progress, check):
    """Seekable multi-pass extraction preserves complete ways and restriction members, including regional seams."""
    paths = []
    maximum = settings.osm_road_xml_max_mb() * 1_000_000
    intermediate_limit = settings.osm_source_max_mib() * osm_source_cache.MIB
    for region_index, source in enumerate(value["sources"]):
        target, metadata = osm_source_cache.obtain(source, download, lambda path: inspect(path, run, check),
                                                  check, progress)
        if metadata["sha256"] != source["sha256"]:
            raise invalid("The selected road source changed before extraction.")
        # Cached sources still need scratch for two simultaneous PBF intermediates and the final bounded XML.
        # The same configured regional byte capacity bounds each native intermediate, including on cache hits.
        if shutil.disk_usage(directory).free < intermediate_limit*2 + maximum:
            raise IntegrationError("OSM_SOURCE_CAPACITY", "Insufficient scratch space for road extraction.", 503)
        progress(f"Extracting complete OSM roads and turn restrictions from {source['region_id']}", 0.06)
        for bounds_index, extent in enumerate(value["study_bounds"]):
            check()
            prefix = f"region-{region_index}-bounds-{bounds_index}"
            cropped, roads = directory / (prefix + ".osm.pbf"), directory / (prefix + "-roads.osm.pbf")
            xml = directory / (prefix + ".osm.xml")
            run([tool(), "extract", "--bbox", ",".join(map(str, extent)), "--strategy", "smart",
                 "--option", "types=restriction", "--output", str(cropped), str(target)], check,
                max_output_bytes=intermediate_limit,
                size_failure="Extracted road PBF exceeds the configured regional byte capacity")
            run([tool(), "tags-filter", "--output", str(roads), str(cropped),
                 "nw/highway", "r/type=restriction"], check, max_output_bytes=intermediate_limit,
                size_failure="Filtered road PBF exceeds the configured regional byte capacity")
            run([tool(), "cat", "--output", str(xml), str(roads)], check, max_output_bytes=maximum)
            paths.append(xml)
            cropped.unlink()
            roads.unlink()
    raw = merge_xml(paths, maximum)
    combined = directory / "roads.osm.xml"
    combined.write_bytes(raw)
    run([tool(), "check-refs", "--check-relations", str(combined)], check)
    for path in paths:
        path.unlink()
    geometry = osm_corridors.geometry_from_xml(raw)
    if not geometry["features"]:
        raise RoadCoverageUnavailable(
            "NO_DRIVABLE_ROADS", "This study extent contains no supported passenger roads.",
            details=osm_corridors.coverage_from_xml(raw, geometry))
    return raw, hashlib.sha256(raw).hexdigest()
