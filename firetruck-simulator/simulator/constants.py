"""
Simulation Constants
Truck dispatch schedule and unit roles - scenario data that doesn't need environment configuration
"""
from api.models.truck import UnitType

# ============================================================================
# UNIT ROLES - the one declaration of which unit types pump water at the fire
# Command and rescue units support the incident: on arrival they stage with the pump off, so their placeholder
# tanks (config/settings.py) stay full and they never resupply (behaviors.py:transition_on_arrival).
# ============================================================================
PUMPING_UNIT_TYPES = frozenset({UnitType.ENGINE, UnitType.TANKER, UnitType.BRUSH})

# ============================================================================
# TRUCK ADDITION SCHEDULE - Demo Optimized
# Shows all truck types within 3 minutes for engaging demonstrations
# ============================================================================

# Format: (elapsed_seconds, unit_type, truck_number)
TRUCK_SCHEDULE = [
    # t=0s: Initial diverse response (show all types quickly)
    (0, UnitType.ENGINE, 42),      # 🚒 Primary attack engine
    (0, UnitType.TANKER, 7),       # 🚛 Water supply tanker

    # t=15s: Wildland unit arrives
    (15, UnitType.BRUSH, 3),       # 🚙 Wildland brush truck

    # t=30s: Command post established
    (30, UnitType.COMMAND, 1),     # 🚗 Command vehicle

    # t=45s: Medical/rescue unit
    (45, UnitType.RESCUE, 5),      # 🚑 Medical rescue unit

    # t=60s+: Additional resources (show system scaling)
    (60, UnitType.ENGINE, 15),     # 🚒 Reinforcement engine
    (75, UnitType.BRUSH, 11),      # 🚙 Additional wildland unit
    (90, UnitType.TANKER, 12),     # 🚛 Additional water supply
    (105, UnitType.ENGINE, 8),     # 🚒 More engines for sustained ops
    (120, UnitType.RESCUE, 3),     # 🚑 Additional rescue capability

    # t=150s+: Extended operations (show rotation/relief)
    (150, UnitType.ENGINE, 19),    # 🚒 Crew relief engine
    (165, UnitType.BRUSH, 7),      # 🚙 Extended wildland coverage
    (180, UnitType.COMMAND, 2),    # 🚗 Additional command support
]
