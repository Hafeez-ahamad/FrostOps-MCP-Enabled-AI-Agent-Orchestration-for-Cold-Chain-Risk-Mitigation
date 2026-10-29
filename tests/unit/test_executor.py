"""Executor states, retries with backoff, escalation, cancel and compensation."""

from datetime import timedelta

import pytest

from engine.actions.models import ActionStatus, GateOutcome
from engine.executor.executor import ExecutionRefused
from sim.mock_services.faults import Fault
from tests.unit.harness import T0, Harness


@pytest.fixture
def h() -> Harness:
    return Harness.build()


def test_queued_then_executing_then_succeeded(h: Harness) -> None:
    h.submit_and_enqueue(h.proposal("create_incident_ticket"))
    (queued,) = h.executor.actions.all()
    assert queued.status is ActionStatus.QUEUED and queued.attempts == 0
    (done,) = h.executor.run_once(T0)
    assert done.status is ActionStatus.SUCCEEDED and done.attempts == 1
    assert done.result["ticket_id"].startswith("TKT-")
    events = [e["EventType"] for e in h.gate.audit.entries()]
    assert "action_executing" in events and "action_succeeded" in events


def test_transient_failure_retries_with_exponential_backoff(h: Harness) -> None:
    h.faults.queue("ticketing.create", Fault(mode="fail"), Fault(mode="fail"))
    h.submit_and_enqueue(h.proposal("create_incident_ticket"))
    (first,) = h.executor.run_once(T0)
    assert first.status is ActionStatus.QUEUED and first.next_attempt_at == T0 + timedelta(
        seconds=60
    )
    assert h.executor.run_once(T0 + timedelta(seconds=30)) == []  # not due yet
    (second,) = h.executor.run_once(T0 + timedelta(seconds=60))
    assert second.next_attempt_at == T0 + timedelta(seconds=60 + 120)
    (third,) = h.executor.run_once(T0 + timedelta(seconds=180))
    assert third.status is ActionStatus.SUCCEEDED and third.attempts == 3


def test_permanent_failure_after_max_attempts(h: Harness) -> None:
    h.unreachable("S1")
    p = h.proposal("notify_driver_restart_cooling")
    # The precondition would stop it at the gate, so make the driver unreachable after approval.
    h.world.events.clear()
    h.submit_and_enqueue(p)
    h.unreachable("S1")
    now = T0
    statuses = []
    for _ in range(3):
        (r,) = h.executor.run_once(now)
        statuses.append(r.status)
        now = r.next_attempt_at or now
    assert statuses == [ActionStatus.QUEUED, ActionStatus.QUEUED, ActionStatus.FAILED]
    assert h.executor.actions.all()[0].last_error == "driver_unreachable"


def test_timeouts_and_partial_responses_are_retried_safely(h: Harness) -> None:
    h.faults.queue("ticketing.create", Fault(mode="slow", latency_ms=10_000), Fault(mode="partial"))
    h.submit_and_enqueue(h.proposal("create_incident_ticket"))
    now = T0
    for _ in range(3):
        (r,) = h.executor.run_once(now)
        now = r.next_attempt_at or now
    assert r.status is ActionStatus.SUCCEEDED
    tickets = [e for e in h.world.log if e["service"] == "ticketing"]
    assert len(tickets) == 1  # the partial reply's ticket was not created twice


def test_business_rejection_is_not_retried(h: Harness) -> None:
    p = h.proposal("divert_to_cold_storage", eta=90.0)
    h.gate.submit(p, T0)
    d = h.gate.decide(h.approval(p), T0)
    h.executor.enqueue(d, T0)
    h.facility_full("CS-VENTURA")  # fills up after approval
    (r,) = h.executor.run_once(T0)
    assert r.status is ActionStatus.FAILED and r.last_error == "facility_full" and r.attempts == 1


def test_cancel_a_queued_action(h: Harness) -> None:
    h.submit_and_enqueue(h.proposal("create_incident_ticket"))
    (queued,) = h.executor.actions.all()
    cancelled = h.executor.cancel(queued.action_id, "dispatcher:alice", T0)
    assert cancelled.status is ActionStatus.CANCELLED
    assert h.executor.run_once(T0) == []
    assert h.world.log == []


def test_cancel_a_succeeded_reversible_action_compensates(h: Harness) -> None:
    p = h.proposal("divert_to_cold_storage", eta=90.0)
    h.gate.submit(p, T0)
    h.executor.enqueue(h.gate.decide(h.approval(p), T0), T0)
    (done,) = h.executor.run_once(T0)
    cancelled = h.executor.cancel(done.action_id, "dispatcher:alice", T0)
    assert cancelled.status is ActionStatus.CANCELLED
    assert cancelled.result["compensation"]["status"] == "cancelled"
    assert h.world.reservations["CS-VENTURA"] == set()


def test_irreversible_succeeded_action_cannot_be_cancelled(h: Harness) -> None:
    p = h.proposal("escalate_tier2_manager")
    h.gate.submit(p, T0)
    h.executor.enqueue(h.gate.decide(h.approval(p), T0), T0)
    (done,) = h.executor.run_once(T0)
    with pytest.raises(ExecutionRefused, match="not reversible"):
        h.executor.cancel(done.action_id, "dispatcher:alice", T0)


def test_enqueue_refuses_anything_but_execute(h: Harness) -> None:
    p = h.proposal("divert_to_cold_storage", eta=90.0)
    d = h.gate.submit(p, T0)
    assert d.outcome is GateOutcome.AWAIT_APPROVAL
    with pytest.raises(ExecutionRefused):
        h.executor.enqueue(d, T0)
