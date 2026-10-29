"""The five safety invariants of spec section 4, one named test each (plus close variants).

1. No action runs unless the policy gate approves it.
2. L3/L4 actions never execute without a recorded human approval/identity.
3. The same incident never triggers the same action twice.
4. The kill switch halts execution within one polling interval.
5. Data-quality failures block action.
"""

from datetime import timedelta

import pytest

from engine.actions.models import ActionRecord, ActionStatus, GateOutcome
from engine.actions.policy import ActionLevel
from engine.executor.executor import ExecutionRefused
from tests.unit.harness import T0, Harness


@pytest.fixture
def h() -> Harness:
    return Harness.build()


def _sent(h: Harness) -> list[dict[str, object]]:
    return [e for e in h.world.log if e["service"] in ("driver", "ticketing", "tms", "escalation")]


# --- invariant 1 ----------------------------------------------------------------------------


def test_invariant_1_no_action_runs_without_gate_approval(h: Harness) -> None:
    """An action row the gate never approved is refused at execution time, and nothing is sent."""
    p = h.proposal("notify_driver_restart_cooling")
    forged = ActionRecord(
        action_id="ACT-forged",
        idempotency_key=p.idempotency_key,
        proposal_id=p.proposal_id,
        proposal_hash=p.content_hash(),
        incident_id=p.incident_id,
        shipment_id=p.shipment_id,
        action=p.action,
        target=p.target,
        level=ActionLevel.L2,
        status=ActionStatus.QUEUED,
        created_at=T0,
        updated_at=T0,
    )
    h.executor.actions.insert(forged)  # bypasses the gate entirely

    (result,) = h.executor.run_once(T0)

    assert result.status is ActionStatus.FAILED and result.last_error is not None
    assert result.last_error.startswith("refused")
    assert _sent(h) == []


def test_invariant_1_executor_rejects_a_decision_the_gate_did_not_make(h: Harness) -> None:
    p = h.proposal("notify_driver_restart_cooling")
    rejected = h.gate.submit(h.proposal("reject_load", incident="INC-S1-02"), T0)
    forged = rejected.model_copy(
        update={"outcome": GateOutcome.EXECUTE, "proposal_id": p.proposal_id}
    )
    with pytest.raises(ExecutionRefused):
        h.executor.enqueue(forged, T0)
    assert h.executor.actions.all() == []


def test_invariant_1_unknown_actions_are_denied(h: Harness) -> None:
    d = h.gate.submit(h.proposal("open_the_cargo_doors"), T0)
    assert d.outcome is GateOutcome.REJECTED and d.reasons == ("unknown_action",)


# --- invariant 2 ----------------------------------------------------------------------------


def test_invariant_2_l3_and_l4_never_execute_without_recorded_human_approval(h: Harness) -> None:
    l3 = h.proposal("divert_to_cold_storage", eta=90.0)
    d = h.gate.submit(l3, T0)
    assert d.outcome is GateOutcome.AWAIT_APPROVAL
    with pytest.raises(ExecutionRefused):
        h.executor.enqueue(d.model_copy(update={"outcome": GateOutcome.EXECUTE}), T0)
    h.executor.run_once(T0 + timedelta(minutes=5))
    assert _sent(h) == []

    l4 = h.gate.submit(h.proposal("reject_load", incident="INC-S1-02"), T0)
    assert l4.outcome is GateOutcome.REJECTED and "never_autonomous" in l4.reasons


def test_invariant_2_l4_stays_blocked_even_with_an_approval(h: Harness) -> None:
    p = h.proposal("reject_load")
    h.gate.submit(p, T0)
    d = h.gate.decide(h.approval(p, role="supervisor"), T0)
    assert d.outcome is GateOutcome.REJECTED
    assert h.executor.actions.all() == []


def test_invariant_2_approval_is_bound_to_the_exact_proposal_hash(h: Harness) -> None:
    p = h.proposal("divert_to_cold_storage", eta=90.0)
    h.gate.submit(p, T0)
    d = h.gate.decide(h.approval(p, proposal_hash="f" * 64), T0)
    assert d.outcome is GateOutcome.REJECTED and "proposal_hash_mismatch" in d.reasons


