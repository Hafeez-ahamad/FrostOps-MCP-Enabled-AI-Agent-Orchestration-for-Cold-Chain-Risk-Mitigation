"""Phase 1 against SQL Server: re-runnable init, append+dedup, the latest-valid view, depot RLS,
view-plus-DENY, and agreement between db/*.sql and engine/data/schema.py."""

from datetime import datetime

import pytest
from sqlalchemy import Engine, func, inspect, select, text
from sqlalchemy.exc import DBAPIError

from engine.data.ingest import ingest_readings
from engine.data.latest import latest_valid_readings
from engine.data.schema import (
    ACTIONS,
    APPROVALS,
    AUDIT_LOG,
    CONTROL_FLAGS,
    DATA_QUALITY_FLAGS,
    EVENT_QUEUE,
    INCIDENTS,
    LATEST_VALID_READING,
    LLM_CALLS,
    PROPOSALS,
    RAW_TABLE,
    READING_QUALITY,
    RULE_HITS,
)
from engine.db import set_depot_scope
from engine.dbadmin import init_database
from engine.settings import Settings
from sim.generator import SimulationOutput

pytestmark = pytest.mark.integration


def _count(engine: Engine, table: object) -> int:
    with engine.connect() as conn:
        return int(conn.execute(select(func.count()).select_from(table)).scalar_one())  # type: ignore[arg-type]


def test_db_init_can_run_twice(
    db_settings: Settings, loaded: SimulationOutput, admin: Engine
) -> None:
    before = _count(admin, RAW_TABLE)
    init_database(db_settings)
    assert _count(admin, RAW_TABLE) == before  # nothing dropped or recreated


def test_ingest_twice_inserts_nothing(
    ingest: Engine, loaded: SimulationOutput, admin: Engine
) -> None:
    before = _count(admin, RAW_TABLE)
    assert ingest_readings(ingest, loaded.readings).inserted == 0
    assert _count(admin, RAW_TABLE) == before


def test_unique_index_rejects_a_duplicate_even_without_the_dedup(
    ingest: Engine, loaded: SimulationOutput
) -> None:
    row = loaded.readings.iloc[0]
    with pytest.raises(DBAPIError), ingest.begin() as conn:
        conn.execute(
            RAW_TABLE.insert(),
            {
                "CNSG_NO": row["CNSG_NO"],
                "PKT_SEQ": int(row["PKT_SEQ"]),
                "HUB_CD": row["HUB_CD"],
            },
        )


def test_latest_valid_view_matches_the_python_reference(
    agent: Engine, admin: Engine, loaded: SimulationOutput
) -> None:
    with admin.connect() as conn:
        statuses = {
            (s, n): st
            for s, n, st in conn.execute(
                select(
                    READING_QUALITY.c.ShipmentID,
                    READING_QUALITY.c.MsgSeqNo,
                    READING_QUALITY.c.DqStatus,
                )
            )
        }
    unique = loaded.readings.drop_duplicates(["CNSG_NO", "PKT_SEQ"])
    expected = latest_valid_readings(
        unique,
        [statuses[(s, int(n))] for s, n in zip(unique["CNSG_NO"], unique["PKT_SEQ"], strict=True)],
    )
    with agent.connect() as conn:
        rows = conn.execute(
            select(LATEST_VALID_READING.c.ShipmentID, LATEST_VALID_READING.c.MsgSeqNo)
        ).all()
    assert sorted((s, int(n)) for s, n in rows) == sorted(
        zip(expected["CNSG_NO"], expected["PKT_SEQ"].astype(int), strict=True)
    )


def test_console_sees_only_its_depots(console: Engine, loaded: SimulationOutput) -> None:
    with console.connect() as conn:
        set_depot_scope(conn, ["DEP-LB"])
        depots = {d for (d,) in conn.execute(select(LATEST_VALID_READING.c.DepotID).distinct())}
    assert depots == {"DEP-LB"}


def test_console_without_a_depot_scope_sees_nothing(
    console: Engine, loaded: SimulationOutput
) -> None:
    assert _count(console, LATEST_VALID_READING) == 0


def test_depot_scope_cannot_be_changed_by_the_session(
    console: Engine, loaded: SimulationOutput
) -> None:
    with console.connect() as conn:
        set_depot_scope(conn, ["DEP-LB"])
        with pytest.raises(DBAPIError):
            set_depot_scope(conn, ["DEP-LB", "DEP-VER", "DEP-ONT", "DEP-SD", "DEP-OXN"])


def test_service_accounts_see_every_depot(agent: Engine, loaded: SimulationOutput) -> None:
    with agent.connect() as conn:
        depots = {d for (d,) in conn.execute(select(LATEST_VALID_READING.c.DepotID).distinct())}
    assert depots == set(loaded.readings["HUB_CD"])


@pytest.mark.parametrize("login", ["agent", "console"])
def test_readers_cannot_touch_the_raw_table(login: str, agent: Engine, console: Engine) -> None:
    engine = {"agent": agent, "console": console}[login]
    with pytest.raises(DBAPIError), engine.connect() as conn:
        conn.execute(select(RAW_TABLE).limit(1)).all()
    with pytest.raises(DBAPIError), engine.begin() as conn:
        conn.execute(text("DELETE FROM ops.DataQualityFlags WHERE 1 = 0"))


def test_ingest_login_cannot_delete_or_update(ingest: Engine) -> None:
    with pytest.raises(DBAPIError), ingest.begin() as conn:
        conn.execute(text("DELETE FROM dbo.RFR_TELEM_LOG WHERE 1 = 0"))
    with pytest.raises(DBAPIError), ingest.begin() as conn:
        conn.execute(text("UPDATE ops.ReadingQuality SET DqStatus = 'valid' WHERE 1 = 0"))


@pytest.mark.parametrize(
    "table",
    [
        RAW_TABLE,
        READING_QUALITY,
        DATA_QUALITY_FLAGS,
        RULE_HITS,
        INCIDENTS,
        PROPOSALS,
        APPROVALS,
        ACTIONS,
        AUDIT_LOG,
        CONTROL_FLAGS,
        LLM_CALLS,
        EVENT_QUEUE,
        LATEST_VALID_READING,
    ],
)
def test_schema_drift(admin: Engine, table: object) -> None:
    """engine/data/schema.py must match what db/*.sql created (spec section 14, schema drift)."""
    actual = {c["name"] for c in inspect(admin).get_columns(table.name, schema=table.schema)}  # type: ignore[attr-defined]
    assert actual == set(table.columns.keys())  # type: ignore[attr-defined]


def test_database_allows_only_one_open_incident_per_shipment(ingest: Engine) -> None:
    row = {
        "ShipmentID": "S-DUP",
        "Status": "open",
        "OpenedAt": datetime(2026, 3, 2),
        "LastHitAt": datetime(2026, 3, 2),
        "RuleIds": "R-TEMP-01",
        "HitCount": 1,
        "FirstSeqNo": 1,
        "LastSeqNo": 1,
        "RiskScore": 1.0,
        "UpdatedAt": datetime(2026, 3, 2),
    }
    with ingest.begin() as conn:
        conn.execute(INCIDENTS.insert(), {**row, "IncidentID": "INC-S-DUP-01"})
    with pytest.raises(DBAPIError), ingest.begin() as conn:
        conn.execute(INCIDENTS.insert(), {**row, "IncidentID": "INC-S-DUP-02"})
