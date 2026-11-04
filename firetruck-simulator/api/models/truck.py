"""
Fire Truck Pydantic Models
Complete telemetry data structures for fire apparatus simulation
"""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

# ============================================================================
# ENUMS
# ============================================================================


class UnitType(str, Enum):
    """Fire apparatus types"""

    ENGINE = "ENGINE"
    TANKER = "TANKER"
    BRUSH = "BRUSH"
    RESCUE = "RESCUE"
    COMMAND = "COMMAND"


class EngineStatus(str, Enum):
    """Engine operational state"""

    RUNNING = "RUNNING"
    IDLE = "IDLE"
    OFF = "OFF"


class PumpStatus(str, Enum):
    """Pump operational state"""

    ACTIVE = "ACTIVE"
    STANDBY = "STANDBY"
    OFF = "OFF"
    MALFUNCTION = "MALFUNCTION"


class SuppressantType(str, Enum):
    """Type of fire suppressant"""

    WATER = "WATER"
    FOAM = "FOAM"
    RETARDANT = "RETARDANT"


class CrewStatus(str, Enum):
    """Crew operational status"""

    ACTIVE = "ACTIVE"
    RESTING = "RESTING"
    ROTATING = "ROTATING"
    MEDICAL = "MEDICAL"


class CrewFatigueLevel(str, Enum):
    """Crew fatigue assessment"""

    FRESH = "FRESH"
    MODERATE = "MODERATE"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class ActivityStatus(str, Enum):
    """Current operational activity"""

    EN_ROUTE = "EN_ROUTE"
    SUPPRESSION = "SUPPRESSION"
    HOLDING = "HOLDING"
    RESUPPLY = "RESUPPLY"
    STAGING = "STAGING"
    EVACUATION = "EVACUATION"


class MissionStatus(str, Enum):
    """Overall mission state"""

    ASSIGNED = "ASSIGNED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class SmokeDensity(str, Enum):
    """Smoke concentration levels"""

    NONE = "NONE"
    LIGHT = "LIGHT"
    MODERATE = "MODERATE"
    HEAVY = "HEAVY"
    SEVERE = "SEVERE"


class EntrapmentRisk(str, Enum):
    """Fire behavior entrapment risk"""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class TransmissionStatus(str, Enum):
    """Transmission health status"""

    NORMAL = "NORMAL"
    WARNING = "WARNING"
    ERROR = "ERROR"


# ============================================================================
# LOCATION & MOVEMENT TELEMETRY
# ============================================================================


class TruckLocation(BaseModel):
    """Location and movement telemetry"""

    latitude: float = Field(..., ge=-90, le=90, description="GPS latitude (WGS84)")
    longitude: float = Field(..., ge=-180, le=180, description="GPS longitude (WGS84)")
    altitude_ft: float = Field(..., ge=0, le=14000, description="Elevation above sea level")
    heading: float = Field(..., ge=0, le=360, description="Compass bearing (0=North, 90=East)")
    speed_mph: float = Field(..., ge=0, le=70, description="Current vehicle speed")
    distance_to_fire_ft: int = Field(..., ge=0, le=50000, description="Distance to nearest fire perimeter")


# ============================================================================
# VEHICLE STATUS TELEMETRY
# ============================================================================


class TruckVehicle(BaseModel):
    """Vehicle health and operational readiness"""

    engine_status: EngineStatus = Field(..., description="Engine operational state")
    fuel_level_pct: int = Field(..., ge=0, le=100, description="Fuel tank percentage")
    fuel_consumption_gph: float = Field(..., ge=0, le=10, description="Current fuel consumption rate")
    battery_voltage: float = Field(..., ge=11, le=15, description="Vehicle battery voltage")
    engine_temp_f: float = Field(..., ge=150, le=250, description="Engine coolant temperature")
    odometer_miles: int = Field(..., ge=0, description="Total vehicle mileage")
    transmission_status: TransmissionStatus = Field(..., description="Transmission health")
    tire_pressure_psi: list[float] = Field(
        ..., min_length=4, max_length=4, description="Individual tire pressures"
    )


