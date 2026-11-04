"""
Incident and Simulation Pydantic Models
Models for fire incidents and simulation configuration
"""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

# ============================================================================
# ENUMS
# ============================================================================


class SimulationPhase(str, Enum):
    """Simulation operational phases"""

    INITIAL_DISPATCH = "INITIAL_DISPATCH"
    EN_ROUTE = "EN_ROUTE"
    INITIAL_ATTACK = "INITIAL_ATTACK"
    BUILDING_RESPONSE = "BUILDING_RESPONSE"
    FULL_RESPONSE = "FULL_RESPONSE"
    SUSTAINED_OPS = "SUSTAINED_OPS"
    ROTATION = "ROTATION"


class EventType(str, Enum):
    """Timeline event types"""

    DISPATCH = "DISPATCH"
    ARRIVAL = "ARRIVAL"
    SUPPRESSION_START = "SUPPRESSION_START"
    RESUPPLY_START = "RESUPPLY_START"
    RESUPPLY_COMPLETE = "RESUPPLY_COMPLETE"
    CREW_ROTATION = "CREW_ROTATION"
    SAFETY_ALERT = "SAFETY_ALERT"
    TRUCK_ADDED = "TRUCK_ADDED"


# ============================================================================
# FIRE LOCATION
# ============================================================================


class FireLocation(BaseModel):
    """Fire incident location"""

    latitude: float = Field(..., ge=-90, le=90, description="Fire latitude")
    longitude: float = Field(..., ge=-180, le=180, description="Fire longitude")
    # Bounded like the broker's own model (firetruck_broker.py), since every snapshot republishes it.
    name: str | None = Field(None, max_length=160, description="Fire incident name")

    model_config = ConfigDict(
        json_schema_extra={"example": {"latitude": 34.2591, "longitude": -118.0853, "name": "Eaton Fire"}}
    )


# ============================================================================
# TIMELINE EVENT
# ============================================================================


class TimelineEvent(BaseModel):
    """Single timeline event"""

    time: str = Field(..., description="Time since start (HH:MM:SS)")
    elapsed_seconds: int = Field(..., ge=0, description="Seconds since start")
    event_type: EventType = Field(..., description="Type of event")
    truck_id: str | None = Field(None, description="Associated truck ID")
    message: str = Field(..., description="Event description")
    # Required: every caller passes an aware UTC time, and a naive utcnow() default could slip in unnoticed.
    timestamp: datetime = Field(..., description="Actual timestamp")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "time": "00:03:20",
                "elapsed_seconds": 200,
                "event_type": "ARRIVAL",
                "truck_id": "ENGINE-42",
                "message": "Engine 42 arrived on scene",
                "timestamp": "2025-01-15T16:03:20Z",
            }
        }
    )


# ============================================================================
# INCIDENT SUMMARY
# ============================================================================


class IncidentSummary(BaseModel):
    """Incident overview information"""

    id: str = Field(..., description="Incident ID")
    name: str = Field(..., description="Incident name")
    location: FireLocation
    start_time: datetime = Field(..., description="Incident start time")
    elapsed_seconds: int = Field(..., ge=0, description="Time since start")
    elapsed_formatted: str = Field(..., description="Duration (HH:MM:SS)")
    phase: SimulationPhase = Field(..., description="Current simulation phase")
    estimated_acres: float = Field(..., ge=0, description="Estimated fire size")
    perimeter_ft: int = Field(..., ge=0, description="Estimated perimeter length")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "id": "INC-7A3F2B",
                "name": "Simulated Fire - Eaton Area",
                "location": {"latitude": 34.2591, "longitude": -118.0853, "name": "Eaton Fire"},
                "start_time": "2025-01-15T16:00:00Z",
                "elapsed_seconds": 600,
                "elapsed_formatted": "00:10:00",
                "phase": "FULL_RESPONSE",
                "estimated_acres": 15,
                "perimeter_ft": 6500,
            }
        }
    )


# ============================================================================
# RESOURCE SUMMARY
# ============================================================================


class ResourceSummary(BaseModel):
    """Summary of deployed resources"""

    total_trucks: int = Field(..., ge=0, description="Total trucks deployed")
    engines: int = Field(0, ge=0, description="Number of engines")
    tankers: int = Field(0, ge=0, description="Number of tankers")
    brush: int = Field(0, ge=0, description="Number of brush trucks")
    rescue: int = Field(0, ge=0, description="Number of rescue units")
    command: int = Field(0, ge=0, description="Number of command vehicles")
    active_suppression: int = Field(0, ge=0, description="Trucks actively suppressing")
    en_route: int = Field(0, ge=0, description="Trucks en route")
    resupplying: int = Field(0, ge=0, description="Trucks resupplying")
    total_personnel: int = Field(0, ge=0, description="Total crew members")
    total_water_capacity_gal: int = Field(0, ge=0, description="Total water capacity")
    total_water_available_gal: int = Field(0, ge=0, description="Total water remaining")
    total_water_deployed_gal: int = Field(0, ge=0, description="Total water used")
    average_water_capacity_pct: int = Field(0, ge=0, le=100, description="Average tank level")
    crews_needing_rotation: int = Field(0, ge=0, description="Crews with high fatigue")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "total_trucks": 8,
                "engines": 4,
                "tankers": 2,
                "brush": 2,
                "rescue": 0,
                "command": 0,
                "active_suppression": 5,
                "en_route": 2,
                "resupplying": 1,
                "total_personnel": 32,
                "total_water_capacity_gal": 12000,
                "total_water_available_gal": 8750,
                "total_water_deployed_gal": 3250,
                "average_water_capacity_pct": 73,
                "crews_needing_rotation": 0,
            }
        }
    )


# ============================================================================
# CURRENT STATUS
# ============================================================================


class CurrentStatus(BaseModel):
    """Current operational status"""

    active_suppression_trucks: int = Field(0, ge=0, description="Trucks actively fighting fire")
    resupplying: int = Field(0, ge=0, description="Trucks resupplying")
    en_route: int = Field(0, ge=0, description="Trucks traveling to scene")
    crew_fatigue_alerts: int = Field(0, ge=0, description="Crews needing rotation")
    safety_concerns: list[str] = Field(default_factory=list, description="Active safety issues")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "active_suppression_trucks": 5,
                "resupplying": 1,
                "en_route": 2,
                "crew_fatigue_alerts": 0,
                "safety_concerns": [],
            }
        }
    )


# ============================================================================
# SIMULATION SUMMARY (FOR AGENT)
# ============================================================================


class SimulationSummary(BaseModel):
    """Complete simulation summary for AgentCore Response Agent"""

    incident: IncidentSummary
    resource_summary: ResourceSummary
    timeline: list[TimelineEvent]
    current_status: CurrentStatus
    recommendations: list[str] = Field(default_factory=list, description="AI recommendations")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "incident": {
                    "id": "INC-7A3F2B",
                    "name": "Simulated Fire - Eaton Area",
                    "location": {"latitude": 34.2591, "longitude": -118.0853},
                    "elapsed_formatted": "01:23:45",
                    "phase": "FULL_RESPONSE",
                },
                "resource_summary": {"total_trucks": 8, "total_personnel": 32},
                "timeline": [
                    {
                        "time": "00:00:00",
                        "event_type": "DISPATCH",
                        "message": "Engine 42 and Engine 15 dispatched",
                    }
                ],
                "current_status": {"active_suppression_trucks": 5, "resupplying": 1, "en_route": 2},
                "recommendations": ["Consider crew rotation for Engine 15 (approaching 2-hour limit)"],
            }
        }
    )
