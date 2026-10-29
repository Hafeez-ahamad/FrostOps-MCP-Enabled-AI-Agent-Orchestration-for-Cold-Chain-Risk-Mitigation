"""Assess newly ingested readings in the database and store the results.

Reads raw rows that have no ops.ReadingQuality row yet (in receive order), runs the checker,
writes one ReadingQuality row per reading and one DataQualityFlags row per failed check, then
runs the freshness check at `now`. Safe to run repeatedly.

On start it warm-starts the checker from the database: highest sequence seen per shipment and
the latest valid reading (the reference for spike/unit/GPS checks). Flatline runs that span
two runner invocations are not carried over.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Engine, case, func, insert, select

from engine.data.schema import DATA_QUALITY_FLAGS, RAW_COLUMNS, RAW_TABLE, READING_QUALITY
from engine.dq.checks import DataQualityChecker, DqFlag, Reading, parse_ts

RQ = READING_QUALITY.c
RAW = RAW_TABLE.c


@dataclass(frozen=True)
class RunnerResult:
    assessed: int
    by_status: dict[str, int]
    flags_written: int


def _flag_row(f: DqFlag) -> dict[str, object]:
    return {
        "ShipmentID": f.shipment_id,
        "MsgSeqNo": f.seq,
        "CheckName": f.check,
        "DqStatus": f.status.value,
        "Detail": f.detail[:400],
        "EvaluatedAt": f.evaluated_at,
    }


def _warm_start(engine: Engine, checker: DataQualityChecker) -> None:
    joined = RAW_TABLE.join(
        READING_QUALITY, (RQ.ShipmentID == RAW.CNSG_NO) & (RQ.MsgSeqNo == RAW.PKT_SEQ)
    )
    arrived = func.max(case((RAW.LEG_STAT == "A", 1), else_=0))
    with engine.connect() as conn:
        summary = conn.execute(
            select(RAW.CNSG_NO, func.max(RAW.PKT_SEQ), func.max(RAW.RCPT_DTTM), arrived)
            .select_from(joined)
            .group_by(RAW.CNSG_NO)
        ).all()
        latest: dict[str, Reading] = {}
        valid = conn.execute(
            select(RAW_TABLE)
            .select_from(joined)
            .where(RQ.DqStatus == "valid")
            .order_by(RAW.CNSG_NO, RAW.EVT_DTTM, RAW.PKT_SEQ)
        ).mappings()
        for row in valid:
            latest[str(row["CNSG_NO"])] = Reading.from_legacy(row)
    for shipment_id, max_seq, last_rcv, has_arrived in summary:
        checker.warm_start(
            str(shipment_id),
            max_seq=int(max_seq),
            last_received=parse_ts(last_rcv),
            en_route=not has_arrived,
            reference=latest.get(str(shipment_id)),
        )


def assess_pending(
    engine: Engine, now: datetime, checker: DataQualityChecker | None = None
) -> RunnerResult:
    checker = checker or DataQualityChecker()
    _warm_start(engine, checker)
    pending_query = (
        select(*[RAW_TABLE.c[c] for c in RAW_COLUMNS])
        .outerjoin(
            READING_QUALITY,
            (RQ.ShipmentID == RAW.CNSG_NO) & (RQ.MsgSeqNo == RAW.PKT_SEQ),
        )
        .where(RQ.ShipmentID.is_(None), RAW.CNSG_NO.is_not(None))
        .order_by(RAW.RCPT_DTTM, RAW.CNSG_NO, RAW.PKT_SEQ)
    )
    quality_rows: list[dict[str, object]] = []
    flag_rows: list[dict[str, object]] = []
    counts = {"valid": 0, "suspect": 0, "invalid": 0}
    with engine.connect() as conn:
        pending = list(conn.execute(pending_query).mappings())
    for row in pending:
        assessment = checker.assess(Reading.from_legacy(row))
        counts[assessment.status.value] += 1
        quality_rows.append(
            {
                "ShipmentID": assessment.shipment_id,
                "MsgSeqNo": assessment.seq,
                "DqStatus": assessment.status.value,
                "AssessedAt": now,
            }
        )
        flag_rows.extend(_flag_row(f) for f in assessment.flags)
    flag_rows.extend(_flag_row(f) for f in checker.check_freshness(now))
    with engine.begin() as conn:
        if quality_rows:
            conn.execute(insert(READING_QUALITY), quality_rows)
        if flag_rows:
            conn.execute(insert(DATA_QUALITY_FLAGS), flag_rows)
    return RunnerResult(assessed=len(quality_rows), by_status=counts, flags_written=len(flag_rows))
