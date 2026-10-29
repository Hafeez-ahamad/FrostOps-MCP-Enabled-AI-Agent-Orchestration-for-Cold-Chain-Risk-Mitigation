"""Counterfactual exposure: what a truck's cargo temperature would have done if cooling had been
restored at a given time (simulated outcome model, eval/EVAL.md).

The simulator is open-loop: actions cannot change the readings it already generated. So the
outcome of an action is recomputed afterwards with the same physics (sim/physics.py): up to
the restore time the true temperature is used as generated; after it, the temperature is
re-stepped with the reefer running and the door closed. Exposure is degree-minutes above the
cargo band's maximum, counted from `start` to the truck's last tick.
"""

from datetime import datetime

from sim.generator import SimulationOutput
from sim.physics import ambient_c, step_temperature


def exposure_degree_minutes(
    sim: SimulationOutput,
    shipment_id: str,
    start: datetime,
    cooling_restored_at: datetime | None = None,
) -> float:
    plan = sim.plans[shipment_id]
    band = sim.cargo_types[plan.cargo_type]
    series = sim.true_temps.get(shipment_id, [])
    exposure = 0.0
    previous: float | None = None  # temperature at the previous tick (true, then counterfactual)
    prev_t: datetime | None = None
    for t, true_temp in series:
        restored = cooling_restored_at is not None and t > cooling_restored_at
        if restored and prev_t is not None and previous is not None:
            minutes = (t - prev_t).total_seconds() / 60
            ambient = ambient_c(t, plan.depot.inland, sim.day_offsets.get(t.date(), 0.0))
            current = step_temperature(previous, ambient, band.setpoint_c, True, 0.0, minutes)
        else:
            current = true_temp
        if prev_t is not None and t > start and current > band.max_c:
            exposure += (current - band.max_c) * (t - prev_t).total_seconds() / 60
        previous, prev_t = current, t
    return round(exposure, 2)
