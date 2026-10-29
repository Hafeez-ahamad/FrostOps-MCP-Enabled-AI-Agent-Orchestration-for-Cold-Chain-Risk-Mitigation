"""Planner agent: picks among the deterministic planner's candidates when they are close.

It sees the incident, the candidates, facility capacity and the SOP (no raw telemetry). It can
only return an index into the candidate list; an out-of-range index counts as no answer.
"""

from datetime import datetime

from pydantic import BaseModel, Field

from engine.agents.context import (
    candidates_text,
    data_block,
    facilities_text,
    incident_text,
    sop_text,
)
from engine.incidents.manager import Incident
from engine.integrations.clients import FacilityStatus
from engine.llm.structured import StructuredLLM
from engine.planner.planner import Plan
from engine.prompts import load_prompt
from engine.sop.retriever import SopClause


class PlanChoice(BaseModel, frozen=True):
    choice: int = Field(ge=0)
    reasons: str = Field(default="", max_length=600)


def choose(
    llm: StructuredLLM | None,
    plan: Plan,
    incident: Incident,
    facilities: list[FacilityStatus] | None,
    sop: list[SopClause],
    *,
    correlation_id: str,
    at: datetime,
) -> PlanChoice | None:
    if llm is None or not plan.candidates:
        return None
    user = "\n".join(
        [
            data_block("incident", incident_text(incident)),
            data_block("candidates", candidates_text(plan)),
            data_block("facilities", facilities_text(facilities)),
            data_block("sop_clause", sop_text(sop)),
        ]
    )
    result = llm.ask(
        "planner",
        load_prompt("planner_choice"),
        user,
        PlanChoice,
        correlation_id=correlation_id,
        at=at,
    )
    if result.parsed is None or result.parsed.choice >= len(plan.candidates):
        return None
    return result.parsed
