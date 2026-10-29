"""In-memory SQLite with the same schemas as SQL Server, for offline tests, demos and replay.

The dbo, ops and rpt schemas are attached databases, so the same SQLAlchemy tables and
queries run unchanged. The latest-valid view is created as a plain table here.
"""

from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.pool import StaticPool

from engine.data.schema import LATEST_VALID_READING, metadata


def in_memory_engine() -> Engine:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    @event.listens_for(engine, "connect")
    def _attach(dbapi_conn: Any, _record: Any) -> None:
        for schema in ("dbo", "ops", "rpt"):
            dbapi_conn.execute(f"ATTACH DATABASE ':memory:' AS {schema}")

    metadata.create_all(engine)
    LATEST_VALID_READING.create(engine)
    return engine
