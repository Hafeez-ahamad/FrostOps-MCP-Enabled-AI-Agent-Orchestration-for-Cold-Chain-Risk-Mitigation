"""Incident open / merge / clear, suspect handling, ranking, persistence, and the smoke scenario."""

import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from engine.data.schema import INCIDENTS, RULE_HITS
from engine.detection import DetectionResult, detect_stream
from engine.incidents.manager import IncidentManager
from engine.incidents.store import save_incidents, save_rule_hits
from engine.rules.engine import RuleInput, RulesEngine
from sim.generator import SimulationOutput
from sim.scenario import Scenario
from tests.fakes import sqlite_engine

RULES = RulesEngine()
T0 = datetime(2026, 3, 2, 12, 0)


def reading(minute: int, temp: float = 2.0, shipment: str = "S1", **kw: object) -> RuleInput:
    fields: dict[str, object] = {
        "shipment_id": shipment,
        "seq": minute // 5 + 1,
        "reading_ts": T0 + timedelta(minutes=minute),
        "depot_id": "DEP-LB",
        "cargo_type": "FRESH",
        "temperature_c": temp,
        "port_congestion": None,
        "risk_classification": "Low Risk",
        "delay_probability": 0.1,
        "eta_delay_min": 0.0,
    }
    fields.update(kw)
    return RuleInput(**fields)  # type: ignore[arg-type]


def feed(manager: IncidentManager, r: RuleInput, dq: str = "valid") -> str | None:
    event = manager.process(r, dq, RULES.evaluate(r, dq))  # type: ignore[arg-type]
    return event.kind if event else None


def test_first_valid_hit_opens_an_incident() -> None:
    m = IncidentManager()
    assert feed(m, reading(0, 5.0)) == "opened"
    incident = m.open_incident_for("S1")
    assert incident is not None and incident.incident_id == "INC-S1-01"
    assert incident.rule_ids == ("R-TEMP-01",)


def test_later_hits_merge_into_the_same_incident() -> None:
    m = IncidentManager()
    feed(m, reading(0, 5.0))
    assert feed(m, reading(5, 5.5)) == "merged"
    assert feed(m, reading(10, 6.0, eta_delay_min=90.0)) == "merged"
    incident = m.open_incident_for("S1")
    assert incident is not None
    assert len(m.incidents) == 1
    assert incident.hit_count == 3
    assert incident.rule_ids == ("R-TEMP-01", "R-ETA-01")


def test_one_open_incident_per_shipment_but_shipments_are_independent() -> None:
    m = IncidentManager()
    feed(m, reading(0, 5.0, shipment="A"))
    feed(m, reading(0, 5.0, shipment="B"))
    assert {i.shipment_id for i in m.ranked_open()} == {"A", "B"}


def test_incident_clears_after_quiet_period_and_a_new_hit_opens_a_new_one() -> None:
    m = IncidentManager(clear_after_min=30)
    feed(m, reading(0, 5.0))
    assert feed(m, reading(25, 3.0)) is None  # still inside the quiet period
    assert feed(m, reading(30, 3.0)) == "cleared"
    assert m.open_incident_for("S1") is None
    assert feed(m, reading(40, 5.0)) == "opened"
    assert sorted(m.incidents) == ["INC-S1-01", "INC-S1-02"]


@pytest.mark.parametrize("dq", ["suspect", "invalid"])
def test_non_valid_readings_never_open_or_clear(dq: str) -> None:
    m = IncidentManager()
    assert feed(m, reading(0, 25.0), dq=dq) is None
    assert m.incidents == {}
    feed(m, reading(5, 5.0))
    assert feed(m, reading(60, 2.0), dq=dq) is None  # a quiet non-valid reading cannot clear
    incident = m.open_incident_for("S1")
    assert incident is not None and incident.status == "open"


def test_suspect_hits_are_kept_for_triage_invalid_ones_are_not() -> None:
    m = IncidentManager()
    feed(m, reading(0, 25.0), dq="suspect")
    feed(m, reading(5, 25.0), dq="invalid")
    assert [h.dq_status for h in m.suspect_hits] == ["suspect"]


def test_risk_grows_while_the_breach_persists() -> None:
    m = IncidentManager()
    feed(m, reading(0, 5.0))
    first = m.open_incident_for("S1")
    feed(m, reading(30, 6.0))
    later = m.open_incident_for("S1")
    assert first is not None and later is not None
    assert later.risk.score > first.risk.score


def test_ranking_puts_the_riskiest_first() -> None:
    m = IncidentManager()
    feed(m, reading(0, 4.5, shipment="mild"))
    feed(m, reading(0, 9.0, shipment="hot", cargo_type="PHARMA"))
    assert [i.shipment_id for i in m.ranked_open()] == ["hot", "mild"]


def test_store_appends_hits_and_upserts_incidents() -> None:
    engine = sqlite_engine()
    m = IncidentManager()
    r = reading(0, 5.0)
    hits = RULES.evaluate(r, "valid")
    m.process(r, "valid", hits)
    with engine.begin() as conn:
        save_rule_hits(conn, hits, T0)
        save_incidents(conn, m.incidents.values(), T0)
    feed(m, reading(5, 6.0))
    with engine.begin() as conn:
        save_incidents(conn, m.incidents.values(), T0 + timedelta(minutes=5))
    with engine.connect() as conn:
        stored = conn.execute(select(INCIDENTS)).mappings().all()
        hit_rows = conn.execute(select(RULE_HITS)).mappings().all()
    assert len(stored) == 1 and stored[0]["HitCount"] == 2
    assert stored[0]["RuleIds"] == "R-TEMP-01"
    assert json.loads(hit_rows[0]["Observed"]) == {"cargo_type": "FRESH", "temperature_c": 5.0}


# --- the smoke scenario end to end (simulated) -------------------------------------------------


@pytest.fixture(scope="module")
def detection(smoke: SimulationOutput, smoke_scenario: Scenario) -> DetectionResult:
    end = smoke_scenario.start + timedelta(hours=smoke_scenario.duration_hours)
    tick = timedelta(minutes=smoke_scenario.reading_interval_min)
    return detect_stream(smoke.readings.to_dict("records"), tick, end)


def test_every_detectable_labelled_incident_is_detected(
    smoke: SimulationOutput, detection: DetectionResult
) -> None:
    opened = {i.shipment_id for i in detection.manager.incidents.values()}
    expected = {lb.shipment_id for lb in smoke.incidents if lb.oracle_action != "no_action"}
    assert expected <= opened


def test_no_incident_on_clean_or_glitch_shipments(
    smoke: SimulationOutput, detection: DetectionResult
) -> None:
    true_shipments = {lb.shipment_id for lb in smoke.incidents if lb.oracle_action != "no_action"}
    false_alarms = [
        i.incident_id
        for i in detection.manager.incidents.values()
        if i.shipment_id not in true_shipments
    ]
    assert false_alarms == []


def test_incidents_never_open_before_the_true_start(
    smoke: SimulationOutput, detection: DetectionResult
) -> None:
    start = {lb.shipment_id: lb.start_ts for lb in smoke.incidents}
    for incident in detection.manager.incidents.values():
        assert incident.opened_at >= start[incident.shipment_id]


def test_glitches_that_cross_a_threshold_stay_suspect_hits(
    smoke: SimulationOutput, detection: DetectionResult
) -> None:
    glitch_shipments = {g.shipment_id for g in smoke.glitches}
    assert detection.manager.suspect_hits, "smoke has spikes that cross 4.0 C"
    assert {h.shipment_id for h in detection.manager.suspect_hits} <= glitch_shipments
