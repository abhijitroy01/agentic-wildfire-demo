"""
Simulation Start Request
The run configuration the broker sends to the private service (api/private.py)
"""

from pydantic import BaseModel, ConfigDict, Field

from .incident import FireLocation

# ============================================================================
# START SIMULATION REQUEST
# ============================================================================


class StartSimulationRequest(BaseModel):
    """Request to start a new simulation"""

    fire_location: FireLocation = Field(..., description="Fire incident location")
    update_interval_seconds: int = Field(2, ge=1, le=10, description="Seconds between simulation updates")
    initial_truck_count: int = Field(4, ge=2, le=12, description="Initial trucks to dispatch")
    max_truck_count: int = Field(12, ge=4, le=20, description="Maximum trucks in simulation")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "fire_location": {"latitude": 34.2591, "longitude": -118.0853, "name": "Eaton Fire"},
                "update_interval_seconds": 2,
                "initial_truck_count": 4,
                "max_truck_count": 12,
            }
        }
    )
