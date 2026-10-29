from datetime import datetime

from hypothesis import given
from hypothesis import strategies as st

from sim.physics import ambient_c, step_temperature

temps = st.floats(min_value=-30.0, max_value=15.0)
ambients = st.floats(min_value=0.0, max_value=45.0)
minutes = st.floats(min_value=1.0, max_value=120.0)


@given(temp=temps, ambient=ambients, mins=minutes)
def test_reefer_off_moves_toward_ambient_without_overshoot(
    temp: float, ambient: float, mins: float
) -> None:
    after = step_temperature(
        temp, ambient, setpoint_c=2.0, reefer_on=False, door_open_min=0, minutes=mins
    )
    low, high = sorted((temp, ambient))
    assert low - 1e-9 <= after <= high + 1e-9
    assert abs(ambient - after) <= abs(ambient - temp) + 1e-9


@given(ambient=ambients)
def test_reefer_on_holds_the_setpoint(ambient: float) -> None:
    after = step_temperature(
        2.0, ambient, setpoint_c=2.0, reefer_on=True, door_open_min=0, minutes=60
    )
    assert abs(after - 2.0) < 1e-9


def test_reefer_on_recovers_toward_setpoint() -> None:
    after = step_temperature(8.0, 25.0, setpoint_c=2.0, reefer_on=True, door_open_min=0, minutes=60)
    assert 2.0 < after < 3.0


def test_reefer_failure_breaches_fresh_band_within_an_hour_on_a_warm_day() -> None:
    after = step_temperature(
        2.0, 25.0, setpoint_c=2.0, reefer_on=False, door_open_min=0, minutes=60
    )
    assert after > 4.0


def test_open_door_warms_faster_than_insulation_alone() -> None:
    closed = step_temperature(2.0, 30.0, 2.0, reefer_on=True, door_open_min=0, minutes=10)
    opened = step_temperature(2.0, 30.0, 2.0, reefer_on=True, door_open_min=10, minutes=10)
    assert opened > closed + 1.0


def test_ambient_peaks_mid_afternoon_and_inland_is_hotter() -> None:
    afternoon = ambient_c(datetime(2026, 3, 2, 15), inland=False, day_offset_c=0)
    dawn = ambient_c(datetime(2026, 3, 2, 3), inland=False, day_offset_c=0)
    inland = ambient_c(datetime(2026, 3, 2, 15), inland=True, day_offset_c=0)
    assert afternoon > dawn
    assert inland > afternoon
