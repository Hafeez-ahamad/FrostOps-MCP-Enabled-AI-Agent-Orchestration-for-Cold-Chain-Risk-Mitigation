"""Ingestion (append + dedup), the DQ runner, the latest-valid reference and the Kaggle mapping."""

from datetime import datetime, timedelta

import pandas as pd
import pytest
from sqlalchemy import func, select

from engine.data.ingest import ingest_readings
from engine.data.kaggle import kaggle_to_raw
from engine.data.latest import latest_valid_readings
from engine.data.schema import DATA_QUALITY_FLAGS, RAW_COLUMNS, RAW_TABLE, READING_QUALITY
from engine.dq.runner import assess_pending
from engine.dq.stream import StreamResult
from sim.generator import SimulationOutput
from sim.scenario import Scenario
from tests.fakes import sqlite_engine


def _count(engine, table) -> int:  # type: ignore[no-untyped-def]
    with engine.connect() as conn:
        return int(conn.execute(select(func.count()).select_from(table)).scalar_one())


def test_ingest_appends_and_skips_duplicates(smoke: SimulationOutput) -> None:
    engine = sqlite_engine()
    readings = smoke.readings
    duplicates = sum(1 for g in smoke.glitches if g.type == "duplicate")

    first = ingest_readings(engine, readings)
    assert first.duplicates_in_batch == duplicates
    assert first.inserted == len(readings) - duplicates
    assert _count(engine, RAW_TABLE) == first.inserted

    second = ingest_readings(engine, readings)  # same data again
    assert second.inserted == 0
    assert second.already_present == first.inserted
    assert _count(engine, RAW_TABLE) == first.inserted


def test_ingest_appends_new_rows_to_existing_data(smoke: SimulationOutput) -> None:
    engine = sqlite_engine()
    half = len(smoke.readings) // 2
    ingest_readings(engine, smoke.readings.iloc[:half])
    result = ingest_readings(engine, smoke.readings)
    assert _count(engine, RAW_TABLE) == len(smoke.readings.drop_duplicates(["CNSG_NO", "PKT_SEQ"]))
    assert result.already_present > 0


def test_ingest_never_writes_ground_truth_columns(smoke: SimulationOutput) -> None:
    assert not any(c.startswith("gt_") for c in RAW_TABLE.columns.keys())
    engine = sqlite_engine()
    ingest_readings(engine, smoke.readings.head(50))
    with engine.connect() as conn:
        assert list(conn.execute(select(RAW_TABLE).limit(1)).keys()) == RAW_COLUMNS


def test_ingest_rejects_rows_without_a_key() -> None:
    frame = pd.DataFrame([{c: None for c in RAW_COLUMNS}])
    with pytest.raises(ValueError, match="CNSG_NO"):
        ingest_readings(sqlite_engine(), frame)


def test_runner_matches_the_in_memory_stream(
    smoke: SimulationOutput, smoke_dq: StreamResult, smoke_scenario: Scenario
) -> None:
    engine = sqlite_engine()
    ingest_readings(engine, smoke.readings)
    end = smoke_scenario.start + timedelta(hours=smoke_scenario.duration_hours)

    result = assess_pending(engine, now=end)

    unique = smoke.readings.drop_duplicates(["CNSG_NO", "PKT_SEQ"])
    assert result.assessed == len(unique)
    expected = {
        (a.shipment_id, a.seq): a.status.value
        for a in smoke_dq.assessments
        if a.status.value != "invalid" or not any(f.check == "duplicate" for f in a.flags)
    }
    with engine.connect() as conn:
        stored = {
            (s, n): st
            for s, n, st in conn.execute(
                select(
                    READING_QUALITY.c.ShipmentID,
                    READING_QUALITY.c.MsgSeqNo,
                    READING_QUALITY.c.DqStatus,
                )
            )
        }
    assert stored == expected


def test_runner_is_incremental(smoke: SimulationOutput, smoke_scenario: Scenario) -> None:
    engine = sqlite_engine()
    end = smoke_scenario.start + timedelta(hours=smoke_scenario.duration_hours)
    ingest_readings(engine, smoke.readings)
    assess_pending(engine, now=end)
    flags_before = _count(engine, DATA_QUALITY_FLAGS)

    again = assess_pending(engine, now=end)

    assert again.assessed == 0
    assert _count(engine, DATA_QUALITY_FLAGS) == flags_before


def test_runner_warm_start_keeps_the_reference_across_runs(smoke: SimulationOutput) -> None:
    """A spike arriving in a second run is still caught against the first run's reference."""
    engine = sqlite_engine()
    spike = next(g for g in smoke.glitches if g.type == "spike")
    rows = smoke.readings[smoke.readings["CNSG_NO"] == spike.shipment_id]
    before = rows[rows["PKT_SEQ"] < spike.first_seq]
    ingest_readings(engine, before)
    assess_pending(engine, now=datetime(2026, 3, 2, 0, 0))
    ingest_readings(engine, rows[rows["PKT_SEQ"] == spike.first_seq])
    result = assess_pending(engine, now=datetime(2026, 3, 2, 0, 0))
    assert result.by_status["suspect"] == 1


def test_latest_valid_picks_one_valid_row_per_shipment() -> None:
    frame = pd.DataFrame(
        [
            {"CNSG_NO": "A", "EVT_DTTM": "2026-03-02 10:00:00", "PKT_SEQ": 1},
            {"CNSG_NO": "A", "EVT_DTTM": "2026-03-02 10:05:00", "PKT_SEQ": 2},
            {"CNSG_NO": "A", "EVT_DTTM": "2026-03-02 10:10:00", "PKT_SEQ": 3},  # suspect
            {"CNSG_NO": "B", "EVT_DTTM": "2026-03-02 10:00:00", "PKT_SEQ": 1},  # invalid
        ]
    )
    latest = latest_valid_readings(frame, ["valid", "valid", "suspect", "invalid"])
    assert latest[["CNSG_NO", "PKT_SEQ"]].values.tolist() == [["A", 2]]


def test_kaggle_rows_map_to_legacy_columns() -> None:
    kaggle = pd.DataFrame(
        [
            {
                "timestamp": "2021-01-01 00:00:00",
                "vehicle_gps_latitude": 40.4,
                "vehicle_gps_longitude": -77.0,
                "iot_temperature": 5.1,
                "cargo_condition_status": 0.7,
                "risk_classification": "High Risk",
                "delay_probability": 0.9,
                "port_congestion_level": 7.2,
                "route_risk_level": 3.3,
                "eta_variation_hours": 1.5,
                "fuel_consumption_rate": 9.9,  # unmapped columns are ignored
            }
        ]
    )
    legacy = kaggle_to_raw(kaggle)
    assert list(legacy.columns) == RAW_COLUMNS
    row = legacy.iloc[0]
    assert row["CNSG_NO"] == "KGL-00000" and row["HUB_CD"] == "KAGGLE"
    assert row["RFR_TMP_C"] == 5.1 and row["ARR_SLIP_MIN"] == 90.0


def test_kaggle_mapping_rejects_other_files() -> None:
    with pytest.raises(ValueError, match="Kaggle"):
        kaggle_to_raw(pd.DataFrame([{"a": 1}]))
