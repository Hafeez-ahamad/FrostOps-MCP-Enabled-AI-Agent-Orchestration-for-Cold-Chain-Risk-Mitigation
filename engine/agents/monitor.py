"""Monitor agent: triage of a confirmed temperature breach (real breach / door open / glitch).

It sees only the incident and the recent temperature series (least privilege: no facilities,
no actions). Its verdict can only re-weight the planner's candidates; it cannot remove every
action, approve anything or change a threshold.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from engine.agents.context import ReadingPoint, data_block, incident_text, telemetry_text
from engine.cargo import CargoType
from engine.incidents.manager import Incident
from engine.llm.structured import StructuredLLM
from engine.prompts import load_prompt

Verdict = Literal["real_breach", "door_open_event", "sensor_glitch", "uncertain"]


class TriageVerdict(BaseModel, frozen=True):
    verdict: Verdict
    confidence: float = Field(ge=0.0, le=1.0)
    reasons: str = Field(default="", max_length=600)


def triage(
    llm: StructuredLLM | None,
    incident: Incident,
    window: list[ReadingPoint],
    band: CargoType | None,
    *,
    correlation_id: str,
    at: datetime,
) -> TriageVerdict | None:
    """The monitor's verdict, or None if no valid answer was available (degraded)."""
    if llm is None:
        return None
    band_text = f"{band.code}: {band.min_c} to {band.max_c} C" if band else "unknown"
    user = "\n".join(
        [
            data_block("incident", incident_text(incident)),
            data_block("cargo_band", band_text),
            data_block("telemetry", telemetry_text(window)),
        ]
    )
    result = llm.ask(
        "monitor",
        load_prompt("monitor_triage"),
        user,
        TriageVerdict,
        correlation_id=correlation_id,
        at=at,
    )
    return result.parsed
