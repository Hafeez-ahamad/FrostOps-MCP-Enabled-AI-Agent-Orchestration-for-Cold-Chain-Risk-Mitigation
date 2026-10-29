"""Failure injection, one test (or group) per row of spec section 14. Simulated fleet.

| Failure                          | Expected behaviour                                         |
|----------------------------------|------------------------------------------------------------|
| Weather/congestion down or slow  | stale/unknown value labelled; plan low-confidence; no L2   |
|                                  | action depends on it                                       |
| Driver-notification API fails    | retry with backoff, then escalate to a human, flag incident |
| DB timeout or deadlock           | retry; if persistent, alert-only mode                      |
| Audit write fails                | action blocked (fail closed) and alert raised              |
| LLM unavailable or malformed     | deterministic fallback, marked degraded                    |
| Duplicate/replayed events        | idempotency prevents a double action                       |
| Schema drift                     | clear error; engine stays in alert-only mode               |

Most tests run the first 10 simulated hours of the smoke scenario (C0 unless stated).
"""

from collections import Counter
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

import engine.runtime as runtime_module
from engine.actions.models import ActionStatus, GateOutcome, Proposal
from engine.data.memory_db import in_memory_engine
from engine.data.schema import PROPOSALS
from engine.dispatch import Dispatcher
from engine.llm.fake import ResponderChatModel
from engine.llm.structured import LLMCallLog, StructuredLLM, TokenBudget
from engine.observability import REGISTRY
from engine.rules.engine import RuleInput
from engine.runtime import Configuration, Runtime
from sim.generator import SimulationOutput
from sim.mock_services.faults import Fault, FaultInjector
from sim.mock_services.state import MockWorld
from sim.mock_services.wiring import in_process_integrations
from sim.scenario import Scenario
from tests.unit.harness import T0, Harness
from tests.unit.test_audit_and_policy import _break_audit
from tests.unit.test_dispatch import dispatcher_approves

HOURS = 10
Rows = list[dict[str, Any]]


