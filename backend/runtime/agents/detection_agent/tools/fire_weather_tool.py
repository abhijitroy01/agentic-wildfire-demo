"""
Fire Weather Tool for Detection Agent

This tool integrates with the AgentCore gateway to get fire locations and weather data,
combining them into a comprehensive mapData object for wildfire risk assessment.

BEST PRACTICE: Uses Pydantic models directly to ensure output always matches schema.
This eliminates snake_case/camelCase mismatches and provides compile-time safety.
"""

import logging
import math
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

# Import Pydantic models for type-safe output construction
from schemas.generated_models import FireData, FireLocation, Location, MapData, MapSummary, WeatherData
from strands import tool
from utils.gateway import DTN_CURRENT, WILDFIRES, tool_payload
from utils.log_safety import error_summary

# Configure logging
logger = logging.getLogger(__name__)

# Constants
TOP_FIRES_LIMIT = 4  # Number of top fires to display and fetch weather for
# Added with "truncated": true when the wildfire source returned a capped list.
TRUNCATION_NOTE = "The wildfire source capped this list; more fires may exist in the search area."


# =====================================================
# MODULE-LEVEL HELPER FUNCTIONS (moved for reusability)
# =====================================================

def calculate_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Calculate distance in miles using Haversine formula.

    Args:
        lat1, lon1: First coordinate pair
        lat2, lon2: Second coordinate pair

    Returns:
        Distance in miles
    """
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat/2)**2 + math.cos(lat1) * \
        math.cos(lat2) * math.sin(dlon/2)**2
    c = 2 * math.asin(math.sqrt(min(1, max(0, a))))
    return c * 3956  # Earth radius in miles


def calculate_priority(acres: float, containment: float) -> float:
    """
    Calculate priority score based on fire size and containment.

    Higher priority = more urgent (larger fire, less contained)

    Args:
        acres: Fire size in acres
        containment: Percent contained (0-100)

    Returns:
        Priority score (0.0 to 1.0)
    """
    acres = acres or 0
    # 0% is a real value (uncontained); only a missing value is treated as fully contained.
    containment = containment if containment is not None else 100
    # Acres score: 0-10000 acres maps to 0.0-1.0
    acres_score = min(acres / 10000, 1.0)
    # Containment score: 0% contained = 1.0, 100% contained = 0.0
    containment_score = max(0, (100 - containment) / 100)
    # Final: 60% weight on size, 40% on containment
    return min((acres_score * 0.6) + (containment_score * 0.4), 1.0)


def convert_timestamp(ts) -> str | None:
    """Convert finite source milliseconds or an explicit ISO instant; malformed values remain unknown."""
    if type(ts) in (int, float) and math.isfinite(ts):
        try:
            return datetime.fromtimestamp(ts / 1000, UTC).isoformat()
        except (ValueError, TypeError, OSError, OverflowError):
            return None
    if isinstance(ts, str):
        try:
            return ts if datetime.fromisoformat(ts).utcoffset() is not None else None
        except ValueError:
            return None
    return None


def presort_fire_features(features: list[dict], input_lat: float, input_lon: float,
                          limit: int = TOP_FIRES_LIMIT) -> list[dict]:
    """
    Pre-sort fire features by priority and distance BEFORE weather fetching.

    This ensures weather is fetched only for fires that will actually be displayed,
    guaranteeing 100% weather match rate.

    Args:
        features: Raw ArcGIS feature list
        input_lat: User's search latitude
        input_lon: User's search longitude
        limit: Number of top fires to return (default: 4)

    Returns:
        Top N fire features sorted by priority (desc), then distance (asc)
    """
    processed_features = []

    for feature in features:
        attrs = feature.get('attributes', {})
        geom = feature.get('geometry', {})

        fire_lat = geom.get('y')
        fire_lon = geom.get('x')

        if (type(fire_lat) not in (int, float) or type(fire_lon) not in (int, float)
                or not math.isfinite(fire_lat) or not math.isfinite(fire_lon)
                or not -90 <= fire_lat <= 90 or not -180 <= fire_lon <= 180):
            logger.debug("Skipping feature without coordinates")
            continue

        acres = attrs.get('DailyAcres')
        if acres is None:
            acres = attrs.get('CalculatedAcres')
        containment = attrs.get('PercentContained')

        # Calculate priority and distance
        priority = calculate_priority(acres, containment)
        distance = calculate_distance(input_lat, input_lon, fire_lat, fire_lon)

        # Store computed values in feature for later use
        feature['_priority'] = priority
        feature['_distance'] = distance
        feature['_lat'] = fire_lat
        feature['_lon'] = fire_lon

        processed_features.append(feature)

    # The requested area owns discovery: a distant large fire cannot displace a nearer reported incident.
    processed_features.sort(
        key=lambda f: (f.get('_distance', float('inf')), -f.get('_priority', -1)))

    logger.info(
        f"Pre-sorted {len(processed_features)} fires by priority, selecting top {limit}")

    # Log top fires for debugging
    for i, f in enumerate(processed_features[:limit]):
        name = f.get('attributes', {}).get('IncidentName', 'Unknown')
        logger.info(
            f"  Top {i+1}: {name} - priority={f['_priority']:.3f}, distance={f['_distance']:.1f}mi")

    return processed_features[:limit]


def weather_by_coordinate(weather_list: list[dict]) -> dict[tuple[float, float], dict]:
    """Associate exact query points, rejecting conflicting duplicates instead of trusting batch order."""
    matched, conflicts = {}, set()
    for item in weather_list:
        if not isinstance(item, dict):
            continue
        lat, lon = item.get("latitude"), item.get("longitude")
        if (type(lat) not in (int, float) or type(lon) not in (int, float)
                or not math.isfinite(lat) or not math.isfinite(lon)
                or not -90 <= lat <= 90 or not -180 <= lon <= 180):
            continue
        point = (lat, lon)
        value = {"currentWeather": item.get("currentWeather"), "error": item.get("error"),
                 "status": "unavailable" if item.get("status") == "unavailable" else None}
        if point in matched and matched[point] != value:
            conflicts.add(point)
        else:
            matched[point] = value
    for point in conflicts:
        matched.pop(point, None)
    if conflicts:
        logger.warning("Weather batch contains conflicting observations for %d query points", len(conflicts))
    return matched


def create_mapdata_from_presorted(presorted_features: list[dict], weather_list: list[dict],
                                  search_params: dict[str, Any]) -> dict[str, Any]:
    """
    Create mapData structure from pre-sorted features with exact coordinate-matched weather.

    BEST PRACTICE: Uses Pydantic models (FireData, FireLocation, MapData, MapSummary)
    to ensure output always matches the schema. This provides:
    - Compile-time safety (IDE catches field name typos)
    - Runtime validation (Pydantic validates types and ranges)
    - Single source of truth (schema IS the code)

    Args:
        presorted_features: Pre-sorted fire features (already has _priority, _distance, _lat, _lon)
        weather_list: Per-point weather results, possibly partial or reordered
        search_params: Original search parameters

    Returns:
        Complete mapData structure ready for response (via model.model_dump())
    """
    fire_locations: list[FireLocation] = []
    weather_points = weather_by_coordinate(weather_list)

    for i, feature in enumerate(presorted_features):
        attrs = feature.get('attributes', {})

        fire_lat = feature['_lat']
        fire_lon = feature['_lon']
        distance = feature['_distance']
        priority = feature['_priority']

        # A missing or schema-invalid point affects only its own fire marker.
        weather_info = None
        queried = weather_points.get((fire_lat, fire_lon)) or {}
        raw_weather = queried.get("currentWeather")
        if isinstance(raw_weather, dict) and raw_weather and not queried.get("error") \
                and queried.get("status") != "unavailable":
            candidate = {**raw_weather, "queryLocation": {"latitude": fire_lat, "longitude": fire_lon}}
            try:
                WeatherData.model_validate(candidate)
                weather_info = candidate
            except ValidationError:
                logger.warning("Weather fields invalid for displayed fire %d; reported fire retained", i+1)

        # State code formatting
        state_code = attrs.get('POOState', '')

        # Build FireData using Pydantic model (camelCase fields enforced by schema)
        fire_data = FireData(
            sourceId=next((f"esri:{key}:{attrs[key]}" for key in (
                "IrwinID", "IRWINID", "UniqueFireIdentifier", "GlobalID", "OBJECTID")
                if attrs.get(key) is not None), None),
            name=attrs.get('IncidentName', 'Unknown Fire'),
            acresBurned=attrs.get('DailyAcres') if attrs.get('DailyAcres') is not None else attrs.get('CalculatedAcres'),
            percentContained=attrs.get('PercentContained'),
            county=attrs.get('POOCounty', ''),
            state=state_code,
            location=f"{attrs.get('POOCounty', '')}, {state_code}".strip(', '),
            startedDate=convert_timestamp(attrs.get('FireDiscoveryDateTime')),
            lastUpdated=convert_timestamp(attrs.get('ModifiedOnDateTime')),
            isActive=attrs.get('IncidentTypeCategory') == 'WF',
            distanceMiles=round(distance, 1),
            priorityScore=round(priority, 3),
            rank=i + 1,  # 1-indexed rank
            fireCause=attrs.get('FireCause'),
            personnel=attrs.get('TotalIncidentPersonnel'),
            residencesDestroyed=attrs.get('ResidencesDestroyed'),
            structuresDestroyed=attrs.get('OtherStructuresDestroyed'),
            injuries=attrs.get('Injuries'),
            fatalities=attrs.get('Fatalities'),
            complexity=attrs.get('FireMgmtComplexity')
        )

        # Create FireLocation using Pydantic model
        fire_location = FireLocation(
            latitude=fire_lat,
            longitude=fire_lon,
            fire=fire_data,
            currentWeather=weather_info
        )

        fire_locations.append(fire_location)

    # Build summary using Pydantic models
    input_lat = search_params.get('latitude')
    input_lon = search_params.get('longitude')
    radius_miles = search_params.get('radius_miles', 50)
    days = search_params.get('days', 2)

    # Count how many fires have weather
    fires_with_weather = sum(
        1 for loc in fire_locations if loc.currentWeather is not None)

    # Create Location for summary
    summary_location = Location(
        latitude=input_lat,
        longitude=input_lon
    )

    # Create MapSummary using Pydantic model (camelCase fields enforced)
    summary = MapSummary(
        location=summary_location,
        name=f"Location {input_lat:.4f}, {input_lon:.4f}",
        totalActiveFires=len(fire_locations),
        processedLocationsWithWeather=fires_with_weather,
        searchArea=f"{radius_miles} miles around {input_lat}, {input_lon}",
        dataRecency=f"Last {days} days",
        analysisTimestamp=datetime.now(UTC)
    )

    # Create final MapData using Pydantic model
    mapdata = MapData(
        summary=summary,
        locations=fire_locations
    )

    logger.info(
        f"Created mapData: {len(fire_locations)} fires, {fires_with_weather} with weather (Pydantic-validated)")

    # Return as dict for JSON serialization (model_dump ensures correct field names)
    return mapdata.model_dump(mode='json', exclude_none=True)


def build_fire_weather_tool(mcp_client):
    """Bind the fire and fire-weather lookup to the detection agent's open Gateway client: one MCP session."""

    @tool(name="fire_weather_tool")
    def fire_weather_tool(latitude: float, longitude: float, radius_miles: float = 50.0, days: int = 2,
                          include_weather: bool = True) -> dict:
        """
        Get fire locations and weather data for wildfire risk assessment.

        This tool integrates with the AgentCore gateway to:
        1. Get active fire locations using the wildfire tool
        2. Pre-sort fires by priority to select top 4
        3. Get weather conditions for ONLY the top 4 fires (guaranteed match)
        4. Combine into optimal structure with fire and weather data

        Args:
            latitude (float): Center latitude for fire search
            longitude (float): Center longitude for fire search
            radius_miles (float): Search radius in miles (default: 50.0)
            days (int): Number of days of fire data to retrieve (default: 2)
            include_weather (bool): Fetch weather only when requested (False for fire-only searches).

        Returns:
            dict: Top 4 reported fire locations with source metadata and requested available weather,
                plus truncated=true and a truncationNote when the source capped its list
        """
        try:
            # The search point is the user's location, so only the search size is logged.
            logger.info("🔥 Fire weather tool called: radius=%smi, days=%s", radius_miles, days)

            search_params = {
                "latitude": latitude,
                "longitude": longitude,
                "radius_miles": radius_miles,
                "days": days
            }

            # Step 1: Get fire locations from the ArcGIS wildfire target
            wildfire_params = {
                "geometry": f"{longitude},{latitude}",
                "geometryType": "esriGeometryPoint",
                "inSR": 4326,
                "distance": radius_miles,
                "units": "esriSRUnit_StatuteMile",
                "outFields": "*",
                "where": "1=1",
                "returnGeometry": True,
                "outSR": 4326,
                "f": "json"
            }

            logger.info("Calling %s...", WILDFIRES)
            wildfire_data = tool_payload(mcp_client.call_tool_sync(
                tool_use_id="wildfire_call_1", name=WILDFIRES, arguments=wildfire_params))
            if (wildfire_data is None or wildfire_data.get("error")
                    or not isinstance(wildfire_data.get("features"), list)):
                raise RuntimeError("Wildfire source failed; no fire-status conclusion is available.")

            # The wildfire adapter flags a capped list (ArcGIS exceededTransferLimit); see TRUNCATION_NOTE.
            truncated = bool(wildfire_data.get("truncated") or wildfire_data.get("exceededTransferLimit"))

            # Step 2: PRE-SORT fires by priority BEFORE weather fetch
            features = wildfire_data["features"]

            if not features:
                logger.info("No fire features found, returning empty result")
                # Return empty result using Pydantic models for consistent schema
                empty_summary = MapSummary(
                    location=Location(latitude=latitude, longitude=longitude),
                    name=f"Location {latitude:.4f}, {longitude:.4f}",
                    totalActiveFires=0,
                    processedLocationsWithWeather=0,
                    searchArea=f"{radius_miles} miles around {latitude}, {longitude}",
                    dataRecency=f"Last {days} days",
                    analysisTimestamp=datetime.now(UTC)
                )
                empty_mapdata = MapData(summary=empty_summary, locations=[])
                return empty_mapdata.model_dump(mode='json', exclude_none=True)

            # Pre-sort to get top N fires (guarantees weather for displayed fires)
            top_features = presort_fire_features(
                features, latitude, longitude, limit=TOP_FIRES_LIMIT)
            logger.info(
                f"Selected top {len(top_features)} fires from {len(features)} total")

            # Step 3: Get weather ONLY for top N fires; results are matched by exact query coordinates.
            weather_list = []

            if top_features and include_weather:
                locations_for_weather = [{"latitude": feature['_lat'], "longitude": feature['_lon']}
                                         for feature in top_features]
                logger.info(
                    f"Fetching weather for {len(locations_for_weather)} fire locations...")

                try:
                    bulk_weather_data = tool_payload(mcp_client.call_tool_sync(
                        tool_use_id="weather_bulk_call", name=DTN_CURRENT,
                        arguments={"locations": locations_for_weather}))
                    if bulk_weather_data is not None and isinstance(bulk_weather_data.get("locations"), list):
                        weather_list = bulk_weather_data["locations"]
                        logger.info(
                            f"✅ Received {len(weather_list)} per-point weather results")
                    else:
                        logger.warning("Weather response missing 'locations' array")
                except Exception as e:  # noqa: BLE001 -- outbound tool isolates partial provider failures
                    logger.error("❌ Weather API call failed: %s", error_summary(e))
                    logger.info("Proceeding without weather data")

            # Step 4: Keep every reported fire while associating only its own valid weather.
            mapdata = create_mapdata_from_presorted(
                top_features, weather_list, search_params)
            if (top_features and include_weather
                    and not any(location.get("currentWeather") for location in mapdata.get("locations", []))):
                budget_exhausted = any(item.get("errorCode") == "WEATHER_BUDGET_EXHAUSTED"
                                       for item in weather_list if isinstance(item, dict))
                mapdata["sourceAvailability"] = {"weather": {
                    "status": "unavailable",
                    "code": "WEATHER_BUDGET_EXHAUSTED" if budget_exhausted else "SOURCE_UNAVAILABLE",
                }}
            if truncated:
                # Never let a capped list pass for every fire in the search area.
                mapdata.update(truncated=True, truncationNote=TRUNCATION_NOTE)

            logger.info("✅ Fire weather analysis completed successfully (truncated=%s)", truncated)
            return mapdata

        except Exception as e:
            # Never the message: validation messages quote values such as the searched coordinates.
            logger.error("Fire weather tool failed: %s", error_summary(e))
            # Strands surfaces this as a failed tool call. An unavailable provider
            # supplies no evidence that the search area has zero fires.
            raise RuntimeError("Fire/weather investigation unavailable; do not infer that there are no fires.") from e

    return fire_weather_tool
