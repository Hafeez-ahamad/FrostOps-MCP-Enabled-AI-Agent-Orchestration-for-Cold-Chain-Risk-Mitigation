"""Per-agent database identities (spec section 10, db/roles.sql) against a real SQL Server.

Every agent role has its own login. These tests try what each role must not be able to do and
expect SQL Server itself to refuse it (permission denied), so the boundary does not depend on
the Python code behaving.
"""

from datetime import timedelta

import pytest
from sqlalchemy import Engine, delete, insert, select, text, update
from sqlalchemy.exc import DBAPIError

from engine.actions.policy import ActionLevel
from engine.data.schema import ACTIONS, AUDIT_LOG, INCIDENTS, LATEST_VALID_READING, PROPOSALS
from engine.db import Login, build_engine
from engine.ops_store import ProposalStore
from engine.runtime import RoleEngines, Runtime
from engine.settings import Settings
from sim.generator import SimulationOutput
from sim.mock_services.faults import FaultInjector
from sim.mock_services.state import MockWorld
from sim.mock_services.wiring import in_process_integrations
from sim.scenario import Scenario
from tests.unit.harness import T0, Harness

pytestmark = pytest.mark.integration

AGENT_LOGINS: tuple[Login, ...] = ("monitor", "planner", "gate", "executor")
EVERY_LOGIN: tuple[Login, ...] = (*AGENT_LOGINS, "agent", "ingest", "console")
DENIED = r"(?i)permission was denied|denied"


@pytest.fixture(scope="module")
def logins(db_settings: Settings, loaded: SimulationOutput) -> dict[Login, Engine]:
    return {login: build_engine(db_settings, login) for login in EVERY_LOGIN}


def _action_row() -> dict[str, object]:
    return {
        "ActionID": "ACT-FORGED",
        "IdempotencyKey": "forged",
        "ProposalID": "PRP-FORGED",
        "ProposalHash": "0" * 64,
        "IncidentID": "INC-FORGED",
        "ShipmentID": "S1",
        "Action": "divert_to_cold_storage",
        "Target": "CS-VENTURA",
        "Level": "L3",
        "Status": "queued",
        "Attempts": 0,
        "Result": "{}",
        "CreatedAt": T0,
        "UpdatedAt": T0,
    }


def test_planner_cannot_insert_actions(logins: dict[Login, Engine]) -> None:
    with pytest.raises(DBAPIError, match=DENIED), logins["planner"].begin() as conn:
        conn.execute(insert(ACTIONS), _action_row())


def test_planner_cannot_write_proposals_except_drafts_through_the_procedure(
    logins: dict[Login, Engine], admin: Engine
) -> None:
    proposal = Harness.build().proposal("create_incident_ticket", incident="INC-ROLE-01")
    row = {
        "ProposalID": proposal.proposal_id,
        "ProposalHash": proposal.content_hash(),
        "IncidentID": proposal.incident_id,
        "ShipmentID": proposal.shipment_id,
        "DepotID": proposal.depot_id,
        "Action": proposal.action,
        "Target": proposal.target,
        "Level": "L2",
        "Status": "approved",
        "Payload": proposal.model_dump_json(),
        "CreatedAt": T0,
        "UpdatedAt": T0,
    }
    with pytest.raises(DBAPIError, match=DENIED), logins["planner"].begin() as conn:
        conn.execute(insert(PROPOSALS), row)

    ProposalStore(logins["planner"]).insert_draft(proposal, ActionLevel.L2, T0)
    with admin.connect() as conn:
        status = conn.execute(
            select(PROPOSALS.c.Status).where(PROPOSALS.c.ProposalID == proposal.proposal_id)
        ).scalar_one()
    assert status == "draft"  # the procedure decides the status, not the caller

    with pytest.raises(DBAPIError, match=DENIED), logins["planner"].begin() as conn:
        conn.execute(
            update(PROPOSALS)
            .where(PROPOSALS.c.ProposalID == proposal.proposal_id)
            .values(Status="approved")
        )


def test_executor_cannot_read_raw_telemetry(logins: dict[Login, Engine]) -> None:
    for query in (
        text("SELECT TOP 1 * FROM dbo.RFR_TELEM_LOG"),
        select(LATEST_VALID_READING).limit(1),
        text("SELECT TOP 1 * FROM ops.RuleHits"),
        text("SELECT TOP 1 * FROM ops.DataQualityFlags"),
    ):
        with pytest.raises(DBAPIError, match=DENIED), logins["executor"].connect() as conn:
            conn.execute(query)


def test_executor_cannot_approve_its_own_proposals(logins: dict[Login, Engine]) -> None:
    with pytest.raises(DBAPIError, match=DENIED), logins["executor"].begin() as conn:
        conn.execute(update(PROPOSALS).values(Status="approved"))


def test_explainer_cannot_write(logins: dict[Login, Engine]) -> None:
    explainer = logins["agent"]
    with explainer.connect() as conn:  # it can read what it explains
        conn.execute(select(INCIDENTS).limit(1)).all()
    for statement in (
        insert(ACTIONS).values(_action_row()),
        update(INCIDENTS).values(Status="cleared"),
        delete(PROPOSALS),
        text(
            "INSERT INTO ops.ControlFlags (Name, Value, UpdatedBy, UpdatedAt) "
            "VALUES ('kill_switch', 0, 'x', SYSDATETIME())"
        ),  # fmt: skip
    ):
        with pytest.raises(DBAPIError, match=DENIED), explainer.begin() as conn:
            conn.execute(statement)


@pytest.mark.parametrize("login", EVERY_LOGIN)
def test_nobody_can_update_or_delete_the_audit_log(
    logins: dict[Login, Engine], login: Login
) -> None:
    for statement in (update(AUDIT_LOG).values(Actor="someone else"), delete(AUDIT_LOG)):
        with pytest.raises(DBAPIError, match=DENIED), logins[login].begin() as conn:
            conn.execute(statement)


def test_the_engine_runs_with_one_login_per_role(
    logins: dict[Login, Engine],
    smoke: SimulationOutput,
    smoke_scenario: Scenario,
) -> None:
    """The first hours of the smoke day with every role on its own login."""
    engines = RoleEngines(
        monitor=logins["monitor"],
        planner=logins["planner"],
        gate=logins["gate"],
        executor=logins["executor"],
        explainer=logins["agent"],
    )
    world = MockWorld(now=smoke_scenario.start, events=smoke.events)
    runtime = Runtime.build(engines, in_process_integrations(world, FaultInjector()))
    end = smoke_scenario.start + timedelta(hours=10)
    cutoff = end.strftime("%Y-%m-%d %H:%M:%S")
    rows = [r for r in smoke.readings.to_dict("records") if str(r["RCPT_DTTM"]) <= cutoff]

    def on_tick(t: object) -> None:
        world.now = t  # type: ignore[assignment]

    runtime.run(rows, timedelta(minutes=5), end, on_tick=on_tick)  # type: ignore[arg-type]
    assert not runtime.alert_only
    assert any(a.status.value == "succeeded" for a in runtime.executor.actions.all())
