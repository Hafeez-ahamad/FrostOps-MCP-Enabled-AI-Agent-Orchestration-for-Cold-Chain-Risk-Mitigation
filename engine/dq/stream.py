"""Run the checker over a stream of legacy rows in receive order, on a virtual clock."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from engine.dq.checks import Assessment, DataQualityChecker, DqFlag, Reading


@dataclass(frozen=True)
class StreamResult:
    assessments: list[Assessment]  # one per input row, same order
    freshness_flags: list[DqFlag]


def assess_stream(
    rows: Iterable[Mapping[str, Any]],
    checker: DataQualityChecker,
    tick: timedelta,
    end: datetime | None = None,
) -> StreamResult:
    """Assess rows (sorted by receive time) and run the freshness check at every tick.

    The freshness check at tick T sees every reading received up to T.
    """
    assessments: list[Assessment] = []
    freshness: list[DqFlag] = []
    next_tick: datetime | None = None
    for row in rows:
        reading = Reading.from_legacy(row)
        received = reading.received
        if received is not None:
            if next_tick is None:
                next_tick = datetime.min + ((received - datetime.min) // tick + 1) * tick
            while next_tick < received:
                freshness.extend(checker.check_freshness(next_tick))
                next_tick += tick
        assessments.append(checker.assess(reading))
    if next_tick is not None and end is not None:
        while next_tick <= end:
            freshness.extend(checker.check_freshness(next_tick))
            next_tick += tick
    return StreamResult(assessments, freshness)
