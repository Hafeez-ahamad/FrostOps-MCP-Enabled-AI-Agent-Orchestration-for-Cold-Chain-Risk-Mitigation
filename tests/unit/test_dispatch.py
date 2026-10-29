"""End to end on the smoke scenario (simulated): detection -> planner -> gate -> executor."""

from collections import Counter
from datetime import datetime, timedelta

import pytest

from engine.actions.models import ActionStatus, Approval, GateOutcome, Proposal
from engine.actions.policy import ActionLevel
from engine.audit.log import verify_chain
from engine.runtime import Runtime
from sim.generator import SimulationOutput
from sim.mock_services.faults import Fault, FaultInjector
from sim.mock_services.state import MockWorld
from sim.mock_services.wiring import in_process_integrations
from sim.scenario import Scenario
from tests.fakes import sqlite_engine


def dispatcher_approves(pending: list[Proposal], now: datetime) -> list[Approval]:
    """A stand-in human: approves every pending proposal as dispatcher 'demo'."""
    return [
        Approval(
            approval_id=f"APR-{p.proposal_id}",
            proposal_id=p.proposal_id,
            proposal_hash=p.content_hash(),
            approver_id="demo",
            approver_role="dispatcher",
            decision="approve",
            decided_at=now,
        )
        for p in pending
    ]


def run(
    smoke: SimulationOutput, scenario: Scenario, approver=None, faults=None
) -> tuple[Runtime, MockWorld]:  # type: ignore[no-untyped-def]
    world = MockWorld(now=scenario.start, events=smoke.events)
    integrations = in_process_integrations(world, faults or FaultInjector())
    runtime = Runtime.build(sqlite_engine(), integrations)

    def on_tick(t: datetime) -> None:
        world.now = t

    end = scenario.start + timedelta(hours=scenario.duration_hours)
    runtime.run(
        smoke.readings.to_dict("records"),
        timedelta(minutes=scenario.reading_interval_min),
        end,
        on_tick=on_tick,
        approver=approver,
    )
    return runtime, world


@pytest.fixture(scope="module")
def approved(smoke: SimulationOutput, smoke_scenario: Scenario) -> tuple[Runtime, MockWorld]:
    return run(smoke, smoke_scenario, approver=dispatcher_approves)


@pytest.fixture(scope="module")
def unattended(smoke: SimulationOutput, smoke_scenario: Scenario) -> tuple[Runtime, MockWorld]:
    return run(smoke, smoke_scenario)


def test_two_auto_actions_and_an_approval_gated_action_run(
    approved: tuple[Runtime, MockWorld],
) -> None:
    runtime, _ = approved
    done = Counter(
        (a.action, a.level)
        for a in runtime.executor.actions.all()
        if a.status is ActionStatus.SUCCEEDED
    )
    assert done[("notify_driver_restart_cooling", ActionLevel.L2)] >= 1
    assert done[("create_incident_ticket", ActionLevel.L2)] >= 1
    assert done[("divert_to_cold_storage", ActionLevel.L3)] >= 1


def test_zero_unauthorised_actions(approved: tuple[Runtime, MockWorld]) -> None:
    runtime, _ = approved
    gate = runtime.gate
    for action in runtime.executor.actions.all():
        proposal = gate.proposals.get(action.proposal_id)
        assert proposal is not None and proposal["Status"] == "approved"
        assert proposal["ProposalHash"] == action.proposal_hash
        if action.level is ActionLevel.L3:
            approval = gate.approvals.for_proposal(action.proposal_id)
            assert approval is not None and approval["Decision"] == "approve"
    assert verify_chain(gate.audit.entries())


def test_without_a_human_no_l3_action_runs(unattended: tuple[Runtime, MockWorld]) -> None:
    runtime, world = unattended
    levels = {a.level for a in runtime.executor.actions.all()}
    assert ActionLevel.L3 not in levels
    assert runtime.gate.proposals.pending(), "L3 proposals wait in the approval queue"
    assert not [e for e in world.log if e["service"] in ("tms", "escalation")]


def test_unreachable_driver_falls_back_to_escalation(
    smoke: SimulationOutput, approved: tuple[Runtime, MockWorld]
) -> None:
    runtime, _ = approved
    (label,) = [lb for lb in smoke.incidents if lb.type == "driver_unreachable"]
    decisions = [d for d in runtime.dispatcher.decisions if label.shipment_id in d.proposal_id]
    notify = [d for d in decisions if d.action == "notify_driver_restart_cooling"]
    assert notify and notify[0].outcome is GateOutcome.REJECTED
    assert any("driver_reachable" in r for r in notify[0].reasons)
    escalations = [
        a
        for a in runtime.executor.actions.all()
        if a.shipment_id == label.shipment_id and a.action == "escalate_tier2_manager"
    ]
    assert escalations and escalations[0].status is ActionStatus.SUCCEEDED


def test_failed_action_marks_needs_attention_and_proposes_escalation(
    smoke: SimulationOutput, smoke_scenario: Scenario
) -> None:
    faults = FaultInjector()
    faults.set("driver.notify", Fault(mode="fail"))  # the notification service is down all day
    runtime, _ = run(smoke, smoke_scenario, faults=faults)
    failed = [
        a for a in runtime.executor.actions.all() if a.action == "notify_driver_restart_cooling"
    ]
    assert failed and all(a.status is ActionStatus.FAILED and a.attempts == 3 for a in failed)
    for action in failed:
        incident = runtime.manager.incidents[action.incident_id]
        # Marked needs_attention when the action failed; it may clear later on its own if the
        # temperature recovers (e.g. a door-open event), but never before the failure.
        if incident.status == "cleared":
            assert incident.cleared_at is not None and incident.cleared_at >= action.updated_at
        else:
            assert incident.status == "needs_attention"
    pending = {
        r["IncidentID"]
        for r in runtime.gate.proposals.pending()
        if r["Action"] == "escalate_tier2_manager"
    }
    assert {a.incident_id for a in failed} <= pending