def metric(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def window(smoke: SimulationOutput, scenario: Scenario, hours: float = HOURS) -> Rows:
    cutoff = (scenario.start + timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    rows: Rows = smoke.readings.to_dict("records")  # type: ignore[assignment]
    return [r for r in rows if str(r["RCPT_DTTM"]) <= cutoff]


def run(
    smoke: SimulationOutput,
    scenario: Scenario,
    *,
    rows: Rows | None = None,
    faults: FaultInjector | None = None,
    config: Configuration = "C0",
    llm: StructuredLLM | None = None,
    approver: Any = None,
) -> Runtime:
    engine = in_memory_engine()
    world = MockWorld(now=scenario.start, events=smoke.events)
    runtime = Runtime.build(
        engine, in_process_integrations(world, faults or FaultInjector()), config=config, llm=llm
    )

    def on_tick(t: datetime) -> None:
        world.now = t

    runtime.run(
        rows if rows is not None else window(smoke, scenario),
        timedelta(minutes=scenario.reading_interval_min),
        scenario.start + timedelta(hours=HOURS),
        on_tick=on_tick,
        approver=approver,
    )
    return runtime


def proposal_rows(runtime: Runtime) -> list[dict[str, Any]]:
    with runtime.engines.gate.connect() as conn:
        return [dict(r) for r in conn.execute(select(PROPOSALS)).mappings()]


def temperature_proposals(runtime: Runtime) -> list[Proposal]:
    found = [runtime.gate.proposals.proposal(r["ProposalID"]) for r in proposal_rows(runtime)]
    return [p for p in found if p is not None and "R-TEMP-01" in p.rule_ids]


def l2_actions(runtime: Runtime) -> Counter[tuple[str, str, str]]:
    return Counter(
        (a.action, a.target, a.status.value)
        for a in runtime.executor.actions.all()
        if a.level.value == "L2"
    )


@pytest.fixture(scope="module")
def normal(smoke: SimulationOutput, smoke_scenario: Scenario) -> Runtime:
    return run(smoke, smoke_scenario)


# --- weather / congestion down or slow ---------------------------------------------------------


@pytest.mark.parametrize("mode", ["fail", "slow"])
def test_weather_and_congestion_down_lower_confidence_but_change_no_l2_action(
    smoke: SimulationOutput, smoke_scenario: Scenario, normal: Runtime, mode: str
) -> None:
    faults = FaultInjector()
    for endpoint in ("weather.get", "congestion.get"):
        faults.set(endpoint, Fault(mode=mode, latency_ms=60_000))  # type: ignore[arg-type]
    down = run(smoke, smoke_scenario, faults=faults)

    assert l2_actions(down) == l2_actions(normal)  # no L2 action depends on weather
    temperature = temperature_proposals(down)
    assert temperature
    assert all("Low confidence" in p.rationale for p in temperature)
    assert all("weather unavailable" in p.rationale for p in temperature)
    normal_temperature = temperature_proposals(normal)
    assert not any("weather" in p.rationale for p in normal_temperature)


def test_a_failed_weather_read_falls_back_to_the_cached_value_marked_stale() -> None:
    h = Harness.build()
    h.faults.queue("weather.get", Fault(), Fault(mode="fail"))
    dispatcher = Dispatcher(None, None, h.integrations, None)  # type: ignore[arg-type]
    reading = RuleInput(
        "S1",
        7,
        T0,
        "DEP-LB",
        "FRESH",
        6.0,
        None,
        None,
        None,
        None,
        latitude=33.77,
        longitude=-118.19,
    )
    assert dispatcher._weather_note(("R-TEMP-01",), reading) is None  # fresh read
    note = dispatcher._weather_note(("R-TEMP-01",), reading)
    assert note is not None and note.startswith("weather stale: last good reading from")
    assert dispatcher._weather_note(("R-ETA-01",), reading) is None  # only for temperature


# --- driver-notification API fails --------------------------------------------------------------


def test_driver_api_failure_retries_with_backoff_then_escalates_and_alerts(
    smoke: SimulationOutput, smoke_scenario: Scenario
) -> None:
    faults = FaultInjector()
    faults.set("driver.notify", Fault(mode="fail"))
    alerts_before = metric("coldchain_alerts_total", kind="action_failed")
    runtime = run(smoke, smoke_scenario, faults=faults)

    failed = [
        a for a in runtime.executor.actions.all() if a.action == "notify_driver_restart_cooling"
    ]
    assert failed and all(a.status is ActionStatus.FAILED and a.attempts == 3 for a in failed)
    entries = runtime.gate.audit.entries()
    for action in failed:
        retries = [
            e["At"]
            for e in entries
            if e["ActionID"] == action.action_id and e["EventType"] == "action_executing"
        ]
        gaps = [b - a for a, b in zip(retries, retries[1:], strict=False)]
        # Backoff 60 s then 120 s; retries run at the next poll (5 min) after it has passed.
        assert len(retries) == 3
        assert gaps[0] >= timedelta(seconds=60) and gaps[1] >= timedelta(seconds=120)
    escalations = {
        r["IncidentID"]
        for r in runtime.gate.proposals.pending()
        if r["Action"] == "escalate_tier2_manager"
    }
    assert {a.incident_id for a in failed} <= escalations
    assert metric("coldchain_alerts_total", kind="action_failed") - alerts_before == len(failed)


# --- database timeout / deadlock ----------------------------------------------------------------


def _failing(times: int, real: Callable[..., None]) -> Callable[..., None]:
    calls = {"n": 0}

    def save(*args: Any, **kwargs: Any) -> None:
        calls["n"] += 1
        if calls["n"] <= times:
            raise OperationalError("INSERT INTO ops.RuleHits", {}, Exception("timeout expired"))
        real(*args, **kwargs)

    return save


def test_a_transient_db_timeout_is_retried_without_changing_anything(
    smoke: SimulationOutput,
    smoke_scenario: Scenario,
    normal: Runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        runtime_module, "save_rule_hits", _failing(2, runtime_module.save_rule_hits)
    )
    runtime = run(smoke, smoke_scenario)
    assert not runtime.alert_only
    assert l2_actions(runtime) == l2_actions(normal)


def test_a_persistent_db_failure_degrades_to_alert_only(
    smoke: SimulationOutput, smoke_scenario: Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        runtime_module, "save_rule_hits", _failing(10**9, runtime_module.save_rule_hits)
    )
    alerts_before = metric("coldchain_alerts_total", kind="alert_only")
    runtime = run(smoke, smoke_scenario, approver=dispatcher_approves)

    assert runtime.alert_only
    assert runtime.alert_reason == "database unavailable (save rule hits)"
    assert runtime.manager.incidents  # incidents are still detected ...
    assert proposal_rows(runtime) == []  # ... but nothing is dispatched
    assert runtime.executor.actions.all() == []
    assert metric("coldchain_alert_only_mode") == 1.0
    assert metric("coldchain_alerts_total", kind="alert_only") - alerts_before == 1
    assert metric("coldchain_alerts_total", kind="incident_in_alert_only") >= 1


def test_leaving_alert_only_needs_a_person_and_is_audited(
    smoke: SimulationOutput, smoke_scenario: Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        runtime_module, "save_rule_hits", _failing(10**9, runtime_module.save_rule_hits)
    )
    runtime = run(smoke, smoke_scenario)
    runtime.clear_alert_only("supervisor:sam", smoke_scenario.start + timedelta(hours=HOURS))
    assert not runtime.alert_only
    entry = runtime.gate.audit.entries()[-1]
    assert (entry["EventType"], entry["Actor"]) == ("alert_only_cleared", "supervisor:sam")


# --- audit write fails ---------------------------------------------------------------------------


def test_audit_failure_blocks_the_gate_and_the_executor_and_raises_an_alert() -> None:
    h = Harness.build()
    h.submit_and_enqueue(h.proposal("create_incident_ticket"))
    before = metric("coldchain_alerts_total", kind="audit_unavailable")
    _break_audit(h)

    (held,) = h.executor.run_once(T0)
    assert held.status is ActionStatus.QUEUED and held.last_error == "audit_unavailable"
    assert h.world.log == []
    decision = h.gate.submit(h.proposal("notify_driver_restart_cooling", shipment="S2"), T0)
    assert decision.outcome is GateOutcome.REJECTED
    assert "audit_unavailable" in decision.reasons
    assert metric("coldchain_alerts_total", kind="audit_unavailable") - before == 2


# --- LLM unavailable or malformed ---------------------------------------------------------------


@pytest.mark.parametrize(
    "reply", ["not json at all", '{"verdict": "real_breach"}', "boom"], ids=str
)
def test_llm_failure_falls_back_to_the_deterministic_plan_and_is_marked_degraded(
    smoke: SimulationOutput, smoke_scenario: Scenario, normal: Runtime, reply: str
) -> None:
    def responder(system: str, user: str) -> str:
        if reply == "boom":
            raise ConnectionError("model server down")
        return reply

    engine = in_memory_engine()
    llm = StructuredLLM(
        ResponderChatModel(responder=responder), "fake", TokenBudget(10**6), "t", LLMCallLog(engine)
    )
    runtime = run(smoke, smoke_scenario, config="C1", llm=llm)
    assert runtime.degraded_steps
    assert l2_actions(runtime) == l2_actions(normal)
    expected = "llm_unavailable" if reply == "boom" else "malformed_output"
    assert llm.calls and all(c["Error"].startswith(expected) for c in llm.calls)


# --- duplicate / replayed events ---------------------------------------------------------------


def test_every_reading_delivered_twice_triggers_no_double_action(
    smoke: SimulationOutput, smoke_scenario: Scenario, normal: Runtime
) -> None:
    doubled = [row for row in window(smoke, smoke_scenario) for _ in (0, 1)]
    runtime = run(smoke, smoke_scenario, rows=doubled)
    assert l2_actions(runtime) == l2_actions(normal)
    keys = [a.idempotency_key for a in runtime.executor.actions.all()]
    assert len(keys) == len(set(keys))


def test_a_replayed_gate_decision_or_proposal_changes_nothing() -> None:
    h = Harness.build()
    proposal = h.proposal("create_incident_ticket")
    decision = h.submit_and_enqueue(proposal)
    again = h.gate.submit(proposal, T0 + timedelta(minutes=1))
    assert again.outcome is GateOutcome.DUPLICATE
    h.executor.enqueue(decision, T0 + timedelta(minutes=1))  # replayed decision
    h.executor.run_once(T0 + timedelta(minutes=2))
    assert len(h.executor.actions.all()) == 1
    assert len([c for c in h.world.log if c["service"] == "ticketing"]) == 1


# --- schema drift ---------------------------------------------------------------------------


def test_schema_drift_puts_the_engine_in_alert_only_with_a_clear_reason(
    smoke: SimulationOutput, smoke_scenario: Scenario
) -> None:
    drift_at = (smoke_scenario.start + timedelta(hours=6)).strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    for row in window(smoke, smoke_scenario):
        if str(row["RCPT_DTTM"]) >= drift_at:  # the source renamed a column at 06:00
            row = dict(row)
            row["RFR_TEMP_C"] = row.pop("RFR_TMP_C")
        rows.append(row)
    runtime = run(smoke, smoke_scenario, rows=rows, approver=dispatcher_approves)

    assert runtime.alert_only
    assert runtime.alert_reason is not None and "RFR_TMP_C" in runtime.alert_reason
    assert metric("coldchain_alerts_total", kind="schema_drift") >= 1
    drift = datetime.fromisoformat(drift_at)
    assert all(r["CreatedAt"] < drift for r in proposal_rows(runtime))
    assert all(a.created_at < drift for a in runtime.executor.actions.all())