# ============================================================================
# FIREFIGHTING CAPACITY TELEMETRY
# ============================================================================


class TruckWater(BaseModel):
    """Water and foam resources"""

    tank_capacity_gal: int = Field(..., ge=500, le=3000, description="Maximum water tank capacity")
    tank_level_gal: int = Field(..., ge=0, le=3000, description="Current water volume")
    tank_level_pct: int = Field(..., ge=0, le=100, description="Percentage remaining")
    foam_capacity_gal: int = Field(..., ge=20, le=100, description="Foam concentrate capacity")
    foam_level_gal: int = Field(..., ge=0, le=100, description="Current foam volume")
    foam_level_pct: int = Field(..., ge=0, le=100, description="Foam percentage remaining")
    pump_status: PumpStatus = Field(..., description="Pump operational state")
    pump_pressure_psi: int = Field(..., ge=0, le=250, description="Current pump discharge pressure")
    flow_rate_gpm: int = Field(..., ge=0, le=1500, description="Water flow rate (gallons per minute)")
    hose_deployed_ft: int = Field(..., ge=0, le=1000, description="Total hose length deployed")
    suppressant_type: SuppressantType = Field(..., description="Current suppressant in use")
    estimated_water_remaining_min: float = Field(
        ..., ge=0, le=120, description="Time until tank empty at current flow"
    )
    total_water_deployed_gal: int = Field(..., ge=0, description="Total water used in incident")


# ============================================================================
# CREW TELEMETRY
# ============================================================================


class TruckCrew(BaseModel):
    """Crew status and safety"""

    count: int = Field(..., ge=1, le=8, description="Current personnel on truck")
    capacity: int = Field(..., ge=2, le=8, description="Maximum crew capacity")
    hours_on_duty: float = Field(..., ge=0, le=24, description="Time since crew dispatch")
    status: CrewStatus = Field(..., description="Crew operational status")
    captain_name: str = Field(..., description="Unit commander name")
    fatigue_level: CrewFatigueLevel = Field(..., description="Fatigue assessment")
    medical_incidents: int = Field(0, ge=0, le=10, description="Number of crew medical issues")
    water_consumption_gal: float = Field(0, ge=0, le=5, description="Crew hydration tracking")
    rest_period_min: int = Field(0, ge=0, le=120, description="Total rest time during incident")


# ============================================================================
# OPERATIONAL STATUS TELEMETRY
# ============================================================================


class TruckOperational(BaseModel):
    """Mission status and tactical assignments"""

    activity_status: ActivityStatus = Field(..., description="Current operational activity")
    assignment_sector: str = Field(..., description="Fire sector assignment (e.g., A1, B2)")
    radio_channel: str = Field(..., description="Current radio frequency")
    dispatch_time: datetime = Field(..., description="Original dispatch timestamp")
    dispatch_time_seconds_ago: int = Field(..., ge=0, description="Time since dispatch")
    arrival_time: datetime | None = Field(None, description="Scene arrival timestamp")
    arrival_time_seconds_ago: int | None = Field(None, ge=0, description="Time since arrival")
    last_resupply_time: datetime | None = Field(None, description="Last water refill timestamp")
    resupply_count: int = Field(0, ge=0, le=50, description="Number of resupply cycles")
    mission_status: MissionStatus = Field(..., description="Overall mission state")


# ============================================================================
# ENVIRONMENTAL READINGS TELEMETRY
# ============================================================================


