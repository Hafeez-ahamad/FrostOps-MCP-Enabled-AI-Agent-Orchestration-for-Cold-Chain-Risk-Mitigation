"""Phase 3 on SQL Server: the gate, approvals, executor and audit chain against real tables.

Runs as the admin login for now; Phase 7 gives the gate and executor their own logins with the
grants of spec section 10, and adds the permission tests.
"""

import pytest
from sqlalchemy import Engine

from engine.actions.models import ActionStatus, GateOutcome
from engine.actions.policy import load_action_catalog
from engine.audit.log import AuditLog, verify_chain
from engine.executor.executor import Executor
from engine.gate.gate import PolicyGate
from sim.mock_services.faults import FaultInjector
from sim.mock_services.state import MockWorld
from sim.mock_services.wiring import in_process_integrations
from tests.unit.harness import T0, Harness

pytestmark = pytest.mark.integration


def test_l2_and_approved_l3_run_on_sql_server_with_a_valid_audit_chain(admin: Engine) -> None:
    world = MockWorld(now=T0)
    integrations = in_process_integrations(world, FaultInjector())
    catalog = load_action_catalog()
    h = Harness(
        admin,
        world,
        FaultInjector(),
        integrations,
        catalog,
        PolicyGate(admin, catalog, integrations),
        Executor(admin, catalog, integrations),
    )

    ticket = h.gate.submit(h.proposal("create_incident_ticket", incident="INC-SQL-01"), T0)
    assert ticket.outcome is GateOutcome.EXECUTE
    h.executor.enqueue(ticket, T0)

    divert = h.proposal("divert_to_cold_storage", incident="INC-SQL-02", eta=90.0)
    assert h.gate.submit(divert, T0).outcome is GateOutcome.AWAIT_APPROVAL
    approved = h.gate.decide(h.approval(divert, approval_id="APR-SQL-1"), T0)
    h.executor.enqueue(approved, T0)

    done = h.executor.run_once(T0)
    assert {a.status for a in done} == {ActionStatus.SUCCEEDED}
    assert verify_chain(AuditLog(admin).entries())
