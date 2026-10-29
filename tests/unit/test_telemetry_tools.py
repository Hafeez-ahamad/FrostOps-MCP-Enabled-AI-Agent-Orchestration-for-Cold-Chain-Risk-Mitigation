import json
from datetime import datetime

import pytest
from langchain_core.messages import AIMessage
from pydantic import SecretStr
from sqlalchemy.dialects import mssql

from engine.chat.graph import build_chat_graph
from engine.chat.stream import run_chat_turn
from engine.chat.tools import build_chat_tools
from engine.db import odbc_connection_string
from engine.tools import telemetry
from tests.fakes import ScriptedChatModel, fleet_row, sqlite_fleet_engine


def test_snapshot_returns_latest_first_and_caps_rows() -> None:
    rows = [
        fleet_row(ShipmentID=f"S-{h:02d}", ReadingTs=datetime(2026, 1, 1, h)) for h in range(24)
    ]
    engine = sqlite_fleet_engine(rows)

    result = telemetry.fleet_snapshot(engine, limit=500)

    assert len(result) == telemetry.MAX_ROWS
    assert result[0]["ShipmentID"] == "S-23"


def test_snapshot_skips_arrived_shipments() -> None:
    engine = sqlite_fleet_engine(
        [fleet_row(ShipmentID="EN-ROUTE"), fleet_row(ShipmentID="DONE", TripStatus="A")]
    )
    assert [r["ShipmentID"] for r in telemetry.fleet_snapshot(engine)] == ["EN-ROUTE"]


def test_readings_near_filters_by_box() -> None:
    engine = sqlite_fleet_engine(
        [
            fleet_row(ShipmentID="A", Latitude=33.8, Longitude=-118.1),
            fleet_row(ShipmentID="B", Latitude=32.7, Longitude=-117.1),
        ]
    )
    result = telemetry.readings_near(engine, 33.8, -118.1, radius_deg=0.5)
    assert [r["Latitude"] for r in result] == [33.8]


@pytest.mark.parametrize(
    ("lat", "lon", "radius", "limit"),
    [(91, 0, 0.5, 5), (0, 181, 0.5, 5), (0, 0, 0, 5), (0, 0, 6, 5), (0, 0, 0.5, 0)],
)
def test_invalid_parameters_rejected(lat: float, lon: float, radius: float, limit: int) -> None:
    engine = sqlite_fleet_engine([])
    with pytest.raises(ValueError):
        telemetry.readings_near(engine, lat, lon, radius, limit)


def test_queries_are_parameterised_and_capped_on_sql_server() -> None:
    query = telemetry.select(telemetry.LATEST_VALID_READING).limit(telemetry.MAX_ROWS)
    sql = str(query.compile(dialect=mssql.dialect()))
    assert "TOP" in sql
    assert "rpt" in sql


def test_non_numeric_coordinates_are_rejected() -> None:
    engine = sqlite_fleet_engine([fleet_row()])
    with pytest.raises(TypeError):
        telemetry.readings_near(engine, "33.8; DROP TABLE x", -118.1)  # type: ignore[arg-type]


def test_connection_targets_coldchaindb_not_master(settings) -> None:  # type: ignore[no-untyped-def]
    password = SecretStr("p;w}d")
    conn = odbc_connection_string(settings.model_copy(update={"sql_agent_password": password}))
    assert "DATABASE=ColdChainDB;" in conn
    assert "master" not in conn
    assert "PWD={p;w}}d};" in conn


def test_missing_password_is_a_clear_error(settings) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(RuntimeError, match="SQL_AGENT_PASSWORD"):
        odbc_connection_string(settings)


def test_tool_reports_unavailable_db_instead_of_crashing(settings) -> None:  # type: ignore[no-untyped-def]
    tools = {t.name: t for t in build_chat_tools(settings)}  # no password set
    output = tools["get_fleet_snapshot"].invoke({"limit": 3})
    assert "Telemetry unavailable" in output


def test_end_to_end_tool_call_through_the_graph(settings) -> None:  # type: ignore[no-untyped-def]
    engine = sqlite_fleet_engine([fleet_row(TemperatureC=6.2)])
    tools = build_chat_tools(settings, engine_factory=lambda: engine)
    call = {"name": "get_fleet_snapshot", "args": {"limit": 1}, "id": "c1", "type": "tool_call"}
    llm = ScriptedChatModel(
        replies=[AIMessage(content="", tool_calls=[call]), AIMessage(content="Breach suspected.")]
    )
    agent = build_chat_graph(llm, tools, system_prompt="sys")

    events = list(run_chat_turn(agent, "status?", thread_id="t"))

    assert [e.kind for e in events] == ["tool_call", "tool_result", "answer"]
    assert json.loads(events[0].content) == {"limit": 1}
    assert "6.2" in events[1].content and events[1].content.startswith("<telemetry_rows>")
    assert events[2].content == "Breach suspected."