class TruckEnvironmental(BaseModel):
    """Environmental hazards monitoring"""

    ambient_temp_f: float = Field(
        ...,
        ge=50,
        le=150,
        description="Air temperature at truck location (wildfire conditions can exceed 130°F)",
    )
    wind_speed_mph: float = Field(..., ge=0, le=80, description="Local wind speed")
    wind_direction: float = Field(..., ge=0, le=360, description="Wind bearing (meteorological)")
    wind_gust_mph: float = Field(..., ge=0, le=100, description="Peak wind gust speed")
    visibility_mi: float = Field(..., ge=0, le=10, description="Horizontal visibility distance")
    air_quality_index: int = Field(..., ge=0, le=500, description="EPA Air Quality Index")
    smoke_density: SmokeDensity = Field(..., description="Smoke concentration level")
    heat_exposure_index: int = Field(..., ge=0, le=100, description="Thermal radiation danger score")
    relative_humidity: int = Field(..., ge=0, le=100, description="Relative humidity percentage")
    precipitation_rate: float = Field(0, ge=0, le=5, description="Current rainfall rate (in/hr)")


# ============================================================================
# SAFETY INDICATORS TELEMETRY
# ============================================================================


class TruckSafety(BaseModel):
    """Safety indicators and warnings"""

    in_danger_zone: bool = Field(..., description="Truck within 500ft of active fire")
    escape_route_clear: bool = Field(..., description="Primary escape route passable")
    lookout_contact: bool = Field(..., description="Visual contact with lookout")
    safety_zone_distance_ft: int = Field(..., ge=0, le=5000, description="Distance to nearest safety zone")
    entrapment_risk: EntrapmentRisk = Field(..., description="Fire behavior entrapment risk level")
    fire_spread_rate_fph: float = Field(
        ..., ge=0, le=1000, description="Fire spread rate toward truck (ft/hr)"
    )
    downhill_position: bool = Field(..., description="Truck positioned below fire (high risk)")
    escape_time_seconds: int = Field(..., ge=0, le=600, description="Time to reach safety zone")
    mayday_active: bool = Field(False, description="Emergency distress signal active")
    last_safety_check: datetime | None = Field(None, description="Last LCES protocol verification")


# ============================================================================
# COMPLETE TRUCK MODEL
# ============================================================================


class FireTruck(BaseModel):
    """Complete fire truck with all telemetry data"""

    # Core identification
    truck_id: str = Field(..., description="Unique identifier (e.g., ENGINE-42)")
    unit_type: UnitType = Field(..., description="Type of fire apparatus")
    call_sign: str = Field(..., description="Radio call sign (e.g., Engine 42)")

    # Telemetry groups
    location: TruckLocation
    vehicle: TruckVehicle
    water: TruckWater
    crew: TruckCrew
    operational: TruckOperational
    environmental: TruckEnvironmental
    safety: TruckSafety

    # Metadata
    # Required: every caller passes an aware UTC time, and a naive utcnow() default could slip in unnoticed.
    last_update: datetime = Field(..., description="Last telemetry update")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "truck_id": "ENGINE-42",
                "unit_type": "ENGINE",
                "call_sign": "Engine 42",
                "location": {
                    "latitude": 34.2605,
                    "longitude": -118.0841,
                    "altitude_ft": 1250,
                    "heading": 225,
                    "speed_mph": 0,
                    "distance_to_fire_ft": 820,
                },
                "vehicle": {
                    "engine_status": "RUNNING",
                    "fuel_level_pct": 85,
                    "fuel_consumption_gph": 2.5,
                    "battery_voltage": 13.8,
                    "engine_temp_f": 195,
                    "odometer_miles": 45000,
                    "transmission_status": "NORMAL",
                    "tire_pressure_psi": [105, 105, 110, 110],
                },
                "water": {
                    "tank_capacity_gal": 750,
                    "tank_level_gal": 520,
                    "tank_level_pct": 69,
                    "foam_capacity_gal": 30,
                    "foam_level_gal": 22,
                    "foam_level_pct": 73,
                    "pump_status": "ACTIVE",
                    "pump_pressure_psi": 150,
                    "flow_rate_gpm": 350,
                    "hose_deployed_ft": 400,
                    "suppressant_type": "WATER",
                    "estimated_water_remaining_min": 1.5,
                    "total_water_deployed_gal": 230,
                },
                "crew": {
                    "count": 4,
                    "capacity": 4,
                    "hours_on_duty": 0.17,
                    "status": "ACTIVE",
                    "captain_name": "Capt. Martinez",
                    "fatigue_level": "FRESH",
                    "medical_incidents": 0,
                    "water_consumption_gal": 0.5,
                    "rest_period_min": 0,
                },
                "operational": {
                    "activity_status": "SUPPRESSION",
                    "assignment_sector": "A1",
                    "radio_channel": "TAC-3",
                    "dispatch_time": "2025-01-15T16:00:00Z",
                    "dispatch_time_seconds_ago": 600,
                    "arrival_time": "2025-01-15T16:07:00Z",
                    "arrival_time_seconds_ago": 180,
                    "last_resupply_time": None,
                    "resupply_count": 0,
                    "mission_status": "IN_PROGRESS",
                },
                "environmental": {
                    "ambient_temp_f": 92,
                    "wind_speed_mph": 15,
                    "wind_direction": 270,
                    "wind_gust_mph": 22,
                    "visibility_mi": 0.5,
                    "air_quality_index": 185,
                    "smoke_density": "MODERATE",
                    "heat_exposure_index": 45,
                    "relative_humidity": 25,
                    "precipitation_rate": 0,
                },
                "safety": {
                    "in_danger_zone": False,
                    "escape_route_clear": True,
                    "lookout_contact": True,
                    "safety_zone_distance_ft": 1200,
                    "entrapment_risk": "LOW",
                    "fire_spread_rate_fph": 150,
                    "downhill_position": False,
                    "escape_time_seconds": 180,
                    "mayday_active": False,
                    "last_safety_check": "2025-01-15T16:09:00Z",
                },
            }
        }
    )


