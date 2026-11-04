"""
Movement Calculator
Calculates realistic truck movements and positions over time
"""
import hashlib
import math

from api.models.incident import FireLocation
from config import get_settings


def get_deterministic_seed(fire_lat: float, fire_lng: float, truck_idx: int) -> int:
    """
    Generate consistent seed for a specific truck at a fire location.
    Same inputs always produce same outputs for reproducibility.
    """
    hash_input = f"{fire_lat:.6f}:{fire_lng:.6f}:{truck_idx}"
    # nosec B324
    return int(hashlib.md5(hash_input.encode(), usedforsecurity=False).hexdigest()[:8], 16)


def calculate_distance_feet(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Calculate distance between two lat/lng points in feet.
    Uses Haversine formula.
    """
    R = 20902231  # Earth radius in feet

    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    delta_lat = math.radians(lat2 - lat1)
    delta_lon = math.radians(lon2 - lon1)

    a = math.sin(delta_lat/2)**2 + math.cos(lat1_rad) * \
        math.cos(lat2_rad) * math.sin(delta_lon/2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

    return R * c


def calculate_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Calculate bearing from point 1 to point 2 in degrees (0-360).
    0 = North, 90 = East, 180 = South, 270 = West
    """
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    delta_lon = math.radians(lon2 - lon1)

    x = math.sin(delta_lon) * math.cos(lat2_rad)
    y = math.cos(lat1_rad) * math.sin(lat2_rad) - \
        math.sin(lat1_rad) * math.cos(lat2_rad) * math.cos(delta_lon)

    bearing = math.atan2(x, y)
    bearing = math.degrees(bearing)
    bearing = (bearing + 360) % 360

    return bearing


def move_point(lat: float, lon: float, distance_ft: float, bearing: float) -> tuple[float, float]:
    """
    Move a point by distance (feet) in direction (bearing degrees).
    Returns new (latitude, longitude), with the longitude kept within [-180, 180].
    """
    R = 20902231  # Earth radius in feet

    lat_rad = math.radians(lat)
    lon_rad = math.radians(lon)
    bearing_rad = math.radians(bearing)

    angular_distance = distance_ft / R

    new_lat_rad = math.asin(
        math.sin(lat_rad) * math.cos(angular_distance) +
        math.cos(lat_rad) * math.sin(angular_distance) * math.cos(bearing_rad)
    )

    new_lon_rad = lon_rad + math.atan2(
        math.sin(bearing_rad) * math.sin(angular_distance) * math.cos(lat_rad),
        math.cos(angular_distance) - math.sin(lat_rad) * math.sin(new_lat_rad)
    )

    new_lat = math.degrees(new_lat_rad)
    new_lon = math.degrees(new_lon_rad)

    # Wrap a move across the antimeridian; longitudes already in range are returned unchanged.
    if new_lon > 180:
        new_lon -= 360
    elif new_lon < -180:
        new_lon += 360

    return new_lat, new_lon


def get_staging_area_position(fire_location: FireLocation, truck_idx: int) -> tuple[float, float]:
    """
    Calculate staging area position for a truck.
    Staging areas are 2-5 miles from fire at various bearings.
    """
    settings = get_settings()

    # Use deterministic seed to pick distance and bearing
    seed = get_deterministic_seed(
        fire_location.latitude, fire_location.longitude, truck_idx)

    # Use seed to determine distance and bearing
    distance_range = settings.staging_distance_max_mi - \
        settings.staging_distance_min_mi
    distance_mi = settings.staging_distance_min_mi + \
        (seed % 30) / 10 * (distance_range / 3)
    bearing = (seed * 37) % 360  # Pseudo-random bearing

    distance_ft = distance_mi * 5280

    lat, lng = move_point(
        fire_location.latitude,
        fire_location.longitude,
        distance_ft,
        bearing
    )

    return lat, lng


def get_initial_fire_perimeter_position(fire_location: FireLocation, truck_idx: int) -> tuple[float, float]:
    """
    Calculate initial position around fire perimeter for arriving trucks.
    Distributes trucks evenly around the fire.
    """
    settings = get_settings()

    # Use deterministic seed for consistent positioning
    seed = get_deterministic_seed(
        fire_location.latitude, fire_location.longitude, truck_idx)

    # Distribute trucks around perimeter
    bearing = (truck_idx * 360 / 12 + (seed % 30)) % 360

    # Position at edge of deployment radius
    distance_ft = settings.deployment_radius_ft + (seed % 500)

    lat, lng = move_point(
        fire_location.latitude,
        fire_location.longitude,
        distance_ft,
        bearing
    )

    return lat, lng


def calculate_travel_progress(
    start_lat: float,
    start_lng: float,
    end_lat: float,
    end_lng: float,
    elapsed_seconds: int,
    speed_mph: float
) -> tuple[float, float, float]:
    """
    Calculate current position when traveling from start to end.

    Returns: (current_lat, current_lng, heading)
    """
    # Calculate total distance
    total_distance_ft = calculate_distance_feet(
        start_lat, start_lng, end_lat, end_lng)

    # Calculate distance traveled
    speed_ft_per_sec = speed_mph * 1.467  # Convert mph to ft/s
    distance_traveled_ft = speed_ft_per_sec * elapsed_seconds

    # Calculate progress ratio (0.0 to 1.0)
    if total_distance_ft == 0:
        return end_lat, end_lng, 0.0

    progress = min(distance_traveled_ft / total_distance_ft, 1.0)

    # Calculate current position
    if progress >= 1.0:
        return end_lat, end_lng, calculate_bearing(start_lat, start_lng, end_lat, end_lng)

    # Interpolate position
    bearing = calculate_bearing(start_lat, start_lng, end_lat, end_lng)
    current_lat, current_lng = move_point(
        start_lat, start_lng, distance_traveled_ft, bearing)

    return current_lat, current_lng, bearing


def get_altitude(latitude: float, longitude: float, fire_location: FireLocation) -> float:
    """
    Estimate altitude based on position.
    Simple model: slightly higher near fire (mountain terrain).
    """
    # Base altitude
    base_altitude = 1000

    # Calculate distance to fire
    distance_ft = calculate_distance_feet(
        latitude, longitude,
        fire_location.latitude, fire_location.longitude
    )

    # Closer to fire = slightly higher (simulating mountain terrain)
    altitude_variation = max(0, 500 - (distance_ft / 20))

    return base_altitude + altitude_variation


def calculate_speed_for_phase(
    activity_status: str,
    distance_to_fire_ft: float
) -> float:
    """
    Calculate appropriate speed based on activity and proximity to fire.
    """
    settings = get_settings()

    if activity_status == "SUPPRESSION" or activity_status == "HOLDING":
        return 0  # Stationary

    if activity_status == "STAGING":
        return 0  # Parked at staging

    if activity_status == "RESUPPLY":
        return settings.resupply_speed_mph

    # EN_ROUTE - speed depends on distance to fire
    if distance_to_fire_ft < 2000:
        return settings.max_speed_near_fire_mph
    elif distance_to_fire_ft < 10000:
        return settings.max_speed_offroad_mph
    else:
        return settings.max_speed_highway_mph
