"""Test doubles: a scripted chat model and in-memory SQLite databases."""

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field
from sqlalchemy import Engine, insert

from engine.data.memory_db import in_memory_engine
from engine.data.schema import LATEST_VALID_READING


class ScriptedChatModel(BaseChatModel):
    """Returns pre-written replies in order and records every list of messages it was sent."""

    replies: list[AIMessage] = Field(default_factory=list)
    calls: list[list[BaseMessage]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted-fake"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.calls.append(list(messages))
        reply = self.replies.pop(0) if self.replies else AIMessage(content="ok")
        return ChatResult(generations=[ChatGeneration(message=reply)])

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> "ScriptedChatModel":  # type: ignore[override]
        return self


def sqlite_engine() -> Engine:
    """In-memory SQLite with dbo, ops and rpt attached and every table created."""
    return in_memory_engine()


def sqlite_fleet_engine(rows: list[dict[str, Any]]) -> Engine:
    """SQLite engine whose latest-valid view holds `rows`."""
    engine = sqlite_engine()
    if rows:
        with engine.begin() as conn:
            conn.execute(insert(LATEST_VALID_READING), rows)
    return engine


def fleet_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "ShipmentID": "S0001-00001",
        "VehicleID": "VEH-001",
        "DepotID": "DEP-LB",
        "CargoType": "FRESH",
        "MsgSeqNo": 1,
        "ReadingTs": datetime(2026, 1, 1, 0, 0),
        "Latitude": 33.8,
        "Longitude": -118.2,
        "TemperatureC": 3.5,
        "CargoConditionCode": 1.0,
        "RiskClassification": "Low Risk",
        "DelayProbability": 0.2,
        "PortCongestionLevel": None,
        "RouteRiskIndex": 2.0,
        "EtaDelayMin": 0.0,
        "TripStatus": "E",
    }
    row.update(overrides)
    return row