def test_invariant_2_a_reused_approval_is_rejected(h: Harness) -> None:
    first = h.proposal("divert_to_cold_storage", eta=90.0)
    second = h.proposal(
        "divert_to_cold_storage", eta=90.0, incident="INC-S1-02", target="CS-COMMERCE"
    )
    h.gate.submit(first, T0)
    h.gate.submit(second, T0)
    approval = h.approval(first, approval_id="APR-1")
    assert h.gate.decide(approval, T0).outcome is GateOutcome.EXECUTE
    replay = approval.model_copy(
        update={"proposal_id": second.proposal_id, "proposal_hash": second.content_hash()}
    )
    d = h.gate.decide(replay, T0)
    assert d.outcome is GateOutcome.REJECTED and "approval_reused" in d.reasons
    again = h.gate.decide(h.approval(first, approval_id="APR-2"), T0)
    assert again.outcome is GateOutcome.REJECTED


def test_invariant_2_an_old_approval_is_rejected(h: Harness) -> None:
    p = h.proposal("divert_to_cold_storage", eta=90.0)
    h.gate.submit(p, T0)
    late = T0 + timedelta(minutes=h.catalog.global_limits.approval_ttl_minutes + 1)
    d = h.gate.decide(h.approval(p, at=T0), late)
    assert d.outcome is GateOutcome.REJECTED and "stale_or_future_approval" in d.reasons


def test_invariant_2_approver_needs_an_allowed_role(h: Harness) -> None:
    p = h.proposal("escalate_tier2_manager")  # approver_roles: [dispatcher]
    h.gate.submit(p, T0)
    d = h.gate.decide(h.approval(p, role="auditor"), T0)
    assert d.outcome is GateOutcome.REJECTED and "approver_role_not_allowed" in d.reasons


def test_invariant_2_approved_l3_records_who_approved_it(h: Harness) -> None:
    p = h.proposal("divert_to_cold_storage", eta=90.0)
    h.gate.submit(p, T0)
    d = h.gate.decide(h.approval(p, approver="alice"), T0)
    h.executor.enqueue(d, T0)
    (done,) = h.executor.run_once(T0)
    assert done.status is ActionStatus.SUCCEEDED
    stored = h.gate.approvals.for_proposal(p.proposal_id)
    assert stored is not None and stored["ApproverID"] == "alice"
    assert stored["ProposalHash"] == p.content_hash()


# --- invariant 3 ----------------------------------------------------------------------------


def test_invariant_3_same_incident_never_triggers_the_same_action_twice(h: Harness) -> None:
    p = h.proposal("create_incident_ticket")
    first = h.submit_and_enqueue(p)
    second = h.gate.submit(
        p.model_copy(update={"rationale": "resubmitted"}), T0 + timedelta(minutes=1)
    )
    assert first.outcome is GateOutcome.EXECUTE
    assert second.outcome is GateOutcome.DUPLICATE
    h.executor.enqueue(first, T0)  # enqueueing the same decision again is a no-op
    h.executor.run_once(T0)
    h.executor.run_once(T0 + timedelta(minutes=10))
    assert len(h.executor.actions.all()) == 1
    assert len([e for e in h.world.log if e["service"] == "ticketing"]) == 1


def test_invariant_3_cooldown_blocks_a_repeat_for_a_new_incident(h: Harness) -> None:
    h.submit_and_enqueue(h.proposal("notify_driver_restart_cooling"))
    again = h.gate.submit(
        h.proposal("notify_driver_restart_cooling", incident="INC-S1-02"), T0 + timedelta(minutes=5)
    )
    assert again.outcome is GateOutcome.REJECTED and "cooldown" in again.reasons
    later = h.gate.submit(
        h.proposal("notify_driver_restart_cooling", incident="INC-S1-03"),
        T0 + timedelta(minutes=11),
    )
    assert later.outcome is GateOutcome.EXECUTE


# --- invariant 4 ----------------------------------------------------------------------------


