"""Deterministic planner: candidates follow the SOP, costing picks the nearest site with room."""

from datetime import datetime

from engine.incidents.manager import Incident
from engine.integrations.clients import FacilityStatus
from engine.planner.planner import DeterministicPlanner, travel_minutes
from engine.risk.score import RiskScore
from engine.rules.engine import RuleInput

PLANNER = DeterministicPlanner()
T0 = datetime(2026, 3, 2, 12, 0)
# A truck between Long Beach and Commerce.
LAT, LON = 33.9, -118.2


def incident(*rules: str) -> Incident:
    return Incident(
        incident_id="INC-S1-01",
        shipment_id="S1",
        depot_id="DEP-LB",
        cargo_type="FRESH",
        status="open",
        opened_at=T0,
        last_hit_at=T0,
        rule_ids=rules,
        hit_count=1,
        first_seq=1,
        last_seq=1,
        risk=RiskScore(10, 0.5, 0.5, 0.5, 0.7),
        latest_evidence=(),
    )


def reading(eta: float = 0.0) -> RuleInput:
    return RuleInput(
        shipment_id="S1",
        seq=1,
        reading_ts=T0,
        depot_id="DEP-LB",
        cargo_type="FRESH",
        temperature_c=6.0,
        port_congestion=None,
        risk_classification="Low Risk",
        delay_probability=0.1,
        eta_delay_min=eta,
        latitude=LAT,
        longitude=LON,
    )


def site(
    fid: str, lat: float, lon: float, available: int = 5, kind: str = "cold_storage"
) -> FacilityStatus:
    return FacilityStatus(
        facility_id=fid, name=fid, kind=kind, lat=lat, lon=lon, capacity=10, available=available
    )


SITES = [
    site("CS-COMMERCE", 34.00, -118.15),
    site("CS-RIVERSIDE", 33.95, -117.40),
    site("IE-OVERFLOW", 34.11, -117.29, kind="overflow_depot"),
]


def test_temperature_breach_without_delay_notifies_the_driver() -> None:
    plan = PLANNER.plan(incident("R-TEMP-01"), reading(), SITES)
    assert plan.primary is not None and plan.primary.action == "notify_driver_restart_cooling"
    assert [c.action for c in plan.companions] == ["create_incident_ticket"]


def test_breach_with_long_delay_diverts_to_the_nearest_site_with_room() -> None:
    plan = PLANNER.plan(incident("R-TEMP-01", "R-ETA-01"), reading(90), SITES)
    assert plan.primary is not None
    assert (plan.primary.action, plan.primary.target) == ("divert_to_cold_storage", "CS-COMMERCE")


def test_full_site_is_skipped() -> None:
    sites = [site("CS-COMMERCE", 34.00, -118.15, available=0), *SITES[1:]]
    plan = PLANNER.plan(incident("R-TEMP-01", "R-ETA-01"), reading(90), sites)
    assert plan.primary is not None and plan.primary.target == "CS-RIVERSIDE"


def test_port_congestion_diverts_to_the_overflow_depot() -> None:
    plan = PLANNER.plan(incident("R-CONG-01", "R-RISK-01"), reading(), SITES)
    assert plan.primary is not None
    assert (plan.primary.action, plan.primary.target) == ("divert_to_overflow_depot", "IE-OVERFLOW")


def test_high_risk_alone_escalates() -> None:
    plan = PLANNER.plan(incident("R-RISK-01"), reading(), SITES)
    assert plan.primary is not None and plan.primary.action == "escalate_tier2_manager"
    assert plan.companions == ()


def test_escalation_is_the_fallback_after_notifying_the_driver() -> None:
    plan = PLANNER.plan(incident("R-TEMP-01"), reading(), SITES)
    actions = [c.action for c in plan.candidates]
    assert actions.index("notify_driver_restart_cooling") < actions.index("escalate_tier2_manager")
    assert actions.index("escalate_tier2_manager") < actions.index("divert_to_cold_storage")


def test_facilities_down_means_no_diversion_and_low_confidence() -> None:
    plan = PLANNER.plan(incident("R-TEMP-01", "R-ETA-01"), reading(90), None)
    assert plan.confidence == "low"
    assert all(not c.action.startswith("divert") for c in plan.candidates)


def test_planning_is_deterministic() -> None:
    a = PLANNER.plan(incident("R-TEMP-01", "R-ETA-01"), reading(90), SITES)
    b = PLANNER.plan(incident("R-TEMP-01", "R-ETA-01"), reading(90), list(reversed(SITES)))
    assert a == b


def test_close_calls_are_flagged() -> None:
    narrow = DeterministicPlanner(margin=1000.0)
    assert narrow.plan(incident("R-TEMP-01"), reading(), SITES).close_call
    assert not PLANNER.plan(incident("R-TEMP-01", "R-ETA-01"), reading(90), SITES).close_call


def test_proposal_carries_evidence_alternatives_and_rationale() -> None:
    plan = PLANNER.plan(incident("R-TEMP-01", "R-ETA-01"), reading(90), SITES)
    assert plan.primary is not None
    p = PLANNER.proposal(plan, plan.primary, incident("R-TEMP-01", "R-ETA-01"), reading(90), T0)
    assert p.planner == "deterministic" and p.eta_delay_min == 90
    assert p.alternatives and "Utility" in p.rationale
    assert len(p.content_hash()) == 64


def test_travel_minutes_uses_the_road_factor() -> None:
    assert round(travel_minutes(34.0, -118.0, 34.0, -118.0)) == 0
    assert travel_minutes(33.0, -118.0, 34.0, -118.0) > 111  # ~111 km straight, x1.3 at 60 km/h
