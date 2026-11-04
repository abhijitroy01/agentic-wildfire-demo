"""
Truck Manager
Manages individual truck instances and their state updates
"""
import random
from datetime import UTC, datetime

from api.models.incident import EventType, FireLocation, TimelineEvent
from api.models.truck import (
    ActivityStatus,
    CrewFatigueLevel,
    CrewStatus,
    EngineStatus,
    EntrapmentRisk,
    FireTruck,
    MissionStatus,
    PumpStatus,
    SuppressantType,
    TransmissionStatus,
    TruckCrew,
    TruckEnvironmental,
    TruckLocation,
    TruckOperational,
    TruckSafety,
    TruckVehicle,
    TruckWater,
    UnitType,
)
from config import (
    CAPTAIN_NAMES,
    RADIO_CHANNELS,
    SECTORS,
    get_crew_capacity,
    get_foam_capacity,
    get_settings,
    get_tank_capacity,
)

from .behaviors import (
    calculate_heat_exposure_index,
    calculate_smoke_density,
    complete_resupply,
    should_trigger_resupply,
    transition_on_arrival,
    transition_to_resupply,
    transition_to_suppression,
    update_crew_fatigue,
    update_fuel_levels,
    update_safety_indicators,
    update_water_levels,
)
from .movement import (
    calculate_bearing,
    calculate_distance_feet,
    calculate_speed_for_phase,
    calculate_travel_progress,
    get_altitude,
    get_deterministic_seed,
    get_initial_fire_perimeter_position,
    get_staging_area_position,
)


def format_time(seconds: int) -> str:
    """Format seconds as HH:MM:SS"""
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


