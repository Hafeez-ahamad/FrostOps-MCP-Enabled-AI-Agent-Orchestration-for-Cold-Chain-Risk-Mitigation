"""Integration fixtures: a real SQL Server (Docker locally, a service container in CI).

Run with: pytest -m integration   (needs .env with the SQL passwords, and ODBC Driver 18)
The session initialises ColdChainDB with the db/ scripts, then loads the smoke scenario.
"""

from datetime import timedelta

import pytest
from sqlalchemy import Engine, text

from engine.data.ingest import ingest_readings
from engine.db import build_engine
from engine.dbadmin import init_database
from engine.dq.runner import assess_pending
from engine.settings import Settings, load_settings
from sim.generator import SimulationOutput
from sim.scenario import Scenario

pytestmark = pytest.mark.integration


@pytest.fixture(scope="session")
def db_settings() -> Settings:
    settings = load_settings()
    if settings.mssql_sa_password is None:
        pytest.fail("integration tests need MSSQL_SA_PASSWORD and the SQL_* passwords (.env)")
    return settings


@pytest.fixture(scope="session")
def admin(db_settings: Settings) -> Engine:
    init_database(db_settings)
    engine = build_engine(db_settings, "admin")
    with engine.begin() as conn:  # start every session from empty tables
        for table in (
            "ops.PolicyVersions",
            "ops.EventQueue",
            "ops.LLMCalls",
            "ops.AuditLog",
            "ops.ControlFlags",
            "ops.Actions",
            "ops.Approvals",
            "ops.Proposals",
            "ops.Incidents",
            "ops.RuleHits",
            "ops.DataQualityFlags",
            "ops.ReadingQuality",
            "dbo.RFR_TELEM_LOG",
        ):
            conn.execute(text(f"DELETE FROM {table}"))
    return engine


@pytest.fixture(scope="session")
def ingest(db_settings: Settings, admin: Engine) -> Engine:
    return build_engine(db_settings, "ingest", fast_executemany=True)


@pytest.fixture(scope="session")
def loaded(ingest: Engine, smoke: SimulationOutput, smoke_scenario: Scenario) -> SimulationOutput:
    ingest_readings(ingest, smoke.readings)
    end = smoke_scenario.start + timedelta(hours=smoke_scenario.duration_hours)
    assess_pending(ingest, now=end)
    return smoke


@pytest.fixture(scope="session")
def agent(db_settings: Settings, loaded: SimulationOutput) -> Engine:
    return build_engine(db_settings, "agent")


@pytest.fixture(scope="session")
def console(db_settings: Settings, loaded: SimulationOutput) -> Engine:
    return build_engine(db_settings, "console")
