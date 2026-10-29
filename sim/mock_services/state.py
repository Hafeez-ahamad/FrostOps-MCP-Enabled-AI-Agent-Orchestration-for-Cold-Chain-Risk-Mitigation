"""Shared state behind the mock services, driven by the simulator's virtual clock."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sim.labels import WorldEvent
from sim.world import World, load_world


@dataclass
class MockWorld:
    world: World = field(default_factory=load_world)
    now: datetime = datetime(2026, 1, 1)
    events: list[WorldEvent] = field(default_factory=list)
    port_congestion: dict[str, float] = field(default_factory=dict)
    ambient_c: float = 20.0
    wind_kmh: float = 5.0
    # facility id -> shipment ids holding a slot
    reservations: dict[str, set[str]] = field(default_factory=dict)
    # idempotency key -> response already given (so retries never repeat a side effect)
    responses: dict[str, dict[str, Any]] = field(default_factory=dict)
    # what the services did, for tests and the demo
    log: list[dict[str, Any]] = field(default_factory=list)

    def _active(self, kind: str, subject_id: str) -> bool:
        return any(
            e.kind == kind and e.subject_id == subject_id and e.start_ts <= self.now < e.end_ts
            for e in self.events
        )

    def driver_reachable(self, shipment_id: str) -> bool:
        return not self._active("driver_unreachable", shipment_id)

    def facility_occupancy(self, facility_id: str) -> tuple[int, int]:
        """(capacity, occupied). A seeded facility_full event fills every slot."""
        capacity = self.world.facility(facility_id).capacity
        if self._active("facility_full", facility_id):
            return capacity, capacity
        return capacity, len(self.reservations.get(facility_id, set()))
