"""
Static Default Values and Seed Data
Values that don't need environment configuration
"""

# ============================================================================
# STATIC SEED DATA - Names, Channels, Sectors
# ============================================================================

CAPTAIN_NAMES = [
    "Capt. Martinez",
    "Capt. Johnson",
    "Capt. Williams",
    "Capt. Chen",
    "Capt. Rodriguez",
    "Capt. Anderson",
    "Capt. Thompson",
    "Capt. Garcia",
    "Capt. Lee",
    "Capt. Taylor",
    "Capt. Davis",
    "Capt. Miller",
]

RADIO_CHANNELS = [
    "TAC-1",
    "TAC-2",
    "TAC-3",
    "TAC-4",
    "TAC-5",
    "COMMAND",
]

SECTORS = [
    "A1", "A2", "A3",
    "B1", "B2", "B3",
    "C1", "C2", "C3",
    "D1", "D2", "D3",
]

# ============================================================================
# SMOKE DENSITY THRESHOLDS (AQI-based) - Static classification
# ============================================================================

SMOKE_NONE_AQI = 50
SMOKE_LIGHT_AQI = 100
SMOKE_MODERATE_AQI = 150
SMOKE_HEAVY_AQI = 200
SMOKE_SEVERE_AQI = 300
