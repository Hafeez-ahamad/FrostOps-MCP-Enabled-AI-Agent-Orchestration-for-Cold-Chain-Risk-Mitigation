"""C0, C1 and C2 end to end on the smoke scenario (simulated), with a scripted LLM.

The scripted LLM only proves the plumbing: messages, logging, fallbacks and the shared gate.
It is not evidence of LLM quality; that is the Phase 5 replay with a real model.
"""

import re
from collections import Counter
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from engine.actions.models import ActionStatus, GateOutcome
from engine.actions.policy import ActionLevel
from engine.audit.log import verify_chain
from engine.bus.messages import correlation_id
from engine.data.memory_db import in_memory_engine
from engine.data.schema import LLM_CALLS
from engine.llm.fake import Responder, ResponderChatModel, heuristic_responder
from engine.llm.structured import LLMCallLog, StructuredLLM, TokenBudget
from engine.runtime import Configuration, Runtime
from sim.generator import SimulationOutput
from sim.mock_services.faults import FaultInjector
from sim.mock_services.state import MockWorld
from sim.mock_services.wiring import in_process_integrations
from sim.scenario import Scenario
from tests.unit.test_dispatch import dispatcher_approves

TEMPS = re.compile(r"^\d\d:\d\d \| (-?[\d.]+) \|", re.MULTILINE)
CANDIDATE = re.compile(r"^\[(\d+)\] (\w+) target=", re.MULTILINE)
RULES = re.compile(r"rules ([A-Z0-9, -]+);")


sensible = heuristic_responder


def run(
    smoke: SimulationOutput,
    scenario: Scenario,
    config: Configuration,
    responder: Responder | None = sensible,
    budget: int = 1_000_000,
    approver: Any = dispatcher_approves,
    explain: bool = False,
) -> Runtime:
    engine = in_memory_engine()
    world = MockWorld(now=scenario.start, events=smoke.events)
    llm = None
    if responder is not None:
        llm = StructuredLLM(
            ResponderChatModel(responder=responder),
            "fake",
            TokenBudget(budget),
            f"test-{config}",
            LLMCallLog(engine),
        )
    runtime = Runtime.build(
        engine,
        in_process_integrations(world, FaultInjector()),
        config=config,
        llm=llm,
        explain=explain,
    )

    def on_tick(t: datetime) -> None:
        world.now = t

    runtime.run(
        smoke.readings.to_dict("records"),
        timedelta(minutes=scenario.reading_interval_min),
        scenario.start + timedelta(hours=scenario.duration_hours),
        on_tick=on_tick,
        approver=approver,
    )
    return runtime


@pytest.fixture(scope="module")
def runs(smoke: SimulationOutput, smoke_scenario: Scenario) -> dict[str, Runtime]:
    return {
        "C0": run(smoke, smoke_scenario, "C0", responder=None),
        "C1": run(smoke, smoke_scenario, "C1"),
        "C2": run(smoke, smoke_scenario, "C2", explain=True),
    }


def primary(runtime: Runtime, shipment: str) -> str:
    first = next(d for d in runtime.dispatcher.decisions if shipment in d.proposal_id)
    return first.action


def llm_rows(runtime: Runtime) -> list[dict[str, Any]]:
    with runtime.engine.connect() as conn:
        return [dict(r) for r in conn.execute(select(LLM_CALLS)).mappings()]


def test_all_three_configurations_run_end_to_end(runs: dict[str, Runtime]) -> None:
    incidents = {name: len(r.manager.incidents) for name, r in runs.items()}
    assert len(set(incidents.values())) == 1, "detection is shared, so the incidents are the same"
    for name, runtime in runs.items():
        done = [a for a in runtime.executor.actions.all() if a.status is ActionStatus.SUCCEEDED]
        assert done, name
        assert verify_chain(runtime.gate.audit.entries()), name


def test_zero_unauthorised_actions_in_every_configuration(runs: dict[str, Runtime]) -> None:
    for runtime in runs.values():
        for action in runtime.executor.actions.all():
            row = runtime.gate.proposals.get(action.proposal_id)
            assert row is not None and row["Status"] == "approved"
            if action.level is ActionLevel.L3:
                assert runtime.gate.approvals.for_proposal(action.proposal_id) is not None


def test_c0_uses_no_llm_and_c1_c2_log_every_call(runs: dict[str, Runtime]) -> None:
    assert llm_rows(runs["C0"]) == []
    for name in ("C1", "C2"):
        rows = llm_rows(runs[name])
        assert rows, name
        incident_cors = {correlation_id(i) for i in runs[name].manager.incidents}
        assert {r["CorrelationID"] for r in rows} <= incident_cors
        assert all(r["PromptTokens"] > 0 and r["Model"] == "scripted-fake" for r in rows)
    roles = Counter(r["Role"] for r in llm_rows(runs["C2"]))
    assert set(roles) == {"monitor", "planner", "explainer"} or set(roles) == {
        "monitor",
        "explainer",
    }
    assert set(Counter(r["Role"] for r in llm_rows(runs["C1"]))) == {"single_agent"}


