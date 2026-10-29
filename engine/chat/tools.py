"""LangChain tool wrappers for the chat explainer.

All dependencies (DB engine, SOP retriever, weather source) are injected or built lazily on
first use, so building the tools never connects to anything or needs a key.
"""

from collections.abc import Callable

from langchain_core.tools import BaseTool, tool
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from engine.db import build_agent_engine
from engine.settings import Settings
from engine.sop.retriever import SopRetriever, build_sop_retriever
from engine.tools import telemetry
from engine.tools.corridor import WeatherFetch, get_corridor_conditions, open_meteo_fetch


def build_chat_tools(
    settings: Settings,
    *,
    engine_factory: Callable[[], Engine] | None = None,
    retriever: SopRetriever | None = None,
    weather_fetch: WeatherFetch | None = None,
) -> list[BaseTool]:
    make_engine = engine_factory or (lambda: build_agent_engine(settings))
    if weather_fetch is None and settings.corridor_live_enabled:
        weather_fetch = open_meteo_fetch
    # Built on first use and then reused.
    engines: list[Engine] = []
    retrievers: list[SopRetriever] = [retriever] if retriever is not None else []

    def db() -> Engine:
        if not engines:
            engines.append(make_engine())
        return engines[0]

    def sop() -> SopRetriever:
        if not retrievers:
            retrievers.append(build_sop_retriever(settings))
        return retrievers[0]

    def run_query(query: Callable[[], list[dict[str, object]]]) -> str:
        try:
            return telemetry.format_rows(query())
        except ValueError as exc:
            return f"<telemetry_rows>\nInvalid request: {exc}\n</telemetry_rows>"
        except (SQLAlchemyError, RuntimeError) as exc:
            return (
                "<telemetry_rows>\nTelemetry unavailable "
                f"({type(exc).__name__}).\n</telemetry_rows>"
            )

    @tool
    def get_fleet_snapshot(limit: int = 10) -> str:
        """Return the latest data-quality-valid reading of each shipment still en route,
        most recent first (max 20 rows). Columns: ShipmentID, VehicleID, DepotID, CargoType,
        MsgSeqNo, ReadingTs, Latitude, Longitude, TemperatureC, CargoConditionCode,
        RiskClassification, DelayProbability, PortCongestionLevel, RouteRiskIndex,
        EtaDelayMin, TripStatus."""
        return run_query(lambda: telemetry.fleet_snapshot(db(), limit))

    @tool
    def find_readings_near(
        latitude: float, longitude: float, radius_deg: float = 0.5, limit: int = 10
    ) -> str:
        """Return en-route shipments whose latest valid reading lies within radius_deg degrees
        (max 5.0) of a latitude/longitude."""
        return run_query(
            lambda: telemetry.readings_near(db(), latitude, longitude, radius_deg, limit)
        )

    @tool("get_corridor_conditions")
    def corridor_conditions(latitude: float, longitude: float) -> str:
        """Return ambient temperature and wind speed for a location. Weather is not port
        congestion."""
        return get_corridor_conditions(latitude, longitude, weather_fetch).as_text()

    @tool
    def search_compliance_sop(query: str) -> str:
        """Search the cold-chain SOP and return matching clauses with their clause numbers."""
        clauses = sop().search(query, k=2)
        if not clauses:
            return "<sop_clause>\nNo matching SOP clause.\n</sop_clause>"
        return "\n".join(
            f'<sop_clause id="{c.clause_id}" source="{c.source_file}">\n'
            f"{c.clause_id} {c.title}\n{c.text}\n</sop_clause>"
            for c in clauses
        )

    return [
        get_fleet_snapshot,
        find_readings_near,
        corridor_conditions,
        search_compliance_sop,
    ]
