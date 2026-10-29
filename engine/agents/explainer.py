"""Explainer agent: a short dispatcher-facing summary of an incident and what was decided.

Read-only. Its answer is accepted only if every rule id it cites belongs to the incident and
every SOP clause it cites was given to it; otherwise the deterministic template is used.
"""

from datetime import datetime

from pydantic import BaseModel, Field

from engine.actions.models import GateDecision
from engine.agents.context import data_block, incident_text, sop_text
from engine.incidents.manager import Incident
from engine.llm.structured import StructuredLLM
from engine.prompts import load_prompt
from engine.sop.retriever import SopClause


class Explanation(BaseModel, frozen=True):
    summary: str = Field(max_length=1200)
    rule_ids: list[str]
    sop_clauses: list[str]


def decisions_text(decisions: list[GateDecision]) -> str:
    if not decisions:
        return "no proposals yet"
    return "\n".join(
        f"{d.action}: {d.outcome.value}" + (f" ({', '.join(d.reasons)})" if d.reasons else "")
        for d in decisions
    )


def template_explanation(
    incident: Incident, decisions: list[GateDecision], sop: list[SopClause]
) -> Explanation:
    """Deterministic fallback (and C0's explanation when no LLM is configured)."""
    outcome = "; ".join(f"{d.action} {d.outcome.value}" for d in decisions) or "no action proposed"
    return Explanation(
        summary=(
            f"Simulated shipment {incident.shipment_id} triggered {', '.join(incident.rule_ids)} "
            f"(risk score {incident.risk.score}). Gate outcome: {outcome}."
        ),
        rule_ids=list(incident.rule_ids),
        sop_clauses=[c.clause_id for c in sop],
    )


def explain(
    llm: StructuredLLM | None,
    incident: Incident,
    decisions: list[GateDecision],
    sop: list[SopClause],
    *,
    correlation_id: str,
    at: datetime,
) -> tuple[Explanation, bool]:
    """(explanation, degraded)."""
    fallback = template_explanation(incident, decisions, sop)
    if llm is None:
        return fallback, False  # no LLM configured: the template is the intended output
    user = "\n".join(
        [
            data_block("incident", incident_text(incident)),
            data_block("decisions", decisions_text(decisions)),
            data_block("sop_clause", sop_text(sop)),
        ]
    )
    result = llm.ask(
        "explainer",
        load_prompt("incident_explainer"),
        user,
        Explanation,
        correlation_id=correlation_id,
        at=at,
    )
    answer = result.parsed
    allowed_clauses = {c.clause_id for c in sop}
    if (
        answer is None
        or not set(answer.rule_ids) <= set(incident.rule_ids)
        or not set(answer.sop_clauses) <= allowed_clauses
    ):
        return fallback, True
    return answer, False
