"""Append-and-dedup ingestion into the raw telemetry table.

Rows are only ever appended; a row whose (CNSG_NO, PKT_SEQ) already exists, in the batch or
in the table, is skipped. Running the same ingestion twice changes nothing. A unique index on
the table backs this up.
"""

from dataclasses import dataclass
from typing import Any

import pandas as pd
from sqlalchemy import Engine, Table, insert, select

from engine.data.schema import RAW_COLUMNS, RAW_TABLE

KEY = ["CNSG_NO", "PKT_SEQ"]
LOOKUP_CHUNK = 500  # shipment ids per existing-key lookup
INSERT_CHUNK = 2000


@dataclass(frozen=True)
class IngestResult:
    received: int
    inserted: int
    duplicates_in_batch: int
    already_present: int


def _records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    clean = frame.astype(object).where(frame.notna(), None)
    return [{str(k): v for k, v in row.items()} for row in clean.to_dict("records")]


def ingest_readings(
    engine: Engine, readings: pd.DataFrame, table: Table = RAW_TABLE
) -> IngestResult:
    missing = [c for c in RAW_COLUMNS if c not in readings.columns]
    if missing:
        raise ValueError(f"readings are missing raw columns: {missing}")
    if readings[KEY].isna().any().any():
        raise ValueError("every reading needs CNSG_NO and PKT_SEQ")

    batch = readings[RAW_COLUMNS].copy()
    batch["LOAD_FLG"] = "Y"
    deduped = batch.drop_duplicates(subset=KEY, keep="first")
    in_batch_dupes = len(batch) - len(deduped)

    existing: set[tuple[str, int]] = set()
    shipment_ids = sorted(deduped["CNSG_NO"].unique())
    with engine.connect() as conn:
        for i in range(0, len(shipment_ids), LOOKUP_CHUNK):
            chunk = shipment_ids[i : i + LOOKUP_CHUNK]
            rows = conn.execute(
                select(table.c.CNSG_NO, table.c.PKT_SEQ).where(table.c.CNSG_NO.in_(chunk))
            )
            existing.update((str(s), int(n)) for s, n in rows)

    is_new = [
        (str(s), int(n)) not in existing
        for s, n in zip(deduped["CNSG_NO"], deduped["PKT_SEQ"], strict=True)
    ]
    new_rows = deduped.loc[is_new]
    records = _records(new_rows)
    with engine.begin() as conn:
        for i in range(0, len(records), INSERT_CHUNK):
            conn.execute(insert(table), records[i : i + INSERT_CHUNK])
    return IngestResult(
        received=len(batch),
        inserted=len(records),
        duplicates_in_batch=in_batch_dupes,
        already_present=len(deduped) - len(records),
    )
