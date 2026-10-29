"""Sensor and transport noise, planned up front from the seed and applied as readings are emitted.

Each glitch hits one shipment that has no incident, inside the first 60% of its planned trip,
after a few clean readings, so every glitch has a clean ground-truth label.

| Type         | Effect on the emitted stream                              |
|--------------|-----------------------------------------------------------|
| spike        | one reading +10..20 C                                     |
| flatline     | 8-12 readings stuck at the first value                    |
| dropout      | 4-12 readings never sent (20-60 min at 5-min intervals)   |
| duplicate    | one reading sent twice                                    |
| out_of_order | one reading received 15-30 min late                       |
| unit_mixup   | 6-12 readings reported in Fahrenheit                      |
| gps_jump     | one reading 0.5-1.0 degrees off in latitude and longitude |
| clock_skew   | 6-12 readings with the device clock 20-60 min ahead       |
"""

from dataclasses import dataclass
from datetime import timedelta

import numpy as np

from sim.incidents import ScenarioError
from sim.scenario import NoiseType, Scenario
from sim.schedule import PlannedShipment

RUN_LENGTHS: dict[str, tuple[int, int]] = {
    "flatline": (8, 12),
    "dropout": (4, 12),
    "unit_mixup": (6, 12),
    "clock_skew": (6, 12),
}
FIRST_CLEAN_READINGS = 5
USABLE_TRIP_SHARE = 0.6


@dataclass(frozen=True)
class GlitchPlan:
    glitch_id: str
    type: NoiseType
    shipment_id: str
    first_seq: int
    last_seq: int
    magnitude: float = 0.0  # spike C, delay/skew minutes, or latitude offset
    lon_offset: float = 0.0  # gps_jump only

    def covers(self, seq: int) -> bool:
        return self.first_seq <= seq <= self.last_seq


def _last_start(p: PlannedShipment, length: int, interval_min: int) -> int:
    """Latest first sequence number that keeps the glitch inside the usable part of the trip."""
    ticks = int(p.planned_min // interval_min)
    return int(ticks * USABLE_TRIP_SHARE) - length


def plan_noise(
    scenario: Scenario,
    plans: list[PlannedShipment],
    reserved: set[str],
    rng: np.random.Generator,
) -> list[GlitchPlan]:
    end = scenario.start + timedelta(hours=scenario.duration_hours)
    used = set(reserved)
    glitches: list[GlitchPlan] = []
    for spec in scenario.noise:
        low, high = RUN_LENGTHS.get(spec.type, (1, 1))
        for _ in range(spec.count):
            length = int(rng.integers(low, high + 1))
            pool = [
                p
                for p in plans
                if p.shipment_id not in used
                and p.planned_arrival <= end
                and _last_start(p, length, scenario.reading_interval_min) >= FIRST_CLEAN_READINGS
            ]
            if not pool:
                raise ScenarioError(f"noise {spec.type}: no eligible shipment left")
            target = pool[int(rng.integers(len(pool)))]
            latest = _last_start(target, length, scenario.reading_interval_min)
            first = int(rng.integers(FIRST_CLEAN_READINGS, latest + 1))
            magnitude, lon_offset = 0.0, 0.0
            if spec.type == "spike":
                magnitude = float(rng.uniform(10.0, 20.0))
            elif spec.type == "out_of_order":
                magnitude = float(rng.uniform(15.0, 30.0))
            elif spec.type == "clock_skew":
                magnitude = float(rng.uniform(20.0, 60.0))
            elif spec.type == "gps_jump":
                signs = rng.choice([-1.0, 1.0], size=2)
                magnitude = float(signs[0] * rng.uniform(0.5, 1.0))
                lon_offset = float(signs[1] * rng.uniform(0.5, 1.0))
            used.add(target.shipment_id)
            glitches.append(
                GlitchPlan(
                    glitch_id=f"GL-{len(glitches) + 1:04d}",
                    type=spec.type,
                    shipment_id=target.shipment_id,
                    first_seq=first,
                    last_seq=first + length - 1,
                    magnitude=magnitude,
                    lon_offset=lon_offset,
                )
            )
    return glitches
