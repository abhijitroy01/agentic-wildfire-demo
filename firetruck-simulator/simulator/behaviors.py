"""
Truck Behavior Logic
Implements state machines and behavior rules for fire trucks
"""
import random
from datetime import datetime

from api.models.incident import FireLocation
from api.models.truck import (
    ActivityStatus,
    CrewFatigueLevel,
    EngineStatus,
    EntrapmentRisk,
    FireTruck,
    PumpStatus,
    SmokeDensity,
)
from config import (
    SMOKE_HEAVY_AQI,
    SMOKE_LIGHT_AQI,
    SMOKE_MODERATE_AQI,
    SMOKE_NONE_AQI,
    get_flow_rate_for_pressure,
    get_settings,
)

from .constants import PUMPING_UNIT_TYPES


def update_water_levels(truck: FireTruck, delta_seconds: float) -> None:
    """
    Update water tank levels based on pump status and time elapsed.
    Modifies truck.water in place.
    """
    if truck.water.pump_status != PumpStatus.ACTIVE:
        return

    # Calculate water consumed
    flow_rate_gpm = truck.water.flow_rate_gpm
    flow_rate_gps = flow_rate_gpm / 60  # Gallons per second
    water_consumed = flow_rate_gps * delta_seconds

    # Update levels
    new_level_gal = max(0, truck.water.tank_level_gal - water_consumed)
    truck.water.tank_level_gal = int(new_level_gal)
    truck.water.tank_level_pct = int(
        (new_level_gal / truck.water.tank_capacity_gal) * 100)

    # Update total deployed
    truck.water.total_water_deployed_gal += int(water_consumed)

    # Update estimated time remaining
    if flow_rate_gpm > 0:
        truck.water.estimated_water_remaining_min = new_level_gal / flow_rate_gpm
    else:
        truck.water.estimated_water_remaining_min = 999


def update_fuel_levels(truck: FireTruck, delta_seconds: float) -> None:
    """
    Update fuel levels based on engine status.
    Modifies truck.vehicle in place.
    """
    settings = get_settings()

    # Determine consumption rate
    if truck.vehicle.engine_status == EngineStatus.OFF:
        return
    elif truck.vehicle.engine_status == EngineStatus.IDLE:
        consumption_gph = settings.fuel_consumption_idle_gph
    else:  # RUNNING
        if truck.water.pump_status == PumpStatus.ACTIVE:
            consumption_gph = settings.fuel_consumption_pumping_gph + \
                settings.fuel_consumption_driving_gph
        elif truck.location.speed_mph > 0:
            consumption_gph = settings.fuel_consumption_driving_gph
        else:
            consumption_gph = settings.fuel_consumption_idle_gph

    # Update fuel
    truck.vehicle.fuel_consumption_gph = consumption_gph
    hours_elapsed = delta_seconds / 3600
    fuel_consumed_pct = (
        consumption_gph / settings.fuel_tank_size_gal) * hours_elapsed * 100
    truck.vehicle.fuel_level_pct = max(
        0, truck.vehicle.fuel_level_pct - int(fuel_consumed_pct))


def update_crew_fatigue(truck: FireTruck, total_hours_on_duty: float) -> None:
    """
    Update crew fatigue level based on hours on duty.
    Modifies truck.crew in place.
    """
    settings = get_settings()
    truck.crew.hours_on_duty = round(total_hours_on_duty, 2)

    if total_hours_on_duty <= settings.fatigue_fresh_max_hours:
        truck.crew.fatigue_level = CrewFatigueLevel.FRESH
    elif total_hours_on_duty <= settings.fatigue_moderate_max_hours:
        truck.crew.fatigue_level = CrewFatigueLevel.MODERATE
    elif total_hours_on_duty <= settings.fatigue_high_max_hours:
        truck.crew.fatigue_level = CrewFatigueLevel.HIGH
    else:
        truck.crew.fatigue_level = CrewFatigueLevel.CRITICAL


def calculate_entrapment_risk(distance_to_fire_ft: int, fire_spread_rate: float) -> EntrapmentRisk:
    """Calculate entrapment risk based on distance and fire behavior"""
    settings = get_settings()

    if distance_to_fire_ft <= settings.entrapment_critical_dist_ft:
        return EntrapmentRisk.CRITICAL
    elif distance_to_fire_ft <= settings.entrapment_high_dist_ft:
        return EntrapmentRisk.HIGH
    elif distance_to_fire_ft <= settings.entrapment_medium_dist_ft:
        return EntrapmentRisk.MEDIUM
    else:
        return EntrapmentRisk.LOW


