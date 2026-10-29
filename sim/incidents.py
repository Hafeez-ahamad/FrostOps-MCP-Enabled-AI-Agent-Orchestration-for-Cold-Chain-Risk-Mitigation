"""Turn the scenario's incident schedule into per-shipment effects and ground-truth labels.

`at_hour` opens a window (default 2 h). Each shipment picked for an incident gets its own
start: the first tick in the window at which it has been driving for MIN_ELAPSED and still
has MIN_REMAINING of its planned trip left, so the incident can play out before arrival.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np

from sim.labels import IncidentLabel, WorldEvent
from sim.scenario import IncidentSpec, Scenario
from sim.schedule import MAX_DELAY_MIN, PlannedShipment
from sim.world import World

MIN_ELAPSED = timedelta(minutes=15)  # the shipment has some readings before the incident
MIN_REMAINING = timedelta(minutes=120)  # enough trip left for the incident to play out
WINDOW = timedelta(hours=2)
DRIVER_UNREACHABLE_TAIL = timedelta(minutes=180)

Candidate = tuple[PlannedShipment, datetime]  # shipment and its incident start


class ScenarioError(ValueError):
    """The scenario asks for something this fleet cannot provide."""


@dataclass
class ShipmentEffects:
    reefer_off_at: datetime | None = None
    door_open: tuple[datetime, float] | None = None  # (start, minutes)
    delay_ramps: list[tuple[datetime, float, float]] = field(default_factory=list)
    risk_bumps: list[tuple[datetime, float]] = field(default_factory=list)


@dataclass
class IncidentPlan:
    labels: list[IncidentLabel] = field(default_factory=list)
    eta_delay_min: dict[str, float] = field(default_factory=dict)  # incident id -> ETA delay
    effects: dict[str, ShipmentEffects] = field(default_factory=dict)
    port_spikes: list[tuple[str, datetime, datetime, float]] = field(default_factory=list)
    facility_full: list[tuple[str, datetime, datetime]] = field(default_factory=list)
    events: list[WorldEvent] = field(default_factory=list)
    # (shipment, port, start, end): may be exposed to a port spike; labelled after the run
    port_exposure: list[tuple[str, str, datetime, datetime]] = field(default_factory=list)

    @property
    def reserved(self) -> set[str]:
        return set(self.effects) | {label.shipment_id for label in self.labels}

    def effects_for(self, shipment_id: str) -> ShipmentEffects:
        return self.effects.setdefault(shipment_id, ShipmentEffects())

    def add_label(self, spec: IncidentSpec, shipment_id: str, start: datetime) -> str:
        incident_id = f"INC-{len(self.labels) + 1:03d}"
        self.labels.append(
            IncidentLabel(
                incident_id=incident_id, type=spec.type, shipment_id=shipment_id, start_ts=start
            )
        )
        return incident_id


def _ceil_to_tick(t: datetime, origin: datetime, tick_min: int) -> datetime:
    ticks = -(-(t - origin) // timedelta(minutes=tick_min))
    return origin + ticks * timedelta(minutes=tick_min)


def _candidates(
    plans: list[PlannedShipment], t0: datetime, reserved: set[str], origin: datetime, tick: int
) -> list[Candidate]:
    pool = []
    for p in plans:
        if p.shipment_id in reserved:
            continue
        start = _ceil_to_tick(max(t0, p.depart + MIN_ELAPSED), origin, tick)
        if start <= min(t0 + WINDOW, p.planned_arrival - MIN_REMAINING):
            pool.append((p, start))
    return pool


def _pick(
    rng: np.random.Generator, pool: list[Candidate], count: int, what: str
) -> list[Candidate]:
    if len(pool) < count:
        raise ScenarioError(f"{what}: needs {count} shipment(s), only {len(pool)} eligible")
    picks = rng.choice(len(pool), size=count, replace=False)
    return [pool[int(i)] for i in sorted(picks)]


def _reefer_failure(
    plan: IncidentPlan, spec: IncidentSpec, p: PlannedShipment, start: datetime, eta: float
) -> str:
    incident_id = plan.add_label(spec, p.shipment_id, start)
    effects = plan.effects_for(p.shipment_id)
    effects.reefer_off_at = start
    plan.eta_delay_min[incident_id] = eta
    if eta > 0:
        effects.delay_ramps.append((start, 30.0, eta))
    return incident_id


def plan_incidents(
    scenario: Scenario, world: World, plans: list[PlannedShipment], rng: np.random.Generator
) -> IncidentPlan:
    plan = IncidentPlan()
    tick = scenario.reading_interval_min
    origin = scenario.start
    for spec in scenario.incidents:
        t0 = _ceil_to_tick(origin + timedelta(hours=spec.at_hour), origin, tick)

        if spec.type == "reefer_failure":
            for p, start in _pick(
                rng, _candidates(plans, t0, plan.reserved, origin, tick), spec.count, spec.type
            ):
                _reefer_failure(plan, spec, p, start, spec.eta_delay_min)

        elif spec.type == "driver_unreachable":
            for p, start in _pick(
                rng, _candidates(plans, t0, plan.reserved, origin, tick), spec.count, spec.type
            ):
                _reefer_failure(plan, spec, p, start, 0.0)
                plan.events.append(
                    WorldEvent(
                        kind="driver_unreachable",
                        subject_id=p.shipment_id,
                        start_ts=start,
                        end_ts=p.planned_arrival + DRIVER_UNREACHABLE_TAIL,
                    )
                )

        elif spec.type == "door_open":
            for p, start in _pick(
                rng, _candidates(plans, t0, plan.reserved, origin, tick), spec.count, spec.type
            ):
                plan.add_label(spec, p.shipment_id, start)
                plan.effects_for(p.shipment_id).door_open = (start, float(rng.uniform(6.0, 12.0)))

        elif spec.type == "route_delay_cascade":
            # Several shipments from the same depot are delayed together.
            candidates = _candidates(plans, t0, plan.reserved, origin, tick)
            by_depot: dict[str, list[Candidate]] = {}
            for c in candidates:
                by_depot.setdefault(c[0].depot.id, []).append(c)
            depots = sorted(d for d, cs in by_depot.items() if len(cs) >= spec.count)
            if not depots:
                raise ScenarioError(
                    f"route_delay_cascade: no depot has {spec.count} eligible shipments"
                )
            depot_id = depots[int(rng.integers(len(depots)))]
            for p, start in _pick(rng, by_depot[depot_id], spec.count, spec.type):
                plan.add_label(spec, p.shipment_id, start)
                effects = plan.effects_for(p.shipment_id)
                effects.delay_ramps.append((start, 60.0, float(rng.uniform(100.0, 150.0))))
                effects.risk_bumps.append((start, 3.0))

        elif spec.type == "port_congestion_spike":
            # Shipments on the road to the port while it is congested are affected.
            assert spec.port is not None
            t1 = t0 + timedelta(hours=spec.duration_hours)
            plan.port_spikes.append((spec.port, t0, t1, float(rng.uniform(7.6, 9.0))))
            plan.events.append(
                WorldEvent(kind="port_congestion", subject_id=spec.port, start_ts=t0, end_ts=t1)
            )
            # Reserve every shipment that could be on the road while the port is congested,
            # including ones a delay could push into the window. Which of them really were
            # is only known after the run (sim.generator labels them).
            reserved = plan.reserved
            max_delay = timedelta(minutes=MAX_DELAY_MIN)
            affected = [
                p
                for p in plans
                if p.port_id == spec.port
                and p.shipment_id not in reserved
                and p.depart < t1
                and p.planned_arrival + max_delay > t0
            ]
            if not affected:
                raise ScenarioError(f"port_congestion_spike: no shipment bound for {spec.port}")
            for p in affected:
                plan.effects_for(p.shipment_id)
                plan.port_exposure.append((p.shipment_id, spec.port, t0, t1))

        else:  # facility_full: the nearest site fills up while a nearby reefer fails
            assert spec.facility is not None
            t1 = t0 + timedelta(hours=spec.duration_hours)
            plan.facility_full.append((spec.facility, t0, t1))
            plan.events.append(
                WorldEvent(kind="facility_full", subject_id=spec.facility, start_ts=t0, end_ts=t1)
            )
            near = [
                (p, start)
                for p, start in _candidates(plans, t0, plan.reserved, origin, tick)
                if start < t1
                and world.cold_storage_by_distance(*p.planned_position(start))[0].id
                == spec.facility
            ]
            for p, start in _pick(rng, near, spec.count, f"facility_full near {spec.facility}"):
                _reefer_failure(plan, spec, p, start, 90.0)
    return plan
