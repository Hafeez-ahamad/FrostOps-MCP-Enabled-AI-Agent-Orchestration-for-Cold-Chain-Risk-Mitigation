"""Single agent (configuration C1): triage and plan choice in one LLM call, with everything the
monitor and planner see combined in one prompt (no separation between roles)."""

from datetime import datetime

from pydantic import BaseModel, Field

from engine.agents.context import (
    ReadingPoint,
    candidates_text,
    data_block,
    facilities_text,
    incident_text,
    sop_text,
    telemetry_text,
)
from engine.agents.monitor import Verdict
from engine.incidents.manager import Incident
from engine.integrations.clients import FacilityStatus
from engine.llm.structured import StructuredLLM
from engine.planner.planner import Plan
from engine.prompts import load_prompt
from engine.sop.retriever import SopClause


class SingleAgentDecision(BaseModel, frozen=True):
    verdict: Verdict
    confidence: float = Field(ge=0.0, le=1.0)
    choice: int = Field(ge=0)
    reasons: str = Field(default="", max_length=600)


def decide(
    llm: StructuredLLM | None,
    plan: Plan,
    incident: Incident,
    window: list[ReadingPoint],
    facilities: list[FacilityStatus] | None,
    sop: list[SopClause],
    *,
    correlation_id: str,
    at: datetime,
) -> SingleAgentDecision | None:
    if llm is None or not plan.candidates:
        return None
    user = "\n".join(
        [
            data_block("incident", incident_text(incident)),
            data_block("telemetry", telemetry_text(window)),
            data_block("candidates", candidates_text(plan)),
            data_block("facilities", facilities_text(facilities)),
            data_block("sop_clause", sop_text(sop)),
        ]
    )
    result = llm.ask(
        "single_agent",
        load_prompt("single_agent"),
        user,
        SingleAgentDecision,
        correlation_id=correlation_id,
        at=at,
    )
    if result.parsed is None or result.parsed.choice >= len(plan.candidates):
        return None
    return result.parsed
