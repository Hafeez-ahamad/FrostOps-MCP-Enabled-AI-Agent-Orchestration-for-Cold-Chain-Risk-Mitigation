"""Detection pipeline: data quality -> SOP rules -> incidents (deterministic; no LLM).

Runs over a stream of legacy rows in receive order. This is the C0 detection path and the
part every configuration (C0-C2) shares; LLM triage of suspect hits comes in Phase 4.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from engine.dq.checks import Assessment, DataQualityChecker, DqFlag
from engine.dq.stream import assess_stream
from engine.incidents.manager import IncidentEvent, IncidentManager
from engine.rules.engine import DqStatusText, RuleHit, RuleInput, RulesEngine


@dataclass(frozen=True)
class DetectionResult:
    assessments: list[Assessment]  # one per input row
    freshness_flags: list[DqFlag]
    hits: list[list[RuleHit]]  # one list per input row (empty for invalid readings)
    events: list[IncidentEvent]
    manager: IncidentManager


def detect_stream(
    rows: Sequence[Mapping[str, Any]],
    tick: timedelta,
    end: datetime | None = None,
    *,
    checker: DataQualityChecker | None = None,
    rules: RulesEngine | None = None,
    manager: IncidentManager | None = None,
) -> DetectionResult:
    dq = assess_stream(rows, checker or DataQualityChecker(), tick, end)
    rules = rules or RulesEngine()
    manager = manager or IncidentManager()
    all_hits: list[list[RuleHit]] = []
    events: list[IncidentEvent] = []
    for row, assessment in zip(rows, dq.assessments, strict=True):
        reading = RuleInput.from_legacy(row)
        status: DqStatusText = assessment.status.value
        hits = rules.evaluate(reading, status)
        all_hits.append(hits)
        event = manager.process(reading, status, hits)
        if event is not None:
            events.append(event)
    return DetectionResult(dq.assessments, dq.freshness_flags, all_hits, events, manager)
