"""
Configuration module for Fire Truck Simulator
"""
from .defaults import (
    CAPTAIN_NAMES,
    RADIO_CHANNELS,
    SECTORS,
    SMOKE_HEAVY_AQI,
    SMOKE_LIGHT_AQI,
    SMOKE_MODERATE_AQI,
    SMOKE_NONE_AQI,
    SMOKE_SEVERE_AQI,
)
from .settings import (
    Settings,
    get_crew_capacity,
    get_flow_rate_for_pressure,
    get_foam_capacity,
    get_settings,
    get_tank_capacity,
)

__all__ = [
    # Static data
    "CAPTAIN_NAMES",
    "RADIO_CHANNELS",
    "SECTORS",
    "SMOKE_HEAVY_AQI",
    "SMOKE_LIGHT_AQI",
    "SMOKE_MODERATE_AQI",
    "SMOKE_NONE_AQI",
    "SMOKE_SEVERE_AQI",
    "Settings",
    "get_crew_capacity",
    "get_flow_rate_for_pressure",
    "get_foam_capacity",
    # Settings
    "get_settings",
    # Helper functions
    "get_tank_capacity",
]
