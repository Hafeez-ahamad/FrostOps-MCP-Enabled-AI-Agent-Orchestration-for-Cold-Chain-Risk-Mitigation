"""Render engine state as delimited DATA blocks for prompts.

Every value that reaches a prompt goes through `data_block`, which escapes angle brackets so
text inside the data (a facility name, an SOP chunk, a note) cannot close the tag and pose as
instructions.
"""

from dataclasses import dataclass
from datetime import datetime

from engine.incidents.manager import Incident
from engine.integrations.clients import FacilityStatus
from engine.planner.planner import Plan
from engine.sop.retriever import SopClause


@dataclass(frozen=True)
class ReadingPoint:
    """One valid reading in the recent window the monitor looks at."""

    ts: datetime
    temperature_c: float | None
    eta_delay_min: float | None


def escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def data_block(tag: str, body: str) -> str:
    return f"<{tag}>\n{escape(body)}\n</{tag}>"


def incident_text(incident: Incident) -> str:
    evidence = "; ".join(
        f"{h.rule_id} observed {h.observed} threshold {h.threshold} ({h.sop_ref})"
        for h in incident.latest_evidence
    )
    return (
        f"incident {incident.incident_id} shipment {incident.shipment_id} "
        f"cargo {incident.cargo_type}\n"
        f"rules {', '.join(incident.rule_ids)}; hits {incident.hit_count}; "
        f"risk score {incident.risk.score}\nevidence: {evidence}"
    )


def telemetry_text(window: list[ReadingPoint]) -> str:
    lines = ["time | temperature_c | eta_delay_min"]
    lines += [f"{p.ts:%H:%M} | {p.temperature_c} | {p.eta_delay_min}" for p in window]
    return "\n".join(lines)


def candidates_text(plan: Plan) -> str:
    return "\n".join(
        f"[{i}] {c.action} target={c.target}; utility {c.utility:.1f}; {c.reason}"
        for i, c in enumerate(plan.candidates)
    )


def facilities_text(facilities: list[FacilityStatus] | None) -> str:
    if facilities is None:
        return "facilities service unavailable"
    return "\n".join(
        f"{f.facility_id} ({f.name}): {f.available} of {f.capacity} free" for f in facilities
    )


def sop_text(clauses: list[SopClause]) -> str:
    return "\n\n".join(f"{c.clause_id} {c.title}\n{c.text}" for c in clauses)
