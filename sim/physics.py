"""Simple, documented cargo-temperature physics (Newton's law of cooling).

    reefer on : dT/dt = (K_INSULATION + K_COOLING) * (S - T) + door * K_DOOR * (A - T)
    reefer off: dT/dt =  K_INSULATION * (A - T)              + door * K_DOOR * (A - T)

T cargo temperature, S setpoint, A ambient, door = 1 while the door is open.
With the reefer on, its controller cancels the steady insulation leak, so cargo settles at
the setpoint. With it off, cargo drifts toward ambient (about 2-3 C per hour on a 20-25 C
day). An open door adds a much faster exchange with outside air.
The rates are illustrative assumptions, not calibrated to any real trailer.
"""

import math
from datetime import datetime

K_INSULATION = 0.002  # per minute
K_DOOR = 0.02  # per minute
K_COOLING = 0.06  # per minute

AMBIENT_MEAN_C = 20.0
AMBIENT_AMPLITUDE_C = 7.0  # half the day-night swing
AMBIENT_PEAK_HOUR = 15.0
INLAND_OFFSET_C = 4.0


def ambient_c(at: datetime, inland: bool, day_offset_c: float) -> float:
    """Daily sine wave peaking mid-afternoon; inland depots run hotter."""
    hour = at.hour + at.minute / 60
    wave = math.cos(2 * math.pi * (hour - AMBIENT_PEAK_HOUR) / 24)
    inland_c = INLAND_OFFSET_C if inland else 0.0
    return AMBIENT_MEAN_C + AMBIENT_AMPLITUDE_C * wave + inland_c + day_offset_c


def step_temperature(
    temp_c: float,
    ambient: float,
    setpoint_c: float,
    reefer_on: bool,
    door_open_min: float,
    minutes: float,
) -> float:
    """Advance the cargo temperature by `minutes` using one-minute Euler steps.

    `door_open_min` is how many of those minutes (from the start) the door is open.
    """
    steps = max(1, round(minutes))
    dt = minutes / steps
    for i in range(steps):
        door = 1.0 if i * dt < door_open_min else 0.0
        if reefer_on:
            rate = (K_INSULATION + K_COOLING) * (setpoint_c - temp_c)
        else:
            rate = K_INSULATION * (ambient - temp_c)
        rate += door * K_DOOR * (ambient - temp_c)
        temp_c += rate * dt
    return temp_c