# ============================================================================
# TRUCK RECORD IN EVIDENCE SNAPSHOTS
# ============================================================================


class TruckUpdateSimplified(BaseModel):
    """A truck in each evidence snapshot (private.py snapshot()); the only server copy of the contract"""

    truck_id: str
    unit_type: UnitType
    latitude: float
    longitude: float
    heading: float
    speed_mph: float
    activity_status: ActivityStatus
    distance_to_fire_ft: int
    water_tank_level_gal: int
    water_tank_level_pct: int
    foam_tank_level_gal: int
    pump_status: PumpStatus
    flow_rate_gpm: int
    estimated_water_remaining_min: float
    crew_count: int
    hours_on_duty: float
    crew_fatigue_level: CrewFatigueLevel
    in_danger_zone: bool
    entrapment_risk: EntrapmentRisk
    escape_route_clear: bool

    @classmethod
    def from_fire_truck(cls, truck: FireTruck) -> TruckUpdateSimplified:
        """Convert full FireTruck to simplified version"""
        return cls(
            truck_id=truck.truck_id,
            unit_type=truck.unit_type,
            latitude=truck.location.latitude,
            longitude=truck.location.longitude,
            heading=truck.location.heading,
            speed_mph=truck.location.speed_mph,
            activity_status=truck.operational.activity_status,
            distance_to_fire_ft=truck.location.distance_to_fire_ft,
            water_tank_level_gal=truck.water.tank_level_gal,
            water_tank_level_pct=truck.water.tank_level_pct,
            foam_tank_level_gal=truck.water.foam_level_gal,
            pump_status=truck.water.pump_status,
            flow_rate_gpm=truck.water.flow_rate_gpm,
            estimated_water_remaining_min=truck.water.estimated_water_remaining_min,
            crew_count=truck.crew.count,
            hours_on_duty=truck.crew.hours_on_duty,
            crew_fatigue_level=truck.crew.fatigue_level,
            in_danger_zone=truck.safety.in_danger_zone,
            entrapment_risk=truck.safety.entrapment_risk,
            escape_route_clear=truck.safety.escape_route_clear,
        )