def test_invariant_4_kill_switch_halts_execution_within_one_polling_interval(h: Harness) -> None:
    for n in range(3):
        h.submit_and_enqueue(
            h.proposal("create_incident_ticket", incident=f"INC-S{n}-01", shipment=f"S{n}")
        )
    h.gate.set_control("kill_switch", True, "supervisor:bob", T0)

    assert h.executor.run_once(T0 + timedelta(minutes=5)) == []  # the very next poll
    assert _sent(h) == []
    assert all(a.status is ActionStatus.QUEUED for a in h.executor.actions.all())
    blocked = h.gate.submit(
        h.proposal("create_incident_ticket", incident="INC-S9-01", shipment="S9"), T0
    )
    assert blocked.outcome is GateOutcome.REJECTED and "kill_switch" in blocked.reasons

    h.gate.set_control("kill_switch", False, "supervisor:bob", T0 + timedelta(minutes=6))
    resumed = h.executor.run_once(T0 + timedelta(minutes=10))
    assert {a.status for a in resumed} == {ActionStatus.SUCCEEDED}


def test_invariant_4_kill_switch_stops_mid_poll(h: Harness) -> None:
    for n in range(3):
        h.submit_and_enqueue(
            h.proposal("create_incident_ticket", incident=f"INC-S{n}-01", shipment=f"S{n}")
        )
    real_call = h.executor._call
    calls = []

    def call_then_kill(record):  # type: ignore[no-untyped-def]
        calls.append(record.action_id)
        h.gate.controls.set("kill_switch", True, "supervisor:bob", T0)
        return real_call(record)

    h.executor._call = call_then_kill  # type: ignore[method-assign]
    h.executor.run_once(T0)
    assert len(calls) == 1


# --- invariant 5 ----------------------------------------------------------------------------


@pytest.mark.parametrize("dq", ["suspect", "invalid"])
def test_invariant_5_data_quality_failure_blocks_action(h: Harness, dq: str) -> None:
    d = h.gate.submit(h.proposal("notify_driver_restart_cooling", dq=dq), T0)
    assert d.outcome is GateOutcome.REJECTED
    assert any(r.startswith("precondition:data_quality_ok") for r in d.reasons)
    assert h.executor.actions.all() == []


def test_invariant_5_claimed_evidence_is_checked_against_the_database(h: Harness) -> None:
    """A planner that claims 'valid' evidence the detection pipeline never recorded is refused."""
    d = h.gate.submit(h.proposal("notify_driver_restart_cooling", record=False), T0)
    assert d.outcome is GateOutcome.REJECTED
    assert any(r.startswith("precondition:rule_breach_confirmed") for r in d.reasons)


def test_invariant_5_holds_even_if_the_policy_forgets_data_quality(h: Harness) -> None:
    policy = h.catalog.actions["create_incident_ticket"]
    trimmed = policy.model_copy(update={"preconditions": ("rule_breach_confirmed",)})
    catalog = h.catalog.model_copy(
        update={"actions": {**h.catalog.actions, "create_incident_ticket": trimmed}}
    )
    gate = type(h.gate)(h.engine, catalog, h.integrations)
    d = gate.submit(h.proposal("create_incident_ticket", dq="suspect"), T0)
    assert d.outcome is GateOutcome.REJECTED
    assert any(r.startswith("precondition:data_quality_ok") for r in d.reasons)


def test_invariant_5_stale_telemetry_blocks_l2(h: Harness) -> None:
    from sqlalchemy import insert

    from engine.data.schema import DATA_QUALITY_FLAGS

    with h.engine.begin() as conn:
        conn.execute(
            insert(DATA_QUALITY_FLAGS),
            {
                "ShipmentID": "S1",
                "MsgSeqNo": None,
                "CheckName": "freshness",
                "DqStatus": "invalid",
                "Detail": "no reading for 20 min",
                "EvaluatedAt": T0 + timedelta(minutes=20),
            },
        )
    d = h.gate.submit(h.proposal("notify_driver_restart_cooling"), T0 + timedelta(minutes=25))
    assert d.outcome is GateOutcome.REJECTED
    assert any("stale" in r for r in d.reasons)
