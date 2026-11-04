"""
Simulation Engine
Core state machine managing the entire simulation lifecycle
"""
import uuid
from datetime import UTC, datetime, timedelta

from api.models.incident import (
    CurrentStatus,
    EventType,
    FireLocation,
    IncidentSummary,
    ResourceSummary,
    SimulationPhase,
    SimulationSummary,
    TimelineEvent,
)
from api.models.truck import ActivityStatus, CrewFatigueLevel, UnitType
from config import get_settings

from .constants import TRUCK_SCHEDULE
from .truck_manager import TruckManager, format_time


class SimulationEngine:
    """Core simulation engine managing state and lifecycle"""

    def __init__(
        self,
        fire_location: FireLocation,
        update_interval_seconds: int = 2,
        max_truck_count: int = 12,
        initial_truck_count: int = 4
    ):
        self.session_id = f"sim-{uuid.uuid4().hex[:12]}"
        self.fire_location = fire_location
        self.update_interval = update_interval_seconds
        self.max_truck_count = max_truck_count
        self.initial_truck_count = initial_truck_count
        self._settings = get_settings()

        # State
        self.start_time = datetime.now(UTC)
        self.elapsed_seconds = 0
        self.is_running = False
        self.phase = SimulationPhase.INITIAL_DISPATCH

        # Incident tracking
        self.incident_id = f"INC-{uuid.uuid4().hex[:6].upper()}"
        self.incident_name = f"Simulated Fire - {fire_location.name or 'Unknown Area'}"
        self.estimated_acres = self._settings.initial_fire_size_min_acres

        # Components
        self.truck_manager = TruckManager(fire_location)
        self.timeline_events: list[TimelineEvent] = []
        self.last_update_time = datetime.now(UTC)

        # Tracking
        # truck_id -> elapsed_seconds
        self.trucks_added_at: dict[str, int] = {}

    async def start(self) -> None:
        """Start the simulation"""
        self.is_running = True
        self.start_time = datetime.now(UTC)
        self.last_update_time = datetime.now(UTC)

        # Dispatch initial trucks
        await self._dispatch_initial_trucks()

    async def stop(self) -> dict:
        """Stop the simulation and return final stats"""
        self.is_running = False

        # Calculate final stats
        total_water_deployed = sum(
            t.water.total_water_deployed_gal
            for t in self.truck_manager.get_all_trucks()
        )

        total_crew_hours = sum(
            t.crew.hours_on_duty * t.crew.count
            for t in self.truck_manager.get_all_trucks()
        )

        return {
            "duration_seconds": self.elapsed_seconds,
            "trucks_deployed": self.truck_manager.get_truck_count(),
            "total_water_deployed_gal": total_water_deployed,
            "total_crew_hours": round(total_crew_hours, 1)
        }

    async def _dispatch_initial_trucks(self) -> None:
        """Dispatch initial trucks at t=0 based on initial_truck_count"""
        # Build list of available trucks from schedule
        list(TRUCK_SCHEDULE)
        trucks_dispatched = 0

        # First dispatch trucks scheduled for t=0
        for elapsed_time, unit_type, truck_number in TRUCK_SCHEDULE:
            if elapsed_time == 0 and trucks_dispatched < self.initial_truck_count:
                truck = self.truck_manager.create_truck(
                    unit_type,
                    truck_number,
                    self.start_time
                )
                self.trucks_added_at[truck.truck_id] = 0
                trucks_dispatched += 1

                self.timeline_events.append(TimelineEvent(
                    time="00:00:00",
                    elapsed_seconds=0,
                    event_type=EventType.DISPATCH,
                    truck_id=truck.truck_id,
                    message=f"{truck.call_sign} dispatched to incident",
                    timestamp=self.start_time
                ))

        # If we need more trucks than scheduled at t=0, dispatch from later in schedule
        remaining_schedule = [t for t in TRUCK_SCHEDULE if t[0] > 0]
        for elapsed_time, unit_type, truck_number in remaining_schedule:
            if trucks_dispatched >= self.initial_truck_count:
                break

            truck = self.truck_manager.create_truck(
                unit_type,
                truck_number,
                self.start_time
            )
            self.trucks_added_at[truck.truck_id] = 0
            trucks_dispatched += 1

            self.timeline_events.append(TimelineEvent(
                time="00:00:00",
                elapsed_seconds=0,
                event_type=EventType.DISPATCH,
                truck_id=truck.truck_id,
                message=f"{truck.call_sign} dispatched to incident (early deployment)",
                timestamp=self.start_time
            ))

    async def update(self) -> list[TimelineEvent]:
        """
        Update simulation state by one time step.
        Returns list of new events that occurred.
        """
        if not self.is_running:
            return []

        current_time = datetime.now(UTC)
        delta_seconds = (current_time - self.last_update_time).total_seconds()
        self.last_update_time = current_time

        self.elapsed_seconds = int(
            (current_time - self.start_time).total_seconds())

        new_events: list[TimelineEvent] = []

        # Update phase
        self._update_phase()

        # Check for new truck dispatches
        await self._check_truck_additions(new_events)

        # Update all trucks
        for truck_id in list(self.truck_manager.trucks.keys()):
            events = self.truck_manager.update_truck(
                truck_id,
                self.elapsed_seconds,
                delta_seconds
            )
            if events:
                new_events.extend(events)
                self.timeline_events.extend(events)

        # Update fire size
        self._update_fire_size(delta_seconds)

        return new_events

    def _update_phase(self) -> None:
        """Determine current simulation phase based on elapsed time"""
        if self.elapsed_seconds < 180:
            self.phase = SimulationPhase.INITIAL_DISPATCH
        elif self.elapsed_seconds < 600:
            self.phase = SimulationPhase.INITIAL_ATTACK
        elif self.elapsed_seconds < 1200:
            self.phase = SimulationPhase.BUILDING_RESPONSE
        elif self.elapsed_seconds < 1800:
            self.phase = SimulationPhase.FULL_RESPONSE
        elif self.elapsed_seconds < 3600:
            self.phase = SimulationPhase.SUSTAINED_OPS
        else:
            self.phase = SimulationPhase.ROTATION

    async def _check_truck_additions(self, events: list[TimelineEvent]) -> None:
        """Dispatch every scheduled truck whose offset has passed, up to the maximum truck count"""
        for elapsed_time, unit_type, truck_number in TRUCK_SCHEDULE:
            if self.truck_manager.get_truck_count() >= self.max_truck_count:
                return
            # Updates run seconds apart, so a truck is due once its offset has passed, not only on that exact second.
            if (elapsed_time <= self.elapsed_seconds and
                    f"{unit_type.value}-{truck_number}" not in self.truck_manager.trucks):

                dispatch_time = self.start_time + \
                    timedelta(seconds=elapsed_time)
                truck = self.truck_manager.create_truck(
                    unit_type,
                    truck_number,
                    dispatch_time
                )
                self.trucks_added_at[truck.truck_id] = elapsed_time

                event = TimelineEvent(
                    time=format_time(elapsed_time),
                    elapsed_seconds=elapsed_time,
                    event_type=EventType.TRUCK_ADDED,
                    truck_id=truck.truck_id,
                    message=f"{truck.call_sign} dispatched to incident",
                    timestamp=dispatch_time
                )
                events.append(event)
                self.timeline_events.append(event)

    def _update_fire_size(self, delta_seconds: float) -> None:
        """Update estimated fire size over time"""
        # Simple growth model: acres per hour
        growth_rate_acres_per_hour = self._settings.fire_growth_moderate_acres_per_hour
        hours_elapsed = delta_seconds / 3600
        self.estimated_acres += growth_rate_acres_per_hour * hours_elapsed

    def get_incident_summary(self) -> IncidentSummary:
        """Get current incident summary"""
        # Calculate fire perimeter (rough estimate)
        radius_ft = (self.estimated_acres * 43560 / 3.14159) ** 0.5
        perimeter_ft = int(2 * 3.14159 * radius_ft)

        return IncidentSummary(
            id=self.incident_id,
            name=self.incident_name,
            location=self.fire_location,
            start_time=self.start_time,
            elapsed_seconds=self.elapsed_seconds,
            elapsed_formatted=format_time(self.elapsed_seconds),
            phase=self.phase,
            estimated_acres=round(self.estimated_acres, 2),
            perimeter_ft=perimeter_ft
        )

    def get_resource_summary(self) -> ResourceSummary:
        """Get resource deployment summary"""
        trucks = self.truck_manager.get_all_trucks()

        # Count by type
        engines = sum(1 for t in trucks if t.unit_type == UnitType.ENGINE)
        tankers = sum(1 for t in trucks if t.unit_type == UnitType.TANKER)
        brush = sum(1 for t in trucks if t.unit_type == UnitType.BRUSH)
        rescue = sum(1 for t in trucks if t.unit_type == UnitType.RESCUE)
        command = sum(1 for t in trucks if t.unit_type == UnitType.COMMAND)

        # Count by status
        active_suppression = sum(
            1 for t in trucks
            if t.operational.activity_status == ActivityStatus.SUPPRESSION
        )
        en_route = sum(
            1 for t in trucks
            if t.operational.activity_status == ActivityStatus.EN_ROUTE
        )
        resupplying = sum(
            1 for t in trucks
            if t.operational.activity_status == ActivityStatus.RESUPPLY
        )

        # Water totals
        total_capacity = sum(t.water.tank_capacity_gal for t in trucks)
        total_available = sum(t.water.tank_level_gal for t in trucks)
        total_deployed = sum(t.water.total_water_deployed_gal for t in trucks)
        avg_pct = int((total_available / total_capacity * 100)
                      if total_capacity > 0 else 0)

        # Crew totals
        total_personnel = sum(t.crew.count for t in trucks)
        crews_needing_rotation = sum(
            1 for t in trucks
            if t.crew.fatigue_level in [CrewFatigueLevel.HIGH, CrewFatigueLevel.CRITICAL]
        )

        return ResourceSummary(
            total_trucks=len(trucks),
            engines=engines,
            tankers=tankers,
            brush=brush,
            rescue=rescue,
            command=command,
            active_suppression=active_suppression,
            en_route=en_route,
            resupplying=resupplying,
            total_personnel=total_personnel,
            total_water_capacity_gal=total_capacity,
            total_water_available_gal=total_available,
            total_water_deployed_gal=total_deployed,
            average_water_capacity_pct=avg_pct,
            crews_needing_rotation=crews_needing_rotation
        )

    def get_current_status(self) -> CurrentStatus:
        """Get current operational status"""
        trucks = self.truck_manager.get_all_trucks()

        active_suppression = sum(
            1 for t in trucks
            if t.operational.activity_status == ActivityStatus.SUPPRESSION
        )
        resupplying = sum(
            1 for t in trucks
            if t.operational.activity_status == ActivityStatus.RESUPPLY
        )
        en_route = sum(
            1 for t in trucks
            if t.operational.activity_status == ActivityStatus.EN_ROUTE
        )

        crew_fatigue_alerts = sum(
            1 for t in trucks
            if t.crew.fatigue_level in [CrewFatigueLevel.HIGH, CrewFatigueLevel.CRITICAL]
        )

        safety_concerns = [
            f"{t.call_sign} in danger zone"
            for t in trucks
            if t.safety.in_danger_zone
        ]

        return CurrentStatus(
            active_suppression_trucks=active_suppression,
            resupplying=resupplying,
            en_route=en_route,
            crew_fatigue_alerts=crew_fatigue_alerts,
            safety_concerns=safety_concerns
        )

    def get_recommendations(self) -> list[str]:
        """Generate AI-style recommendations"""
        recommendations = []
        trucks = self.truck_manager.get_all_trucks()

        # Check for crew rotation needs
        for truck in trucks:
            if truck.crew.hours_on_duty >= 1.8 and truck.crew.hours_on_duty < 2.0:
                recommendations.append(
                    f"Consider rotating {truck.call_sign} crew (approaching 2-hour mark)"
                )
            elif truck.crew.fatigue_level == CrewFatigueLevel.CRITICAL:
                recommendations.append(
                    f"⚠️ URGENT: {truck.call_sign} crew at critical fatigue level - immediate rotation required"
                )

        # Check for low water: only units pumping in suppression draw their tanks down
        for truck in trucks:
            if 20 < truck.water.tank_level_pct < 40 and truck.operational.activity_status == ActivityStatus.SUPPRESSION:
                recommendations.append(
                    f"{truck.call_sign} at {truck.water.tank_level_pct}% capacity, schedule resupply in next 15 minutes"
                )

        # Check for safety concerns
        danger_trucks = [t for t in trucks if t.safety.in_danger_zone]
        if danger_trucks:
            for truck in danger_trucks:
                recommendations.append(
                    f"⚠️ {truck.call_sign} in danger zone - monitor closely"
                )

        return recommendations

    def get_simulation_summary(self) -> SimulationSummary:
        """Get complete simulation summary for AgentCore"""
        return SimulationSummary(
            incident=self.get_incident_summary(),
            resource_summary=self.get_resource_summary(),
            timeline=self.timeline_events[-20:],  # Last 20 events
            current_status=self.get_current_status(),
            recommendations=self.get_recommendations()
        )