class TruckManager:
    """Manages all fire truck instances in a simulation"""

    def __init__(self, fire_location: FireLocation):
        self.fire_location = fire_location
        self.trucks: dict[str, FireTruck] = {}
        self.truck_counter = 0
        self.staging_positions: dict[str, tuple[float, float]] = {}
        self.fire_positions: dict[str, tuple[float, float]] = {}
        self.resupply_timers: dict[str, float] = {}  # Tracks resupply duration
        self._settings = get_settings()

    def create_truck(
        self,
        unit_type: UnitType,
        truck_number: int,
        dispatch_time: datetime
    ) -> FireTruck:
        """Create a new fire truck instance"""

        truck_id = f"{unit_type.value}-{truck_number}"
        call_sign = f"{unit_type.value.title()} {truck_number}"

        # Get staging position (where truck starts)
        staging_lat, staging_lng = get_staging_area_position(
            self.fire_location,
            self.truck_counter
        )
        self.staging_positions[truck_id] = (staging_lat, staging_lng)

        # Get fire perimeter position (where truck will go)
        fire_lat, fire_lng = get_initial_fire_perimeter_position(
            self.fire_location,
            self.truck_counter
        )
        self.fire_positions[truck_id] = (fire_lat, fire_lng)

        self.truck_counter += 1

        # Calculate initial distance to fire
        distance_to_fire = calculate_distance_feet(
            staging_lat, staging_lng,
            self.fire_location.latitude, self.fire_location.longitude
        )

        # Get altitude
        altitude = get_altitude(staging_lat, staging_lng, self.fire_location)

        # Calculate heading toward fire
        heading = calculate_bearing(
            staging_lat, staging_lng,
            self.fire_location.latitude, self.fire_location.longitude
        )

        # Use deterministic seed for captain selection
        seed = get_deterministic_seed(
            self.fire_location.latitude,
            self.fire_location.longitude,
            self.truck_counter
        )
        captain_idx = seed % len(CAPTAIN_NAMES)
        captain_name = CAPTAIN_NAMES[captain_idx]

        # Assign sector and radio channel
        sector_idx = self.truck_counter % len(SECTORS)
        sector = SECTORS[sector_idx]

        radio_idx = self.truck_counter % len(RADIO_CHANNELS)
        radio_channel = RADIO_CHANNELS[radio_idx]

        # Get tank capacities from config
        water_capacity = get_tank_capacity(unit_type.value)
        foam_capacity = get_foam_capacity(unit_type.value)
        crew_capacity = get_crew_capacity(unit_type.value)

        # Build truck
        truck = FireTruck(
            truck_id=truck_id,
            unit_type=unit_type,
            call_sign=call_sign,
            location=TruckLocation(
                latitude=staging_lat,
                longitude=staging_lng,
                altitude_ft=altitude,
                heading=heading,
                speed_mph=0,
                distance_to_fire_ft=int(distance_to_fire)
            ),
            vehicle=TruckVehicle(
                engine_status=EngineStatus.RUNNING,
                fuel_level_pct=100,
                fuel_consumption_gph=0,
                battery_voltage=13.8,
                engine_temp_f=195,
                odometer_miles=random.randint(
                    self._settings.odometer_min_miles,
                    self._settings.odometer_max_miles
                ),
                transmission_status=TransmissionStatus.NORMAL,
                tire_pressure_psi=[105, 105, 110, 110]
            ),
            water=TruckWater(
                tank_capacity_gal=water_capacity,
                tank_level_gal=water_capacity,
                tank_level_pct=100,
                foam_capacity_gal=foam_capacity,
                foam_level_gal=foam_capacity,
                foam_level_pct=100,
                pump_status=PumpStatus.OFF,
                pump_pressure_psi=0,
                flow_rate_gpm=0,
                hose_deployed_ft=0,
                suppressant_type=SuppressantType.WATER,
                estimated_water_remaining_min=0,
                total_water_deployed_gal=0
            ),
            crew=TruckCrew(
                count=crew_capacity,
                capacity=crew_capacity,
                hours_on_duty=0,
                status=CrewStatus.ACTIVE,
                captain_name=captain_name,
                fatigue_level=CrewFatigueLevel.FRESH,
                medical_incidents=0,
                water_consumption_gal=0,
                rest_period_min=0
            ),
            operational=TruckOperational(
                activity_status=ActivityStatus.EN_ROUTE,
                assignment_sector=sector,
                radio_channel=radio_channel,
                dispatch_time=dispatch_time,
                dispatch_time_seconds_ago=0,
                arrival_time=None,
                arrival_time_seconds_ago=None,
                last_resupply_time=None,
                resupply_count=0,
                mission_status=MissionStatus.IN_PROGRESS
            ),
            environmental=self._calculate_environmental_conditions(
                staging_lat, staging_lng, distance_to_fire
            ),
            safety=TruckSafety(
                in_danger_zone=False,
                escape_route_clear=True,
                lookout_contact=True,
                safety_zone_distance_ft=min(5000, int(distance_to_fire * 0.8)),
                entrapment_risk=EntrapmentRisk.LOW,
                fire_spread_rate_fph=self._settings.fire_spread_moderate_fph,
                downhill_position=False,
                escape_time_seconds=300,
                mayday_active=False,
                last_safety_check=dispatch_time
            ),
            last_update=datetime.now(UTC)
        )

        self.trucks[truck_id] = truck
        return truck

    def update_truck(
        self,
        truck_id: str,
        elapsed_seconds: int,
        delta_seconds: float
    ) -> list[TimelineEvent] | None:
        """
        Update a single truck's state.
        Returns list of events that occurred during this update.
        """
        if truck_id not in self.trucks:
            return None

        truck = self.trucks[truck_id]
        events: list[TimelineEvent] = []
        current_time = datetime.now(UTC)

        # Update operational times
        truck.operational.dispatch_time_seconds_ago = elapsed_seconds
        if truck.operational.arrival_time:
            truck.operational.arrival_time_seconds_ago = int(
                (current_time - truck.operational.arrival_time).total_seconds()
            )

        # Update crew fatigue
        hours_on_duty = elapsed_seconds / 3600
        update_crew_fatigue(truck, hours_on_duty)

        # Handle state transitions based on activity status
        if truck.operational.activity_status == ActivityStatus.EN_ROUTE:
            self._update_en_route(truck, elapsed_seconds, events)

        elif truck.operational.activity_status == ActivityStatus.SUPPRESSION:
            self._update_suppression(
                truck, delta_seconds, current_time, events)

        elif truck.operational.activity_status == ActivityStatus.RESUPPLY:
            self._update_resupply(
                truck, truck_id, delta_seconds, current_time, events)

        # Update environmental conditions
        self._update_environmental(truck)

        # Update safety indicators
        update_safety_indicators(truck, self.fire_location)

        # Update fuel
        update_fuel_levels(truck, delta_seconds)

        # Update last_update timestamp
        truck.last_update = current_time

        return events if events else None

    def _update_en_route(
        self,
        truck: FireTruck,
        elapsed_seconds: int,
        events: list[TimelineEvent]
    ) -> None:
        """Update truck in EN_ROUTE status"""
        # Determine if going to fire or returning from resupply
        if truck.operational.resupply_count > 0 and truck.water.tank_level_pct == 100:
            # Returning from resupply to fire
            start_lat, start_lng = self.staging_positions[truck.truck_id]
            end_lat, end_lng = self.fire_positions[truck.truck_id]
            travel_time = elapsed_seconds - int(
                (truck.operational.last_resupply_time -
                 truck.operational.dispatch_time).total_seconds()
            ) - self._settings.resupply_duration_sec
        else:
            # Initial travel to fire
            start_lat, start_lng = self.staging_positions[truck.truck_id]
            end_lat, end_lng = self.fire_positions[truck.truck_id]
            travel_time = elapsed_seconds

        # Calculate current position with dynamic speed based on distance to fire
        distance = calculate_distance_feet(
            start_lat, start_lng,
            self.fire_location.latitude, self.fire_location.longitude
        )
        speed = calculate_speed_for_phase(
            truck.operational.activity_status.value,
            int(distance)
        )
        current_lat, current_lng, heading = calculate_travel_progress(
            start_lat, start_lng,
            end_lat, end_lng,
            travel_time,
            speed
        )

        # Update position
        truck.location.latitude = current_lat
        truck.location.longitude = current_lng
        truck.location.heading = heading
        truck.location.speed_mph = speed

        # Update distance to fire
        distance = calculate_distance_feet(
            current_lat, current_lng,
            self.fire_location.latitude, self.fire_location.longitude
        )
        truck.location.distance_to_fire_ft = int(distance)

        # Check if arrived at fire
        if distance < self._settings.arrival_threshold_ft:
            # Pumping units start suppression; the other units stage with the pump off
            transition_on_arrival(truck)
            truck.location.speed_mph = 0
            truck.location.latitude = end_lat
            truck.location.longitude = end_lng
            # Publish the distance from the assigned point, not from the position before arrival
            truck.location.distance_to_fire_ft = int(calculate_distance_feet(
                end_lat, end_lng, self.fire_location.latitude, self.fire_location.longitude))

            # Record arrival if first time
            if truck.operational.arrival_time is None:
                truck.operational.arrival_time = datetime.now(UTC)
                truck.operational.arrival_time_seconds_ago = 0

                events.append(TimelineEvent(
                    time=format_time(elapsed_seconds),
                    elapsed_seconds=elapsed_seconds,
                    event_type=EventType.ARRIVAL,
                    truck_id=truck.truck_id,
                    message=f"{truck.call_sign} arrived on scene",
                    timestamp=datetime.now(UTC)
                ))

    def _update_suppression(
        self,
        truck: FireTruck,
        delta_seconds: float,
        current_time: datetime,
        events: list[TimelineEvent]
    ) -> None:
        """Update truck in SUPPRESSION status"""
        # Update water levels
        update_water_levels(truck, delta_seconds)

        # Check if resupply needed
        if should_trigger_resupply(truck):
            transition_to_resupply(truck, current_time)

            events.append(TimelineEvent(
                time=format_time(truck.operational.dispatch_time_seconds_ago),
                elapsed_seconds=truck.operational.dispatch_time_seconds_ago,
                event_type=EventType.RESUPPLY_START,
                truck_id=truck.truck_id,
                message=f"{truck.call_sign} proceeding to resupply (water at {truck.water.tank_level_pct}%)",
                timestamp=current_time
            ))

            # Initialize resupply timer
            self.resupply_timers[truck.truck_id] = 0

    def _update_resupply(
        self,
        truck: FireTruck,
        truck_id: str,
        delta_seconds: float,
        current_time: datetime,
        events: list[TimelineEvent]
    ) -> None:
        """Update truck in RESUPPLY status"""
        # Track resupply time
        if truck_id not in self.resupply_timers:
            self.resupply_timers[truck_id] = 0

        self.resupply_timers[truck_id] += delta_seconds

        # Phase 1: Travel to staging
        resupply_travel_phase = self._settings.resupply_travel_phase_sec
        resupply_total = self._settings.resupply_total_duration_sec

        if self.resupply_timers[truck_id] < resupply_travel_phase:
            # Moving toward staging area
            fire_lat, fire_lng = self.fire_positions[truck_id]
            staging_lat, staging_lng = self.staging_positions[truck_id]

            current_lat, current_lng, heading = calculate_travel_progress(
                fire_lat, fire_lng,
                staging_lat, staging_lng,
                int(self.resupply_timers[truck_id]),
                self._settings.resupply_speed_mph
            )

            truck.location.latitude = current_lat
            truck.location.longitude = current_lng
            truck.location.heading = heading
            truck.location.speed_mph = self._settings.resupply_speed_mph

        # Phase 2: Refilling at staging
        elif self.resupply_timers[truck_id] < resupply_total:
            # At staging area, refilling
            staging_lat, staging_lng = self.staging_positions[truck_id]
            truck.location.latitude = staging_lat
            truck.location.longitude = staging_lng
            truck.location.speed_mph = 0

        # Phase 3: Resupply complete - transition back to SUPPRESSION
        else:
            # Complete resupply and go back to fire
            complete_resupply(truck)
            del self.resupply_timers[truck_id]

            # Transition directly to SUPPRESSION (since trucks are close)
            transition_to_suppression(truck)
            truck.location.speed_mph = 0

            # Move back to fire position
            fire_lat, fire_lng = self.fire_positions[truck_id]
            truck.location.latitude = fire_lat
            truck.location.longitude = fire_lng

            events.append(TimelineEvent(
                time=format_time(
                    truck.operational.dispatch_time_seconds_ago),
                elapsed_seconds=truck.operational.dispatch_time_seconds_ago,
                event_type=EventType.RESUPPLY_COMPLETE,
                truck_id=truck_id,
                message=f"{truck.call_sign} resupply complete, returning to suppression",
                timestamp=current_time
            ))

        # Update distance to fire
        distance = calculate_distance_feet(
            truck.location.latitude, truck.location.longitude,
            self.fire_location.latitude, self.fire_location.longitude
        )
        truck.location.distance_to_fire_ft = int(distance)

    def _calculate_environmental_conditions(
        self,
        latitude: float,
        longitude: float,
        distance_to_fire: float
    ) -> TruckEnvironmental:
        """Calculate environmental conditions based on position"""
        settings = self._settings

        # Temperature increases closer to fire (capped at 145°F to stay within model limits)
        distance_1000ft = distance_to_fire / 1000
        temp = settings.base_temp_f + (settings.temp_increase_per_1000ft *
                                       max(0, 10 - distance_1000ft))
        temp = min(145, temp)  # Cap to stay within Pydantic validation range

        # Visibility decreases closer to fire
        visibility = settings.base_visibility_mi - \
            (settings.visibility_decrease_per_1000ft * max(0, 10 - distance_1000ft))
        visibility = max(0.1, visibility)

        # AQI increases closer to fire
        aqi = settings.base_aqi + int(settings.aqi_increase_per_1000ft *
                                      max(0, 10 - distance_1000ft))
        aqi = min(500, aqi)

        # Smoke density based on AQI
        smoke = calculate_smoke_density(aqi)

        # Heat exposure based on distance
        heat_exposure = calculate_heat_exposure_index(int(distance_to_fire))

        # Add some variation
        wind_variation = random.uniform(-3, 3)

        return TruckEnvironmental(
            ambient_temp_f=round(temp, 1),
            wind_speed_mph=round(settings.base_wind_mph + wind_variation, 1),
            wind_direction=random.randint(0, 360),
            wind_gust_mph=round(settings.base_wind_mph +
                                wind_variation + 5, 1),
            visibility_mi=round(visibility, 2),
            air_quality_index=aqi,
            smoke_density=smoke,
            heat_exposure_index=heat_exposure,
            relative_humidity=settings.base_humidity_pct,
            precipitation_rate=0
        )

    def _update_environmental(self, truck: FireTruck) -> None:
        """Update environmental conditions based on current position"""
        truck.environmental = self._calculate_environmental_conditions(
            truck.location.latitude,
            truck.location.longitude,
            truck.location.distance_to_fire_ft
        )

    def get_all_trucks(self) -> list[FireTruck]:
        """Get list of all trucks"""
        return list(self.trucks.values())

    def get_truck(self, truck_id: str) -> FireTruck | None:
        """Get specific truck by ID"""
        return self.trucks.get(truck_id)

    def remove_truck(self, truck_id: str) -> None:
        """Remove a truck from the simulation"""
        if truck_id in self.trucks:
            del self.trucks[truck_id]
        if truck_id in self.resupply_timers:
            del self.resupply_timers[truck_id]

    def get_truck_count(self) -> int:
        """Get total number of active trucks"""
        return len(self.trucks)
