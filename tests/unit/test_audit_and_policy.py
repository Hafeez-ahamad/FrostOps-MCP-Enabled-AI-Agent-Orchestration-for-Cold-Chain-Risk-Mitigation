"""Action catalogue validation, the hash-chained audit log, and fail-closed behaviour."""

from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import text, update

from engine.actions.models import ActionStatus, GateOutcome
from engine.actions.policy import ActionLevel, ActionPolicy, load_action_catalog
from engine.audit.log import AuditLog, AuditUnavailable, verify_chain
from engine.data.schema import AUDIT_LOG
from tests.fakes import sqlite_engine
from tests.unit.harness import T0, Harness


def test_catalog_matches_the_spec_minimum() -> None:
    catalog = load_action_catalog()
    levels = {name: a.level for name, a in catalog.actions.items()}
    assert sum(level is ActionLevel.L2 for level in levels.values()) >= 2
    assert sum(level is ActionLevel.L3 for level in levels.values()) >= 1
    assert levels["reject_load"] is ActionLevel.L4
    assert catalog.actions["notify_driver_restart_cooling"].cooldown_minutes == 10
    assert catalog.actions["notify_driver_restart_cooling"].max_per_shipment_per_hour == 3
    assert catalog.global_limits.max_auto_actions_per_hour == 30
    assert catalog.global_limits.kill_switch is False and catalog.global_limits.dry_run is False


def test_every_l2_action_requires_clean_data_in_the_yaml() -> None:
    for action in load_action_catalog().actions.values():
        if action.level is ActionLevel.L2:
            assert "data_quality_ok" in action.preconditions, action.name


def test_l3_without_approvers_is_invalid() -> None:
    with pytest.raises(ValidationError):
        ActionPolicy(
            name="x",
            level=ActionLevel.L3,
            reversible="no",
            preconditions=(),
            integration="ticketing",
        )


def test_preconditions_must_be_listed_explicitly(tmp_path: Path) -> None:
    bad = tmp_path / "actions.yaml"
    bad.write_text(
        "version: 1\nactions:\n  a: {level: L2, reversible: 'yes', integration: ticketing}\n"
        "global_limits: {max_auto_actions_per_hour: 1, kill_switch: false, dry_run: false,"
        " approval_ttl_minutes: 5}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="explicitly"):
        load_action_catalog(bad)


def test_policy_hash_changes_with_the_policy_text(tmp_path: Path) -> None:
    original = load_action_catalog()
    changed = tmp_path / "actions.yaml"
    from engine.actions.policy import ACTIONS_PATH

    changed.write_text(ACTIONS_PATH.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8")
    assert load_action_catalog(changed).policy_hash != original.policy_hash


# --- audit log --------------------------------------------------------------------------------


def test_audit_chain_verifies_and_detects_tampering() -> None:
    engine = sqlite_engine()
    log = AuditLog(engine)
    for n in range(3):
        log.append("event", "tester", T0 + timedelta(minutes=n), detail={"n": n})
    assert verify_chain(log.entries())
    with engine.begin() as conn:
        conn.execute(update(AUDIT_LOG).where(AUDIT_LOG.c.AuditID == 2).values(Actor="mallory"))
    assert not verify_chain(log.entries())


def test_deleting_an_audit_row_breaks_the_chain() -> None:
    engine = sqlite_engine()
    log = AuditLog(engine)
    for n in range(3):
        log.append("event", "tester", T0, detail={"n": n})
    with engine.begin() as conn:
        conn.execute(AUDIT_LOG.delete().where(AUDIT_LOG.c.AuditID == 2))
    assert not verify_chain(log.entries())


def _break_audit(h: Harness) -> None:
    with h.engine.begin() as conn:
        conn.execute(text("DROP TABLE ops.AuditLog"))


def test_audit_failure_raises_audit_unavailable() -> None:
    h = Harness.build()
    _break_audit(h)
    with pytest.raises(AuditUnavailable):
        AuditLog(h.engine).append("event", "tester", T0)


def test_gate_rejects_when_the_audit_write_fails() -> None:
    h = Harness.build()
    p = h.proposal("create_incident_ticket")
    _break_audit(h)
    d = h.gate.submit(p, T0)
    assert d.outcome is GateOutcome.REJECTED and "audit_unavailable" in d.reasons
    assert h.gate.proposals.get(p.proposal_id) is None


def test_executor_does_not_call_the_integration_when_audit_fails() -> None:
    """Fail closed: the 'executing' audit entry comes first; without it nothing is sent."""
    h = Harness.build()
    h.submit_and_enqueue(h.proposal("create_incident_ticket"))
    _break_audit(h)
    (held,) = h.executor.run_once(T0)
    assert held.status is ActionStatus.QUEUED and held.last_error == "audit_unavailable"
    assert h.world.log == []


def test_every_executed_action_is_preceded_by_its_audit_entry() -> None:
    h = Harness.build()
    h.submit_and_enqueue(h.proposal("create_incident_ticket"))
    h.executor.run_once(T0)
    events = [e["EventType"] for e in h.gate.audit.entries()]
    assert events.index("action_executing") < events.index("action_succeeded")
    assert events[:3] == ["gate_decision", "action_enqueued", "action_executing"]
    assert verify_chain(h.gate.audit.entries())


def test_control_changes_are_audited() -> None:
    h = Harness.build()
    h.gate.set_control("dry_run", True, "supervisor:bob", T0)
    entry = h.gate.audit.entries()[-1]
    assert entry["EventType"] == "dry_run_set" and entry["Actor"] == "supervisor:bob"


def test_dry_run_decides_but_never_executes() -> None:
    h = Harness.build()
    h.gate.set_control("dry_run", True, "supervisor:bob", T0)
    d = h.gate.submit(h.proposal("create_incident_ticket"), T0)
    assert d.outcome is GateOutcome.DRY_RUN
    assert h.executor.actions.all() == []


def test_global_rate_limit_caps_auto_actions_per_hour() -> None:
    h = Harness.build()
    limit = h.catalog.global_limits.max_auto_actions_per_hour
    outcomes = [
        h.submit_and_enqueue(
            h.proposal("create_incident_ticket", incident=f"INC-S{n}-01", shipment=f"S{n}")
        ).outcome
        for n in range(limit + 1)
    ]
    assert outcomes[:limit] == [GateOutcome.EXECUTE] * limit
    assert outcomes[-1] is GateOutcome.REJECTED
