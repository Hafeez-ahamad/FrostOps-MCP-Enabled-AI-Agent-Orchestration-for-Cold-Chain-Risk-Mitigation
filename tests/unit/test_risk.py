"""Risk score: bounds, monotonicity in every factor, and documented anchor values."""

from dataclasses import replace

from hypothesis import given
from hypothesis import strategies as st

from engine.risk.score import RiskInputs, risk_score

BASE = RiskInputs(
    temp_excess_c=1.0,
    port_congestion=None,
    delay_probability=None,
    eta_rule_active=False,
    active_minutes=30.0,
    eta_delay_min=0.0,
    sensitivity=0.7,
)

inputs = st.builds(
    RiskInputs,
    temp_excess_c=st.one_of(st.none(), st.floats(0.01, 40)),
    port_congestion=st.one_of(st.none(), st.floats(7.01, 10)),
    delay_probability=st.one_of(st.none(), st.floats(0.66, 1)),
    eta_rule_active=st.booleans(),
    active_minutes=st.floats(0, 600),
    eta_delay_min=st.floats(0, 300),
    sensitivity=st.floats(0, 1),
)


@given(inputs)
def test_score_is_between_0_and_100(i: RiskInputs) -> None:
    assert 0.0 <= risk_score(i).score <= 100.0


@given(inputs, st.floats(0, 20))
def test_more_temperature_excess_never_lowers_risk(i: RiskInputs, extra: float) -> None:
    if i.temp_excess_c is None:
        return
    more = replace(i, temp_excess_c=i.temp_excess_c + extra)
    assert risk_score(more).score >= risk_score(i).score


@given(inputs, st.floats(0, 300))
def test_longer_duration_never_lowers_risk(i: RiskInputs, extra: float) -> None:
    assert (
        risk_score(replace(i, active_minutes=i.active_minutes + extra)).score >= risk_score(i).score
    )


@given(inputs, st.floats(0, 300))
def test_longer_delay_never_lowers_risk(i: RiskInputs, extra: float) -> None:
    assert (
        risk_score(replace(i, eta_delay_min=i.eta_delay_min + extra)).score >= risk_score(i).score
    )


@given(inputs)
def test_eta_rule_never_lowers_risk(i: RiskInputs) -> None:
    assert risk_score(replace(i, eta_rule_active=True)).score >= risk_score(i).score


def test_more_sensitive_cargo_scores_higher() -> None:
    assert risk_score(replace(BASE, sensitivity=1.0)).score > risk_score(BASE).score


def test_documented_anchor_values() -> None:
    # 1 C over for 30 min, no delay, FRESH: 100 x 0.2 x 0.5 x 0.5 x 0.7 = 3.5
    assert risk_score(BASE).score == 3.5
    # 5 C over for an hour with a 2 h delay, PHARMA: 100 x 1 x 1 x 1 x 1 = 100
    worst = replace(BASE, temp_excess_c=5.0, active_minutes=60, eta_delay_min=120, sensitivity=1.0)
    assert risk_score(worst).score == 100.0


def test_just_opened_incident_still_scores() -> None:
    assert risk_score(replace(BASE, active_minutes=0)).score > 0