def test_every_handoff_is_a_persisted_typed_message(runs: dict[str, Runtime]) -> None:
    for name, runtime in runs.items():
        history = runtime.dispatcher.ctx.bus.history()
        kinds = Counter(m.body.type for m in history)
        assert kinds["IncidentOpened"] >= len(runtime.manager.incidents) - 1, name
        assert (
            kinds["PlanProposed"] >= 1
            and kinds["ActionApproved"] >= 1
            and kinds["ActionExecuted"] >= 1
        )
        consumed = [m for m in history if m.body.type in ("IncidentOpened", "PlanProposed")]
        assert all(m.correlation_id.startswith("COR-INC-") for m in consumed)


def test_triage_changes_the_door_open_decision_but_not_the_gate(
    smoke: SimulationOutput, runs: dict[str, Runtime]
) -> None:
    door = next(lb for lb in smoke.incidents if lb.type == "door_open" and lb.true_breach_ts)
    assert primary(runs["C0"], door.shipment_id) == "notify_driver_restart_cooling"
    assert primary(runs["C1"], door.shipment_id) == "create_incident_ticket"
    assert primary(runs["C2"], door.shipment_id) == "create_incident_ticket"
    reefer = next(
        lb
        for lb in smoke.incidents
        if lb.type == "reefer_failure" and lb.oracle_action.startswith("notify")
    )
    for runtime in runs.values():
        assert primary(runtime, reefer.shipment_id) == "notify_driver_restart_cooling"


def test_llm_proposals_are_labelled(runs: dict[str, Runtime]) -> None:
    labels = Counter(
        (p["Payload"].count('"planner":"llm"') > 0)
        for p in [
            runs["C1"].gate.proposals.get(d.proposal_id) for d in runs["C1"].dispatcher.decisions
        ]
        if p is not None
    )
    assert labels[True] >= 1


def test_explainer_answers_are_kept(runs: dict[str, Runtime]) -> None:
    explanations = runs["C2"].dispatcher.ctx.explanations
    assert explanations and all(e.rule_ids for e in explanations.values())


# --- degraded modes -----------------------------------------------------------------------


def _actions(runtime: Runtime) -> Counter[tuple[str, str]]:
    return Counter((a.action, a.target) for a in runtime.executor.actions.all())


@pytest.mark.parametrize("config", ["C1", "C2"])
def test_malformed_llm_output_falls_back_to_c0_and_is_marked_degraded(
    smoke: SimulationOutput,
    smoke_scenario: Scenario,
    runs: dict[str, Runtime],
    config: Configuration,
) -> None:
    garbage = run(
        smoke, smoke_scenario, config, responder=lambda s, u: "I refuse to answer in JSON"
    )
    assert garbage.degraded_steps
    assert _actions(garbage) == _actions(runs["C0"])
    assert all(r["Error"] == "malformed_output" for r in llm_rows(garbage))


def test_unavailable_llm_degrades_to_c0(
    smoke: SimulationOutput, smoke_scenario: Scenario, runs: dict[str, Runtime]
) -> None:
    offline = run(smoke, smoke_scenario, "C2", responder=None)
    assert offline.degraded_steps and _actions(offline) == _actions(runs["C0"])


def test_exhausted_budget_degrades_and_logs_the_refusals(
    smoke: SimulationOutput, smoke_scenario: Scenario
) -> None:
    starved = run(smoke, smoke_scenario, "C1", budget=1)
    errors = Counter(r["Error"] for r in llm_rows(starved))
    assert errors["token_budget_exhausted"] >= 1 and starved.degraded_steps


def test_an_llm_choice_still_needs_a_human_for_l3(
    smoke: SimulationOutput, smoke_scenario: Scenario
) -> None:
    def always_last(system: str, user: str) -> str:
        last = max((int(i) for i, _ in CANDIDATE.findall(user)), default=0)
        return f'{{"verdict": "real_breach", "confidence": 0.9, "choice": {last}, "reasons": "x"}}'

    unattended = run(smoke, smoke_scenario, "C1", responder=always_last, approver=None)
    assert not [a for a in unattended.executor.actions.all() if a.level is ActionLevel.L3]
    awaiting = [
        d for d in unattended.dispatcher.decisions if d.outcome is GateOutcome.AWAIT_APPROVAL
    ]
    assert awaiting