def calculate_smoke_density(aqi: int) -> SmokeDensity:
    """Calculate smoke density from AQI"""
    if aqi < SMOKE_NONE_AQI:
        return SmokeDensity.NONE
    elif aqi < SMOKE_LIGHT_AQI:
        return SmokeDensity.LIGHT
    elif aqi < SMOKE_MODERATE_AQI:
        return SmokeDensity.MODERATE
    elif aqi < SMOKE_HEAVY_AQI:
        return SmokeDensity.HEAVY
    else:
        return SmokeDensity.SEVERE


def update_safety_indicators(truck: FireTruck, fire_location: FireLocation) -> None:
    """
    Update all safety indicators based on truck position and conditions.
    Modifies truck.safety in place.
    """
    settings = get_settings()
    distance_ft = truck.location.distance_to_fire_ft

    # Danger zone
    truck.safety.in_danger_zone = distance_ft < settings.danger_zone_threshold_ft

    # Entrapment risk
    fire_spread_rate = truck.safety.fire_spread_rate_fph
    truck.safety.entrapment_risk = calculate_entrapment_risk(
        distance_ft, fire_spread_rate)

    # Escape route (assume clear unless in critical situation)
    truck.safety.escape_route_clear = truck.safety.entrapment_risk != EntrapmentRisk.CRITICAL

    # Lookout contact (assume true unless in severe smoke)
    truck.safety.lookout_contact = truck.environmental.smoke_density != SmokeDensity.SEVERE

    # Downhill position (simplified: check if truck bearing to fire is upward)
    # For now, set based on random seed
    seed = hash(truck.truck_id) % 100
    truck.safety.downhill_position = seed < 20  # 20% chance of downhill

    # Escape time (estimate: distance / safe retreat speed)
    safe_retreat_speed_fps = 15 * 1.467  # 15 mph in ft/s
    truck.safety.escape_time_seconds = int(
        truck.safety.safety_zone_distance_ft / safe_retreat_speed_fps)


def should_trigger_resupply(truck: FireTruck) -> bool:
    """Check if truck should transition to resupply state"""
    settings = get_settings()
    return (
        truck.water.tank_level_pct < settings.resupply_trigger_pct and
        truck.operational.activity_status == ActivityStatus.SUPPRESSION
    )


def get_pump_pressure_for_status(status: ActivityStatus) -> int:
    """Get appropriate pump pressure for activity status"""
    settings = get_settings()
    if status == ActivityStatus.SUPPRESSION:
        return random.randint(
            settings.pump_pressure_suppression_min,
            settings.pump_pressure_suppression_max
        )
    else:
        return 0


def stop_pump(truck: FireTruck) -> None:
    """
    Stop a truck's pump: pump off, zero discharge pressure and zero flow.
    Modifies truck.water in place.
    """
    truck.water.pump_status = PumpStatus.OFF
    truck.water.pump_pressure_psi = 0
    truck.water.flow_rate_gpm = 0


def transition_to_suppression(truck: FireTruck) -> None:
    """Transition truck to active suppression mode"""
    truck.operational.activity_status = ActivityStatus.SUPPRESSION
    truck.water.pump_status = PumpStatus.ACTIVE
    truck.water.pump_pressure_psi = get_pump_pressure_for_status(
        ActivityStatus.SUPPRESSION)
    truck.water.flow_rate_gpm = get_flow_rate_for_pressure(
        truck.water.pump_pressure_psi)
    truck.vehicle.engine_status = EngineStatus.RUNNING


def transition_on_arrival(truck: FireTruck) -> None:
    """Set an arriving unit's activity by role: pumping units start suppression, others stage, pump off."""
    if truck.unit_type in PUMPING_UNIT_TYPES:
        transition_to_suppression(truck)
        return
    truck.operational.activity_status = ActivityStatus.STAGING
    stop_pump(truck)


def transition_to_resupply(truck: FireTruck, resupply_start_time: datetime) -> None:
    """Transition truck to resupply mode"""
    truck.operational.activity_status = ActivityStatus.RESUPPLY
    stop_pump(truck)
    truck.operational.last_resupply_time = resupply_start_time
    truck.operational.resupply_count += 1


def complete_resupply(truck: FireTruck) -> None:
    """Complete resupply and refill tank"""
    truck.water.tank_level_gal = truck.water.tank_capacity_gal
    truck.water.tank_level_pct = 100
    truck.water.foam_level_gal = truck.water.foam_capacity_gal
    truck.water.foam_level_pct = 100
    truck.vehicle.fuel_level_pct = 100  # Also refuel

    # Transition back to EN_ROUTE to return to fire
    truck.operational.activity_status = ActivityStatus.EN_ROUTE


def calculate_heat_exposure_index(distance_to_fire_ft: int) -> int:
    """
    Calculate heat exposure index (0-100) based on distance.
    Closer = higher exposure.
    """
    if distance_to_fire_ft >= 2000:
        return 10
    elif distance_to_fire_ft >= 1000:
        return 30
    elif distance_to_fire_ft >= 500:
        return 50
    else:
        return 80
