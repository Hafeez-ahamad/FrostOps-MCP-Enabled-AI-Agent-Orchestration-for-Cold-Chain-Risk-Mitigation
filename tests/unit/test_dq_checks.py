"""Each data-quality check at and around its threshold (policy/data_quality.yaml)."""

from datetime import datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from engine.dq.checks import DataQualityChecker, DqStatus, Reading, load_dq_config

T0 = datetime(2026, 3, 2, 12, 0)
CFG = load_dq_config()


def reading(seq: int, temp: float = 2.0, minutes: float | None = None, **kw: object) -> Reading:
    at = T0 + timedelta(minutes=5 * seq if minutes is None else minutes)
    fields: dict[str, object] = {
        "shipment_id": "S1",
        "seq": seq,
        "ts": at,
        "received": at + timedelta(seconds=30),
        "lat": 33.80,
        "lon": -118.19,
        "temp_c": temp,
        "trip_status": "E",
    }
    fields.update(kw)
    return Reading(**fields)  # type: ignore[arg-type]


def checks(a: object) -> set[str]:
    return {f.check for f in a.flags}  # type: ignore[attr-defined]


def primed(temp: float = 2.0) -> DataQualityChecker:
    """A checker that has already seen one valid reading (the reference)."""
    checker = DataQualityChecker(CFG)
    assert checker.assess(reading(1, temp)).status is DqStatus.VALID
    return checker


def test_first_clean_reading_is_valid() -> None:
    assert DataQualityChecker(CFG).assess(reading(1)).status is DqStatus.VALID


def test_duplicate_sequence_is_invalid() -> None:
    checker = primed()
    a = checker.assess(reading(1))
    assert a.status is DqStatus.INVALID and checks(a) == {"duplicate"}


def test_missing_temperature_is_invalid() -> None:
    a = DataQualityChecker(CFG).assess(reading(1, temp_c=None))
    assert a.status is DqStatus.INVALID and checks(a) == {"missing_value"}


@pytest.mark.parametrize(
    ("temp", "ok"), [(-40.0, True), (-40.01, False), (60.0, True), (60.01, False)]
)
def test_plausible_temperature_range_boundary(temp: float, ok: bool) -> None:
    a = DataQualityChecker(CFG).assess(reading(1, temp))
    assert ("out_of_range" not in checks(a)) is ok


def test_position_outside_region_is_invalid() -> None:
    a = DataQualityChecker(CFG).assess(reading(1, lat=40.4, lon=-77.0))
    assert a.status is DqStatus.INVALID and "out_of_region" in checks(a)


@pytest.mark.parametrize(("ahead_s", "flagged"), [(120, False), (121, True)])
def test_clock_skew_boundary(ahead_s: int, flagged: bool) -> None:
    r = reading(2)
    assert r.ts is not None
    skewed = reading(2, received=r.ts - timedelta(seconds=ahead_s))
    assert ("clock_skew" in checks(primed().assess(skewed))) is flagged


def test_late_reading_is_out_of_order() -> None:
    checker = primed()
    checker.assess(reading(3))
    a = checker.assess(reading(2))
    assert a.status is DqStatus.SUSPECT and checks(a) == {"out_of_order"}


def test_spike_boundary_uses_rate_times_elapsed_plus_margin() -> None:
    # 5 minutes after the reference: allowed = 1.5 * 5 + 1.0 = 8.5 C
    allowed = CFG.max_temp_rate_c_per_min * 5 + CFG.spike_margin_c
    assert primed().assess(reading(2, 2.0 + allowed)).status is DqStatus.VALID
    a = primed().assess(reading(2, 2.0 + allowed + 0.01))
    assert a.status is DqStatus.SUSPECT and checks(a) == {"spike"}


def test_spike_does_not_move_the_reference() -> None:
    checker = primed()
    checker.assess(reading(2, 20.0))
    assert checker.assess(reading(3, 2.1)).status is DqStatus.VALID


@given(st.floats(min_value=-5.0, max_value=10.0))
def test_fahrenheit_copy_of_the_reference_is_a_unit_mixup(celsius: float) -> None:
    fahrenheit = celsius * 1.8 + 32.0
    checker = primed(celsius)
    a = checker.assess(reading(2, fahrenheit))
    assert a.status is DqStatus.INVALID and checks(a) == {"unit_mismatch"}


def test_real_warming_is_not_a_unit_mixup() -> None:
    checker = primed(2.0)
    assert checker.assess(reading(2, 4.5)).status is DqStatus.VALID


def test_gps_speed_boundary() -> None:
    # 5 minutes at 130 km/h = 10.83 km. ~0.1 degree latitude = 11.1 km -> too fast.
    near = primed().assess(reading(2, lat=33.80 + 0.09))
    far = primed().assess(reading(2, lat=33.80 + 0.11))
    assert near.status is DqStatus.VALID
    assert far.status is DqStatus.SUSPECT and checks(far) == {"gps_jump"}


def test_flatline_flags_from_the_configured_run_length() -> None:
    checker = DataQualityChecker(CFG)
    statuses = [checker.assess(reading(i, 2.37)).status for i in range(1, CFG.flatline_run + 2)]
    assert statuses[: CFG.flatline_run - 1] == [DqStatus.VALID] * (CFG.flatline_run - 1)
    assert statuses[CFG.flatline_run - 1 :] == [DqStatus.SUSPECT] * 2


def test_freshness_flags_once_per_gap_and_only_en_route() -> None:
    checker = primed()
    first = reading(1)
    assert first.received is not None
    limit = timedelta(minutes=CFG.freshness_minutes)
    assert checker.check_freshness(first.received + limit) == []
    stale = checker.check_freshness(first.received + limit + timedelta(seconds=1))
    assert [f.check for f in stale] == ["freshness"] and stale[0].seq is None
    assert checker.check_freshness(first.received + 2 * limit) == []  # not repeated
    checker.assess(reading(2, trip_status="A"))  # arrived: no more readings expected
    assert checker.check_freshness(first.received + 10 * limit) == []
