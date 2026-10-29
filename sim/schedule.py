"""Trip planning: which vehicle carries which shipment, where and when.

The whole schedule is fixed up front from the seed. Each vehicle repeats a cycle of
outbound trip (loaded, emits readings), return trip (empty, no readings), dwell, and a slack
buffer that absorbs the largest delay the simulator can produce, so trips never overlap.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np

from engine.geo import haversine_km
from sim.scenario import Scenario
from sim.world import Depot, Destination, World

ROAD_FACTOR = 1.3  # road distance / straight-line distance
AVG_SPEED_KMH = 60.0
MIN_TRIP_KM = 25.0
MAX_TRIP_KM = 250.0
DWELL_MIN = 60.0
MAX_DELAY_MIN = 180.0  # the simulator never delays a trip by more than this
SLACK_MIN = MAX_DELAY_MIN  # so a delayed trip never overlaps the vehicle's next one


@dataclass(frozen=True)
class PlannedShipment:
    shipment_id: str
    vehicle_id: str
    depot: Depot
    cargo_type: str
    destination: Destination
    depart: datetime
    planned_min: float
    route_risk: float
    base_delay_min: float

    @property
    def planned_arrival(self) -> datetime:
        return self.depart + timedelta(minutes=self.planned_min)

    @property
    def port_id(self) -> str | None:
        return self.destination.port_id

    def position_at_progress(self, progress: float) -> tuple[float, float]:
        p = min(max(progress, 0.0), 1.0)
        lat = self.depot.lat + p * (self.destination.lat - self.depot.lat)
        lon = self.depot.lon + p * (self.destination.lon - self.depot.lon)
        return lat, lon

    def planned_position(self, at: datetime) -> tuple[float, float]:
        return self.position_at_progress((at - self.depart) / timedelta(minutes=self.planned_min))


def _round_to_tick(minutes: float, interval: int) -> float:
    return round(minutes / interval) * interval


def _trip_minutes(depot: Depot, dest: Destination) -> float:
    km = haversine_km(depot.lat, depot.lon, dest.lat, dest.lon) * ROAD_FACTOR
    return km / AVG_SPEED_KMH * 60


def plan_shipments(
    scenario: Scenario, world: World, rng: np.random.Generator
) -> list[PlannedShipment]:
    end = scenario.start + timedelta(hours=scenario.duration_hours)
    cargo_codes = list(world.cargo_types)
    cargo_shares = [world.cargo_types[c].share for c in cargo_codes]
    raw: list[tuple[datetime, str, Depot, str, Destination, float, float, float]] = []

    for v in range(scenario.vehicles):
        vehicle_id = f"VEH-{v + 1:03d}"
        depot = world.depots[int(rng.integers(len(world.depots)))]
        cargo = cargo_codes[int(rng.choice(len(cargo_codes), p=cargo_shares))]
        in_range = [
            d
            for d in world.destinations
            if MIN_TRIP_KM
            <= haversine_km(depot.lat, depot.lon, d.lat, d.lon) * ROAD_FACTOR
            <= MAX_TRIP_KM
        ]
        ports = [d for d in in_range if d.port_id]
        others = [d for d in in_range if not d.port_id]
        depart: datetime | None = None
        while depart is None or depart < end:
            pool = ports if ports and rng.random() < scenario.port_bound_share else others
            dest = pool[int(rng.integers(len(pool)))]
            planned = _trip_minutes(depot, dest)
            route_risk = float(rng.uniform(1.0, 6.5))
            base_delay = float(rng.uniform(0.0, 15.0))
            cycle = _round_to_tick(
                2 * planned + DWELL_MIN + SLACK_MIN, scenario.reading_interval_min
            )
            if depart is None:
                # Start each vehicle at a random point of its first cycle, so the fleet is in
                # steady state from the first minute instead of all leaving at once.
                offset = _round_to_tick(float(rng.uniform(0, cycle)), scenario.reading_interval_min)
                depart = scenario.start + timedelta(minutes=offset)
                if depart >= end:
                    break
            raw.append((depart, vehicle_id, depot, cargo, dest, planned, route_risk, base_delay))
            depart += timedelta(minutes=cycle)

    raw.sort(key=lambda r: (r[0], r[1]))
    return [
        PlannedShipment(
            shipment_id=f"S{scenario.seed:04d}-{n + 1:05d}",
            vehicle_id=vehicle_id,
            depot=depot,
            cargo_type=cargo,
            destination=dest,
            depart=depart,
            planned_min=planned,
            route_risk=route_risk,
            base_delay_min=base_delay,
        )
        for n, (
            depart,
            vehicle_id,
            depot,
            cargo,
            dest,
            planned,
            route_risk,
            base_delay,
        ) in enumerate(raw)
    ]
